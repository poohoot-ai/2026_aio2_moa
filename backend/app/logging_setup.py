"""JSON 로그 — event 를 고정 목록에서만 쓴다 (D-03 1.5).

트레이싱 SaaS 를 쓰지 않는다. 이 로그 파일과 DB 이력이 MTTD · MTTR 의 재료다.
event 이름을 즉흥으로 늘리면 집계가 깨진다.
"""

import datetime as dt
import json
import logging
import logging.handlers
import sys
import time
import uuid

# D-03 1.5 — 고정 목록(이벤트 이름을 임의로 만들지 않는다).
# 목록 밖의 이름은 log() 가 거부한다 — 즉흥으로 늘리면 MTTD · MTTR 집계가 깨진다
EVENTS = {
    "request",                  # 모든 HTTP 요청 — 미들웨어 1곳에서만
    "llm_call",                 # DEP-001 호출
    "tool_call",                # 메뉴 조회 등 도구 (D-13)
    "route_decision",           # 분석 1건의 경로 분류 결과 — events_by_type · needs_review
    "brand_decision",           # 매장 기준 판별 — llm_calls 는 0 이어야 한다
    "schema_invalid",           # F-11 계약 검증 실패 — F-5 탐지 신호
    "history_save_failed",      # 비치명 — 삼키고 warning
    "checkpoint_save_failed",   # 치명 — 다음 단계로 넘기지 않는다
    "incident_detected",        # Monitor — MTTD 시작점 (증분 3)
    "recovery_started",         # Recovery (증분 3)
    "recovery_verified",        # Verification 만 쓴다 — MTTR 종료점 (증분 3)
    "escalated",                # Notification (증분 3)
    "ops_login",                # API-040 — result 만, 비밀번호 · 토큰 원문 금지
    "share_completed",          # API-034
    # v1.1 편입 3종
    "session_created",          # 세션 생성 — O-6 · 처리량
    "checkpoint_saved",         # 단계 진행 — F-3 재개 근거 (checkpoint_save_failed 의 짝)
    "db_unavailable",           # DB 닿지 못함 — F-6 탐지 신호
    "circuit_update_failed",    # interpreter circuit-state 갱신 실패 — 요청 처리는 계속한다
}

_logger = logging.getLogger("moa")
_state = {"files": False}


def setup(name: str = "service") -> None:
    """표준 출력 + 파일 2개 (교안코드규약 4.11 · D-03 1.5).

      logs/{name}.log          전체
      logs/{name}-errors.log   ERROR 만 (agent 는 errors.log — D-03 1.5 이름 그대로)

    파일은 장애 대응 층이 읽는 재료다. 쓸 수 없으면 표준 출력만 남기고 /health 가 down 을 알린다.
    """
    if _logger.handlers:
        return
    formatter = logging.Formatter("%(message)s")      # 메시지가 이미 JSON 한 줄이다
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    _logger.addHandler(console)
    try:
        from app import config

        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        everything = _daily_file(config.LOG_DIR / f"{name}.log")
        everything.setFormatter(formatter)
        errors_name = "errors.log" if name == "service" else f"{name}-errors.log"
        errors = _daily_file(config.LOG_DIR / errors_name)
        errors.setLevel(logging.ERROR)
        errors.setFormatter(formatter)
        _logger.addHandler(everything)
        _logger.addHandler(errors)
        _state["files"] = True
    except OSError:
        _state["files"] = False
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


LOG_RETENTION_DAYS = 14        # PRD 9.3 추적 — 로그 14일 보관


def _daily_file(path) -> logging.Handler:
    """UTC 자정마다 {파일}.YYYY-MM-DD 로 넘기고 14일 치만 남긴다.

    파일은 컨테이너 밖(호스트 ./logs/<컨테이너>)에 마운트된다 — 배포로 컨테이너가 다시 만들어져도
    남아야 장애 실험의 로그 증빙이 된다. 순환이 없으면 그 파일이 끝없이 커진다.
    """
    return logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=LOG_RETENTION_DAYS, encoding="utf-8", utc=True)


def logs_writable() -> bool:
    """/health 의 logs_writable (API-001). 파일 핸들러가 붙었고 폴더에 지금도 쓸 수 있는가."""
    import os

    from app import config

    return _state["files"] and os.access(config.LOG_DIR, os.W_OK)


def new_trace_id() -> str:
    return "tr_" + uuid.uuid4().hex[:12].upper()


def log(event: str, *, level: str = "info", **fields) -> None:
    if event not in EVENTS:
        raise ValueError(f"로그 event 는 D-03 1.5 의 고정 목록에서만 쓴다: {event}")
    # UTC · 밀리초 3자리. 로컬 시간대로 적으면 MTTD · MTTR 을 다른 PC 의 로그와 맞출 수 없다
    # (2026-11-07 QA H-32). 파일 순환의 UTC 자정 설정과 개별 로그의 시각은 별개 조건이다
    payload = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
               "event": event, "level": level}
    payload.update({k: v for k, v in fields.items() if v is not None})
    getattr(_logger, level)(json.dumps(payload, ensure_ascii=False))
