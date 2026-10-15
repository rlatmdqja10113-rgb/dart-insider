# dart-insider — 매일 아침 "임원이 자기 돈으로 산 것"만 골라 보내주는 텔레그램 브리핑

DART의 **임원ㆍ주요주주 특정증권등 소유상황보고서**를 밤마다 자동 수집해서,
**개인 임원·대주주가 자기 회사 주식을 장내에서 사고판 것만** 골라 아침 7시에 텔레그램으로 보낸다.

공시 데이터를 다 보여주는 사이트(예: GoInsider)는 이미 있다. 이 프로젝트는 그 반대다 —
**안 보여주는 것**이 상품이다.

| 걸러내는 것 | 이유 |
|---|---|
| 자산운용사·연기금·모회사·조합 등 **법인의 지분 변동** | 포트폴리오 조정이지 "내부자가 자기 돈으로 산 것"이 아니다. 별도 섹션으로 뺀다 |
| 스톡옵션 행사·증여·상속·무상증자 | 사고판 게 아니다. 아예 뺀다 |
| 옵션 행사 직후 매도 | 현금화다. 남기되 ⚠ 표시 |
| 1천만 원 미만 거래 | 신호가 약하다 (기준 조정 가능) |

| 묶어주는 것 | 이유 |
|---|---|
| 같은 회사에서 일주일 안에 임원 2명 이상 매수 (**동반 매수**) | 단독 매수보다 신호가 강하다고 알려진 패턴 |
| 직위 순 정렬 (대표이사 > 사장 > 등기임원 > 사외이사) | 회사 사정을 잘 아는 사람일수록 위로 |
| 맨 위 한 줄 요약 | "오늘 볼 게 있나"를 3초 안에 |

추천 문구는 없다. 공개 데이터의 정리·알림만 한다 (유사투자자문업 신고 대상이 되지 않도록).

## 무료 / 유료 채널

| | 무료 채널 | 유료 채널 |
|---|---|---|
| 한 줄 요약, 동반매수 | 상위 3곳 | 전체 |
| 임원 매수 / 매도 | 상위 5건 / 3건 | 전체 (기본 15건) |
| 수량·단가 상세 | ✗ | ✓ |
| 법인·기관 지분 변동 | 건수만 | 목록 |

무료 채널이 유입구, 유료 채널이 수익이다. 유료 채널 결제는 텔레그램 유료 구독(Stars) 또는
스티비·메일리 같은 플랫폼의 유료 구독 링크를 쓰면 결제 시스템을 직접 만들 필요가 없다.

## 15분 안에 띄우기

1. **DART API 키** — https://opendart.fss.or.kr → 인증키 신청. 무료, 하루 2만 건.
2. 설치
   ```bash
   cd dart-insider
   pip install -r requirements.txt
   cp .env.example .env     # DART_API_KEY 채우기
   ```
3. 첫 실행 (발송 없이 리포트 파일만)
   ```bash
   python -m insider run --days 7 --dry-run
   cat data/reports/$(date +%F).telegram.txt   # 텔레그램에 갈 내용 그대로
   python -m insider stats                     # parsed_fail 이 많으면 아래 '알아둘 것' 참고
   ```

## 텔레그램 채널 만들기

1. **봇**: 텔레그램에서 @BotFather → `/newbot` → 이름 정하면 토큰이 나온다 → `.env` 의 `TELEGRAM_BOT_TOKEN`
2. **무료 채널**: 텔레그램 → 새 채널 → 공개 채널로 만들고 아이디 정하기 (예: `@insider_brief`)
   → 채널 관리자에 방금 만든 봇 추가 (메시지 작성 권한) → `.env` 의 `TELEGRAM_CHAT_ID=@insider_brief`
3. **유료 채널** (나중에): 비공개 채널을 하나 더 만들고 봇을 관리자로 추가 → 채널의 chat id 는
   봇에게 채널 글을 하나 전달(forward)한 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 에서 `-100…` 으로 시작하는 id 확인
   → `.env` 의 `TELEGRAM_PAID_CHAT_ID`. 비워두면 유료 발송은 건너뛴다.
4. 테스트 발송: `python -m insider run` (dry-run 빼면 실제로 간다)

## 밤에 알아서 돌게 하기 — 내 컴퓨터 꺼져 있어도 됨

**GitHub Actions (서버 없음, 무료)**

1. GitHub 에 저장소를 만들고 이 폴더를 올린다 (`.env` 는 `.gitignore` 에 있어서 안 올라간다).
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에
   `DART_API_KEY`(필수), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, (유료 시작하면) `TELEGRAM_PAID_CHAT_ID`.
3. (선택) 같은 화면 **Variables 탭**에 `SITE_TITLE`, `SITE_TAGLINE`, `SITE_SUBSCRIBE_URL`(= 무료 채널 주소 `https://t.me/insider_brief`).
4. (선택) 아카이브 사이트: **Settings → Pages → Source** 를 **GitHub Actions** 로 바꾸고, Variables 에
   `ENABLE_PAGES` = `true` 추가. `https://<아이디>.github.io/<저장소>/` 에 매일 갱신된다 (비공개 저장소는 유료 요금제 필요).
5. **Actions 탭 → nightly-insider-report → Run workflow** 로 한 번 수동 실행.

이후 평일 아침 07:10 KST 에 GitHub 서버에서 수집 → **아직 안 보낸 공시만** 텔레그램 발송 → DB 저장이 자동으로 돈다.

- **월요일 아침**엔 금요일 낮~주말 접수분이 나간다. 접수일이 메시지 첫 줄에 찍힌다 (`10/16 접수 공시 47건 중 …`).
- **같은 날 여러 번 돌려도** (수동 실행, Re-run) 이미 보낸 공시는 다시 안 나간다. 새 공시가 없으면 조용히 끝난다.
- 새 공시가 스톡옵션·증여 같은 제외 대상뿐이면 '볼 것 없음' 메시지는 보내지 않는다.
- **발송이 실패하면** 실행이 빨간 X 로 끝나고(GitHub 가 메일로 알려줌), 다음 실행이 그 공시를 다시 보낸다.
- 내 컴퓨터에서 `run` 을 돌리면 GitHub 쪽과 별개로 채널에 또 올라간다. 로컬에선 `--dry-run` 이나 `report --unsent` 만 쓴다.

**직접 서버 / 라즈베리파이**: `crontab.example` 참고.

## 명령어

| 명령 | 설명 |
|---|---|
| `python -m insider run [--days 7] [--max-age 5] [--dry-run]` | 수집 → 원문 파싱 → 아직 안 보낸 공시 리포트·발송 → 사이트 |
| `python -m insider report --unsent` | 다음 실행 때 나갈 내용 미리보기 (DB 를 바꾸지 않음) |
| `python -m insider report --date 2026-10-15 --lookback 1` | 접수일 기준 기간 리포트 출력 (마크다운 + 무료/유료 텔레그램) |
| `python -m insider backfill --days 365` | 과거 데이터 적재 (나중에 "이 회사 임원이 샀을 때 주가는?" 통계용) |
| `python -m insider site [--days 30]` | 정적 사이트만 생성 |
| `python -m insider stats` | DB 현황 |
| `python -m insider reparse` | 저장된 원문(`data/raw`)으로 파서만 다시 실행. 네트워크 불필요. 파서를 고친 뒤 쓴다 |

`--days` 는 공시 **목록 조회 범위**(이미 받은 공시는 DB 가 걸러준다), `--max-age` 는 **이보다 오래된 미발송 공시는
보내지 않는다**는 기준이다. DART 가 원문을 늦게 올려 실패한 공시(`014 파일이 존재하지 않습니다`)는 `--days` 안에서 매번 다시 시도한다.

## 구조

```
insider/
  config.py       .env → 설정
  dart_client.py  DART OpenAPI (list.json / document.xml)
  parser.py       공시 원문 표에서 거래 행 추출, 사유 분류(buy/sell/other)
  scoring.py      개인/법인 판별(reporter_kind), 직위·금액 가중치, 옵션행사 후 매도 판정, 동반매수
  report.py       텔레그램 메시지(무료/유료 티어, HTML) + 마크다운
  notify.py       텔레그램(채널, 4096자 분할), 이메일
  site.py         정적 아카이브 사이트
  pipeline.py     위를 잇는 run_daily()
  db.py           SQLite (filings, trades, reports)
tests/            네트워크 없이 도는 테스트 7개 + DART 원문 모양의 픽스처
```

## 알아둘 것

- **원문 표 구조는 회사마다 조금씩 다르다.** 파서는 "보고사유"+"증감" 헤더가 있는 표를 찾아 느슨하게 읽는다.
  실패한 공시는 `filings.parsed = -1` 에 사유와 함께 남는다. 처음 몇 주는
  `sqlite3 data/insider.sqlite "SELECT corp_name, parse_error FROM filings WHERE parsed=-1"` 로 보면서 `parser.py` 를 다듬는다.
  이 프로젝트에서 사람 손이 가는 거의 유일한 부분이다.
- **개인/법인 판별**(`scoring.reporter_kind`)은 이름 패턴 기반이다. 잘못 분류된 게 보이면 `ENTITY_RE` 에 단어를 추가한다.
- 2026-10-16 기준 실제 공시 196건으로 검증했다 (거래 576행, 파싱 실패 0). 서식이 바뀌면 `data/raw` 의 원문을 보고 `parser.py` 를 고친 뒤 `reparse` 로 확인한다.
- 내부자 매수(특히 동반매수)의 통계적 근거는 학계에서 꾸준히 보고되지만 개별 종목에선 당연히 틀린다.
  메시지 하단 문구를 지우지 말 것.

## 다음 단계

1. 2~4주 무료 채널에 돌려서 리포트 품질 확인 → 주식 커뮤니티에 리포트 몇 장 공유
2. 구독자 300~500명 → 유료 채널 열기 (월 5천~1만 원)
3. `backfill` + 시세 API 로 "이 종목 임원 매수 후 3개월 수익률" 통계 → 유료 전용
4. 관심 종목 등록 → 즉시 알림 (현재는 하루 1회)
