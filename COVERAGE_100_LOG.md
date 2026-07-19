# 전체 소스 커버리지 100% 진행 로그

## 목표

- 범위: `app/` 아래 모든 Python 모듈
- 기준: statement 100%, branch 100%
- 측정: omit 설정 없이 `coverage run --branch --source=app`
- 외부 네트워크, Telegram, SMTP 실제 발송은 테스트에서 금지하고 mock/fixture로 검증
- 운영 이미지에는 테스트 도구와 테스트 파일을 포함하지 않음

## 2026-07-19 기준선

| 항목 | 결과 |
|---|---:|
| 테스트 | 418 passed |
| 전체 statement | 5,349 |
| 미실행 statement | 1,057 |
| 전체 branch | 1,388 |
| partial branch | 84 |
| 전체 커버리지 | 79% |

주요 미충족 모듈은 CLI, 외부 수집기, DB lifecycle, formatter, jobs,
observability, scheduler, Telegram 경계다. 기존 핵심 도메인 서비스는 statement와
branch 모두 100%다.

## 작업 분할

- [완료] KIND·DART·Naver·HTTP·가격 수집기: 884문장·256분기, miss/partial 0
- [완료] jobs·observability·scheduler: 757문장·178분기, miss/partial 0
- [완료] CLI·DB·formatter·Telegram: 875문장·204분기, miss/partial 0
- [완료] 기본 수신자 `dummy1.kim@gmail.com` 고정 및 SMTP 미설정 no-op 안전성
- [완료] Python scheduler 이메일 등록·상태 로그 및 fake SMTP 전달 검증
- [완료] coverage omit 제거, 통합 회귀와 발견된 P1 수정
- [완료] Docker·마이그레이션·이메일 dry-run 최종 검증

## 중간 결과

### 인터페이스 경계

- 대상: `app/cli.py`, `app/db.py`, `app/services/formatter.py`,
  `app/services/telegram.py`
- 결과: 875 statements, 204 branches, 100%
- 테스트: 44 passed
- 실제 Telegram·네트워크 호출: 없음

### 잡·관측성·스케줄러

- 대상: `app/services/jobs.py`, `app/services/observability.py`,
  `app/services/scheduler.py`
- 결과: 757 statements, 178 branches, 100%
- 테스트: 관련 테스트 70 passed
- 이메일 일정: 매일 08:32, 월요일 08:37 KST
- 시작 로그: 고정 수신자 수, 이메일 잡 등록, SMTP enabled/waiting만 기록
- 실제 SMTP·Telegram·외부 네트워크 호출: 없음

### 외부 수집기

- 대상: `app/collectors` 전체
- 결과: 884 statements, 256 branches, 100%
- 테스트: 97 passed
- 실제 KIND·DART·Naver·시세 네트워크 호출: 없음

### 고정 이메일 수신자

- 기본 설정: `dummy1.kim@gmail.com`
- SMTP 미설정: notification 예약과 SMTP 연결 없이 `skipped`
- 완전한 SMTP 설정: Fake SMTP STARTTLS로 기본 수신자 1건 전달
- 실제 외부 이메일 발송: 없음

## 전체 통합 최종 결과

- coverage omit 목록 제거 완료
- 전체 `app/`: 5,353 statements, 1,386 branches
- coverage: miss 0, partial 0, 100%
- 테스트: 511 passed, 16 warnings, 실패 0
- Alembic 로깅 설정이 기존 앱 로거를 비활성화하던 회귀 수정
- 시장 성공률은 IPO별 cutoff 이전 최신 완전 D1 평가 1건으로 정규화하고,
  정규화 후 최근 20개 IPO만 집계하도록 수정
- CI와 로컬 `pytest --cov=app`이 CLI·collector·formatter·scheduler를 포함한
  모든 `app/` 모듈을 동일한 100% gate로 검사

## 이메일 설정 원칙

- 기본 수신자: `dummy1.kim@gmail.com`
- Python APScheduler의 일일 08:32, 월요일 08:37 KST 이메일 잡 사용
- SMTP 자격증명이 없으면 외부 연결 없이 `skipped`로 종료
- SMTP 비밀번호는 저장소나 로그에 기록하지 않음

## 최종 운영 검증

- Ruff check·format, `git diff --check`: 통과
- `alembic current`: `20260719_0012 (head)`
- `alembic check`: 신규 upgrade 없음
- `pip check`: 깨진 의존성 없음
- 운영 SQLite: integrity check `ok`, 외래키 위반 0건
- Compose 정적 구성과 운영 이미지 재빌드: 통과
- 읽기 전용·비루트 UID 10001 컨테이너의 빈 DB migration·healthcheck: 통과
- 운영 이미지에 `tests/`, `analysis/`, pytest, coverage, Ruff, nbformat 없음
- 컨테이너 기본 수신자와 일일·주간 이메일 trigger: 통과
- 로컬 일일 이메일 dry-run: SMTP 연결·notification 예약 없이 `skipped`

첫 read-only smoke는 검증용 tmpfs의 UID/GID 옵션 누락으로 `/app/data/raw`
생성 권한이 없어 중단됐다. tmpfs 소유권을 이미지의 비루트 UID/GID 10001로
맞춘 뒤 동일 검증을 다시 실행해 최종 통과했다. 애플리케이션이나 이미지의 권한을
완화하지 않았다.

## 실제 발송 최종검수

- 2026-07-19 실제 테스트 메일 발송 전 구성 상태를 비밀값 없이 점검
- `.env` 파일 없음
- `EMAIL_FROM`, `SMTP_HOST`, SMTP 사용자명·비밀번호 없음
- 기본 수신자 `dummy1.kim@gmail.com`과 Python scheduler trigger는 정상
- `send-test-email` 결과: `processed=0`, `sent=0`, `skipped=true`
- 이메일 `notification_events`: 0건
- 따라서 거짓 성공, pending 예약, 외부 SMTP 연결과 실제 발송은 발생하지 않음
- 실제 발송 검수는 SMTP 발신 설정이 제공되는 즉시 재개

### Gmail SMTP 준비

- 발신자·수신자: `dummy1.kim@gmail.com`
- SMTP: `smtp.gmail.com:587`, STARTTLS
- 로컬 `.env` 생성 및 파일 권한 `0600` 적용
- `.env`의 Git ignore 적용 확인
- Google 앱 비밀번호만 비어 있으며 일반 계정 비밀번호는 사용하지 않음
- 앱 비밀번호 입력 전에는 `email_configured=false`; 실제 연결·발송 미실행

### 실제 Gmail 발송 최종 결과

- 앱 비밀번호 입력 후 비밀값을 출력하지 않고 `email_configured=true` 확인
- SMTP 연결 테스트: `processed=1`, `sent=1`, 실패·불명 상태 0
- 실제 scheduler 일일 판단 잡: `processed=1`, `sent=1`, 실패·불명 상태 0
- 현재 일정 대상 IPO가 없어 일일 판단 메일의 `items=0`; 검증 준비도 안내는 포함
- 이메일 알림 이력 2건 모두 `sent`, `sent_at`과 Message-ID 기록, 오류 없음
- `send_daily_email_summary` 잡 이력: `succeeded`, processed 1, success 1, failed 0
- 동일 일일 이벤트 재호출: SMTP 전달 함수 미진입, `duplicate=1`, 재발송 0
- 일일 이벤트 `attempt_count=1` 유지
- Gmail 받은편지함에서 `[Korea IPO] SMTP 연결 테스트`와
  `[Korea IPO] 일간 판단 2026-07-19` 실제 수신 확인
- `.env`는 권한 `0600`, Git·Docker 빌드 컨텍스트 제외; 비밀번호는 로그에 미기록

## 이메일 v2 긴급 개선 및 재발송

### v1 실수 확인

- 판단·일정이 모두 0건인데도 일간 메일이 실제 발송됨
- 판단 대상 필터가 청약 종료 종목의 환불·납입·상장 일정까지 제거함
- 2026-07-20~2026-07-26의 에이치엘지노믹스 상장(7월 24일)이 주간 메일에서 누락됨
- 결론보다 내부 검증 blocker와 ISO 생성 시각이 먼저 노출됨
- 결측값이 `-%`, `-주`로 보이고 빈 위험을 `등록된 위험 없음`으로 잘못 표현함
- 월요일 08:32 일간과 08:37 주간 메일이 연속 발송될 수 있었음

### v2 개선

- 판단 항목과 일정 항목을 별도 집계하고 청약 종료 종목의 주요 일정은 유지
- 판단·일정 모두 0건이면 notification 예약과 SMTP 연결 없이 `skipped`
- 일정만 있으면 schedule-only 브리핑 발송
- 월요일 일간 브리핑은 주간 브리핑으로 통합
- 청약 시작일 09:00 KST를 판단 노출 cutoff로 적용
- 데이터 준비도 미충족 시 strict PIT 백테스트를 실행하지 않고 확률·기대수익을 fail-closed
- 이벤트 키를 `daily/weekly-decision-email:v2:날짜`로 버전업
- 제목·preheader·요약·오늘 할 일·주요 일정 순으로 재구성
- Outlook 호환 presentation table과 인라인 스타일로 HTML 재작성
- `정보 없음`, `위험 정보 미확인 — 무위험을 의미하지 않습니다.`로 결측 표현 수정

### v2 검증 및 실제 발송

- 전체 테스트: 520 passed, 16 warnings, 실패 0
- 전체 `app/`: 5,488 statements, 1,434 branches, miss 0, partial 0, 100%
- Ruff check·format, `git diff --check`: 통과
- 실제 수신자: `dummy1.kim@gmail.com`
- 실제 제목: `[Korea IPO] 7월 20일–7월 26일 주간 브리핑 · 일정 1`
- 실제 내용: 청약 판단 0건, 주요 일정 1건, 에이치엘지노믹스 상장 2026-07-24
- 이벤트 키: `weekly-decision-email:v2:2026-07-20`
- SMTP 결과: processed 1, sent 1, duplicate 0, failed 0, 불명 상태 0
- notification event: `sent`, attempt 1, Message-ID·sent_at 기록, 오류 없음
- 잡 이력: `send_weekly_email_summary` succeeded, processed 1, success 1, failed 0
- 같은 키 재검증: SMTP 전달 함수 미진입, duplicate 1, attempt 1 유지
- Gmail 받은편지함에서 제목·보낸사람·본문·상장 일정 실제 수신 확인
- Gmail 데스크톱 렌더링에서 제목, 요약 카드, 일정 표, 검증 상태, 면책문구의 시각 계층과 잘림 없음 확인
- 비밀번호와 앱 비밀번호는 출력·로그·저장소에 기록하지 않음

## 이메일 v3 전체 명단·공식 근거·교정 발송

### 최종 코드 품질

- 전체 테스트: 658 passed, 16 warnings, 실패 0
- 전체 `app/`: 8,923 statements, 2,544 branches
- statement miss 0, partial branch 0, 총 coverage 100.00%
- Ruff check·format, `git diff --check`, `pip check`: 통과
- Alembic current/head/check: `20260719_0014`, 신규 upgrade 없음
- 운영 DB healthcheck: database ok, schema current, email configured

### 공식 데이터·메일 계약

- 7월 core: 매드업, 레몬헬스케어, 레메디, 에이치엘지노믹스 4개
- 공식 명단: KIND 공모 4 = core 4 = DART 계보 4
- 성숙 신규상장: KIND listing 3 = 기준일 예상 listing 3
- 기관 경쟁률·참여기관·의무보유·가격분포: 4/4
- 최종 일반청약 경쟁률·신청자·배정 물량: 4/4
- 신주/구주·free float·유통가능 시가총액: 4/4
- 최근 분기 매출·영업이익·순이익·공모가 시가총액: 4/4
- 성숙한 D0/D1/D3: 3/3, 에이치엘지노믹스는 `대기`
- PER/PBR/PSR·비교기업·평가 할인·공식 환불일은 미수집 상태를 숨기지 않음
- gross 과거 결과 n=26은 설명용으로만 표시, strict point-in-time 표본 0/30이라 추천·확률 비활성
- HTML/plain text 모두 4개 포함, HTML 70,962 bytes, source ref 누락 0
- 샘플 `ABC테크`와 기본 가정 `1,000:1` 혼입 0

### Docker 최종 검수

- 이미지: `korea-ipo-telegram-bot:codex-final`
- UID/GID 10001 비루트, read-only root, capability 전체 제거, no-new-privileges
- 빈 DB를 `20260719_0014`까지 fresh migration하고 healthcheck 통과
- runtime image의 pytest·coverage·Ruff 존재 여부 모두 false

### 실제 SMTP 발송과 교정

- 고정 수신자: `dummy1.kim@gmail.com`
- 최초 v3 canary: event 4/artifact 1, SMTP `sent=1`, 실패·unknown 0
- 최초 제목에서 예정·실제 상장을 중복 세어 `상장 이벤트 7`이 된 것을 원장 QA에서 발견
- unique IPO별 event count로 수정하고 모든 결측 종목을 `데이터 보강 필요`에 포함
- 교정 최종 제목: `[Korea IPO] 2026년 7월 전체 4개 · 청약 이벤트 2 · 상장 이벤트 4 · 데이터 보강 필요 4`
- 교정 최종본: event 5/artifact 2, SMTP `sent=1`, 실패·unknown 0, attempt 1
- 동일 최종 명령 재실행: `sent=0`, `duplicate=1`; SMTP 재전달 없이 attempt 1 유지
- 최종 Message-ID·subject/text/html SHA-256·universe hash·evidence hash 일치 확인
- 최종 artifact의 4개 회사명, sample 0, 기본 1,000:1 0, open 품질 이슈 0 확인
- 인앱 브라우저의 로컬 `data:` URL 접근은 보안정책으로 차단되어 우회하지 않음
- SMTP 수락은 확인했지만 Gmail v3 실제 inbox 수신·320px clipping 확인은 사용자 확인 대기

## 종목별 일반청약 D-1 이메일·기관 가격분포 차트

### 구현 결과

- 자동 발송 기준을 상장일에서 **일반청약 시작일의 KST 달력 D-1**로 변경
- D-1 17:10 KIND 일정, 17:20 KIND 신규상장, 17:30 공개 DART, 18:10 품질 대조, 18:30 발송
- 같은 날 대상 N개는 합치지 않고 IPO별 N개의 독립 이메일·event·content artifact로 처리
- 종목별 렌더 또는 SMTP 한 건 실패가 다음 종목을 중단하지 않음
- 본문 최상단 `한눈에 보기`, 회사별 제목, 참여기관 비중·신청수량 비중의 독립 0~100% 막대 추가
- 가격미제시는 마지막 별도 행, 참여기관 최다와 신청수량 최다를 각각 표시
- DART 원문 제공 비율을 억지로 100%에 맞추지 않고, 전체 count/share가 완전할 때만 raw 합계로 비중 계산
- 가격분포 표의 `-`는 이 표에서만 명시적 0건·0주·0%로 보존해 실제 `null`과 구분
- event key는 `version + subscription_start + sha256(offering_id)` occurrence로 안정화하고 evidence hash는 감사 metadata에만 보존
- 월요일 주간 v3 이메일 예약 제거; 월간 전체 메일은 수동 preview/canary로 보존
- 외부 무발송 CLI: `preview-d1-emails --send-date YYYY-MM-DD [--ipo 회사명] [--html] [--manifest-only]`

### 운영 데이터 검수

- 공개 DART 재수집: 4종목 처리, success 4, partial/failed 0
- 공식 대조: KIND 공모 4 = core 4 = DART 4, 성숙 KIND 신규상장 3/3, open DQ 0
- append-only observation: 1,423건
- 7월 4종목 모두 최신 가격분포 6개 논리 구간
- 단일 HTML 크기: 레메디 29,429B, 매드업 29,416B, 에이치엘지노믹스 30,447B, 레몬헬스케어 29,419B
- 참여기관 수 최다 구간은 4종목 모두 밴드 상단: 95.62~96.61%
- 신청수량 최다 구간도 4종목 모두 밴드 상단: 94.81~97.02%

### 최종 품질 게이트

- 전체 테스트: 681 passed, 실패 0
- 전체 `app/`: 9,347 statements, 2,678 branches
- statement miss 0, partial branch 0, 총 coverage 100.00%
- Ruff check·format, `git diff --check`, `pip check`: 통과
- Alembic current/head/check: `20260719_0014`, 신규 upgrade 없음
- 운영 DB healthcheck: database ok, schema current, email configured
- Docker 이미지: `korea-ipo-telegram-bot:codex-final`, digest `sha256:9822fe8fea59…`
- Docker fresh migration/healthcheck 통과, UID/GID 10001, read-only root, capability 제거
- runtime image에 pytest·coverage·Ruff 없음
- Docker scheduler: D-1 18:30 등록, 주간 v3 이메일 미등록

### 발송 최종검수

- 기준일 2026-07-19, 다음 날 2026-07-20 일반청약 시작 대상: 0건
- 실제 D-1 job: processed 0, sent 0, failed 0, delivery unknown 0, 정상 `skipped`
- 실행 전후 notification event 5건·content artifact 2건으로 불변
- 일정이 아닌 날 시험 목적으로 임의 발송하지 않았으며, 다음 실제 D-1 대상일의 SMTP ledger와 Gmail inbox 확인만 남음
