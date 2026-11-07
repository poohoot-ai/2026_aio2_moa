"""라우터 공통 — 인증 · 낙관적 잠금 · 멱등 · 백그라운드 분석.

경로 함수는 이 파일의 의존성으로 세션을 받는다. 경로마다 토큰 검사를 쓰면
한 곳을 빠뜨렸을 때 그 경로에서만 조용히 남의 세션이 열린다 (G-A3).
"""

from fastapi import Header, Path

from app import db, envelope, logging_setup
from app.services import edits, idempotency, ops_auth, sessions


# --- 담당자 세션 (D-02 1.1) ---------------------------------------------------
def session_of(session_id: str = Path(...),
               x_session_token: str | None = Header(default=None)) -> dict:
    """X-Session-Token 이 가리키는 세션. 경로의 session_id 와 다르면 404 다."""
    if not x_session_token:
        raise envelope.session_token_missing()
    session = sessions.load(session_id, x_session_token)
    if not session:
        raise envelope.session_not_found()
    return session


def session_by_token(x_session_token: str | None = Header(default=None)) -> dict:
    """경로에 session_id 가 없는 API(메뉴 검색)용 — 매장 기준을 세션에서 주입한다."""
    if not x_session_token:
        raise envelope.session_token_missing()
    from app import ids

    session = db.fetch_one("SELECT * FROM app.order_session WHERE token_hash = %s",
                           (ids.token_hash(x_session_token),))
    if not session:
        raise envelope.session_not_found()
    return session


def expected_version(if_match: str | None = Header(default=None)) -> int | None:
    """If-Match: {version}. 없으면 None — 잠금 없이 쓴다(시더 · 첫 호출)."""
    if if_match is None or not if_match.strip():
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError:
        raise envelope.bad_request(detail="If-Match must be an integer", field="If-Match")


# --- 운영 대시보드 (D-02 1.1) -------------------------------------------------
def ops_token(authorization: str | None = Header(default=None)) -> str:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not ops_auth.verify(token):
        raise envelope.ops_auth_failed()
    return token


# --- 쓰기 공통 ----------------------------------------------------------------
def write(fn, *, key: str | None, endpoint: str, session_id: str | None = None):
    """Idempotency-Key 재시도면 보관한 응답을, 아니면 실행 결과를 돌려준다.

    서비스 예외를 D-02 오류 코드로 옮기는 것도 여기서 한다 — 경로마다 쓰면 갈라진다.
    """
    try:
        kept = idempotency.lookup(key, endpoint)
    except idempotency.KeyReused as exc:
        raise envelope.conflict("IDEMPOTENCY_KEY_REUSED", "요청을 처리하지 못했어요. 다시 시도해 주세요",
                                detail=str(exc))
    if kept is not None:
        return kept["data"]

    data = mapped(fn)
    idempotency.remember(key, endpoint, data, session_id=session_id)
    return data


def mapped(fn):
    """서비스 예외 → D-02 오류 코드."""
    try:
        return fn()
    except edits.Conflict as exc:
        raise envelope.stale_version(exc.current)
    except edits.NotApplicable as exc:
        if str(exc) == "UNDO_UNAVAILABLE":
            raise envelope.unprocessable("UNDO_UNAVAILABLE", "되돌릴 내용이 없어요")
        raise envelope.unprocessable("NOT_APPLICABLE", "그 내용은 반영하지 못했어요. 다시 확인해 주세요",
                                     detail=str(exc))


def run_pipeline_background(session_id: str, trace_id: str) -> None:
    """202 뒤에서 도는 분석. 실패하면 세션을 failed 로 둔다 — running 으로 영원히 남지 않게.

    failed 는 HTTP 200 + status 로 화면에 간다(D-02 2.3). 입력 원문은 남아 있다.
    """
    token = envelope.current_trace.set(trace_id)
    try:
        sessions.run_pipeline(session_id, trace_id=trace_id)
    except Exception as exc:          # noqa: BLE001 — 백그라운드라 올릴 곳이 없다
        # `request` 를 쓰지 않는다 — 미들웨어가 이미 그 요청의 한 줄을 남겼다. 두 번 쓰면 요청 수가
        # 두 배로 보인다(D-03 1.5). 분석이 멈췄다 = 다음 체크포인트를 저장하지 못했다 (치명 · FR-013)
        logging_setup.log("checkpoint_save_failed", level="error", session_id=session_id,
                          trace_id=trace_id, step=_last_checkpoint(session_id),
                          db_down=isinstance(exc, db.DbUnavailable) or None,
                          # 예외 **전문**은 남기지 않는다 — 대화 · 프롬프트 · 비밀값이 섞여 들어오는 경로다
                          # (2026-11-07 QA H-33 · NFR-003). 오류 유형과 trace_id 로 추적한다
                          detail=f"pipeline failed: {type(exc).__name__}")
        try:
            db.execute("UPDATE app.order_session SET status = 'failed', updated_at = NOW() "
                       " WHERE session_id = %s AND status IN ('running','recovering')",
                       (session_id,))
        except Exception:     # noqa: BLE001 — DB 자체가 없으면 표시할 방법이 없다
            pass
    finally:
        envelope.current_trace.reset(token)


def _last_checkpoint(session_id: str) -> str | None:
    """멈춘 자리 — 마지막으로 저장된 체크포인트. DB 가 없으면 모른다."""
    try:
        row = db.fetch_one("SELECT checkpoint FROM app.order_session WHERE session_id = %s",
                           (session_id,))
        return row["checkpoint"] if row else None
    except Exception:          # noqa: BLE001 — 기록용 부가 정보다
        return None
