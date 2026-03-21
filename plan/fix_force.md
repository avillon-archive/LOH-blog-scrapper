# retry 로그 안전성 강화 + --force 옵션 보완

## Context
retry 시 failed 일괄삭제 버그로 실패 기록이 사라지는 문제 발생.
유저가 이미지 폴더와 기록을 모두 삭제하고 새로 다운로드 예정.
레거시 호환 불필요.

## 대상 파일
- [download_images.py](download_images.py)
- [run_all.py](run_all.py)

---

## 수정 1: `--force`에서 `seen_urls`도 비우기 (이미 완료)

현재 `--force`는 `done_post_urls`만 비움. `seen_urls`(downloaded_urls.txt)도 비워야 진정한 전체 재다운로드.
`img_hashes`는 유지 → 동일 콘텐츠 중복 저장 방지 (디스크 부담 없음).

```python
seen_urls = set() if force_download else load_seen(DONE_FILE)
```

---

## 수정 2: `_load_done_post_urls` 레거시 호환 제거 (기록 삭제 완료)

레거시 형식(URL만) 호환 코드 제거. 항상 `post_url\timage_count` 형식 기대.
count 없는 행은 무시하거나 0으로 처리.

```python
def _load_done_post_urls(filepath: Path) -> dict[str, int]:
    if not filepath.exists():
        return {}
    result: dict[str, int] = {}
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        url = parts[0].strip()
        count = int(parts[1]) if len(parts) >= 2 and parts[1].strip().isdigit() else 0
        result[url] = count
    return result
```

`process_post` 진입부에서 레거시 분기(`stored_count < 0`) 제거.
count가 없으면 0으로 처리 → 이미지가 0개 이상이면 재처리됨 (안전).

---

## 검증
1. `python run_all.py --images --force` — seen_urls 무시, img_hashes로 중복 방지 확인
2. `done_posts_images.txt`에 `url\tcount` 형식으로 기록되는지 확인
3. 이미지 수 변경 재처리 검증: 1차 실행 후 `done_posts_images.txt`에서 특정 포스트의 count를 수동으로 변경 → 재실행 시 해당 포스트만 재처리되는지 확인
