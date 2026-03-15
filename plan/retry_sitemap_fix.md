# EN/JA 사이트맵 인덱스 캐싱 추가

## Context
`--retry` 모드에서 EN/JA 사이트맵 인덱스(`_build_multilang_date_index()`)가 매번 새로 fetch되는 문제.
KO는 `_maybe_refresh_posts_list()`에서 로컬 최신 날짜 vs 원격 최신 날짜를 비교해 건너뛰지만, EN/JA는 이런 로직이 없다.
반복 retry 시 불필요한 네트워크 요청을 줄이기 위해 캐싱을 추가한다.

## 수정 대상
- `download_images.py` — `_build_multilang_date_index()` 함수 및 호출부

## 구현 방안

### 1. 캐시 파일 저장
- `_build_multilang_date_index()`에서 인덱스 구축 후 결과를 디스크에 저장
- 경로: `loh_blog/multilang_sitemap_index.json` (date → [(url, lang)] 구조)
- 각 언어별 최신 날짜도 함께 저장: `{"_meta": {"en_latest": "2025-...", "ja_latest": "2025-..."}, "2025-01-01": [["url", "en"], ...] }`

### 2. 날짜 비교 스킵 로직
- `_build_multilang_date_index()` 호출 시:
  1. 캐시 파일이 존재하면 `_meta`에서 각 언어 최신 날짜를 읽음
  2. 각 언어 사이트맵에서 최신 날짜만 확인 (`fetch_newest_single_sitemap_date()` 재사용)
  3. 캐시된 날짜 == 원격 날짜이면 해당 언어 스킵
  4. 변경된 언어만 전체 fetch 후 캐시 갱신
  5. 둘 다 스킵이면 캐시 파일에서 로드하여 반환

### 3. 구체적 코드 변경

**`download_images.py`:**

```python
MULTILANG_INDEX_CACHE = ROOT_DIR / "multilang_sitemap_index.json"

def _build_multilang_date_index() -> dict[str, list[tuple[str, str]]]:
    from build_posts_list import parse_sitemap, fetch_newest_single_sitemap_date
    import json

    # 1) 캐시 로드
    cached_index, cached_meta = _load_multilang_cache()

    # 2) 각 언어별 최신 날짜 비교
    need_refresh = {}  # lang -> True if refresh needed
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        sitemap_url = f"https://{lang_host}/sitemap-posts.xml"
        remote_date = fetch_newest_single_sitemap_date(sitemap_url)
        cached_date = cached_meta.get(f"{lang}_latest", "")

        if remote_date and cached_date == remote_date:
            print(f"  [{lang.upper()}] 최신 상태 ({cached_date}), 갱신 불필요")
        else:
            need_refresh[lang] = True
            if remote_date:
                print(f"  [{lang.upper()}] 갱신 필요 (캐시: {cached_date or '없음'} → 원격: {remote_date})")

    # 3) 모두 최신이면 캐시 반환
    if not need_refresh and cached_index:
        return cached_index

    # 4) 변경된 언어만 fetch, 나머지는 캐시 유지
    date_index = {k: v for k, v in cached_index.items()} if cached_index else {}
    new_meta = dict(cached_meta)

    # 갱신 대상 언어의 기존 항목 제거
    for lang in need_refresh:
        date_index = {d: [(u, l) for u, l in entries if l != lang]
                      for d, entries in date_index.items()}
        date_index = {d: entries for d, entries in date_index.items() if entries}

    # 새로 fetch
    for lang in need_refresh:
        lang_host = MULTILANG_BLOG_HOSTS[lang]
        sitemap_url = f"https://{lang_host}/sitemap-posts.xml"
        resp = fetch_with_retry(sitemap_url, allow_redirects=True, timeout=30)
        if not resp:
            print(f"  [{lang.upper()}] 사이트맵 fetch 실패, 건너뜀")
            continue
        resp.encoding = resp.apparent_encoding or "utf-8"
        try:
            entries = parse_sitemap(resp.text)
        except Exception as exc:
            print(f"  [{lang.upper()}] 사이트맵 파싱 실패: {exc}")
            continue

        count = 0
        latest = ""
        for post_url, date in entries:
            if date:
                date_index.setdefault(date, []).append((post_url, lang))
                count += 1
                if date > latest:
                    latest = date
        new_meta[f"{lang}_latest"] = latest
        print(f"  [{lang.upper()}] {count}개 포스트 인덱싱 완료")

    # 5) 캐시 저장
    _save_multilang_cache(date_index, new_meta)
    return date_index


def _load_multilang_cache():
    """캐시 파일에서 date_index와 meta를 로드. 없으면 ({}, {}) 반환."""
    import json
    if not MULTILANG_INDEX_CACHE.exists():
        return {}, {}
    try:
        with open(MULTILANG_INDEX_CACHE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        meta = raw.pop("_meta", {})
        # JSON은 list[list]로 저장되므로 list[tuple]로 변환
        index = {d: [(u, l) for u, l in entries] for d, entries in raw.items()}
        return index, meta
    except Exception:
        return {}, {}


def _save_multilang_cache(date_index, meta):
    import json
    raw = {d: entries for d, entries in date_index.items()}
    raw["_meta"] = meta
    with open(MULTILANG_INDEX_CACHE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
```

## 검증
1. `--retry` 첫 실행: EN/JA 사이트맵 fetch → `multilang_sitemap_index.json` 생성 확인
2. `--retry` 재실행: "최신 상태, 갱신 불필요" 메시지 출력, 사이트맵 재요청 없이 캐시에서 로드 확인
3. 사이트맵 업데이트 후: 해당 언어만 갱신되는지 확인
