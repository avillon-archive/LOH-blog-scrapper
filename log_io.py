# -*- coding: utf-8 -*-
"""
log_io.py - CSV 기반 로그 파일 I/O 모듈

구분자 기반 로그 파일(done, failed, stale, image_map 등)의 읽기/쓰기를
한 곳에서 관리한다. 모든 신규 CSV 파일은 utf-8-sig 인코딩, 헤더 행 포함.
"""
import csv
import io
import threading
from pathlib import Path
from typing import Callable

from config import ROOT_DIR  # noqa: F401

# ── File I/O lock (append/filter/remove 용) ──────────────────────────────
_file_lock = threading.Lock()

# ── LineBuffer 레지스트리 ─────────────────────────────────────────────────
_line_buffers: list["LineBuffer"] = []
_line_buffers_lock = threading.Lock()


# ── CSV 헬퍼 ──────────────────────────────────────────────────────────────

def csv_line(*fields: str) -> str:
    """단일 CSV 행 문자열 반환 (줄바꿈 없음)."""
    buf = io.StringIO()
    csv.writer(buf).writerow(fields)
    return buf.getvalue().rstrip("\r\n")


def _split_row(line: str, maxsplit: int = -1) -> list[str]:
    """CSV 행을 파싱하여 필드 리스트를 반환한다."""
    reader = csv.reader(io.StringIO(line))
    row = next(reader, [])
    return [f.strip() for f in row]


def _is_header(line: str, expected: str) -> bool:
    """첫 줄이 예상 헤더와 일치하는지 확인한다. BOM 제거 포함."""
    cleaned = line.strip().lstrip("\ufeff")
    return cleaned == expected


# ── append / filter / remove ──────────────────────────────────────────────

def _ensure_header(filepath: Path, header: str) -> None:
    """파일이 없거나 비어있으면 BOM + 헤더를 기록한다."""
    if filepath.exists() and filepath.stat().st_size > 0:
        return
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        f.write(header + "\n")


def append_line(filepath: Path, line: str, *, header: str | None = None) -> None:
    """Append one line to file (thread-safe). header 지정 시 파일 미존재면 헤더 먼저 기록."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        if header is not None:
            _ensure_header(filepath, header)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def filter_file_lines(filepath: Path, keep_fn: Callable[[str], bool]) -> None:
    """Filter lines by predicate (thread-safe, in-place). 헤더 행은 항상 유지."""
    if not filepath.exists():
        return
    with _file_lock:
        lines = filepath.read_text(encoding="utf-8").splitlines()
        if not lines:
            return
        # 첫 줄이 헤더(콤마 포함 + 숫자/URL이 아닌 문자열)이면 보존
        header_line = None
        data_lines = lines
        first = lines[0].strip().lstrip("\ufeff")
        if first and not first.startswith("http") and "," in first:
            header_line = lines[0]
            data_lines = lines[1:]
        new_lines = [line for line in data_lines if keep_fn(line)]
        parts = []
        if header_line is not None:
            parts.append(header_line)
        parts.extend(new_lines)
        filepath.write_text(
            "\n".join(parts) + ("\n" if parts else ""), encoding="utf-8"
        )


def remove_lines_by_prefix(filepath: Path, prefix: str) -> None:
    """Remove lines that start with prefix (thread-safe, in-place)."""
    filter_file_lines(filepath, lambda line: not line.startswith(prefix))


# ── FailedLog ─────────────────────────────────────────────────────────────

_FAILED_HEADER = "post_url,reason"


class FailedLog:
    """파일 기반 실패 이력 (post_url, reason)을 스레드 안전하게 관리.

    download_md.py 및 download_html.py 에서 공통으로 사용된다.
    download_images.py 는 3-tuple (post_url, img_url, reason) 구조를 사용하므로
    별도 구현을 유지한다.
    """

    def __init__(self, filepath: Path, lock: threading.Lock) -> None:
        self._filepath = filepath
        self._lock = lock
        self._cache: set[tuple[str, str]] | None = None

    def _load(self) -> set[tuple[str, str]]:
        entries: set[tuple[str, str]] = set()
        if not self._filepath.exists():
            return entries
        for line in self._filepath.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line or _is_header(line, _FAILED_HEADER):
                continue
            parts = _split_row(line, maxsplit=1)
            if len(parts) == 2 and parts[0] and parts[1]:
                entries.add((parts[0], parts[1]))
        return entries

    def record(self, post_url: str, reason: str) -> None:
        """실패 항목을 기록한다. 동일 (post_url, reason) 쌍은 중복 기록하지 않는다."""
        key = (post_url, reason)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            if key in self._cache:
                return
            self._cache.add(key)
            append_line(self._filepath, csv_line(post_url, reason),
                        header=_FAILED_HEADER)

    def remove(self, post_url: str) -> None:
        """post_url 에 해당하는 모든 실패 항목을 삭제한다."""
        # CSV에서는 첫 필드가 post_url로 시작하는 행을 제거
        # 쌍따옴표로 감싸진 경우와 그렇지 않은 경우 모두 처리
        def _should_keep(line: str) -> bool:
            stripped = line.strip().lstrip("\ufeff")
            if not stripped:
                return True
            parts = _split_row(stripped, maxsplit=1)
            return not parts or parts[0] != post_url

        filter_file_lines(self._filepath, _should_keep)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            self._cache = {k for k in self._cache if k[0] != post_url}

    def load_post_urls(self) -> set[str]:
        """실패 목록의 post_url 집합을 반환한다."""
        return load_failed_post_urls(self._filepath)


# ── write_text_unique ─────────────────────────────────────────────────────

_DONE_HEADER = "slug,post_url"


def write_text_unique(
    target_dir: Path,
    slug: str,
    suffix: str,
    content: str,
    done_map: dict[str, str],
    done_urls: set[str],
    post_url: str,
    lock: threading.Lock,
    done_file: Path,
    force_overwrite: bool = False,
) -> str | None:
    """slug 충돌을 해소하며 텍스트 파일을 저장하고 done 상태를 갱신한다.

    download_md.py 와 download_html.py 에서 동일하게 사용되는
    "잠금 외부 탐색 → 잠금 내부 확정·쓰기" 패턴을 공통화한다.

    1단계(잠금 외부): 동일 내용 파일 탐색 (I/O 집중 구간)
    2단계(잠금 내부): 최종 경로 확정·쓰기·done 상태 갱신

    Args:
        force_overwrite: True 이면 동일 slug 파일이 존재하고 내용이 다를 때
                         _2 suffix 없이 기존 파일을 덮어쓴다.

    Returns:
        실제 저장(또는 기존 일치) 파일의 slug 문자열.
        post_url 이 already-done 상태이면 None.
    """
    # 빠른 비잠금 확인
    if post_url in done_urls:
        return None

    # ── 1단계: 충돌 탐색 (잠금 외부) ──────────────────────────────────
    path = target_dir / f"{slug}{suffix}"
    next_idx = 2
    if force_overwrite:
        pass
    else:
        while path.exists():
            try:
                if path.read_text(encoding="utf-8") == content:
                    break
            except OSError:
                pass
            path = target_dir / f"{slug}_{next_idx}{suffix}"
            next_idx += 1

    # ── 2단계: 경로 확정·쓰기·상태 갱신 (잠금 내부) ───────────────────
    actual_slug: str | None = None
    with lock:
        if post_url in done_urls:
            return None

        if path.exists():
            try:
                on_disk = path.read_text(encoding="utf-8")
            except OSError:
                on_disk = None

            if on_disk == content:
                actual_slug = path.stem
                if actual_slug not in done_map:
                    done_map[actual_slug] = post_url
                done_urls.add(post_url)
            elif force_overwrite:
                path.write_text(content, encoding="utf-8")
                actual_slug = path.stem
                done_map[actual_slug] = post_url
                done_urls.add(post_url)
            else:
                while path.exists():
                    path = target_dir / f"{slug}_{next_idx}{suffix}"
                    next_idx += 1
                path.write_text(content, encoding="utf-8")
                actual_slug = path.stem
                done_map[actual_slug] = post_url
                done_urls.add(post_url)
        else:
            path.write_text(content, encoding="utf-8")
            actual_slug = path.stem
            done_map[actual_slug] = post_url
            done_urls.add(post_url)

    # 잠금 해제 후 파일 I/O
    if actual_slug is not None:
        append_line(done_file, csv_line(actual_slug, post_url),
                    header=_DONE_HEADER)
    return actual_slug


# ── LineBuffer ────────────────────────────────────────────────────────────


class LineBuffer:
    """스레드 안전한 지연 flush 파일 버퍼.

    append_line 호출을 메모리에 누적하다가 flush_every 건 이상 쌓이면
    자동으로 파일에 일괄 기록한다. 프로세스 종료 전 flush_all()을 반드시
    호출해야 미기록 데이터 유실을 방지할 수 있다.

    download_images.py 의 고빈도 파일(downloaded_urls.txt, image_map.csv 등)에
    사용하기 위해 설계됐다. 모듈 수준 append_line 과 달리 _file_lock 을 경유하지
    않으므로 _state_lock / _save_lock 과 경합하지 않는다.
    """

    def __init__(
        self, filepath: Path, flush_every: int = 100,
        header: str | None = None,
    ) -> None:
        self._filepath = filepath
        self._flush_every = flush_every
        self._header = header
        self._buf: list[str] = []
        self._lock = threading.Lock()
        with _line_buffers_lock:
            _line_buffers.append(self)

    def add(self, line: str) -> None:
        """줄을 버퍼에 추가한다. 버퍼가 flush_every 건을 초과하면 자동 flush."""
        with self._lock:
            self._buf.append(line)
            if len(self._buf) >= self._flush_every:
                self._flush_locked()

    def flush_all(self) -> None:
        """버퍼에 남은 모든 줄을 파일에 기록한다. run 종료 시 반드시 호출."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """_lock 보유 상태에서 호출해야 한다."""
        if not self._buf:
            return
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
        needs_header = (
            self._header is not None
            and (not self._filepath.exists() or self._filepath.stat().st_size == 0)
        )
        if needs_header:
            # 신규 파일: BOM + 헤더 + 데이터
            with open(self._filepath, "w", encoding="utf-8-sig", newline="") as f:
                f.write(self._header + "\n")
                f.write("\n".join(self._buf) + "\n")
        else:
            with open(self._filepath, "a", encoding="utf-8") as f:
                f.write("\n".join(self._buf) + "\n")
        self._buf.clear()


def flush_all_buffers() -> int:
    """등록된 모든 LineBuffer를 플러시한다. 플러시한 버퍼 수를 반환."""
    with _line_buffers_lock:
        buffers = list(_line_buffers)
    for buf in buffers:
        try:
            buf.flush_all()
        except Exception:
            pass
    return len(buffers)


# ── Reader 함수들 ─────────────────────────────────────────────────────────

def load_image_map(filepath: Path) -> dict[str, str]:
    """image_map.csv → {clean_url: relative_path}."""
    image_map: dict[str, str] = {}
    if not filepath.exists():
        return image_map
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip().lstrip("\ufeff")
        if not row or _is_header(row, "clean_url,relative_path"):
            continue
        parts = _split_row(row, maxsplit=1)
        if len(parts) != 2:
            continue
        image_map[parts[0]] = parts[1]
    return image_map


def load_done_file(filepath: Path) -> dict[str, str]:
    """done_*.csv → {slug: post_url}."""
    done: dict[str, str] = {}
    if not filepath.exists():
        return done
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip().lstrip("\ufeff")
        if not row or _is_header(row, _DONE_HEADER):
            continue
        parts = _split_row(row, maxsplit=1)
        if len(parts) == 2 and parts[1]:
            done[parts[0]] = parts[1]
    return done


def load_failed_post_urls(filepath: Path) -> set[str]:
    """Return first-column post URLs from failed log file."""
    failed: set[str] = set()
    if not filepath.exists():
        return failed
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip().lstrip("\ufeff")
        if not row or _is_header(row, _FAILED_HEADER) or _is_header(row, "post_url,img_url,reason"):
            continue
        parts = _split_row(row)
        if parts and parts[0]:
            failed.add(parts[0])
    return failed


def load_stale(filepath: Path) -> dict[str, set[str]]:
    """stale 파일을 로드. {post_url: set[clean_url]}."""
    result: dict[str, set[str]] = {}
    if not filepath.exists():
        return result
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip().lstrip("\ufeff")
        if not row or _is_header(row, "post_url,unmapped_urls"):
            continue
        parts = _split_row(row, maxsplit=1)
        if len(parts) == 2 and parts[0]:
            result[parts[0]] = set(parts[1].split("|"))
    return result


def load_posts(filepath: str | Path) -> list[tuple[str, str, str]]:
    """all_posts.csv / custom_posts.txt 로드."""
    posts: list[tuple[str, str, str]] = []
    path = Path(filepath)
    if not path.exists():
        print(f"[warning] file not found: {filepath}")
        return posts
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if _is_header(line, "url,lastmod,published_time"):
                continue
            parts = _split_row(line)
            url = parts[0] if parts else ""
            lastmod = parts[1] if len(parts) >= 2 else ""
            published = parts[2] if len(parts) >= 3 else ""
            posts.append((url, lastmod, published))
    return posts
