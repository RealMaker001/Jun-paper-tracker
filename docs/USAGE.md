# Jun-paper-tracker 사용법

arXiv에서 주제별 최신 논문을 매일 수집해 이 저장소의 README와 목록 파일로 관리합니다.
**비용: 0원** — 무료 API(arXiv API, Hugging Face Papers API)와 GitHub Actions만 사용합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `config.toml` | 수집 주제·검색어·분류·주기 관련 설정 (여기만 고치면 됨) |
| `tracker.py` | 수집·코드 링크 확인·문서 생성 (Python 3.11+ 표준 라이브러리만 사용) |
| `data/papers.json` | 모든 논문 데이터 (한 줄에 논문 하나) |
| `data/state.json` | 마지막 데이터 변경 시각 |
| `README.md` | **자동 생성** — 주제별 요약과 최신 논문 |
| `lists/<주제>/` | **자동 생성** — 주제별 연도·월별 전체 목록 |
| `.github/workflows/collect-papers.yml` | 매일 자동 실행 |

`README.md`와 `lists/`는 실행할 때마다 다시 만들어지므로 직접 고치지 마세요.

## 동작 방식

1. 주제마다 arXiv에서 `제목 또는 초록에 phrases 중 하나` AND `categories 중 하나`인 논문을 최신 제출순으로 검색합니다.
2. 그 주제의 가장 최근 논문 날짜보다 `overlap_days`일 앞까지 훑습니다.
   실행이 며칠 실패해도 다음 실행에서 자동으로 따라잡습니다(최대 `max_pages_per_topic` x 100편).
3. 코드 링크
   - 초록·코멘트에 적힌 GitHub 주소를 먼저 씁니다.
   - 없으면 Hugging Face Papers에서 찾습니다. 새 논문은 바로 확인하고, 제출 후 1·3·7·14·30·60일째에 다시 확인합니다.
4. 데이터가 실제로 바뀐 날에만 커밋합니다. 새 논문이 없는 날에는 커밋이 생기지 않습니다.
5. 한 주제가 실패해도 나머지 주제는 저장합니다. 모든 주제가 실패한 경우에만 Actions가 실패로 표시됩니다.

## GitHub에서 쓰기

1. 이 폴더 내용을 GitHub 저장소에 올립니다.
2. 저장소 **Settings → Actions → General → Workflow permissions**를 **Read and write permissions**로 설정합니다.
3. **Actions** 탭 → *Collect arXiv Papers* → **Run workflow**를 누릅니다.
   처음에 더 많이 채우려면 `days`에 30 같은 값을 넣습니다.
4. 이후에는 매일 12:00(KST)에 자동으로 실행됩니다.

비용: 공개 저장소는 Actions가 무료입니다. 비공개 저장소는 무료 계정에 월 2,000분이 주어지고, 1회 실행은 수 분 수준입니다.
참고로 GitHub는 저장소에 60일 동안 활동이 없으면 예약 실행(schedule)을 멈춥니다. 이 트래커는 새 논문이 있을 때마다 커밋하므로 보통은 해당되지 않습니다.

## 내 PC에서 쓰기

```bash
python tracker.py                # 수집 + 코드 링크 + 문서 생성
python tracker.py --days 30      # 최근 30일 다시 수집
python tracker.py --topic slam   # 특정 주제만
python tracker.py --render-only  # 문서만 다시 생성
python tracker.py --skip-code    # 코드 링크 확인 생략 (빠름)
```

## 주제 추가·수정

`config.toml`에 블록을 추가합니다.

```toml
[[topics]]
name = "Depth Estimation"          # README 제목
slug = "depth-estimation"          # 폴더 이름 (영문 소문자·하이픈, 바꾸지 않기)
phrases = ["depth estimation", "monocular depth"]
categories = ["cs.CV"]             # 비우면 전체 분류
exclude = ["eye tracking"]         # 제외할 구문 (선택)
```

- 검색어를 바꾸면 `python tracker.py --days 30 --topic <slug>`로 과거분을 다시 채울 수 있습니다.
- 이미 수집된 논문은 검색어를 바꿔도 지워지지 않습니다. 지우려면 `data/papers.json`에서 해당 줄을 지우거나, 그 논문의 `topics` 값을 고칩니다.
- `slug`를 바꾸면 기존 논문과 연결이 끊어집니다.
