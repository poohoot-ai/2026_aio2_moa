"""입력과 분석 — API-002 · 010 ~ 015 · 027 (SCR-001 → SCR-002).

분석은 202 를 먼저 돌려주고 백그라운드로 돈다 (D-02 API-012). 동기로 기다리면
호출 제한시간을 넘기고, 프로세스가 죽으면 요청이 통째로 사라진다.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app import config, db, envelope, logging_setup
from app.api import deps
from app.services import brand, brand_flow, chat_parse, meta, projection, sessions

router = APIRouter(prefix="/api")


@router.get("/meta")
def get_meta():
    return envelope.ok(meta.meta_view())


@router.get("/sample-conversation")
def get_sample():
    return envelope.ok(meta.sample_conversation())


# --- API-011 미리보기 · API-012 분석 요청 공통 입력 ------------------------------
def _too_large() -> envelope.ApiFailure:
    return envelope.bad_request("대화가 너무 길어요. 조금 나눠서 넣어 주세요",
                                code="CONVERSATION_TOO_LARGE", field="conversation")


async def _read_conversation(request: Request) -> tuple[str, str]:
    """JSON(raw_text) 또는 multipart(file). 파일은 `.txt` 확장자 + 텍스트 내용일 때만 받는다(chat_parse.judge_file)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise envelope.bad_request(detail="file field missing", field="file")
        data = await upload.read()
        text, code = chat_parse.judge_file(upload.filename or "", data)
        if code == "TOO_LARGE":
            raise _too_large()
        if code:
            raise envelope.bad_request(chat_parse.FILE_ERRORS[code], code=code, field="file")
        return text, "file"

    try:
        body = await request.json()
    except ValueError:
        raise envelope.bad_request(detail="body is not json")
    if not isinstance(body, dict):
        raise envelope.bad_request(detail="body must be an object")
    raw_text = body.get("raw_text")
    if raw_text is not None and not isinstance(raw_text, str):
        raise envelope.bad_request(detail="raw_text must be a string", field="raw_text")
    source = body.get("source") or "paste"
    if source not in ("paste", "file", "sample"):
        raise envelope.bad_request(detail="source must be paste|file|sample", field="source")
    return raw_text or "", source


def _check_size(raw_text: str, parsed: dict | None = None) -> None:
    if len(raw_text.encode("utf-8")) > config.MAX_INPUT_BYTES:
        raise _too_large()
    if parsed and parsed["message_count"] > config.MAX_MESSAGES:
        raise _too_large()


@router.post("/conversations/preview")
async def preview(request: Request):
    """형식만 본다. LLM 호출 0건 (API-011)."""
    raw_text, _ = await _read_conversation(request)
    _check_size(raw_text)
    parsed = chat_parse.parse(raw_text)
    unreadable, store_request = _split_store_request(raw_text, parsed)
    return envelope.ok({
        "raw_text": raw_text,
        "messages": parsed["messages"],
        "summary_text": parsed["summary_text"],
        "unreadable_lines": unreadable,
        "store_request": store_request,
        "can_proceed": parsed["can_proceed"],
    })


def _split_store_request(raw_text: str, parsed: dict) -> tuple[list[dict], str | None]:
    """대화 끝의 보낸 사람 없는 줄이 **매장 이름을 담고 있으면** 매장 요청 한 줄이다 (PRD 2.4.1).

    읽지 못한 줄로 두면 "스타벅스로 갈 예정이야"를 쓴 담당자에게 "N번째 줄은 읽지 못했어요"가 뜬다.
    매장 이름이 없는 끝줄("아까 말한 거 그대로요")은 그대로 읽지 못한 줄이다(D-02 API-011 예시).
    매장 사전은 DB 에 있다 — DB 가 없으면 예전처럼 읽지 못한 줄로 둔다(LLM 호출 0건 유지).
    """
    unreadable = list(parsed["unreadable_lines"])
    lines = [n for n, line in enumerate(raw_text.splitlines(), start=1) if line.strip()]
    if not (unreadable and parsed["messages"] and unreadable[-1]["line_no"] == lines[-1]):
        return unreadable, None
    try:
        hits = brand._find(unreadable[-1]["text"], brand._dictionary())
    except db.DbUnavailable:
        return unreadable, None
    if not hits:
        return unreadable, None
    return unreadable[:-1], unreadable[-1]["text"]


@router.post("/sessions")
async def create_session(request: Request, background: BackgroundTasks):
    raw_text, source = await _read_conversation(request)
    parsed = chat_parse.parse(raw_text)
    if not parsed["message_count"]:
        # LLM 호출 0건으로 끝난다 (PRD 8.1 #2-1)
        raise envelope.bad_request("대화를 붙여넣어 주세요", code="CONVERSATION_EMPTY",
                                   field="conversation", detail="messages[] is empty after parsing")
    _check_size(raw_text, parsed)

    trace_id = envelope.current_trace.get() or logging_setup.new_trace_id()
    started = brand_flow.start(raw_text, source=source, trace_id=trace_id,
                               owner_token=request.headers.get("X-Session-Token"))

    if started["kind"] == "reused":
        # FR-011 — 새로 분석하지 않는다. 요청이 이미 이 세션의 토큰을 갖고 있으므로 다시 주지 않는다
        existing = started["session"]
        data = projection.session_created_view(existing, session_token=None)
        data.update({"reused": True, "user_notice": started["user_notice"]})
        data.pop("session_token")
        return envelope.ok(data)

    if started["kind"] == "awaiting_brand":
        return envelope.ok({
            "session_id": started["session_id"],
            "session_token": started["session_token"],
            "status": sessions.AWAITING_BRAND,
            "brand_question": started["brand_question"],
        })

    session = db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s",
                           (started["session_id"],))
    background.add_task(deps.run_pipeline_background, started["session_id"], trace_id)
    data = projection.session_created_view(session, session_token=started["session_token"])
    data["snapshot_id"] = started["snapshot_id"]
    return envelope.ok(data, status=202)


@router.post("/sessions/{session_id}/brand-answer")
async def brand_answer(request: Request, background: BackgroundTasks,
                       session: dict = Depends(deps.session_of)):
    try:
        body = await request.json()
    except ValueError:
        raise envelope.bad_request(detail="body is not json")
    answer = (body or {}).get("answer")
    if answer not in ("starbucks", "generic", "other"):
        raise envelope.bad_request(detail="answer must be starbucks|generic|other", field="answer")
    if session["status"] != sessions.AWAITING_BRAND:
        # 되묻기는 분석 전 1회다 (D-02 API-013 · 되묻기 횟수 상한)
        raise envelope.conflict("ALREADY_ANSWERED", "이미 처리된 요청이에요",
                                detail=f"status={session['status']}")
    try:
        brand_flow.answer(session, answer, body.get("store_name"), run=False)
    except ValueError as exc:
        raise envelope.conflict("ALREADY_ANSWERED", "이미 처리된 요청이에요", detail=str(exc))

    background.add_task(deps.run_pipeline_background, session["session_id"], session["trace_id"])
    fresh = db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s",
                         (session["session_id"],))
    data = projection.session_created_view(fresh, session_token=None)
    data.pop("session_token")
    return envelope.ok(data, status=202)


def _parsed_counts(session_id: str) -> dict:
    row = db.fetch_one(
        "SELECT message_count, person_count FROM app.snapshot WHERE session_id = %s "
        " ORDER BY snapshot_no DESC LIMIT 1", (session_id,))
    return row or {}


@router.get("/sessions/{session_id}/progress")
def progress(session: dict = Depends(deps.session_of)):
    session_id = session["session_id"]
    review_count = sessions.review_count(session_id)
    notes = projection.step_notes(session, _parsed_counts(session_id))
    return envelope.ok(projection.progress_view(session, review_count=review_count,
                                                step_notes=notes))


@router.get("/sessions/{session_id}")
def session_result(session: dict = Depends(deps.session_of)):
    session_id = session["session_id"]
    rows = sessions.items(session_id)
    review_count = sessions.review_count(session_id)
    chat = projection.chat_intro(session, rows, review_count=review_count)
    data = projection.session_view(session, rows, review_count=review_count, chat=chat)
    if session["status"] == sessions.AWAITING_BRAND:
        data["rows"] = []
    return envelope.ok(data)


@router.post("/sessions/{session_id}/brand-change")
async def brand_change(request: Request, background: BackgroundTasks,
                       session: dict = Depends(deps.session_of),
                       version: int | None = Depends(deps.expected_version)):
    try:
        body = await request.json()
    except ValueError:
        raise envelope.bad_request(detail="body is not json")
    brand_id = (body or {}).get("brand_id")
    if brand_id not in ("starbucks", "generic"):
        raise envelope.bad_request(detail="brand_id must be starbucks|generic", field="brand_id")

    # 멱등 재생을 쓰지 않는다 — 보관 응답을 돌려줘도 아래 재분석은 다시 예약되고,
    # 이미 만든 항목 ID 와 부딪혀 세션이 failed 가 된다
    result = deps.mapped(lambda: brand_flow.change(session, brand_id, expected_version=version,
                                                   run=False))
    background.add_task(deps.run_pipeline_background, session["session_id"], session["trace_id"])
    fresh = db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s",
                         (session["session_id"],))
    data = projection.session_created_view(fresh, session_token=None)
    data.pop("session_token")
    data["user_message"] = result.get("user_message")
    return envelope.ok(data, status=202)

