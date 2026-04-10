# download_media/ 패키지

`--images` 가 처리하지 않는 비이미지 미디어(mp4/웹엠/mp3/wav 등) 를 다운로드하고
html_local 에 로컬 경로로 삽입하는 opt-in 파이프라인. YouTube/Vimeo iframe 은 수집하지 않는다.

## 실행

```bash
python run_all.py --media                  # 전체 미디어 수집
python run_all.py --media --custom         # custom_posts.txt 소스
python run_all.py --media --retry          # failed_media.csv 재처리
python run_all.py --media --force          # done_posts_media.csv 무시하고 전체 재처리
```

`--media` 는 **opt-in** 이다. 인수 없이 `python run_all.py` 실행 시 media 단계는 수행되지 않는다
(PIPELINE_ORDER 에 포함되지 않음). 전체 파이프라인 순서가 필요하면 `PIPELINE_FULL_ORDER = (html, images, media, md, html-local)` 를 참조.

## 수집 카테고리 (4종)

| 카테고리 | 설명 | 탐지 위치 | anchor_type |
|---|---|---|---|
| **A. GDrive 오디오** | BGM/OST/음악/사운드트랙/soundtrack 컨텍스트로 `_detect_non_image_urls` 가 이미지에서 제외한 GDrive 앵커 | 현재 blog HTML | `inline` |
| **B. `<video>`/`<audio>` 태그** | Ghost CMS 마이그레이션 생존분. 예: `<video src="www.lordofheroes.com/videos/*.mp4">`. poster 이미지도 같이 수집 | 현재 blog HTML | `inline` |
| **C. 포럼 시절 삭제된 미디어** | XE3 포럼 시대 `<video>` 가 Ghost 마이그레이션에서 제거된 케이스. Wayback 포럼 스냅샷 (`_fetch_wayback_post_soup`) 에서만 탐색 | Wayback 포럼 HTML | `positioned` / `append` |
| **D. 직접 미디어 확장자 앵커** | `<a href="*.mp4">`, `*.mp3` 등 | 현재 blog HTML | `inline` |

Cat C 대상 판별: 현재 blog HTML 에 `community-ko.lordofheroes.com/storage/app/public/thumbnails/` 참조가 있으면 포럼 시대 포스트로 간주 (`is_forum_era_post`). 해당 포스트에 대해서만 Wayback 포럼 HTML 을 추가로 scan.

## 파이프라인 (모듈 구조)

```
download_media/
  constants.py         # MEDIA_DIR, 파일 경로, anchor_type 상수
  state.py             # LineBuffer, 락, _failed_media_log (ImageFailedLog 재사용)
  persistence.py       # media_map.csv / seen / done_posts / failed 로드·기록
  fetch.py             # Content-Type 무관 바이트 fetch (_fetch_media, _fetch_wayback_media)
  collect.py           # collect_media_urls — 현재 HTML 스캔 (Cat A/B/D)
  wayback_discover.py  # discover_forum_media — Wayback 포럼 HTML (Cat C)
  download.py          # download_one_media — 직접 → Wayback 폴백
  runner.py            # _process_post_media + run_media (ThreadPoolExecutor)
  __init__.py          # run_media 재노출
```

fetch/Wayback 로직은 `download_images.fetch` 를 직접 재사용한다
(`_wayback_oldest`, `_cdn_to_forum_url`, `_fetch_wayback_post_soup`, `_get_content_tag`).
CDX 캐시는 `download_images.state._wayback_cache` 를 공유한다.

## 산출물 파일

ROOT_DIR 하위:

| 파일 | 내용 |
|---|---|
| `media/{category}/{YYYY}/{MM}/{filename}` | 다운로드된 비디오/오디오/포스터 파일 |
| `media_map.csv` | `post_url,media_url,relative_path,anchor_type,anchor_text` |
| `downloaded_media_urls.txt` | seen URL 세트 (`media:{clean_url}`) |
| `failed_media.csv` | `post_url,media_url,reason` |
| `done_posts_media.csv` | `post_url,media_count` (완료 플래그) |

`media_map.csv` 는 post 별로 여러 행을 가진다 (한 URL 이 여러 포스트에서 공유될 수 있음).
URL 단위 중복 제거는 `downloaded_media_urls.txt` 가 담당한다. post 단위 build index 는
`download_media.persistence.load_media_map_entries` → html_local 이 소비.

## Cat C 위치 기반 삽입 (핵심)

포럼 시절 `<video>` 가 마이그레이션에서 사라진 포스트에서, 원래 위치에 미디어를 다시 꽂는다.

### 수집 (`wayback_discover._extract_anchor_text`)

Wayback 포럼 HTML 의 각 media tag 직전에서 **의미 있는 텍스트 블록(`<p>`/`<h1-6>`/`<blockquote>`)** 의 tail 을 120자까지 추출한다. 추출된 텍스트는 `media_map.csv` 의 `anchor_text` 필드에 저장되고 `anchor_type=positioned`. 20자 미만이면 `anchor_type=append`.

### 주입 (`HtmlLocalizer._inject_recovered_media`)

1. `media_map` 의 post 별 `positioned` 엔트리를 순회
2. 현재 blog HTML 의 `.post-content` 내부에서 `anchor_text` 를 **부분 문자열**로 포함하는 가장 얕은 블록 요소를 찾음
3. 해당 요소 **직후** `<figure class="recovered-media"><video controls src="../../media/...">` 삽입
4. 매칭 실패 → `append` 로 강등
5. `append` 엔트리는 `.post-content` 말미에 단일 `<section class="recovered-media-append">` 블록으로 묶어서 추가

## stale 재생성

`download_html_local` 는 두 가지 트리거로 포스트를 재생성한다:

1. **image_map 또는 media_map 에 신규 매핑 추가** — 기존 stale 엔트리의 unmapped URL 이 해소되면 refresh
2. **media_map 에 해당 post 엔트리 존재** — media 를 주입해야 하므로 무조건 refresh (`--force` 없어도)

두 번째 규칙 때문에 `--media` 실행 후 `--html-local` 을 다시 돌리면 자동으로 반영된다.

## 제약 / v1 한계

- Cat C `<video poster=>` 는 무시 (v1 에서는 poster 없이 `<video>` 만 주입)
- Cat C 앵커 텍스트 매칭은 첫 번째 매치를 사용. 동일 텍스트가 여러 번 등장하면 첫 위치에 삽입 (position_hint 미구현)
- 해시 기반 content dedup 없음. URL 단위로만 dedup. 동일 바이트가 다른 URL 로 오면 두 파일 저장
- `www.lordofheroes.com/videos/*.mp4` 등 현재 404 인 URL 은 Wayback 폴백으로 복구

## 실패 처리

`failed_media.csv` 는 `download_images.models.ImageFailedLog` 를 재사용하므로 3-tuple `(post_url, media_url, reason)` 구조. `--retry --media` 는 해당 파일에서 post_url 을 추출해 재처리하고, 성공한 media_url 은 `remove_from_failed_media` 로 엔트리 제거.
