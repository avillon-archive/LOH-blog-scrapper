# LOH Blog Scrapper

Claude Sonnet/Opus 4.6과 GPT-5.3-Codex을 이용하여 작성한 사이트맵 기반 블로그 포스트 스크래퍼.

이 스크립트는 서비스 종료가 예상되는 게임 블로그의 콘텐츠를 보존하기 위한 용도로 제작되었습니다.

> **면책**: 사용자는 대상 사이트의 `robots.txt`와 이용약관을 직접 확인할 책임이 있습니다. rate limit 기본값은 서버 부하를 최소화하도록 보수적으로 설정되어 있으니, 이를 높이지 않는 것을 권장합니다.

## 설정

`config.default.toml`을 `config.toml`로 복사한 뒤 원하는 값만 수정합니다. `config.toml`이 없으면 `config.default.toml`의 기본값이 사용됩니다.

```bash
cp config.default.toml config.toml
```

주요 설정 항목:

| 섹션 | 항목 | 설명 |
|------|------|------|
| `[paths]` | `output_dir` | 산출물 디렉토리 (절대 경로 권장) |
| `[network]` | `default_max_workers`, `blog_rate_limit` 등 | 병렬 워커 수, rate limit, 타임아웃, 재시도 |
| `[urls]` | `blog_host`, `wayback_cdx_api` 등 | 블로그 도메인, CDN, 외부 API |
| `[categories]` | `valid`, `tags.*` | 유효 카테고리, 다국어 카테고리 매핑 |
| `[file_types]` | `img_exts`, `dl_keywords` 등 | 수집 대상 파일 확장자, 다운로드 키워드 |

## 빠른 시작

```bash
python run_all.py                    # 전체 파이프라인 (html → images → {md, html-local}, media 제외)
python run_all.py --images           # 이미지만 수집
python run_all.py --media            # 비이미지 미디어(mp4/오디오 등)만 수집 (opt-in)
python run_all.py --html-local       # 오프라인 HTML만 재생성
python run_all.py --retry            # 실패 목록 재처리 (원본/Wayback)
python run_all.py --retry --media    # failed_media.csv 재처리
python run_all.py --retry-fallback   # 실패 이미지 multilang/kakao 폴백
python run_all.py --force            # 전체 재다운로드
```

전체 CLI 옵션은 [CONTEXT_RUN.md](CONTEXT_RUN.md) 참조.

## 이미지

- 현재 접근 불가능한 이미지는 [Wayback Machine](https://web.archive.org/)을 이용하여 과거 스냅샷으로부터 다운로드 시도
- 이미지 경로에서 `/size/w숫자`를 제거한 원본을 수집
- `img` 태그뿐만 아니라 `a` 태그로 제공된 고해상도를 수집(배경화면, 로페이퍼 등)
- 본문에 첨부된 것 외에 포스트 썸네일(`og:image`)도 수집
- 파일 경로는 카테고리/연도/월 구조를 따름

## MD

- 포스트 제목과 본문, 작성일, 원문 주소를 추출
- 파일 경로는 카테고리를 따름

## HTML

- 블로그 폐쇄를 대비한 HTML 원본 저장
- 파일 경로는 카테고리를 따름

## HTML-LOCAL

- 블로그 폐쇄를 대비해 HTML 원본의 내부 링크/이미지를 로컬 경로로 치환
- `media_map.csv` 가 있으면 `<video>`/`<audio>`/`<source>` src·poster 도 로컬 경로로 치환
- Ghost 마이그레이션 과정에서 제거된 XE3 포럼 시절 비디오는 Wayback 포럼 스냅샷에서 복구해 원래 위치에 재삽입 (`<figure class="recovered-media">`)
- 유튜브 임베딩은 로컬 `file://` 에서는 동작하지 않음 (의도적으로 치환 대상에서 제외)

## MEDIA (opt-in)

- `--images` 가 처리하지 않는 비이미지 미디어 전용 파이프라인. `PIPELINE_ORDER` 에 포함되지 않으며 `--media` 를 명시해야 실행
- 수집 대상: ① GDrive BGM/OST 앵커 ② 현재 HTML 의 `<video>`/`<audio>` 태그 ③ 포럼 시절 삭제된 비디오 (Wayback 복구) ④ 직접 미디어 확장자 앵커
- 상세: [CONTEXT_MEDIA.md](CONTEXT_MEDIA.md)