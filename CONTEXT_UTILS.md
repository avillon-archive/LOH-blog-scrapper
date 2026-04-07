# utils.py

## 네트워크

- 블로그 도메인 요청은 `_TokenBucket` rate limiter 통과 (대규모 >100건: 10 req/s, 소규모 ≤100건: 20 req/s).
- `fetch_with_retry`: 3회 재시도 + 백오프(1→2초). 404/410 즉시 포기. **HTTP 429는 Retry-After를 존중하며 retry 횟수를 소모하지 않음**.

---

## FailedLog

`download_md.py` / `download_html.py` 공통 `(post_url, reason)` 2-tuple 실패 관리.

**스레드 안전 패턴**: lock 내부에서 in-memory 캐시 갱신, lock 외부에서 파일 기록. `append_line` 내부 `_file_lock`이 원자성 보장.

---

## write_text_unique

slug 충돌 해소 + 파일 저장. MD/HTML 공통.

- **2단계**: ①잠금 외부에서 동일 내용 기존 파일 탐색 → ②잠금 내부에서 최종 경로 확정·쓰기·done 갱신.
- `force_overwrite=True`: 동일 slug 파일 존재 시 `_2` suffix 없이 덮어쓴다 (`--custom` 모드용).
- `post_url`이 already-done이면 `None` 반환.

---

## LineBuffer

스레드 안전 지연 flush 파일 버퍼 (100건 단위). `download_images/` 고빈도 파일에 사용.

모듈 수준 `append_line`과 달리 `_file_lock`을 경유하지 않으므로 `_state_lock`/`_save_lock`과 경합하지 않는다. run 종료 시 `flush_all()` 필수.

---

## run_pipeline

MD/HTML 공통 ThreadPoolExecutor 루프. 진행도 출력 간격: **100개 이하면 10개 단위, 초과면 50개 단위**. `download_images/`는 독립 구현이지만 동일 간격 규칙 적용.

---

## HTML 인덱스

파이프라인 간 HTML 재활용. `build_html_index(html_dir, done_file)` → `{post_url: Path}`. `fetch_post_html(url, html_index)` → 로컬 우선 조회, 없으면 서버 fetch.

`run_all.py`에서 HTML 단계 완료 후 KO/EN/JA 인덱스를 merge하여 images/md에 전달. html-local은 `html_index`를 받지 않고 `html/` 디렉토리를 직접 읽는다.

---

# build_posts_list.py

사이트맵 XML(`xml.etree.ElementTree`) 파싱. namespace 유무 모두 처리. `<lastmod>`에서 `YYYY-MM-DD` 추출, 없으면 빈 문자열로 맨 뒤 정렬. 가장 오래된 포스트: 2020-07-27.

`build_multilang_and_write()`: EN/JA sitemap-posts + sitemap-pages → `all_links_{lang}.txt` 생성.
