# Moa 에이전트 서버 (backend)

단톡방 대화를 붙여넣으면 사람별 주문으로 정리해 주는 서비스의 API 서버입니다.
이 폴더 하나로 실행 · 테스트 · DB 초기화까지 할 수 있습니다. 프론트엔드 코드는 필요 없습니다.

- 스택: Python 3.11 · FastAPI · PostgreSQL(원격 관리형, psycopg3)
- 분석 · 채팅 · 답장은 **업무 층 그래프**(`app/graph/`, D-15)를 지납니다. 주문 해석 서비스 주소
  (`ORDER_INTERPRETER_PRIMARY_URL`)를 주고 서비스를 `INTERPRETER_SOURCE=llm`으로 띄우면 LLM이 읽고,
  주소가 없거나 회로 차단 중이면 **규칙 해석**으로 돕니다 — LLM 키가 없어도 전체 흐름이 동작합니다
- 호출 계약의 정본은 `03.구현문서/API스펙.md`(D-02)입니다. 아래 "API 요약"은 그중 화면 개발에 필요한 부분입니다

---

## 1. 실행

### 1-1. `.env` 만들기

```bash
cp .env.example .env
```

| 변수 | 필요 여부 | 어디서 받나 |
|------|----------|------------|
| `DATABASE_URL` | 실제 데이터로 개발하려면 필요 | **팀 리드에게 받습니다.** 저장소 · 메신저 채널에 붙여넣지 않습니다 |
| `APP_RW_PASSWORD` · `HEALING_RW_PASSWORD` | `DATABASE_URL`과 함께 | 팀 리드 |
| `OPS_DASHBOARD_PASSWORD` | 선택 | 비우면 `moa-ops`. 운영 대시보드 로그인 비밀번호 |
| `OPENAI_API_KEY` | 선택 | 팀 리드. 주문 해석 서비스를 `INTERPRETER_SOURCE=llm`으로 띄울 때만 필요 |
| `ORDER_INTERPRETER_PRIMARY_URL` · `_BACKUP_URL` | 선택 | 기본값 그대로(`localhost:8101` · `8102`). 비우면 `/health`가 `unset`으로 봅니다 |
| 나머지 | 기본값 그대로 | — |

`.env`는 `.gitignore` · `.dockerignore`에 들어 있습니다. 커밋하지 않습니다.

> **DB 없이도 서버는 뜹니다.** `DATABASE_URL`을 비워 두면 `/health`가 `degraded`로 답하고,
> 기준 정보 · 예시 대화 · 대화 미리보기만 동작합니다. 나머지 API는 `200` + `error.code: "DB_UNAVAILABLE"`을 돌려줍니다.
> 프론트엔드는 이 응답을 받으면 안내 문구를 보여 줍니다. 예시 데이터로 채우려면 미리보기 모드(`MOA_UI_PREVIEW=1`)를 켭니다.

### 1-2. 직접 실행

```bash
uv venv -p 3.11
uv pip install -r requirements-dev.txt
uv run uvicorn app.main:app --reload --port 8000
```

주문 해석 서비스 2계통(1차 · 대체)도 띄우면 `/health`가 `ok`가 됩니다. 터미널 2개에서 실행합니다.

```bash
INTERPRETER_ROLE=primary uv run uvicorn interpreter.main:app --port 8101
INTERPRETER_ROLE=backup  uv run uvicorn interpreter.main:app --port 8102
```

PowerShell은 `$env:INTERPRETER_ROLE="primary"; uv run uvicorn interpreter.main:app --port 8101` 처럼 씁니다.
기본은 규칙 해석(같은 입력에 같은 결과)이고, `INTERPRETER_SOURCE=llm`을 붙이면 실제 LLM을 부릅니다.
`INTERPRETER_MODE=error` · `delay` · `invalid`로 장애(F-1 · F-2 · F-5)를 흉내 낼 수 있습니다.

uv 없이 쓰려면 `python -m venv .venv` → 가상환경 활성화 → `pip install -r requirements-dev.txt` → `uvicorn app.main:app --reload --port 8000`.

### 1-3. Docker로 실행

```bash
docker build -t moa-backend .
docker run --rm -p 8000:8000 --env-file .env moa-backend
```

`.env`는 이미지에 들어가지 않습니다. `--env-file`로 실행할 때 넘깁니다.

### 1-4. 떴는지 확인

| 주소 | 기대 |
|------|------|
| http://localhost:8000/health | `"status": "ok"` · `checks.llm`에 provider · 모델 (DB 미설정이면 `"degraded"` · `checks.db: false`, 주문 해석 서비스를 안 띄웠으면 `order_interpreter`가 `down`) |
| http://localhost:8000/docs | Swagger — 엔드포인트 31개 |

프론트엔드는 `BACKEND_URL` 환경변수로 이 주소를 봅니다(기본 `http://localhost:8000`).

---

## 2. 시연 데이터

```bash
python tools/seed_demo.py           # 성격이 다른 세션 3건을 만든다
python tools/seed_demo.py --clean   # 시연 세션만 지운다
```

| # | 세션 | 화면에서 볼 수 있는 것 |
|---|------|----------------------|
| 1 | 스타벅스 · 확인 필요 남음 | SCR-002 확인 질문 |
| 2 | 일반 카페 · 공유 완료 | SCR-005 주문서 · O-6 주문 확정 |
| 3 | 매장 되묻기 대기 | SCR-001 되묻기 모달 |

세션마다 `session_id`와 **관리 토큰**(`X-Session-Token`)을 찍어 줍니다. 토큰은 해시로만 저장되므로 이 출력을 놓치면
그 세션은 다시 열 수 없습니다(`--clean` 후 다시 만들면 됩니다). Swagger(`/docs`)나 curl로 바로 불러 볼 수 있습니다.

```bash
curl -H "X-Session-Token: {토큰}" http://localhost:8000/api/sessions/{session_id}
```

> 화면은 지금 `?s={토큰}`만으로는 세션을 열지 못합니다(`session_id`를 같은 브라우저 상태에서만 가져옴). 시더 세션은 API로 확인하고,
> 화면 확인은 화면에서 새로 [정리하기]로 만든 세션으로 합니다. 운영 대시보드(SCR-006) 주문 처리 기록에는 시더 세션이 보입니다.

### DB를 처음부터 만들 때 (새 Supabase 프로젝트)

```bash
python sql/run_sql.py            # 01 롤 → 02 · 03 스키마 → 04 뷰 → 10 ~ 13 메뉴 · 회로 시드
python sql/run_sql.py --check    # 실행하지 않고 테이블 · 뷰 · 롤 수만 본다
```

`.env`의 `DATABASE_URL`은 **소유자(postgres) 계정**, 포트는 **Session pooler `:5432`** 입니다(`:6543` 아님).
서버는 소유자가 아니라 `app_rw` · `healing_rw` 롤로 붙습니다 — 롤 비밀번호가 `.env`에 있어야 합니다.

---

## 3. API 요약 (화면 개발용)

### 3-1. 공통 규칙

| 항목 | 규칙 |
|------|------|
| 응답 포장 | 성공 `{"data": {...}, "meta": {...}}` · 실패 `{"error": {...}, "meta": {...}}`. **둘이 함께 오지 않습니다.** `/health`만 포장이 없습니다 |
| 화면에 띄울 문구 | `error.user_message`만 씁니다. `error.detail`은 기술 사유라 **화면에 띄우지 않습니다** |
| 담당자 인증 | `X-Session-Token: {관리 토큰}` 헤더. 화면 URL의 `?s=` 값을 헤더로 옮겨 보냅니다. **API 경로에 토큰을 넣지 않습니다** |
| 운영자 인증 | `POST /api/ops/login`으로 받은 토큰을 `Authorization: Bearer {ops_token}`로. 서버 재시작 시 다시 로그인 |
| 동시 수정 | 쓰기 요청에 `If-Match: {version}`. 어긋나면 `409 STALE_VERSION` → 조용히 다시 불러옵니다 |
| 재시도 | 쓰기 요청에 `Idempotency-Key: {uuid}`(선택). 10분 안에 같은 키면 처음 응답을 그대로 돌려줍니다 |
| 분석 | `POST /api/sessions`는 **202**로 바로 답하고 분석은 뒤에서 돕니다. `GET .../progress`를 1초마다 불러 `status`가 끝 상태가 되면 멈춥니다 |
| 금액 · 판정 | 금액 합계 · 공유 가능 여부 · 문구는 전부 서버가 만듭니다. 화면이 계산 · 조립하지 않습니다 |
| 추적 | 모든 응답에 `meta.trace_id`와 `X-Trace-Id` 헤더. 버그 제보 시 이 값을 함께 주세요 |

### 3-2. 상태 코드

| 코드 | 언제 | 화면 처리 |
|------|------|----------|
| 200 | 정상 · **업무 실패도 200**(`data.status: "failed"`) · **DB 장애도 200**(`error.code: "DB_UNAVAILABLE"`, `retryable: true`) | `user_message` 표시 |
| 202 | 분석 시작 · 매장 되묻기 답 · 매장 기준 바꾸기 | 진행 상태 폴링 시작 |
| 400 | 대화 없음 · 이미지 · 지원하지 않는 파일 · 너무 김 · 요청 형식 | 입력은 그대로 두고 안내만 |
| 401 | 토큰 없음(`SESSION_TOKEN_MISSING`) · 운영 비밀번호 틀림(`OPS_AUTH_FAILED`) | |
| 404 | 다른 세션 · 토큰 불일치(`SESSION_NOT_FOUND`) · 없는 항목(`NOT_FOUND`) | 존재 여부를 구분하지 않습니다 |
| 409 | `STALE_VERSION` · `SHARE_BLOCKED` · `ALREADY_DECIDED` · `ALREADY_ANSWERED` | |
| 422 | `UNDO_UNAVAILABLE` · `NOT_APPLICABLE` · `IMAGE_UNAVAILABLE` | |
| 500 | `INTERNAL_ERROR` | "잠시 후 다시 눌러 주세요" |

### 3-3. 엔드포인트 (31개)

| ID | 메서드 · 경로 | 인증 | 화면 |
|----|--------------|------|------|
| API-001 | `GET /health` | — | 배관 확인 |
| API-002 | `GET /api/meta` | — | 가격 고지 · 매장 라벨 · 정책 수치 |
| API-010 | `GET /api/sample-conversation` | — | SCR-001 예시로 채워보기 |
| API-011 | `POST /api/conversations/preview` | — | SCR-001 미리보기 (JSON `raw_text` 또는 multipart `file`) |
| API-012 | `POST /api/sessions` | — (응답으로 토큰 발급) | SCR-001 정리하기 |
| API-013 | `POST /api/sessions/{id}/brand-answer` | 세션 | 되묻기 모달 |
| API-014 | `GET /api/sessions/{id}/progress` | 세션 | SCR-002 진행 4단계 (1초 폴링) |
| API-015 | `GET /api/sessions/{id}` | 세션 | SCR-002 주문서 패널 |
| API-020 | `GET /api/sessions/{id}/questions` | 세션 | 확인 질문 (한 번에 하나) |
| API-021 | `POST /api/sessions/{id}/questions/{qid}/answer` | 세션 · If-Match | 빠른 답 버튼 |
| API-022 | `POST /api/sessions/{id}/messages` | 세션 · If-Match | 채팅 수정 (`data.kind`로 분기) |
| API-023 | `PATCH /api/sessions/{id}/items/{item_id}` | 세션 · If-Match | 행 편집 |
| API-024 | `POST /api/sessions/{id}/undo` | 세션 | 되돌리기 (직전 1회) |
| API-025 | `GET /api/sessions/{id}/evidence` | 세션 | 원본 보기 (`?item_id=` · `?question_id=`) |
| API-026 | `GET /api/menus/search?q=` | 세션 | 메뉴 찾기 모달 (매장 기준은 세션에서) |
| API-027 | `POST /api/sessions/{id}/brand-change` | 세션 · If-Match | [새로 정리] |
| API-030 | `GET /api/sessions/{id}/order-sheet` | 세션 | SCR-005 주문서 |
| API-031 | `POST /api/sessions/{id}/replies/analyze` | 세션 | 답장 반영 |
| API-032 | `POST /api/sessions/{id}/suggestions/{sid}/decision` | 세션 · If-Match | 제안 반영 · 무시 |
| API-033 | `GET /api/sessions/{id}/share?format=text` | 세션 | 공유 내용 |
| API-034 | `POST /api/sessions/{id}/share/complete` | 세션 | 복사 · 저장 = 완료 |
| API-040 | `POST /api/ops/login` | — | O-0 |
| API-041 | `POST /api/ops/logout` | ops | [나가기] |
| API-042 | `GET /api/ops/incidents` | ops | O-1 · O-4 (`approvals`) |
| API-043 | `GET /api/ops/incidents/{incident_id}` | ops | O-2 |
| API-044 | `POST /api/ops/incidents/{incident_id}/decision` | ops | O-4 승인 · 거부 |
| API-045 | `GET /api/ops/status` | ops | O-3 |
| API-046 | `GET /api/ops/metrics` | ops | O-5 |
| API-047 | `GET /api/ops/orders` | ops | O-6 목록 |
| API-048 | `GET /api/ops/orders/{order_id}` | ops | O-6 펼침 |
| API-049 | `POST /api/ops/faults` | ops | 장애 주입 (`ENVIRONMENT=lab`에서만) |

`ENABLE_DEBUG_API=true`일 때만 `GET /api/debug/sessions/{id}/trace` · `POST /api/debug/boom`이 추가됩니다. 공개 URL에서는 켜지 않습니다.

요청 · 응답 예시는 Swagger(`/docs`)에서 바로 호출해 볼 수 있습니다. 세션 API는 `X-Session-Token` 헤더를 직접 넣어야 합니다.

---

## 4. 테스트

```bash
uv run pytest -q                                  # 계약 테스트 — DB 없이 돈다 (로컬 .env 가 있어도 무시)
MOA_LIVE_DB=1 uv run pytest -q tests/test_live_flow.py   # .env 의 DB 로 전체 흐름 (만든 세션은 지운다)
```

| 파일 | 무엇을 보나 |
|------|------------|
| `tests/test_api_contract.py` | 포장 · 인증 · 오류 코드 · 소유권 404 · "DB 장애는 503이 아니다" |
| `tests/test_intake_rules.py` | 해석 규칙 — 수량 · 사이즈 · 온도 점검 (양방향) |
| `tests/test_llm.py` | provider 추상화 — 계약 강제 · 오류 갈래 · **재시도 0회** (가짜 HTTP) |
| `tests/test_interpreter.py` | 주문 해석 서비스 모드 4종 · 계약 검증 · 장애 코드 |
| `tests/test_live_flow.py` | 정리 → 질문 답 → 되돌리기 → 매장 바꾸기 → 공유 완료 · 되묻기 · 운영 대시보드 |

CI(GitHub Actions)는 일회용 PostgreSQL에 `sql/run_sql.py`로 스키마를 올리고 두 파일을 모두 돌립니다(커버리지 75% 기준).

---

## 5. 폴더 구조

```
backend/
├── app/
│   ├── main.py          앱 생성 · trace_id · 요청 로그 · 오류 포장 · 라우터 등록
│   ├── api/             HTTP 층 — 인증 · 입력 검사 · 응답 포장만. 업무 로직 없음
│   │   ├── deps.py      세션 토큰 · ops 토큰 · If-Match · 멱등 · 백그라운드 분석
│   │   ├── health.py    API-001
│   │   ├── intake.py    API-002 · 010 ~ 015 · 027
│   │   ├── review.py    API-020 ~ 026
│   │   ├── order.py     API-030 ~ 034
│   │   ├── ops.py       API-040 ~ 049
│   │   └── debug.py     API-090 · 091
│   ├── services/        업무 로직 (매장 판별 · 해석 · 메뉴 매칭 · 검증 · 정산 · 사건)
│   ├── llm.py           ★ provider 추상화 — openai · anthropic · ollama 분기가 있는 유일한 파일
│   ├── contracts.py     계약 클래스 (OrderEvents · MenuMatches · ReviewVerdicts) — LLM 출력 스키마이자 검증 기준
│   ├── prompts.py       prompts/*.md 조립 — 고정 system + 가변 context (문장은 쓰지 않는다)
│   ├── tools/           업무 조회 툴 5종 · 경로별 호출 허용 · 전문가 루프 · PII 가림 (D-10 · D-13)
│   ├── graph/           ★ 업무 층 그래프 N-01 ~ N-10 (D-12 · D-15) — 분석 · 채팅 · 답장이 같은 진입 · 출구를 지난다
│   ├── interpreter_client.py  주문 해석 서비스 호출 — 5초 제한 · 재시도 없음 · 계약 검증
│   ├── config.py        환경변수는 여기서만 읽는다
│   ├── db.py            롤별 연결 풀 (app_rw · healing_rw)
│   ├── envelope.py      응답 포장 · 오류 코드
│   ├── ids.py           식별자 (ORD-0012 · 세션 범위 접두어)
│   └── logging_setup.py JSON 로그 — event 고정 목록
├── interpreter/         주문 해석 서비스 (DEP-001) — 1차 · 대체가 같은 코드, 모드 4종
├── prompts/             ★ 에이전트 런타임 프롬프트 5개 — 제품 자산. 정본은 D-11 프롬프트명세서, 바꾸면 D-11 이력 먼저
├── sql/                 스키마 · 시드 · run_sql.py (DB 초기화)
├── tools/               seed_demo.py(시연 세션) · build_sbux_seed.py(스타벅스 메뉴 시드 생성)
├── tests/
├── requirements.txt     운영용 (Dockerfile 이 설치)
├── requirements-dev.txt 테스트용
├── Dockerfile
└── .env.example
```

---

## 6. 알아 둘 제약

| 제약 | 영향 | 비고 |
|------|------|------|
| 같은 대화 재입력 | 같은 관리 토큰을 보낸 요청만 기존 결과를 돌려준다(`reused: true`). 토큰이 없거나 다르면 새로 분석한다(D-02 1.2 개정) — 화면이 아직 토큰을 보내지 않아 같은 브라우저에서도 새로 분석된다 | 화면 `create_session`에 토큰 전달은 선택 |
| 공유 이미지 미구현 | `GET .../share`가 `image_available: false`와 대체 문구를 준다. 텍스트 복사는 항상 된다 | |
| 분석 요청 횟수 제한(429) 미구현 | D-02의 분당 10회 제한이 아직 없다 | |
| 응답 속도 | 원격 DB 왕복 1회 약 0.3초 — 진행 상태 조회 약 1초, 정리 1건 약 20초, **확인 질문 답 1번에 화면 갱신까지 13 ~ 15초**(D-05 실측) | DB 리전이 `eu-north-1`. 서울 리전이면 크게 준다 |
| 기본은 규칙 해석 | `.env`에 해석 서비스 주소가 없으면 그래프가 규칙 해석 한 묶음으로 돈다. LLM으로 돌리려면 주소 + 서비스 `INTERPRETER_SOURCE=llm`. 데모 시드(`sql/14`)는 1차를 `circuit_open`으로 두므로 대체 주소도 있어야 LLM을 탄다 | |
| Menu Agent · 보조 검토는 후보 · 의견만 | 규칙이 못 정한 항목만 LLM 에이전트가 툴로 후보를 채우고(Menu), 대상이 모호한 항목에 의견을 남긴다(보조 검토). **메뉴 확정 · 통과 판정은 하지 않는다** — 담당자가 확인 질문에서 고른다. 해석 서비스가 없으면 둘 다 안 불린다 | `app/graph/agents.py` · D-12 10.3 |
