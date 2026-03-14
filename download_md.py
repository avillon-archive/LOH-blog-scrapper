"""Markdown exporter for Lord of Heroes blog posts."""

from pathlib import Path
import re
import threading
import urllib.parse

from bs4 import BeautifulSoup, NavigableString, Tag

from utils import (
    DEFAULT_MAX_WORKERS,
    FailedLog,
    clean_url,
    ensure_utf8_console,
    extract_category,
    fetch_with_retry,
    load_done_file,
    load_image_map,
    load_posts,
    run_pipeline,
    url_to_slug,
    write_text_unique,
)

ROOT_DIR = Path(__file__).parent / "loh_blog"
MD_DIR = ROOT_DIR / "md"
DONE_FILE = ROOT_DIR / "done_md.txt"
FAILED_FILE = ROOT_DIR / "failed_md.txt"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"

BLOCK_CONTAINERS = {
    "div",
    "section",
    "figure",
    "article",
    "main",
    "aside",
    "header",
    "footer",
    "nav",
}

# done_map / done_urls 갱신을 원자적으로 처리
_md_done_lock = threading.Lock()
# FailedLog 내부 캐시 보호 전용 (done 락과 분리해 불필요한 경합 방지)
_md_fail_lock = threading.Lock()

# 모듈 레벨 FailedLog 인스턴스 (utils.FailedLog 로 공통화)
_failed_log = FailedLog(FAILED_FILE, _md_fail_lock)


# ---------------------------------------------------------------------------
# Markdown 빌딩 헬퍼
# ---------------------------------------------------------------------------


def _append_block(md_lines: list[str], text: str):
    if text:
        md_lines.append(text)
        md_lines.append("")


def _escape_table_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def img_to_md(
    img_tag: Tag,
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
) -> str:
    """<img> 태그를 Markdown 이미지 링크로 변환한다.

    - image_map에 URL이 있으면 img_prefix + 상대경로 사용
      img_prefix는 process_post에서 target_dir depth 기준으로 자동 계산된다.
        md/slug.md          → img_prefix="../"    → ../images/YYYY/MM/x.png
        md/카테고리/slug.md → img_prefix="../../" → ../../images/YYYY/MM/x.png
    - 없으면 절대 URL로 폴백 (링크가 끊기지 않도록)
    """
    src = img_tag.get("src") or img_tag.get("data-src") or ""
    alt = img_tag.get("alt") or ""
    if not src:
        return ""

    abs_src = urllib.parse.urljoin(post_url, src) if post_url else src
    key = clean_url(abs_src)
    relative_path = image_map.get(key)

    if not relative_path:
        return f"![{alt}]({abs_src})"

    return f"![{alt}]({img_prefix}{relative_path})"


def _normalize_href(href: str, post_url: str) -> str:
    if not href:
        return href
    lowered = href.lower()
    if href.startswith("#") or lowered.startswith("mailto:") or lowered.startswith("javascript:"):
        return href
    return urllib.parse.urljoin(post_url, href) if post_url else href


INLINE_MAX_DEPTH = 60


def _children_inline(
    tag: Tag,
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
    depth: int = 0,
) -> str:
    if depth > INLINE_MAX_DEPTH:
        return tag.get_text(" ", strip=False)

    parts: list[str] = []
    for child in tag.children:
        piece = inline_to_md(child, post_url, image_map, img_prefix=img_prefix, depth=depth + 1)
        if piece is None:
            continue
        if not isinstance(piece, str):
            piece = str(piece)
        parts.append(piece)
    return "".join(parts)


def _wrap_inline_code(text: str) -> str:
    runs = re.findall(r"`+", text)
    max_run = max((len(run) for run in runs), default=0)
    fence = "`" * max(1, max_run + 1)
    inner = text.replace("\n", " ")
    if inner.startswith("`") or inner.endswith("`"):
        return f"{fence} {inner} {fence}"
    return f"{fence}{inner}{fence}"


def _code_block_fence(text: str) -> str:
    runs = re.findall(r"`+", text)
    max_run = max((len(run) for run in runs), default=0)
    return "`" * max(3, max_run + 1)


def _wrap_marker(inner: str, marker: str) -> str:
    """인라인 강조 마커(**, *, ~~)를 씌운다.

    CommonMark 규칙상 닫는 마커 직전에 공백이 있으면 강조로 인식되지 않는다.
    앞뒤 공백을 마커 바깥으로 이동시켜 이 문제를 해결한다.

    whitespace-only인 경우 마커를 씌우지 않고 원문 공백을 그대로 반환한다.
    (중첩 strong 평탄화 시 공백 소멸 방지)
    """
    stripped = inner.strip()
    if not stripped:
        return inner  # 공백은 공백 그대로 보존
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()):]
    return f"{lead}{marker}{stripped}{marker}{trail}"


def _strip_marker(text: str, marker: str) -> str:
    """text가 동일 마커로 외부 래핑된 경우에만 마커를 제거한다 (중첩 마커 평탄화용).

    마커 문자 경계를 직접 확인하므로 ``**bold**`` 내부에서 ``*``를 오탐하지 않는다.
    예) _strip_marker("**text**", "**") → "text"   (이중 bold 제거)
        _strip_marker("**text**", "*")  → "**text**" (오탐 없음, inner[0]=="*" 감지)
        _strip_marker("*text*",   "*")  → "text"
    """
    n = len(marker)
    while True:
        if len(text) <= 2 * n:
            break
        if text[:n] != marker or text[-n:] != marker:
            break
        inner = text[n:-n]
        # 안쪽 경계가 마커 문자이면 더 긴 마커(예: ** 내부 *)이므로 중단
        if inner and (inner[0] == marker[0] or inner[-1] == marker[0]):
            break
        text = inner
    return text


def inline_to_md(
    elem,
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
    depth: int = 0,
) -> str:
    if depth > INLINE_MAX_DEPTH:
        if isinstance(elem, Tag):
            return elem.get_text(" ", strip=False)
        if isinstance(elem, NavigableString):
            return str(elem)
        return ""

    if isinstance(elem, NavigableString):
        return str(elem)
    if not isinstance(elem, Tag):
        return ""

    name = elem.name
    if name == "br":
        return "  \n"
    if name == "img":
        return img_to_md(elem, post_url, image_map, img_prefix=img_prefix)
    if name in ("strong", "b"):
        inner = _children_inline(elem, post_url, image_map, img_prefix=img_prefix, depth=depth)
        inner = _strip_marker(inner, "**")
        return _wrap_marker(inner, "**")
    if name in ("em", "i"):
        inner = _children_inline(elem, post_url, image_map, img_prefix=img_prefix, depth=depth)
        inner = _strip_marker(inner, "*")
        return _wrap_marker(inner, "*")
    if name in ("del", "s", "strike"):
        inner = _children_inline(elem, post_url, image_map, img_prefix=img_prefix, depth=depth)
        inner = _strip_marker(inner, "~~")
        return _wrap_marker(inner, "~~")
    if name == "code":
        inner = elem.get_text()
        return _wrap_inline_code(inner) if inner.strip() else ""
    if name == "a":
        inner = _children_inline(elem, post_url, image_map, img_prefix=img_prefix, depth=depth)
        href = _normalize_href(elem.get("href") or "", post_url)
        return f"[{inner}]({href})" if inner.strip() else href
    if name == "span":
        return _children_inline(elem, post_url, image_map, img_prefix=img_prefix, depth=depth)

    return _children_inline(elem, post_url, image_map, img_prefix=img_prefix, depth=depth)


def _convert_list(
    tag: Tag,
    md_lines: list[str],
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
    ordered: bool = False,
    depth: int = 0,
):
    prefix = "  " * depth
    for idx, li in enumerate(tag.find_all("li", recursive=False), start=1):
        marker = f"{idx}." if ordered else "-"
        inline_parts: list[str] = []
        sub_lists: list[Tag] = []

        for child in li.children:
            if isinstance(child, Tag) and child.name in ("ul", "ol"):
                sub_lists.append(child)
            else:
                inline_parts.append(
                    inline_to_md(child, post_url, image_map, img_prefix=img_prefix)
                )

        text = "".join(inline_parts).strip()
        md_lines.append(f"{prefix}{marker} {text}" if text else f"{prefix}{marker}")

        for sub in sub_lists:
            _convert_list(
                sub,
                md_lines,
                post_url,
                image_map,
                img_prefix=img_prefix,
                ordered=(sub.name == "ol"),
                depth=depth + 1,
            )


def _convert_table(
    tag: Tag,
    md_lines: list[str],
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
):
    thead = tag.find("thead")
    tbody = tag.find("tbody")
    header_rows = thead.find_all("tr") if thead else []
    body_rows = tbody.find_all("tr") if tbody else []

    if not header_rows:
        all_rows = tag.find_all("tr")
        if not all_rows:
            return  # 행이 없는 빈 <table> — 출력할 내용 없음
        header_rows = all_rows[:1]
        if body_rows:
            # thead 없이 tbody만 있는 경우: tbody의 첫 행을 헤더로 사용하고
            # body_rows에서 해당 행을 제외해야 중복 출력을 방지할 수 있다.
            header_set = set(header_rows)
            body_rows = [r for r in body_rows if r not in header_set]
        else:
            body_rows = all_rows[1:]

    def row_to_cells(row: Tag) -> list[str]:
        return [
            _escape_table_cell(
                _children_inline(
                    cell, post_url, image_map, img_prefix=img_prefix
                ).strip()
            )
            for cell in row.find_all(["th", "td"])
        ]

    for idx, row in enumerate(header_rows):
        cells = row_to_cells(row)
        md_lines.append("| " + " | ".join(cells) + " |")
        if idx == len(header_rows) - 1:
            md_lines.append("| " + " | ".join(["---"] * len(cells)) + " |")

    for row in body_rows:
        cells = row_to_cells(row)
        if cells:
            md_lines.append("| " + " | ".join(cells) + " |")


def _append_blockquote(
    elem: Tag,
    md_lines: list[str],
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
):
    quoted: list[str] = []
    for child in elem.children:
        convert_node(child, quoted, post_url, image_map, img_prefix=img_prefix)
    for line in quoted:
        md_lines.append(f"> {line}" if line.strip() else ">")
    md_lines.append("")


def _append_pre(elem: Tag, md_lines: list[str]):
    code_tag = elem.find("code")
    code_text = (code_tag or elem).get_text()
    lang = ""
    if code_tag:
        for cls in (code_tag.get("class") or []):
            if cls.startswith("language-"):
                lang = cls[9:]
                break

    fence = _code_block_fence(code_text)
    md_lines.append(f"{fence}{lang}")
    md_lines.extend(code_text.splitlines())
    md_lines.append(fence)
    md_lines.append("")


def convert_node(
    elem,
    md_lines: list[str],
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str = "../",
):
    if isinstance(elem, NavigableString):
        text = str(elem).strip()
        if text:
            md_lines.append(text)
        return

    if not isinstance(elem, Tag):
        return

    name = elem.name
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        _append_block(md_lines, f"{'#' * level} {elem.get_text(strip=True)}")
        return

    if name == "p":
        _append_block(
            md_lines,
            _children_inline(elem, post_url, image_map, img_prefix=img_prefix).strip(),
        )
        return

    if name == "img":
        _append_block(md_lines, img_to_md(elem, post_url, image_map, img_prefix=img_prefix))
        return

    if name in ("ul", "ol"):
        _convert_list(
            elem,
            md_lines,
            post_url,
            image_map,
            img_prefix=img_prefix,
            ordered=(name == "ol"),
            depth=0,
        )
        md_lines.append("")
        return

    if name == "table":
        _convert_table(elem, md_lines, post_url, image_map, img_prefix=img_prefix)
        md_lines.append("")
        return

    if name == "hr":
        _append_block(md_lines, "---")
        return

    if name == "blockquote":
        _append_blockquote(elem, md_lines, post_url, image_map, img_prefix=img_prefix)
        return

    if name == "pre":
        _append_pre(elem, md_lines)
        return

    if name == "figcaption":
        text = elem.get_text(strip=True)
        _append_block(md_lines, f"*{text}*" if text else "")
        return

    if name in BLOCK_CONTAINERS:
        for child in elem.children:
            convert_node(child, md_lines, post_url, image_map, img_prefix=img_prefix)
        return

    _append_block(
        md_lines,
        _children_inline(elem, post_url, image_map, img_prefix=img_prefix).strip(),
    )


def collapse_blank_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank
    return result


def post_to_md(
    soup: BeautifulSoup,
    post_url: str,
    post_date: str,
    image_map: dict[str, str],
    category: str = "",
    img_prefix: str = "../",
) -> str:
    md_lines: list[str] = []

    title_tag = soup.find("h1", class_=re.compile(r"post-title|article-title", re.I))
    if title_tag is None:
        title_tag = soup.find("h1")

    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        og_title = soup.find("meta", property="og:title")
        title = og_title["content"] if og_title and og_title.get("content") else ""

    if title:
        _append_block(md_lines, f"# {title}")

    if post_date:
        md_lines.append(f"**작성일:** {post_date}")
    if category:
        md_lines.append(f"**카테고리:** {category}")
    md_lines.append(f"**원문:** {post_url}")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")

    body = (
        soup.find("section", class_=re.compile(r"post-content|article-body|gh-content", re.I))
        or soup.find("div", class_=re.compile(r"post-content|article-body|gh-content", re.I))
        or soup.find("article")
        or soup.find("main")
    )
    if body is None:
        body = soup.find("body") or soup

    for author_card in body.find_all("div", class_="author-card"):
        author_card.decompose()

    for cls in ["post-share", "post-tags", "post-nav", "related-posts", "comments"]:
        for unwanted in body.find_all(class_=re.compile(cls, re.I)):
            unwanted.decompose()

    if title:
        for h1 in body.find_all("h1"):
            if h1.get_text(strip=True) == title:
                h1.decompose()

    # title_tag가 body 범위 밖(예: <header>)에 있을 경우 위의 sweep에서 제거되지 않으므로
    # 여기서 별도로 decompose한다. body h1 sweep에서 이미 제거된 경우에는
    # parent가 None이 되므로 parent 체크로 이중 호출을 방지한다.
    if title_tag and title_tag.parent is not None:
        title_tag.decompose()

    for child in body.children:
        convert_node(child, md_lines, post_url, image_map, img_prefix=img_prefix)

    md_lines = collapse_blank_lines(md_lines)
    while md_lines and md_lines[-1].strip() == "":
        md_lines.pop()

    return "\n".join(md_lines) + "\n"


# ---------------------------------------------------------------------------
# 포스트 단위 처리 (스레드 안전)
# ---------------------------------------------------------------------------


def process_post(
    post_url: str,
    post_date: str,
    done_slugs: dict[str, str],
    done_urls: set[str],
    image_map: dict[str, str],
    force_overwrite: bool = False,
) -> bool:
    # 빠른 비잠금 확인
    if post_url in done_urls:
        return True

    slug = url_to_slug(post_url)
    resp = fetch_with_retry(post_url)
    if resp is None:
        _failed_log.record(post_url, "fetch_post_failed")
        return False

    soup = BeautifulSoup(resp.text, "lxml")

    # 카테고리 추출 → 저장 경로 결정
    category = extract_category(soup)
    target_dir = MD_DIR / category if category else MD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    # target_dir의 ROOT_DIR 기준 depth로 img_prefix 자동 계산
    # md/ (depth=1) → "../"  /  md/카테고리/ (depth=2) → "../../"
    depth = len(target_dir.relative_to(ROOT_DIR).parts)
    img_prefix = "../" * depth

    md_text = post_to_md(soup, post_url, post_date, image_map, category, img_prefix)

    # write_text_unique 가 slug 충돌 해소·쓰기·done 갱신을 일괄 처리한다.
    # None 반환은 already-done 을 의미하므로 성공으로 처리한다.
    try:
        write_text_unique(
            target_dir, slug, ".md", md_text,
            done_slugs, done_urls, post_url,
            _md_done_lock, DONE_FILE,
            force_overwrite=force_overwrite,
        )
    except OSError as e:
        _failed_log.record(post_url, f"write_failed:{e}")
        return False
    return True


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def run_md(posts: list[tuple[str, str]], retry_mode: bool = False, force_download: bool = False) -> None:
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)
    image_map = load_image_map(IMAGE_MAP_FILE)
    done_slugs = load_done_file(DONE_FILE)
    done_urls: set[str] = set() if force_download else set(done_slugs.values())

    process_fn = lambda url, date: process_post(url, date, done_slugs, done_urls, image_map, force_overwrite=force_download)

    run_pipeline(
        posts,
        process_fn,
        _failed_log,
        retry_mode,
        label="MD",
        max_workers=DEFAULT_MAX_WORKERS,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Markdown exporter")
    parser.add_argument("--retry", action="store_true", help="Retry failed list")
    parser.add_argument(
        "--posts",
        default=str(ROOT_DIR / "all_posts.txt"),
        help="Posts list file",
    )
    args = parser.parse_args()

    posts = load_posts(args.posts)
    run_md(posts, retry_mode=args.retry)
