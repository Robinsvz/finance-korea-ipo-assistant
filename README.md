# Korea IPO Assistant

국내 공모주 일정, OpenDART 공시·재무, 네이버 뉴스·검색 추이, 증권사별 예상 배정, 시장 환경, 상장 후 가격을 연결하는 개인용 분석·알림 시스템입니다. Telegram과 이메일을 지원하고, 실제 청약·배정·매도 결과를 별도 원장에 기록해 판단과 실현 성과를 분리합니다.

이 프로젝트는 참고용 데이터 정리 도구입니다. 청약 신청, 증권사 로그인, 주문 제출, 매수·매도 자동화 기능은 없으며 출력 점수와 등급은 투자 추천이나 수익 보장이 아닙니다.

## 주요 기능

- KIND 전체 페이지 일정 수집, 일정 필드 보존, 라이프사이클 상태 자동 갱신
- OpenDART 법인코드 캐시, 공시 페이지네이션·정정 계보, 표 항목 기반 수요예측 추출
- 연결재무제표(CFS) 우선 및 별도재무제표(OFS) fallback
- 네이버 뉴스 기간 기반 페이지네이션, 관련성 필터, 재배포 중복·부정형 위험 표현 제거
- 종목코드 정확 일치 확인과 `pykrx`·FinanceDataReader fallback을 이용한 D0/D1/D3 거래일·가격 수집
- `rules-v3-simple` 4개 축 평가: 수요 45, 공급 30, 가격 15, 시장 10
- 중대 위험 hard gate, 데이터 완성도와 `Insufficient Data`; 재무·뉴스·배정은 총점과 분리
- 증권사 설정과 최근 실제 경쟁률을 이용한 균등·비례 예상 배정
- 사용자 현금·예상 배정·수수료·기회비용을 반영한 `BLOCKED`/`DATA_REQUIRED`/`VALUE_SKIP`/`RESEARCH_ONLY`/`CANDIDATE` 판단
- 청약 시작일 09:00 KST 이전 마지막 점수만 사용하는 시점 누출 방지 검증과 D0/D1 분리 백테스트
- 과거 티커·가격·성과 백필, 데이터 준비도 보고서, 재현 가능한 검증 노트북
- 일반청약 시작 D-1 종목별 facts-only 이메일, 기관 가격분포 차트, 수신자별 멱등성, STARTTLS/SSL, 부분 실패 격리
- 실제 청약·배정·분할 매도 원장과 실현 손익·투입 현금 수익률 집계
- Telegram 허용 채팅 목록, 긴 응답 분할, 알림 멱등성, 수집 상태·출처·점수 근거 조회
- Alembic 마이그레이션, 잡 실행 이력, 운영 경보, Docker Compose, GitHub Actions CI

현재 데이터 준비도와 단계별 운영 기준은 [MASTER_PLAN.md](MASTER_PLAN.md)를 기준 문서로 사용합니다. 기존 v1/v2 상세 이력은 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md)에 보존합니다.

## 요구 환경

- Python 3.9 또는 3.11
- SQLite 3
- Docker 운영 시 Docker Engine과 Compose
- 실제 외부 수집 시 OpenDART 및 Naver API 자격증명
- Telegram 운영 시 봇 토큰, 기본 채팅 ID, 허용 채팅 ID
- 이메일 운영 시 SMTP 서버와 발신·수신 주소

## 로컬 설치

```bash
cd /Users/jk/Documents/GitHub/korea-ipo-telegram-bot
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli healthcheck
```

`requirements.lock`은 Python 3.9/3.11에서 검증한 개발·CI 재현용 잠금 파일이며 가격 수집 패키지와 품질 도구를 포함합니다. Docker 운영 이미지는 테스트 도구를 제외한 `requirements.runtime.lock`만 설치합니다. 범위 지정 의존성만 필요하면 `requirements.txt`를 사용할 수 있습니다.

## 환경 변수

`.env.example`을 `.env`로 복사한 뒤 필요한 값을 입력합니다. `.env`는 Git에서 제외됩니다.

| 변수 | 용도 | 필수 조건 |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy DB URL | 기본값 `sqlite:///data/ipo.db` |
| `IPO_TIMEZONE` | 스케줄 시간대 | 기본값 `Asia/Seoul` |
| `IPO_DEFAULT_USER_CASH` | 점수 계산용 기본 청약 현금 | 기본값 1천만원 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API 토큰 | 봇·발송 운영 시 필수 |
| `TELEGRAM_CHAT_ID` | 자동 알림 기본 수신자 | 자동 알림 시 필수 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 명령을 허용할 개인·그룹 채팅 ID 목록 | 봇 실행 시 필수, 쉼표 구분 |
| `TELEGRAM_REFRESH_ALLOWED_USER_IDS` | `/refresh` 쓰기 작업 허용 개인 user ID | `/refresh` 사용 시 필수, 쉼표 구분 |
| `DART_API_KEY` | OpenDART API 키 | DART 수집 시 필수 |
| `NAVER_CLIENT_ID` | Naver API 클라이언트 ID | 뉴스·DataLab 수집 시 필수 |
| `NAVER_CLIENT_SECRET` | Naver API 클라이언트 시크릿 | 뉴스·DataLab 수집 시 필수 |
| `EMAIL_TO` | v3 운영 이메일 수신자 | `dummy1.kim@gmail.com` 단일 고정 |
| `EMAIL_FROM` | 발신 주소 | 이메일 발송 시 필수 |
| `EMAIL_REPLY_TO` | 회신 주소 | 선택 |
| `SMTP_HOST`, `SMTP_PORT` | SMTP 서버와 포트 | 이메일 발송 시 필수 |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | SMTP 인증 | 서버가 인증을 요구할 때 필수 |
| `SMTP_SECURITY` | `starttls` 또는 `ssl` | 기본값 `starttls` |
| `SMTP_TIMEOUT_SEC` | SMTP 연결 타임아웃 | 기본값 20초 |
| `HTTP_TIMEOUT_SEC` | 외부 요청 타임아웃 | 기본값 20초 |
| `HTTP_MAX_RETRIES` | 공통 HTTP 최대 재시도 횟수 | 기본값 3 |
| `HTTP_BACKOFF_BASE_SEC` | 지수 백오프 기준 시간 | 기본값 0.5초 |

키가 없어도 마이그레이션, KIND, 샘플 데이터, 점수·배정 계산, 이메일 미리보기와 테스트는 동작합니다. 기본 이메일 수신자는 `dummy1.kim@gmail.com`으로 고정되어 있습니다. 자격증명이 필요한 수집 잡은 누락 이유를 반환합니다. Telegram 봇은 허용 채팅 목록이 없으면 안전하게 기동을 거부하고, SMTP 발신자·호스트가 설정되지 않은 예약 발송은 외부 연결 없이 `skipped`로 종료합니다.

## 첫 동작 확인

```bash
.venv/bin/python -m app.cli seed-sample
.venv/bin/python -m app.cli week
.venv/bin/python -m app.cli ipo ABC테크
.venv/bin/python -m app.cli score ABC테크
.venv/bin/python -m app.cli calc ABC테크 1000만원 --broker 한국투자증권
.venv/bin/python -m app.cli trend ABC테크
.venv/bin/python -m app.cli financial ABC테크
.venv/bin/python -m app.cli backtest
.venv/bin/python -m app.cli validation-report --minimum-samples 30
.venv/bin/python -m app.cli preview-email
```

샘플 시드는 upsert 방식이라 반복 실행해도 동일 자연키 데이터가 늘어나지 않습니다.

## 수집·분석 CLI

```bash
# 일정과 상태
.venv/bin/python -m app.cli collect-schedule --from-date 2026-01-01 --to-date 2026-12-31
.venv/bin/python -m app.cli refresh-statuses
.venv/bin/python -m app.cli today
.venv/bin/python -m app.cli week

# 종목별 외부 데이터
.venv/bin/python -m app.cli collect-dart 종목명
.venv/bin/python -m app.cli collect-financials 종목명 --years 3
.venv/bin/python -m app.cli collect-news-trend 종목명
.venv/bin/python -m app.cli collect-market
.venv/bin/python -m app.cli resolve-ticker 종목명
.venv/bin/python -m app.cli collect-prices 종목명

# 과거 가격·성과 백필: 미래 날짜와 10년 초과 범위는 거부
.venv/bin/python -m app.cli backfill-history --from-date 2026-01-01 --to-date 2026-07-19

# 계산과 사후 검증
.venv/bin/python -m app.cli score 종목명
.venv/bin/python -m app.cli score --all
.venv/bin/python -m app.cli calc 종목명 1000만원 --broker 증권사명
.venv/bin/python -m app.cli evaluate 종목명
.venv/bin/python -m app.cli result 종목명
.venv/bin/python -m app.cli backtest
.venv/bin/python -m app.cli validation-report --minimum-samples 30 --csv analysis/validation.csv
```

`validation-report`는 기본적으로 현재 `rules-v3-simple`의 정확한 규칙 해시를 고정합니다. 기준일까지 상장한 실종목 중 청약 시작일 09:00 KST 전에 생성되고 hard gate를 통과했으며 당시 확보된 수요·공급 근거를 가진 점수와 D0/D1 성과의 완전한 쌍만 검증 표본으로 인정합니다. 과거 계보를 조회할 때는 `--model-version`과 `--rules-hash`를 함께 지정해야 하며, 최소 표본 30건 전에는 예측 확률이나 기대수익을 운영 판단에 노출하지 않습니다.

같은 증권사의 실제 신청자 수와 비례 경쟁률을 기록하면 최근 3건 이상부터 중앙값과 분위수 기반 가정을 우선 사용합니다.

```bash
.venv/bin/python -m app.cli record-allocation-actual 종목명 한국투자증권 250000 1320.5 --source manual
```

## 실제 참여와 실현 손익

예상 배정과 실제 참여 성과를 섞지 않도록 신청·배정·분할 매도를 별도 원장에 기록합니다. 사용자가 실제 값을 제공하기 전에는 어떤 참여 기록도 자동 생성하지 않습니다.

```bash
.venv/bin/python -m app.cli record-participation 종목명 한국투자증권 \
  --applied-shares 100 --cash-committed 1000000 --cash 5000000 \
  --allocated-shares 2 --fee 2000 --action RESEARCH_ONLY \
  --decision-at 2026-07-19T08:30:00+09:00
.venv/bin/python -m app.cli record-exit 1 --shares 1 --price 35000 --cost 50 \
  --strategy D0 --sold-at 2026-07-20T09:05:00+09:00
.venv/bin/python -m app.cli record-exit 1 --shares 1 --price 33000 --cost 50 \
  --strategy D1 --sold-at 2026-07-21T15:30:00+09:00
.venv/bin/python -m app.cli participation-summary 종목명
```

실현 순손익은 참여 시점에 고정한 최종 공모가를 기준으로 `매도대금 - 배정주식 공모가 원가 - 청약 수수료 - 매도 비용`으로 집계합니다. 원장 상태는 배정 대기·미배정·보유·일부 실현·전량 실현을 구분하며, 요약은 종목별 기록과 전체 누적 실현 손익을 함께 반환합니다.

## 이메일

```bash
# 외부 발송 없는 내용 확인
.venv/bin/python -m app.cli preview-email
.venv/bin/python -m app.cli preview-monthly-email --month 2026-07
.venv/bin/python -m app.cli preview-monthly-email --month 2026-07 --html --part 1
.venv/bin/python -m app.cli preview-d1-emails --send-date 2026-07-19 --manifest-only
.venv/bin/python -m app.cli preview-d1-emails --send-date 2026-07-19 --ipo 종목명 --html

# SMTP 설정 확인과 월간 감사본의 명시적 수동 canary
.venv/bin/python -m app.cli send-test-email --strict
.venv/bin/python -m app.cli send-monthly-email --month 2026-07 --canary
```

v3 자동 이메일은 **일반청약 시작일의 전날(D-1, KST 달력일) 18:30**에 종목마다 한 통씩 발송합니다. 기준은 상장일이 아닙니다. 같은 날 청약을 시작하는 IPO가 N개면 서로 합치지 않고 N개의 독립 메일을 만들며, 한 종목의 실패가 다른 종목 발송을 막지 않습니다. 월요일 주간 v3 집계 메일은 예약 발송에서 제거하거나 `skipped`로 끝내고, 월간 전체 화면은 모집단·과거 데이터 감사를 위한 명시적 preview/canary로만 보존합니다.

각 종목 메일은 본문 맨 위의 `한눈에 보기`에서 청약 일정·공모가·밴드·주관사와 핵심 사실을 먼저 보여 줍니다. 기관 가격분포는 공모가 밴드 대비 어느 가격 구간에 참여가 몰렸는지 확인할 수 있도록 **참여기관 수**와 **신청수량**을 서로 다른 막대로 표시합니다. 정확한 합계·출처·기준시각을 함께 적고, 가격 미제시 건은 별도 구간으로 둡니다. 원자료 비율 합계가 100%와 다르더라도 억지로 정규화하지 않으며, 확인되지 않은 값이나 사후 일반청약 결과를 D-1 사실로 가장하지 않습니다.

수신자는 `dummy1.kim@gmail.com` 한 명으로 고정됩니다. D-1 17:30에 공개 DART 근거를 다시 수집하고 18:10에 품질을 대조한 뒤 18:30에 발송합니다. 최신 공식 KIND 청약기간과 현재 일정의 일치만 일정 hard gate로 사용하며, DART 가격분포가 아직 없으면 종목을 숨기지 않고 `공시 전`과 품질 경고를 표시합니다. event key는 안정적인 offering identity의 SHA-256 요약과 청약 시작일로 고정해 근거가 갱신돼도 같은 occurrence를 재발송하지 않고, universe/evidence hash는 감사 metadata에 보존합니다. SMTP 결과가 불명확한 메일은 자동 재전송하지 않습니다. 현재 DB의 gross 과거 결과는 설명용 분포로만 표시하며, point-in-time 완전 표본 30건과 별도 검증 승인을 충족하기 전에는 양수 확률·기대수익·청약 추천을 만들지 않습니다.

D-1 전환의 코드·회귀·Docker 검증은 완료됐고, 실제 SMTP·inbox 검수는 다음 D-1 대상일에 수행합니다. 상세 증거와 미완료 항목은 [IPO_EMAIL_V3_AND_HISTORICAL_EVIDENCE_PLAN.md](plans/IPO_EMAIL_V3_AND_HISTORICAL_EVIDENCE_PLAN.md)의 P2.1과 진행 로그를 기준으로 판단합니다.

## 사람 검토 상태

공시나 기사가 수집됐다는 사실은 사람 검토 완료로 취급하지 않습니다. 자동 수집은 `collected`, 아래 명령으로 근거와 검토자를 남긴 경우만 `reviewed`입니다.

```bash
.venv/bin/python -m app.cli review-diligence 종목명
.venv/bin/python -m app.cli mark-reviewed 종목명 disclosure --by reviewer --note "수요예측 표 확인" --evidence opendart:접수번호
.venv/bin/python -m app.cli mark-reviewed 종목명 articles --by reviewer --note "주요 기사 확인" --evidence https://example.com/article
```

## Telegram

```bash
.venv/bin/python -m app.cli send-test-telegram --strict
.venv/bin/python -m app.cli run-bot
```

지원 명령은 다음과 같습니다.

| 명령 | 설명 |
|---|---|
| `/start`, `/help` | 전체 명령과 사용 예시 |
| `/today`, `/week` | 오늘·향후 7일 일정 |
| `/ipo 종목명` | 일정, 공모가, 증권사, 상태 |
| `/score 종목명` | rules-v3-simple 점수, gate, 완성도, 누락 데이터 |
| `/calc 종목명 금액 [증권사]` | 증권사별 균등·비례 예상 배정 |
| `/trend 종목명`, `/financial 종목명` | 뉴스·검색 및 재무 요약 |
| `/result 종목명`, `/backtest` | 상장 후 결과와 등급별 검증 |
| `/status` | 잡별 최근 상태와 마지막 오류 |
| `/sources 종목명` | 원본 출처와 수집 시각 |
| `/why 종목명` | 점수 상승·감점 근거 |
| `/refresh 종목명` | 허용된 사용자만 해당 종목 외부 수집 재시도 |

모든 명령은 `TELEGRAM_ALLOWED_CHAT_IDS`를 먼저 검사하고, `/refresh`는 `TELEGRAM_REFRESH_ALLOWED_USER_IDS`와 DB lease 기반 사용자별 60초 cooldown도 추가 검사합니다. 수동 갱신의 단계별 결과는 `telegram_manual_refresh` 잡 이력으로 기록됩니다. 3,900자를 넘는 응답은 줄 경계를 우선해 분할합니다. 동일 요약·운영 경보는 lease가 있는 이벤트 키로 중복 발송을 막습니다.

## 자동 스케줄

```bash
.venv/bin/python -m app.cli run-scheduler
```

| KST | 잡 |
|---|---|
| 07:00 | KIND 일정 전체 페이지 수집 |
| 07:05 | KIND 당월 신규상장 전체 페이지 수집 |
| 07:10 | IPO 상태 갱신 |
| 07:15 | 당월 IPO DART 공개 공시·문서 계보 수집 |
| 07:35 | 활성 IPO OpenDART API 공시 수집 |
| 07:40 | 활성 IPO 재무 수집 |
| 08:00 | 활성 IPO 뉴스·검색 수집 |
| 08:10 | KIND 공모·신규상장·DART 월간 근거 대조 |
| 08:15 | KOSDAQ·최근 IPO 시장 환경 수집 |
| 08:20 | 활성 IPO 점수 upsert |
| 08:30 | 오늘 일정 알림 |
| 월요일 08:35 | 주간 일정 알림 |
| 16:30 | 상장일~D3 가격 수집 |
| 17:00 | D3 결과 평가 |
| 17:10 | 다음 날 청약 시작 IPO의 KIND 일정 새로고침 |
| 17:20 | 다음 날 청약 시작 IPO의 KIND 신규상장 새로고침 |
| 17:30 | 다음 날 청약 시작 IPO의 공개 DART 근거 새로고침 |
| 18:10 | 다음 날 청약 시작 IPO의 일정·표시 근거 품질 대조 |
| 18:30 | 일반청약 시작 D-1 종목별 facts-only 이메일(대상 N개면 N통) |
| 매월 1일 09:00 | 등급별 백테스트 알림 |

각 잡은 `max_instances=1`, `coalesce=true`, 1시간 misfire 유예를 사용합니다. 종목 단위 작업은 한 종목 실패가 배치를 중단하지 않으며 실패한 종목만 1회 추가 재시도합니다. 결과는 `succeeded`, `partial`, `failed`로 `job_runs`에 기록되고 실패·부분 실패는 Telegram 운영 경보로 전달됩니다. Telegram과 이메일 발송 잡은 서로 독립적이라 한 채널 장애가 다른 채널을 중단시키지 않습니다. 주간 Telegram 일정 알림은 유지하지만 주간 v3 이메일은 발송하지 않습니다.

상태는 CLI `status` 또는 Telegram `/status`에서 확인합니다.

## Docker Compose 운영

먼저 `.env`를 구성하고 데이터 디렉터리의 백업 정책을 정합니다.

```bash
cp .env.example .env
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f scheduler telegram-bot
```

Compose는 `migrate`를 먼저 완료한 뒤 비루트 사용자로 `scheduler`와 `telegram-bot`을 각각 실행합니다. SQLite는 UID 10001이 쓸 수 있는 명명 볼륨 `korea-ipo-telegram-bot-data`에 저장하고, `configs/`는 읽기 전용으로 연결합니다. 컨테이너 루트 파일시스템은 읽기 전용이며 모든 Linux capability를 제거합니다. 헬스체크는 DB 연결과 Alembic head 일치를 검사하고 종료 유예 시간은 30초입니다.

운영 이미지는 `requirements.runtime.lock`을 사용하므로 `pytest`, coverage, Ruff를 포함하지 않습니다. `.dockerignore`는 로컬 가상환경, 테스트, Git 이력, 로컬 DB와 `.env`가 빌드 컨텍스트에 들어가지 않도록 차단합니다.

기존 호스트 `data/ipo.db`를 최초 1회 명명 볼륨으로 가져오려면 서비스를 시작하기 전에 다음처럼 복사합니다. 원본은 삭제하지 않습니다.

```bash
docker volume create korea-ipo-telegram-bot-data
docker run --rm \
  -v "$PWD/data:/from:ro" \
  -v korea-ipo-telegram-bot-data:/to \
  alpine sh -c 'cp -a /from/. /to/ && chown -R 10001:10001 /to'
docker compose up -d
```

```bash
docker compose down
```

## DB 마이그레이션, 백업, 복구

호스트에서 직접 실행할 때는 운영 변경 전 SQLite 파일을 정지 상태에서 백업하고 체크섬을 남깁니다. Compose 명명 볼륨은 별도의 volume backup 절차를 사용해야 합니다.

```bash
docker compose stop scheduler telegram-bot
cp data/ipo.db data/ipo.backup-YYYYMMDD-HHMMSS.db
shasum -a 256 data/ipo.backup-YYYYMMDD-HHMMSS.db
.venv/bin/alembic upgrade head
.venv/bin/alembic current
sqlite3 data/ipo.db "PRAGMA foreign_key_check;"
```

마이그레이션 롤백은 자동 downgrade가 아니라 백업 복원 방식입니다. 서비스를 정지한 뒤 문제가 생긴 DB를 별도 이름으로 보존하고 검증한 백업을 `data/ipo.db`로 복사한 다음 헬스체크를 실행합니다.

이 작업에서 생성한 v1 이전 백업은 `data/ipo.pre-v2-20260718.db`이며 SHA-256은 `3133f3e414a7d0e1f7ef828b5f3837e3666331c63e9d5114a69c395444635d8f`입니다.

## 설정 파일

- `configs/scoring_rules_v3.yaml`: 운영 `rules-v3-simple` 4개 축, 등급 경계, 완성도, hard gate, 시장 기준
- `configs/scoring_rules.yaml`: 기존 rules-v2 호환·과거 스냅샷 재현용
- `configs/brokers.yaml`: 증권사 별칭, 수수료, 증거금률, 청약 단위, 배정 기본 가정
- `configs/sources.yaml`: 외부 소스 활성화·우선순위·호출 간격·캐시 정책

점수 가중치 합이 100이 아니거나 등급 경계가 역전되면 점수 엔진이 실행을 거부합니다. 점수 스냅샷에는 `model_version`, 설정 전체의 `rules_hash`, 계산 시각과 입력 지문을 저장합니다. 점수는 기업 특성의 공통 비교값이고, 예상 배정·수수료·현금 기회비용은 사용자별 가치 판단 단계에서 별도로 계산합니다.

## 테스트와 품질 게이트

```bash
.venv/bin/ruff check app tests alembic scripts
.venv/bin/ruff format --check app tests alembic scripts
.venv/bin/pytest --cov=app --cov-report=term-missing
.venv/bin/alembic check
.venv/bin/python -m pip check
```

테스트는 외부 네트워크 없이 KIND/DART/Naver/가격/Telegram·SMTP 계약, 상태 전이, 자연키 upsert, 점수·gate 경계, 배정·가치 판단, 참여 원장, 한국 거래일, 시점 누출, 결과 평가, 백테스트, 빈 DB 마이그레이션을 검증합니다. GitHub Actions는 Python 3.9와 3.11에서 Ruff, Alembic, pytest·coverage, 의존성 검사를 실행합니다.

`app/` 전체의 statement와 branch coverage 하한은 모두 100%입니다. omit 예외 없이 CLI·외부 collector·포매터·스케줄러까지 계측하므로 미실행 문장이나 분기가 생기면 로컬 품질 게이트와 CI가 실패합니다. 현재 기준은 681개 테스트, 9,347문장·2,678분기, 누락·부분 분기 0입니다.

검증 노트북은 다음 명령으로 재생성한 뒤 처음부터 끝까지 실행할 수 있습니다.

```bash
.venv/bin/python scripts/build_validation_notebook.py
.venv/bin/python scripts/build_report_artifact.py
```

- `analysis/ipo_validation.ipynb`: 시점 일관성·준비도·성과 코호트 검증
- `analysis/ipo_report_artifact.json`: 동일 DB 스냅샷 기반 기술 리포트 원본

## 데이터·보안 원칙

- 원문 스냅샷은 소스, 소스 키, SHA-256 해시, 수집 시각과 연결합니다.
- 합성 샘플은 `is_sample`/`data_origin`으로 표시하고 성과 검증에서 제외합니다.
- API 키·토큰·DB 자격증명은 오류 메시지와 운영 로그에서 마스킹합니다.
- 환불일과 납입일은 분리하며 환불일 근거가 없으면 `NULL`과 `미확정`을 유지합니다.
- 데이터가 부족하면 낮은 점수 등급 대신 `Insufficient Data`를 표시합니다.
- 검증에는 청약 시작일 09:00 KST 전에 실제로 확보된 데이터와 마지막 점수만 사용합니다.
- D0 시초가와 D1 종가는 별도 성과로 보고, 수수료·배정 확률·현금 기회비용을 제외한 gross 성과를 실현 순손익으로 표현하지 않습니다.
- 자동화 범위는 수집·계산·알림까지이며 모든 투자 행동은 사용자가 직접 결정합니다.

## 공식 기준과 제한

- KRX의 신규상장 당일 가격제한폭은 공모가격을 기준으로 60~400%입니다: [KRX 업무규정 안내](https://regulation.krx.co.kr/contents/RGL/03/03020203/RGL03020203.jsp)
- OpenDART 고유번호 파일은 법인 고유번호와 종목코드를 연결하는 공식 기준입니다: [OpenDART 개발가이드](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DE001&apiId=AE00004)
- 일반청약자 물량의 균등배정 제도 배경은 금융위원회 공식 안내를 따릅니다: [금융위원회 보도자료](https://www.fsc.go.kr/po010106/74611?curPage=1&srchBeginDt=&srchCtgry=6&srchEndDt=&srchKey=&srchText=)
- 수요예측·공모가격 결정 관련 운영 규정은 최신 금융투자협회 규정을 확인해야 합니다: [금융투자협회 규정](https://law.kofia.or.kr/service/law/detailArticlePrint.do?contentSeq=305013&historySeq=1782&seq=140)

법령·거래소·협회 규정은 변경될 수 있으므로 운영 전 최신 원문을 다시 확인해야 합니다. 이 도구의 출력은 정보 정리와 개인 기록을 위한 것이며 투자자문이 아닙니다.
