# Fix: Ghost CMS 이미지 원본 해상도 다운로드

## Context
Ghost CMS는 HTML에서 이미지 URL에 `/size/w1200` 같은 리사이즈 파라미터를 붙여서 제공한다. 현재 스크래퍼는 이 URL을 그대로 다운로드하기 때문에, 원본 1920x1080 이미지 대신 1200x675로 축소된 이미지가 저장된다. `clean_url()`이 이미 `/size/w\d+`를 제거하는 로직을 갖고 있지만, 중복 제거(dedup) 키 생성에만 사용되고 실제 다운로드 URL에는 적용되지 않는 것이 원인이다.

## 수정 사항

### 1. `collect_image_urls()._add()`에서 URL 정규화 (download_images.py:1291)

`results.append((url, utype))` → `results.append((clean_url(url), utype))`

추출 시점에서 `/size/w\d+`를 제거하면 이후 모든 다운로드/Wayback 조회에 원본 URL이 사용된다.

### 2. `_normalized_link_key()`에 `/size/w` 제거 추가 (download_images.py:318)

`path = urllib.parse.unquote(parsed.path or "")` 뒤에 `path = SIZE_W_RE.sub("", path)` 추가.

Wayback 아카이브 HTML에서 추출한 후보 URL에 여전히 `/size/w`가 포함될 수 있으므로, 매칭 시 양쪽 모두 정규화하여 방어적으로 처리한다.

### 수정 불필요 파일
- [utils.py](utils.py): `SIZE_W_RE`, `clean_url()` — 이미 존재, 변경 없음

## 재다운로드 시 추적 파일 처리

코드 수정 후 기존 이미지를 원본 해상도로 다시 받으려면 추적 파일 초기화가 필요하다.

- `seen_urls`(downloaded_urls.txt)는 `clean_url()` 기반 키를 사용 → 수정 후에도 같은 키가 생성되어 "already" 스킵됨
- `done_post_urls`(done_posts_images.txt)는 포스트 단위 완료 표시 → 포스트 자체가 스킵됨
- `image_hashes`는 1200px 해시를 갖고 있어 1920px 이미지와 불일치 → 중복 파일 발생 가능

**삭제 대상 파일** (loh_blog/ 하위):
1. `downloaded_urls.txt` — 이미지 단위 완료 기록
2. `done_posts_images.txt` — 포스트 단위 완료 기록
3. `image_hashes.tsv` — 콘텐츠 해시 → 경로 매핑
4. `image_map.tsv` — clean_url → 경로 매핑
5. `failed_images.txt` — 실패 기록 (초기화)
6. `images/` 폴더 내 기존 이미지 파일 삭제 (1200px 파일과 1920px 파일이 공존하는 것을 방지)

> `force_download` 플래그는 `done_post_urls`만 우회하고 `seen_urls`는 여전히 적용되므로, 플래그만으로는 불충분하다.

## 검증 방법
1. 추적 파일 삭제 후 스크래퍼 재실행
2. `------_09--_---_KO.EN.png` 파일 해상도가 1920x1080인지 확인
3. `image_map.tsv` 키가 기존과 동일한 형태인지 확인 (clean_url 기반이므로 구조 변화 없음)
