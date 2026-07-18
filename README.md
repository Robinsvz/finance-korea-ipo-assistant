# Korea IPO Telegram Bot

국내 공모주 일정을 수집하고 OpenDART 공시·재무, 네이버 뉴스·검색 추이, 증권사별 예상 배정, 시장 환경, 상장 후 가격을 연결하는 개인용 알림·분석 시스템입니다. 데이터 수집부터 D3 결과 평가와 등급별 백테스트까지 스케줄러가 자동 실행합니다.

이 프로젝트는 참고용 데이터 정리 도구입니다. 청약 신청, 증권사 로그인, 주문 제출, 매수·매도 자동화 기능은 없으며 출력 점수와 등급은 투자 추천이나 수익 보장이 아닙니다.

## 주요 기능

- KIND 전체 페이지 일정 수집, 일정 필드 보존, 라이프사이클 상태 자동 갱신
- OpenDART 법인코드 캐시, 공시 페이지네이션·정정 계보, 표 항목 기반 수요예측 추출
- 연결재무제표(CFS) 우선 및 별도재무제표(OFS) fallback
- 네이버 뉴스 기간 기반 페이지네이션, 관련성 필터, 재배포 중복·부정형 위험 표현 제거
- `pykrx` 우선, FinanceDataReader 보조 방식의 실제 D0/D1/D3 거래일·가격 수집
- YAML 기반 rules-v2 100점 평가, 위험 패널티, 데이터 완성도와 `Insufficient Data`
- 증권사 설정과 최근 실제 경쟁률을 이용한 균등·비례 예상 배정
- 상장일 09:00 KST 이전 마지막 점수만 사용하는 결과 평가와 등급별 백테스트
- Telegram 허용 채팅 목록, 긴 응답 분할, 알림 멱등성, 수집 상태·출처·점수 근거 조회
- Alembic 마이그레이션, 잡 실행 이력, 운영 경보, Docker Compose, GitHub Actions CI

## 요구 환경

- Python 3.9 또는 3.11
- SQLite 3
- Docker 운영 시 Docker Engine과 Compose
- 실제 외부 수집 시 OpenDART 및 Naver API 자격증명
- Telegram 운영 시 봇 토큰, 기본 채팅 ID, 허용 채팅 ID

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

`requirements.lock`은 Python 3.9/3.11에서 검증한 재현용 잠금 파일이며 가격 수집 패키지와 개발 품질 도구도 포함합니다. 범위 지정 의존성만 필요하면 `requirements.txt`를 사용할 수 있습니다.

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
| `HTTP_TIMEOUT_SEC` | 외부 요청 타임아웃 | 기본값 20초 |
| `HTTP_MAX_RETRIES` | 공통 HTTP 최대 재시도 횟수 | 기본값 3 |
| `HTTP_BACKOFF_BASE_SEC` | 지수 백오프 기준 시간 | 기본값 0.5초 |

키가 없어도 마이그레이션, KIND, 샘플 데이터, 점수·배정 계산, 테스트는 동작합니다. 자격증명이 필요한 수집 잡은 누락 이유를 반환합니다. Telegram 봇은 허용 채팅 목록이 없으면 안전하게 기동을 거부합니다.

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
.venv/bin/python -m app.cli collect-prices 종목명

# 계산과 사후 검증
.venv/bin/python -m app.cli score 종목명
.venv/bin/python -m app.cli score --all
.venv/bin/python -m app.cli calc 종목명 1000만원 --broker 증권사명
.venv/bin/python -m app.cli evaluate 종목명
.venv/bin/python -m app.cli result 종목명
.venv/bin/python -m app.cli backtest
```

같은 증권사의 실제 신청자 수와 비례 경쟁률을 기록하면 최근 3건 이상부터 중앙값과 분위수 기반 가정을 우선 사용합니다.

```bash
.venv/bin/python -m app.cli record-allocation-actual 종목명 한국투자증권 250000 1320.5 --source manual
```

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
| `/score 종목명` | rules-v2 점수, 완성도, 누락 데이터 |
| `/calc 종목명 금액 [증권사]` | 증권사별 균등·비례 예상 배정 |
| `/trend 종목명`, `/financial 종목명` | 뉴스·검색 및 재무 요약 |
| `/result 종목명`, `/backtest` | 상장 후 결과와 등급별 검증 |
| `/status` | 잡별 최근 상태와 마지막 오류 |
| `/sources 종목명` | 원본 출처와 수집 시각 |
| `/why 종목명` | 점수 상승·감점 근거 |
| `/refresh 종목명` | 허용된 사용자만 해당 종목 외부 수집 재시도 |

모든 명령은 `TELEGRAM_ALLOWED_CHAT_IDS`를 먼저 검사하고, `/refresh`는 `TELEGRAM_REFRESH_ALLOWED_USER_IDS`와 사용자별 60초 cooldown도 추가 검사합니다. 3,900자를 넘는 응답은 줄 경계를 우선해 분할합니다. 동일 요약·운영 경보는 lease가 있는 이벤트 키로 중복 발송을 막습니다.

## 자동 스케줄

```bash
.venv/bin/python -m app.cli run-scheduler
```

| KST | 잡 |
|---|---|
| 07:00 | KIND 일정 전체 페이지 수집 |
| 07:10 | IPO 상태 갱신 |
| 07:15 | 활성 IPO DART 공시 수집 |
| 07:40 | 활성 IPO 재무 수집 |
| 08:00 | 활성 IPO 뉴스·검색 수집 |
| 08:15 | KOSDAQ·최근 IPO 시장 환경 수집 |
| 08:20 | 활성 IPO 점수 upsert |
| 08:30 | 오늘 일정 알림 |
| 월요일 08:35 | 주간 일정 알림 |
| 16:30 | 상장일~D3 가격 수집 |
| 17:00 | D3 결과 평가 |
| 매월 1일 09:00 | 등급별 백테스트 알림 |

각 잡은 `max_instances=1`, `coalesce=true`, 1시간 misfire 유예를 사용합니다. 종목 단위 작업은 한 종목 실패가 배치를 중단하지 않으며 실패한 종목만 1회 추가 재시도합니다. 결과는 `succeeded`, `partial`, `failed`로 `job_runs`에 기록되고 실패·부분 실패는 Telegram 운영 경보로 전달됩니다.

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

- `configs/scoring_rules.yaml`: 100점 가중치, 등급 경계, 완성도, 위험 키워드, 시장 기준
- `configs/brokers.yaml`: 증권사 별칭, 수수료, 증거금률, 청약 단위, 배정 기본 가정
- `configs/sources.yaml`: 외부 소스 활성화·우선순위·호출 간격·캐시 정책

점수 가중치 합이 100이 아니거나 등급 경계가 역전되면 점수 엔진이 실행을 거부합니다. 점수 스냅샷에는 `model_version`, 설정 전체의 `rules_hash`, 계산 시각을 저장합니다.

## 테스트와 품질 게이트

```bash
.venv/bin/ruff check app tests alembic
.venv/bin/ruff format --check app tests alembic
.venv/bin/pytest --cov=app --cov-report=term-missing
.venv/bin/alembic check
.venv/bin/python -m pip check
```

테스트는 외부 네트워크 없이 KIND/DART/Naver/가격/Telegram 계약, 상태 전이, 자연키 upsert, 점수 경계·완성도, 배정 경곗값, 한국 거래일, 시점 누출, 결과 평가, 백테스트, 빈 DB 마이그레이션을 검증합니다. GitHub Actions는 Python 3.9와 3.11에서 Ruff, Alembic, pytest·coverage, 의존성 검사를 실행합니다.

## 데이터·보안 원칙

- 원문 스냅샷은 소스, 소스 키, SHA-256 해시, 수집 시각과 연결합니다.
- API 키·토큰·DB 자격증명은 오류 메시지와 운영 로그에서 마스킹합니다.
- 환불일과 납입일은 분리하며 환불일 근거가 없으면 `NULL`과 `미확정`을 유지합니다.
- 데이터가 부족하면 낮은 점수 등급 대신 `Insufficient Data`를 표시합니다.
- 상장 전 평가에는 상장 당일 09:00 KST 이후 생성된 점수를 사용하지 않습니다.
- 자동화 범위는 수집·계산·알림까지이며 모든 투자 행동은 사용자가 직접 결정합니다.
