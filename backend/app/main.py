"""Moa 에이전트 서버 — 앱 생성 · 공통 처리 · 라우터 등록만 한다.

경로는 app/api, 업무 로직은 app/services 에 있다. 호출 계약은 D-02 API스펙이다.

여기서 하는 공통 처리 4가지
  1 trace_id   요청마다 하나. 응답 meta · 로그 · 사건이 같은 값을 쓴다 (D-02 2.1)
  2 request    모든 HTTP 요청을 로그 event `request` 로 남긴다 (D-03 1.5)
  3 오류 포장  어떤 예외든 `{"error": {...}}` 로 나간다 — 화면은 user_message 만 쓴다
  4 DB 장애    503 이 아니라 200 + DB_UNAVAILABLE 이다 (재시작으로 고쳐지지 않는다, R-14)
"""

import asyncio
import time

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, db, envelope, logging_setup
from app.api import debug, health, intake, ops, order, review
from app.services import history, metrics

logging_setup.setup()
if config.OPS_PASSWORD_MISSING:
    # PRD R-17 — 코드 기본값이 없으므로 값이 비면 **아무도 운영 대시보드에 들어가지 못한다**.
    # .env 의 OPS_DASHBOARD_PASSWORD 를 12자 이상으로 채운다
    logging_setup.log("ops_login", level="warning", detail="ops_password_not_set")

app = FastAPI(title="Moa 에이전트 서버", version=health.VERSION,
              description="단톡방 대화 → 사람별 주문 정리. 호출 계약은 D-02 API스펙")

app.include_router(health.router)
app.include_router(intake.router)
app.include_router(review.router)
app.include_router(order.router)
app.include_router(ops.router)
if config.ENABLE_DEBUG_API:
    app.include_router(debug.router)


# --- 1 · 2 trace_id 와 요청 로그 ----------------------------------------------
_NO_HISTORY = ("/health", "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def trace_and_log(request: Request, call_next):
    """요청마다 정확히 한 줄의 `request` (교안코드규약 4.11 · D-03 1.5).

    처리되지 않은 예외도 **여기서** 500 으로 바꿔 한 줄을 남긴다. 바깥 처리기에 맡기면 그 시점엔
    trace_id 가 이미 풀려 method · status · duration_ms · trace_id 가 빠진 줄이 된다(D-06 확인에서 발견).
    """
    trace_id = request.headers.get("X-Trace-Id") or logging_setup.new_trace_id()
    token = envelope.current_trace.set(trace_id)
    started = time.monotonic()
    error = None
    try:
        response = await call_next(request)
    except Exception as exc:       # noqa: BLE001 — 어떤 예외든 포장된 500 으로 나간다
        error = f"{type(exc).__name__}: {exc}"[:500]
        response = envelope.internal_error(type(exc).__name__).response()
        # 5.6.1 에이전트 실행(FT-02) — 처리되지 않은 예외 1회가 사건 판정 조건이다.
        # 동기 DB 쓰기를 이벤트 루프에서 돌리면 백엔드가 그 동안 모든 요청을 못 받는다
        # (2026-11-06 preflight 에서 실측) — history 와 같이 executor 로 보낸다
        asyncio.get_running_loop().run_in_executor(
            None, lambda: metrics.record("agent_error_count", "agent_process", 1,
                                         threshold=1, breached=True, trace_id=trace_id))
    finally:
        envelope.current_trace.reset(token)
    duration_ms = int((time.monotonic() - started) * 1000)
    path = request.url.path
    status = response.status_code
    level = "error" if status >= 500 else "warning" if status >= 400 else "info"

    # 토큰 · 본문은 남기지 않는다 (NFR-003). 경로에는 session_id 만 있다
    logging_setup.log("request", level=level, trace_id=trace_id, method=request.method, path=path,
                      status=status, duration_ms=duration_ms, error=error)
    if not path.startswith(_NO_HISTORY) and config.db_configured():
        # 이력 저장은 응답을 붙잡지 않는다 — 실패해도 삼킨다(history 모듈)
        asyncio.get_running_loop().run_in_executor(
            None, lambda: history.save_request(method=request.method, path=path, status=status,
                                               duration_ms=duration_ms, error=error,
                                               trace_id=trace_id))
    response.headers["X-Trace-Id"] = trace_id
    return response


# --- 3 · 4 오류 포장 ------------------------------------------------------------
@app.exception_handler(envelope.ApiFailure)
async def on_api_failure(_: Request, exc: envelope.ApiFailure):
    return exc.response()


@app.exception_handler(db.DbUnavailable)
async def on_db_unavailable(request: Request, exc: db.DbUnavailable):
    logging_setup.log("db_unavailable", level="warning", trace_id=envelope.current_trace.get(),
                      path=request.url.path, detail=str(exc)[:300])
    # 5.6.1 DB(FT-03) — 연결 오류를 메트릭으로 남긴다. DB 가 죽어 있으면 기록도 실패하는데
    # 그건 삼켜진다. 그 구간은 healing 의 DbOutage 가 메모리에 보류해 복구 후 적는다
    asyncio.get_running_loop().run_in_executor(
        None, lambda: metrics.record("db_connection_error", "menu_store", 1,
                                     threshold=2, breached=True,
                                     trace_id=envelope.current_trace.get()))
    return envelope.db_down(str(exc)).response()


@app.exception_handler(psycopg.OperationalError)
async def on_db_operational(request: Request, exc: psycopg.OperationalError):
    # 연결 끊김 · statement_timeout(2초) 초과 — F-6 의 입구라 DB 장애와 같은 응답이다
    logging_setup.log("db_unavailable", level="warning", trace_id=envelope.current_trace.get(),
                      path=request.url.path, detail=str(exc).strip().splitlines()[0][:300])
    return envelope.db_down(type(exc).__name__).response()


@app.exception_handler(RequestValidationError)
async def on_validation(_: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    field = ".".join(str(p) for p in first.get("loc", [])[1:]) or None
    return envelope.bad_request(detail=str(first.get("msg", "invalid request"))[:300],
                                field=field).response()


@app.exception_handler(StarletteHTTPException)
async def on_http(_: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return envelope.not_found("route").response()
    return envelope.ApiFailure(exc.status_code, "BAD_REQUEST",
                               "요청을 처리하지 못했어요. 다시 시도해 주세요",
                               detail=str(exc.detail)).response()


@app.exception_handler(Exception)
async def on_unhandled(_: Request, exc: Exception):
    # 보통은 미들웨어가 먼저 받는다. 여기는 그 바깥에서 난 예외의 마지막 안전망이다 —
    # `request` 는 미들웨어만 쓴다(한 요청 = 한 줄)
    return envelope.internal_error(type(exc).__name__).response()
