"""API-001 상태 확인 — "살아 있음"이 아니라 "일할 수 있음"을 답한다 (D-02 API-001).

3분기 — 재시작으로 고쳐지는 것만 503 이다.
  ok        전부 정상
  degraded  DB 끊김 · 주문 해석 대체 경로 · 체크포인트 60초 미갱신 (200)
  down      api_key 없음 · 로그 쓰기 불가 (503)

DB 를 한 번 붙어 보고 실패하면 나머지 DB 질의를 건너뛴다. 붙을 때마다 연결 제한시간
(2초)을 기다리면 Docker HEALTHCHECK 제한시간(3초)을 넘겨 프론트가 뜨지 못한다.
"""

import datetime

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import config, db, llm, logging_setup
from app.services import menu, trace

router = APIRouter()

VERSION = "0.1.0"
CHECKPOINT_STALE_SEC = 60      # PRD 5.6.1 F-3 판정 기준
_PING_TIMEOUT_SEC = 0.8          # 두 서비스 합쳐 2초 안 — HEALTHCHECK 제한시간 3초를 지킨다


def _ping(url: str) -> str:
    """주문 해석 서비스 상태. unset(주소 없음) · up · down."""
    if not url:
        return "unset"
    try:
        res = httpx.get(f"{url}/health", timeout=_PING_TIMEOUT_SEC)
        return "up" if res.status_code == 200 else "down"
    except httpx.HTTPError:
        return "down"


def _active_route() -> str:
    """활성 경로의 원천은 메모리가 아니라 DB 다 (D-02 API-001 — 재시작 시 차단된 서비스를 다시 부르지 않게)."""
    row = db.fetch_one("SELECT dependency_key FROM healing.circuit_state "
                       " WHERE is_active_route AND dependency_key LIKE 'order_interpreter_%'")
    return "backup" if row and row["dependency_key"].endswith("backup") else "primary"


@router.get("/health")
def health():
    api_key = config.llm_key_present()
    interpreter = {"primary": _ping(config.ORDER_INTERPRETER_PRIMARY_URL),
                   "backup": _ping(config.ORDER_INTERPRETER_BACKUP_URL), "active": "primary"}
    checks = {"api_key": api_key, "logs_writable": logging_setup.logs_writable(), "db": False,
              "order_interpreter": interpreter, "checkpoint_age_sec": None,
              # 어느 provider · 모델이 붙었는가 — 키 값은 내보내지 않는다 (NFR-003)
              "llm": llm.info()}

    db_state = db.health()
    checks["db"] = db_state["state"] == "connected"
    checks["db_text"] = db_state["text"]
    if checks["db"]:
        checks["menu_rows"] = db_state.get("menu_rows")
        try:
            interpreter["active"] = _active_route()
            checks["checkpoint_age_sec"] = trace.checkpoint_age_sec()
        except Exception:      # noqa: BLE001 — 부가 신호다. 실패해도 db 칸이 이미 말한다
            pass

    # 재시작 · 환경변수 수정으로 고쳐지는 것만 down(503) — LLM 이 아직 연결 전이면 키가 없어도 일할 수 있다
    configured = [v for k, v in interpreter.items() if k != "active" and v != "unset"]
    if (config.LLM_REQUIRED and not api_key) or not checks["logs_writable"]:
        status, code = "down", 503
    elif (not checks["db"]
          or "down" in configured
          or interpreter["active"] == "backup"
          or (checks["checkpoint_age_sec"] or 0) > CHECKPOINT_STALE_SEC):
        status, code = "degraded", 200
    else:
        status, code = "ok", 200

    now = datetime.datetime.now(datetime.timezone.utc)
    return JSONResponse(status_code=code, content={
        "status": status,
        "checks": checks,
        "version": VERSION,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
    })


@router.get("/ready")
def readiness():
    """Gateway readiness: database reachable and active menu catalogs preloaded/checksummed."""
    db_state = db.health()
    checks = {"db": db_state["state"] == "connected", "db_text": db_state["text"],
              "menu_preload": False, "menu_catalogs": 0}
    if checks["db"]:
        try:
            preloaded = menu.preload_active()
            checks["menu_preload"] = True
            checks["menu_catalogs"] = preloaded["catalogs"]
        except Exception as exc:  # Readiness must fail closed; /health remains diagnostic.
            checks["menu_preload_error"] = type(exc).__name__
    ready = checks["db"] and checks["menu_preload"]
    now = datetime.datetime.now(datetime.timezone.utc)
    return JSONResponse(status_code=200 if ready else 503, content={
        "status": "ready" if ready else "not_ready", "checks": checks, "version": VERSION,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
    })
