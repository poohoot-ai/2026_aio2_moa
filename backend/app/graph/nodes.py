"""노드 함수 — D-12 2절 N-01 ~ N-10. 함수 docstring 첫 줄의 노드 ID 가 D-16 대조 기준이다.

교안코드규약 3.1 — `_node` 는 `f(state) -> dict`(갱신분만), `_edge` 는 다음 노드를 고른다.
업무 판정 로직은 여기 새로 쓰지 않는다 — services/ 의 규칙 함수를 부른다. 노드는 순서 · 경계 · 공통 검사만 맡는다.
"""

import re
import uuid

from langgraph.types import Send

from app import config, db, ids, interpreter_client, logging_setup
from app.graph import agents
from app.services import (changes, chat_parse, consolidate, edits, intake, menu as menu_service,
                          sessions, settlement, suggestions, validation)
from app.tools import pii

ROUTES = ("add", "update", "cancel", "confirm", "ignore")
_AMOUNT = re.compile(r"\d[\d,]*\s*원")

# 규칙 해석으로 대체한 사유 → O-6 처리 기록 문구 (interpreter_unset 은 설정상 규칙 — 기록하지 않는다)
_RECOVERY_TEXT = {
    "DEP_HTTP_5XX": "주문 해석 잠시 멈춤", "DEP_TIMEOUT": "주문 해석 응답 지연",
    "UNKNOWN": "주문 해석 연결 실패", "circuit_open": "주문 해석 차단 중",
    "regenerate_exhausted": "주문 해석 결과 형식 오류", "missing_events": "주문 해석 일부 누락",
    "llm_call_cap": "AI 호출 상한 도달",
}


class GraphRejected(ValueError):
    """N-01 진입 거부 — 그래프를 돌지 않는다. 호출부는 API 오류로 바꾼다."""


# --- N-01 ------------------------------------------------------------------------------
def input_guard_node(state: dict) -> dict:
    """N-01 입력 가드 — 단일 진입. 세션 고정 · 상한 · 매장 확정 확인 · 해석 대상 준비.

    PII 가림은 LLM 으로 나가는 유일한 경계(`interpreter_client.interpret`)가 한다(D-13) — 여기서 다시 부르지 않는다.
    """
    session, source = state["session"], state["source"]
    if session.get("status") == sessions.AWAITING_BRAND:
        raise GraphRejected("매장 기준이 정해지지 않았다 — 매장 되묻기 답을 먼저 받는다")

    if source == "snapshot":
        snapshot = db.fetch_one(
            "SELECT snapshot_id FROM app.snapshot WHERE session_id = %s "
            " ORDER BY snapshot_no DESC LIMIT 1", (session["session_id"],))
        messages = db.fetch_all(
            "SELECT message_id, seq, sender, text FROM app.message "
            " WHERE snapshot_id = %s AND is_reply = false ORDER BY seq", (snapshot["snapshot_id"],))
        existing = []
    elif source == "chat":
        messages = [{"message_id": "chat", "seq": 1, "sender": "담당자", "text": state["text"]}]
        existing = _existing_items(session["session_id"])
    else:
        messages = [{"message_id": f"reply{i}", "seq": i, "sender": m["sender"], "text": m["text"]}
                    for i, m in enumerate(state.get("messages") or [], start=1)]
        existing = _existing_items(session["session_id"])

    if len(messages) > config.MAX_MESSAGES:
        raise GraphRejected(f"메시지가 {config.MAX_MESSAGES}건을 넘는다")
    if sum(len((m.get("text") or "").encode("utf-8")) for m in messages) > config.MAX_INPUT_BYTES:
        raise GraphRejected("입력이 1MB 를 넘는다")
    return {"messages": [dict(m) for m in messages], "existing": existing}


def _existing_items(session_id: str) -> list[dict]:
    rows = db.fetch_all(
        "SELECT i.item_id, i.person, COALESCE(m.name, i.menu_text) AS menu_text, i.quantity, i.options "
        "  FROM app.order_item i LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
        " WHERE i.session_id = %s AND i.removed = false ORDER BY i.item_id", (session_id,))
    return [{"message_id": r["item_id"], "person": r["person"], "menu_text": r["menu_text"],
             "quantity": r["quantity"], "options": r["options"] or {}} for r in rows]


# --- N-02 ------------------------------------------------------------------------------
def batch_node(state: dict) -> dict:
    """N-02 묶음 나누기 — 4건씩, 묶음마다 앞 메시지 4건. 해석 서비스가 없으면 전체를 규칙 한 묶음으로.

    재개: 이 분석의 Intake 결과가 이미 저장돼 있으면(checkpoint = chat_read) 묶음을 만들지 않고 그 저장본을
    넘긴다 — N-03(LLM)을 건너뛰고 N-04 부터 다시 돈다(PRD 5.2 Handoff · D-12 10.4).
    """
    messages = state["messages"]
    if state.get("source") == "snapshot":
        saved = _saved_events(state["session"])
        if saved is not None:
            # 로그 event 는 고정 목록(D-03 결정 ⑥) — 새 이름 대신 checkpoint_saved 에 resumed 를 단다
            logging_setup.log("checkpoint_saved", session_id=state["session"]["session_id"],
                              trace_id=state.get("trace_id"), checkpoint="chat_read",
                              resumed=True, events=len(saved))
            service = _pick_service() if _interpreter_ready() else None
            return {"batches": [], "events": saved, "resumed": True, "service": service,
                    "plan": _record_plan(state, [], service, resumed=True)}
    if not _interpreter_ready():
        # 규칙 해석은 5초 제한이 없고, 참조 주문이 묶음 경계를 넘어 앞 메시지를 찾아야 한다
        batches = [{"batch_no": 1, "prev": [], "targets": messages, "use_llm": False}]
        return {"batches": batches, "service": None, "plan": _record_plan(state, batches, None)}
    # 킬 스위치는 분석 1회에 **한 번** 읽는다 — 묶음마다 읽으면 한 분석 안에서 1차 · 규칙이 섞인다
    service = _pick_service()
    # 묶음 크기는 고른 서비스의 모델이 5초 안에 답하는 크기다 (D-12 3.1 · 10.2 실측)
    size = config.BATCH_SIZE_BACKUP if service == "backup" else config.BATCH_SIZE
    ctx = config.BATCH_CONTEXT
    batches = []
    for no, start in enumerate(range(0, len(messages), size), start=1):
        targets = messages[start:start + size]
        prev = messages[max(0, start - ctx):start]
        batches.append({"batch_no": no, "prev": prev, "targets": targets, "use_llm": True,
                        "service": service, "person_prev": _person_context(messages, targets, prev)})
    # 분석 1회 LLM 호출 상한 — 묶음마다 최악 (1 + 재생성 2)회. 넘는 묶음은 처음부터 규칙
    allowed = config.MAX_LLM_CALLS_PER_RUN // (1 + config.MAX_REGENERATE)
    for batch in batches[allowed:]:
        batch["use_llm"] = False
    return {"batches": batches, "service": service, "plan": _record_plan(state, batches, service)}


def _record_plan(state: dict, batches: list[dict], service: str | None, *, resumed: bool = False) -> dict:
    """처리 계획 (Planner · PRD 5.9.3) — N-02 가 이미 정한 값을 계획 한 장으로 모아 남긴다.

    계획을 바꾸는 판단은 하지 않는다(값은 위 batch_node 가 정했다). 기록만 한다 — 실행 결과와 대조하려고.
    로그 event 는 고정 목록(D-03 결정 ⑥)이라 새 이름 대신 route_decision 에 stage="plan" 을 단다.
    `source` 키는 쓰지 않는다 — 분류 결과 route_decision(source=snapshot · chat · reply)과 섞이지 않게.
    """
    llm_batches = [b["batch_no"] for b in batches if b.get("use_llm")]
    rule_batches = [b["batch_no"] for b in batches if not b.get("use_llm")]
    plan = {
        "plan_id": "pl_" + uuid.uuid4().hex[:10],
        "run_source": state.get("source"),
        "resumed": resumed,                       # True 면 Intake(N-03)를 건너뛰는 계획
        "service": service,                       # primary · backup · None(규칙)
        "message_count": len(state.get("messages") or []),
        "batch_count": len(batches),
        "llm_batches": llm_batches,
        "rule_batches": rule_batches,
        "llm_call_budget": config.MAX_LLM_CALLS_PER_RUN,
        "max_regenerate": config.MAX_REGENERATE,
        "done_when": ["every_message_has_event", "schema_valid", "checkpoint_chat_read"],
    }
    session = state.get("session") or {}
    logging_setup.log("route_decision", session_id=session.get("session_id"),
                      trace_id=state.get("trace_id"), stage="plan", plan=plan)
    return plan


def _person_context(messages: list[dict], targets: list[dict], prev: list[dict]) -> list[dict]:
    """이 묶음에 말한 사람들이 **대화 전체에서** 한 다른 말 (2026-11-05 사용자 보고 7번).

    메시지 하나만 보고 판정하면 "네네 스타벅스입니다~"가 주문이 되고 한 사람이 말한 횟수만큼 잔이 늘었다.
    그 사람의 다른 말이 함께 있으면 어느 문장이 주문인지 가릴 수 있다.
    대화 전체를 묶음마다 넣지 않는 이유: 메시지 상한이 500건이라 프롬프트가 묶음 수만큼 커진다.
    **그 묶음의 발화자 것만** 골라 상한을 둔다.
    """
    people = {m.get("sender") for m in targets if m.get("sender")}
    shown = {m["message_id"] for m in targets} | {m["message_id"] for m in prev}
    picked = [m for m in messages if m.get("sender") in people and m["message_id"] not in shown]
    return [dict(m) for m in picked[:config.PERSON_CONTEXT]]


def _saved_events(session: dict) -> list[dict] | None:
    """재개할 Intake 저장본. 마지막 성공 단계가 chat_read 일 때만 — 그 뒤 단계(N-09)는 한 트랜잭션이라 반쪽이 없다.

    저장본이 없거나 예전 행(payload NULL)이 섞여 있으면 None — 처음부터 돈다.
    """
    if session.get("checkpoint") != "chat_read":
        return None
    rows = db.fetch_all(
        "SELECT payload FROM app.order_event WHERE session_id = %s AND snapshot_id = "
        "  (SELECT snapshot_id FROM app.snapshot WHERE session_id = %s ORDER BY snapshot_no DESC LIMIT 1)",
        (session["session_id"], session["session_id"]))
    if not rows or any(r.get("payload") is None for r in rows):
        return None
    return [dict(r["payload"]) for r in rows]


def dispatch_batches_edge(state: dict) -> list[Send] | str:
    """N-02 → N-03 fan-out (교안코드규약 4.6). 재개면 N-03 을 건너뛰고 N-04 로."""
    if state.get("resumed"):
        return "route"
    return [Send("intake", {"session": state["session"], "trace_id": state.get("trace_id"),
                            "batch": b, "existing": state.get("existing") or []})
            for b in state["batches"]] or [Send("intake", {"session": state["session"],
                                                         "trace_id": state.get("trace_id"),
                                                         "batch": {"batch_no": 0, "prev": [],
                                                                   "targets": [], "use_llm": False},
                                                         "existing": []})]


def _interpreter_ready() -> bool:
    return bool(config.ORDER_INTERPRETER_PRIMARY_URL or config.ORDER_INTERPRETER_BACKUP_URL)


def _backup_ready() -> bool:
    """대체 해석 서비스를 지금 쓸 수 있는가 — 주소가 있고 회로가 열려 있지 않다(킬 스위치와 같은 기준)."""
    if not config.ORDER_INTERPRETER_BACKUP_URL:
        return False
    if not config.db_configured():
        return True
    try:
        row = db.fetch_one("SELECT state FROM healing.circuit_state "
                           " WHERE dependency_key = 'order_interpreter_backup'", role="healing")
    except Exception:      # noqa: BLE001 — 스위치를 못 읽으면 _pick_service 와 같이 쓸 수 있다고 본다
        return True
    return (row or {}).get("state", "up") in ("up", "degraded")


def _pick_service() -> str | None:
    """킬 스위치 — healing.circuit_state 를 읽어 1차 · 대체를 고른다(D-12 8절 (2)). 직접 재시도하지 않는다."""
    states = {}
    if config.db_configured():
        try:
            rows = db.fetch_all("SELECT dependency_key, state, cooldown_until FROM healing.circuit_state",
                                role="healing")
            states = {r["dependency_key"]: r for r in rows}
        except Exception:      # noqa: BLE001 — 스위치를 못 읽으면 마지막 기본값(1차)
            states = {}

    def ok(key: str) -> bool:
        # 회로 해석은 interpreter_client.circuit_usable 한 곳이다 — 냉각이 지난 circuit_open 은
        # 시험 요청 1회를 받는다(PRD 5.6 F-4). 여기서 따로 판단하면 요청 경로와 규칙이 갈린다
        row = states.get(key) or {}
        return interpreter_client.circuit_usable(row.get("state", "up"), row.get("cooldown_until"))

    if config.ORDER_INTERPRETER_PRIMARY_URL and ok("order_interpreter_primary"):
        return "primary"
    if config.ORDER_INTERPRETER_BACKUP_URL and ok("order_interpreter_backup"):
        return "backup"
    return None


# --- N-03 ------------------------------------------------------------------------------
def intake_node(state: dict) -> dict:
    """N-03 의도분류 (Intake Agent) — classify.md 로 묶음을 읽어 이벤트마다 route 를 정한다.

    계약 위반 · 누락 → 재생성 2회 → 대체 모델 재요청 1회(FR-026 · FT-04) → 소진이면 확인 필요.
    해석 서비스 장애 · 호출 상한이면 그 묶음은 규칙 해석.
    """
    batch, session = state["batch"], state["session"]
    targets = batch["targets"]
    if not targets:
        return {"events": []}
    context = {"brand_id": session["brand_id"], "menu_version": session["menu_version"],
               "menu_as_of": config.MENU_AS_OF, "prev_messages": batch["prev"],
               "person_messages": batch.get("person_prev") or [],
               "existing_items": state.get("existing") or []}
    wire = [{"message_id": m["message_id"], "order": m.get("seq"), "sender": m["sender"],
             "text": m["text"]} for m in targets]
    target_ids = {m["message_id"] for m in targets}

    calls, reason = 0, "interpreter_unset"
    service = batch.get("service") if batch["use_llm"] else None
    if batch["use_llm"] and service is None:
        reason = "circuit_open"
    elif not batch["use_llm"] and _interpreter_ready():
        reason = "llm_call_cap"
    attempts = 0
    while service and attempts < 1 + config.MAX_REGENERATE:
        try:
            calls += 1
            result = interpreter_client.interpret_resilient(
                "extract_events", wire, context, service=service,
                trace_id=state.get("trace_id"), session_id=session["session_id"])
            known = target_ids | {m["message_id"] for m in batch["prev"]}
            events = [_restore_ids(e, known) for e in result.model_dump()["events"]]
            if len(target_ids) == 1:
                # 해석할 메시지가 하나뿐이면 이벤트의 주인은 그 메시지다 — 대체 모델이 [이미 정리된 주문]의
                # 항목 id 를 message_id 칸에 넣은 일이 실제로 있었다. 대상 칸의 모르는 id 는 비운다
                only = next(iter(target_ids))
                for e in events:
                    if e["message_id"] not in known:
                        e["message_id"] = only
                    if e.get("target_message_id") and e["target_message_id"] not in known:
                        e["target_message_id"] = None
            missing = target_ids - {e["message_id"] for e in events}
            if missing:
                # 누락된 메시지만 **규칙 해석으로 채운다** — 묶음 전체를 재생성하지 않는다(2026-11-05 3차 보고 2번).
                #   왜 바꿨나: 모델은 주문이 아닌 말(인사 · 맞장구)의 이벤트를 아예 내지 않는 일이 잦다.
                #   그때마다 재생성 2회를 쓰고 결국 **묶음 전체가 규칙으로** 떨어졌다 — 호출 3회가 7회로 늘고
                #   (실측 2026-11-05: 11메시지 3묶음에 7회 · 9.9초) 잘 읽은 메시지의 해석까지 버려졌다.
                #   규칙으로 채운 이벤트도 N-04 인원별 정리를 똑같이 지난다
                filled = intake.extract_events([m for m in targets if m["message_id"] in missing],
                                               brand_id=session["brand_id"],
                                               menu_version=session["menu_version"])
                _demote_unsupported(filled, session)
                logging_setup.log("schema_invalid", level="warning", session_id=session["session_id"],
                                  trace_id=state.get("trace_id"), agent="intake",
                                  detail="missing_events_filled", message_ids=sorted(missing))
                return {"events": _ordered(intake.from_contract(events, targets, known_ids=known) + filled),
                        "llm_calls": calls,
                        "degraded": [{"batch_no": batch["batch_no"], "reason": "missing_events",
                                      "messages": len(missing)}]}
            return {"events": _ordered(intake.from_contract(events, targets, known_ids=known)),
                    "llm_calls": calls}
        except interpreter_client.InterpreterError as exc:
            if exc.fault_code == "AGENT_OUTPUT_INVALID":
                attempts += 1
                reason = "regenerate_exhausted"
                context["validation_feedback"] = {"error": exc.fault_code, "missing_fields": exc.missing_fields,
                                                "attempt": attempts, "instruction": "Regenerate the complete response and satisfy the declared schema; do not omit required fields."}
                if attempts >= 1 + config.MAX_REGENERATE and service == "primary" and _backup_ready():
                    # FT-04 대체 모델 재요청 1회 (승인 PRD FR-026 · PRD 5.6 F-5) — 1차 재생성을 다 쓴 묶음만.
                    # 성공하면 그 묶음은 대체 모델 해석으로, 실패하면 아래에서 확인 필요로 담당자에게 넘긴다.
                    # attempts 를 MAX_REGENERATE 로 되돌려 while 조건이 딱 한 번만 더 돌게 한다.
                    # 로그 event 는 고정 목록이라 새 이름 대신 schema_invalid 에 detail 을 단다(D-03 결정 ⑥)
                    logging_setup.log("schema_invalid", level="warning", session_id=session["session_id"],
                                      trace_id=state.get("trace_id"), agent="intake",
                                      detail="backup_model_retry", batch_no=batch["batch_no"])
                    service, attempts = "backup", config.MAX_REGENERATE
                continue
            reason = exc.fault_code                       # 의존 서비스 장애 — 복구는 장애 대응 층
            break
    if attempts >= 1 + config.MAX_REGENERATE:
        reason = "regenerate_exhausted" if reason != "missing_events" else "missing_events"

    if reason == "regenerate_exhausted":
        events = intake.from_contract(
            [{"message_id": m["message_id"], "event_type": "ignore"} for m in targets],
            targets, known_ids=target_ids)
        return {"events": _ordered(events), "llm_calls": calls,
                "degraded": [{"batch_no": batch["batch_no"], "reason": "regenerate_exhausted",
                              "messages": len(targets), "needs_review": True}]}
    events = intake.extract_events(targets, brand_id=session["brand_id"],
                                   menu_version=session["menu_version"])
    degraded = [] if reason == "interpreter_unset" else [
        {"batch_no": batch["batch_no"], "reason": reason, "messages": len(targets)}]
    return {"events": _ordered(events), "llm_calls": calls, "degraded": degraded}


def _demote_unsupported(filled: list[dict], session: dict) -> None:
    """모델이 **빠뜨린** 메시지를 규칙으로 채울 때 — 메뉴 근거가 없는 추가 · 변경은 주문으로 올리지 않는다.

    모델은 주문이 아닌 말의 이벤트를 내지 않는 일이 잦다. 빠뜨렸다는 것 자체가 "주문 아님"에 가까운 신호인데,
    규칙은 "저도"(참조) · "하나"(수량) · "변경"(변경 말)만 보고 주문으로 읽었다 — "혹시 저도 시켜도 되나요?",
    "네네 변경 내용까지 확인했습니다~", "ㅋㅋㅋㅋ 저도 고민되네요", "일단 10잔으로 확인되네요"가 전부 확인 질문이
    되거나 앞 메뉴를 끌어와 잔이 늘었다(2026-11-05 5차 보고 · ORD-0023 실측 13건 중 8건).
    메뉴판에 걸리는 메뉴 · 특성 · 옵션을 말했거나, 메뉴 이름만 덩그러니 말한 것("나 얼박사!")은 남긴다.
    """
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    for event in filled:
        if event.get("event_type") not in ("add", "update"):
            continue
        signals = {consolidate.menu_signal(i, brand_id=brand_id, menu_version=menu_version)
                   for i in event.get("items") or []}
        if signals & {"menu", "trait", "option"}:
            continue
        text = event.get("_raw_text") or ""
        if consolidate._bare_menu(text):
            continue
        consolidate._to_ignore(event, text)
        event["consolidated"] = "llm_skipped_no_menu"


def _restore_ids(event: dict, known: set[str]) -> dict:
    """모델이 message_id 칸에 줄 전체("m3 [박서연] 라떼요")를 넣은 경우를 입력의 id 로 되돌린다.

    대체 모델(gpt-4o-mini)에서 실제로 나왔다 — 컨텍스트 틀의 "{message_id} [{sender}] {text}" 줄에서
    id 가 어디서 끝나는지 헷갈린다. 되돌리지 않으면 "그 메시지의 이벤트가 없다"(누락)로 보여 재생성을 다 쓰고
    규칙으로 떨어진다. **입력에 있던 id 로 시작할 때만** 되돌린다 — 없는 id 를 만들지 않는다.
    """
    def fix(value):
        if not isinstance(value, str) or value in known:
            return value
        head = value.split(" ", 1)[0].strip()
        return head if head in known else value

    return {**event, "message_id": fix(event.get("message_id")),
            "target_message_id": fix(event.get("target_message_id")),
            "evidence_message_ids": [fix(v) for v in event.get("evidence_message_ids") or []]}


def _ordered(events: list[dict]) -> list[dict]:
    for k, event in enumerate(events):
        event["_order"] = (event.get("seq") or 0, k)
    return events


# --- N-04 ------------------------------------------------------------------------------
def route_node(state: dict) -> dict:
    """N-04 경로 분기 — 병렬 결과를 메시지 순서로 정렬하고 이벤트마다 route 를 확정한다.

    분석(snapshot)이면 여기서 **Intake 결과를 먼저 저장**하고 checkpoint = chat_read 를 남긴다(PRD 5.2 Handoff).
    LLM 단계와 규칙 단계의 경계다 — 뒤에서 죽으면 N-02 가 이 저장본으로 이어 간다. 재개 중이면 다시 쓰지 않는다.
    """
    events = sorted(state.get("events") or [], key=lambda e: tuple(e.get("_order") or (0, 0)))
    for event in events:
        if event["event_type"] not in ROUTES:               # 계약이 이미 막는다 — 도달 불가
            event["event_type"] = "ignore"
        event["route"] = event["event_type"]
    if state.get("source") == "snapshot":
        # 인원별 정리 — 대화 전체가 함께 있는 유일한 지점이다(N-03 은 묶음 4건만 본다).
        # LLM 해석 · 규칙 해석 양쪽에 똑같이 걸린다 (2026-11-05 사용자 보고 2 · 7 · 8번).
        # 재개(resumed)로 저장본을 다시 읽어 온 경우에도 거는다 — 저장본은 N-03 결과이므로 정리 전이다
        session = state["session"]
        consolidate.consolidate(events, brand_id=session["brand_id"],
                                menu_version=session["menu_version"])
    if state.get("source") == "snapshot" and not state.get("resumed"):
        _link_persons(events, state.get("messages") or [])
        session_id = state["session"]["session_id"]
        snapshot = db.fetch_one("SELECT snapshot_id FROM app.snapshot WHERE session_id = %s "
                                " ORDER BY snapshot_no DESC LIMIT 1", (session_id,))
        with db.transaction() as cur:
            sessions._insert_events(cur, session_id, snapshot["snapshot_id"], events, state.get("trace_id"))
            sessions.set_checkpoint(cur, session_id, "chat_read")
    # reducer 칸을 덮어쓸 수 없으므로 정렬본은 rows 앞단계로 넘긴다
    return {"rows": [], "cancels": [], "targets": {"ordered": events}}


def _link_persons(events: list[dict], messages: list[dict]) -> None:
    """대리 주문("서연이 것도 라떼")의 주문자 후보를 참여자에서 찾아 붙인다 — **확정하지 않는다** (2026-11-04).

    N-04 에서 하는 이유: 모든 묶음의 이벤트와 대화 전체의 참여자가 함께 있는 유일한 지점이다(N-03 은 묶음 4건만 본다).
    이벤트 저장 직전이라 재개 저장본에도 남는다.
      - 후보가 있으면 항목에 `person_check` 를 단다 → N-07 이 확인 필요(`ambiguous_person`) → 담당자가 질문에서
        "박서연 님 맞아요"를 눌러야 주문자가 바뀐다. 그 전까지 주문자 칸은 대화에 적힌 표현("서연이") 그대로다
      - 이름 그대로 적었으면("박서연 것도") 묻지 않는다. 참여자가 아니면(엄마 · 수진이) 그대로 둔다
      - 대리 주문 앞머리("서연이 것도")는 메뉴 표현에서 뗀다 — 남기면 엄격 매칭이 메뉴를 확정하지 못한다
    """
    senders = {m["message_id"]: m.get("sender") for m in messages}
    participants = list(dict.fromkeys(m["sender"] for m in messages if m.get("sender")))
    if participants == [chat_parse.UNNAMED]:
        return                      # 이름 없는 대화 — 이을 참여자 이름이 없다(보고 1번)
    for event in events:
        if event.get("route") != "add":
            continue
        raw = event.get("_raw_text") or ""
        phrase = intake.proxy_phrase(raw)
        sender = senders.get(event["message_id"])
        said = event.get("person") if event.get("person") and event.get("person") != sender else None
        from_llm = said is not None
        if said is None and phrase:
            said = phrase[0]
        if not said:
            continue
        kind, candidates = intake.link_person(said, participants)
        if kind == "outsider" and not from_llm:
            continue                       # 규칙이 읽은 '이름'이 참여자가 아니면 이름이 아니다("아이스 거 말고")
        named_here = phrase is not None and (phrase[0] == said or (
            kind != "outsider" and intake.link_person(phrase[0], participants)[1] == candidates))
        shown = phrase[0] if (kind == "exact" and said not in raw and named_here) else intake.person_label(said)
        check = None
        if kind in ("linked", "ambiguous") or (kind == "exact" and said not in raw):
            check = {"said": shown, "candidates": candidates}   # LLM 이 성까지 붙여 준 경우도 사람이 확인한다
        event["person"] = (said if kind == "exact" else shown) if check is None else shown
        for item in event.get("items") or []:
            item["person"] = event["person"]
            if check:
                item["person_check"] = check
            text = (item.get("match_text") or "").strip()
            head = phrase[1].strip() if named_here else ""
            if head and text.startswith(head) and text[len(head):].strip():
                item["match_text"] = text[len(head):].strip()


def route_edge(state: dict) -> list[Send] | str:
    """N-04 → N-05(add) · N-08(ignore). update · cancel · confirm 은 add 항목이 생긴 뒤 N-06 이 순서대로."""
    events = state["targets"]["ordered"]
    sends = []
    if state["source"] == "snapshot":
        adds = [(i, e) for i, e in enumerate(events) if e["route"] == "add"]
        sends += [Send("menu", {"session": state["session"], "event": e, "index": i, "events": events,
                                "route": e["route"], "service": state.get("service"),
                                "agent_ok": n < config.MENU_AGENT_MAX_ITEMS, "trace_id": state.get("trace_id")})
                  for n, (i, e) in enumerate(adds)]
    sends += [Send("ignore", {"session": state["session"], "event": e, "index": i,
                              "events": events, "route": e["route"]})
              for i, e in enumerate(events) if e["route"] == "ignore"]
    return sends or "target"


# --- N-05 ------------------------------------------------------------------------------
def menu_node(state: dict) -> dict:
    """N-05 메뉴 매칭 (Menu Agent) — 규칙 먼저. 규칙이 못 정한 항목만 LLM 에이전트가 툴로 후보를 채운다.

    에이전트는 menu_id 를 확정하지 않는다 — 후보 · 순서 · 사유만(PRD 5.1 "후보 제안 · 정렬" · SC-8 오답 확정 0).
    해석 서비스가 없거나(규칙 모드) 실패하면 규칙 결과 그대로다. 툴 호출 왕복은 해석 서비스가 중계한다(graph/agents.py).
    """
    matched, llm_calls = [], 0
    for k, item in enumerate(state["event"].get("items", [])):
        row = match_one(item, state["event"], state["events"], state["index"], state["session"])
        if _needs_menu_agent(row, state):
            row, n = agents.menu_agent(state["session"], row, _window(state["events"], state["index"]),
                                       route=state["route"], service=state["service"],
                                       trace_id=state.get("trace_id"))   # 이력 · API-090 이 분석 1회로 묶인다
            llm_calls += n
            row = _retry_with_split(row, item, state)
        matched.append({"index": state["index"], "k": k, "row": row})
    return {"matched": matched, "llm_calls": llm_calls}


def _retry_with_split(row: dict, item: dict, state: dict) -> dict:
    """Menu Agent 가 원문을 (메뉴 부분, 요청 부분)으로 나눴으면 메뉴 부분으로 **규칙 매칭을 다시** 한다 (2026-11-04).

    "아이스 라떼 바닐라빈 파우더 조금"처럼 규칙의 기타 요청 표현 목록에 없는 요청이 메뉴에 붙으면 엄격 매칭이
    메뉴를 정하지 못하고 요청 문구도 남지 않았다. LLM 은 나누기만 하고, 확정은 규칙이 한다 — 다시 맞춰도 못 정하면
    에이전트 후보를 그대로 두고 요청 문구만 기타 요청에 남긴다(유실 방지).
    """
    split = row.get("llm_split")
    if not split:
        return row
    custom = intake.move_to_custom(row.get("custom_request"), {"request": split["custom_request"]},
                                   {"request": split["custom_request"]})
    retry = match_one({**item, "match_text": split["menu_text"], "custom_request": custom},
                      state["event"], state["events"], state["index"], state["session"])
    if retry.get("menu_id"):
        return {**retry, "agent_tools": row.get("agent_tools"), "agent_reason": row.get("agent_reason"),
                "matched_by": "rule_after_llm_split"}
    return {**row, "custom_request": custom}


def _needs_menu_agent(row: dict, state: dict) -> bool:
    """규칙이 메뉴를 정하지 못한 항목만 — 정한 항목 · 참조 후보를 이미 찾은 항목은 부르지 않는다(D-11 4.2)."""
    if not state.get("service") or not state.get("agent_ok") or row.get("menu_id"):
        return False
    return not (row.get("reference") and row.get("candidates"))


def _window(events: list[dict], index: int) -> list[dict]:
    """에이전트에게 보여 줄 대화 — 그 메시지와 앞 4건(D-12 3.1과 같은 문맥 길이)."""
    picked = events[max(0, index - config.BATCH_CONTEXT):index + 1]
    return [{"message_id": e["message_id"], "sender": e.get("person") or "", "text": e.get("_raw_text") or ""}
            for e in picked]


_SENTENCE = re.compile(r"[.!?\n]")


def _menu_sentence(item: dict, event: dict, session: dict) -> dict | None:
    """한 메시지에 취향 표현과 메뉴 이름이 함께 있으면 **메뉴 이름이 있는 문장**으로 다시 맞춘다.

    "전 오늘 좀 피곤해서 달달한 거 먹을래요. 카라멜 마키아또 아이스로 주세요"가 취향 질문이 되고
    말한 메뉴가 사라졌다(2026-11-05 사용자 보고 6번). 문장을 하나씩 맞춰 보고 **정확히 한 문장만**
    메뉴로 확정되면 그 결과를 쓴다. 두 문장이 각각 메뉴면 주문이 둘이라는 뜻이므로 손대지 않는다.
    """
    raw = (event.get("_raw_text") or "").strip()
    sentences = [s.strip() for s in _SENTENCE.split(raw) if s.strip()]
    if len(sentences) < 2:
        return None
    found = []
    for sentence in sentences:
        for draft in intake.build_items(sentence, item.get("person") or event.get("person") or ""):
            hit = sessions.match_item(draft, brand_id=session["brand_id"],
                                      menu_version=session["menu_version"])
            if hit.get("menu_id"):
                found.append((draft, hit))
    if len(found) != 1:
        return None
    draft, hit = found[0]
    return {**item, "match_text": draft["match_text"], "traits": [],
            "options": {**(item.get("options") or {}), **(draft.get("options") or {})},
            "ambiguous_options": {k: v for k, v in (item.get("ambiguous_options") or {}).items()
                                  if k not in (draft.get("options") or {})},
            "temperature_hint": draft.get("temperature_hint"),
            "custom_request": item.get("custom_request") or draft.get("custom_request")}


def _solid_reference(candidate: dict, events: list[dict], session: dict) -> bool:
    """가리킨 주문이 **그 자체로 확정되는** 주문인가 — 참조 자동 확정의 조건이다 (보고 3번).

    "아아 톨 하나랑 카페라떼 하나 주세요"처럼 한 문장에 메뉴가 둘이면 거짓이다. 그때 자동으로 고르면
    "저도 같은 걸로요"가 둘 중 하나로 조용히 정해진다 — 묻는 쪽이 맞다.
    """
    source_id = candidate.get("source_message_id")
    source = next((e for e in events if e["message_id"] == source_id), None) if source_id else None
    if source is None:
        return False
    text = source.get("_raw_text") or ""
    # 그 메시지가 가리키는 메뉴가 **하나뿐일 때만** 자동 확정한다 (2026-11-05 2차 보고 3번).
    # 엄격 매칭으로 보던 것을 바꿨다 — "그러면 아이스 유스베리 티 어떠세요"처럼 권유 어미가 붙으면
    # 엄격 매칭은 확정하지 못해, 후보가 하나뿐인데도 담당자에게 물었다
    found = menu_service.mentions(session["brand_id"], session["menu_version"], text)
    return len(found) == 1 and found[0] == candidate["menu_id"]


def match_one(item: dict, event: dict, events: list[dict], index: int, session: dict) -> dict:
    """항목 하나를 메뉴판과 맞춘다 — N-05 와 N-06(메뉴가 바뀐 update)이 같은 함수를 쓴다."""
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    matched = sessions.match_item(item, brand_id=brand_id, menu_version=menu_version)
    if not matched.get("menu_id"):
        rescued = _menu_sentence(item, event, session)
        if rescued is not None:
            item = rescued
            matched = sessions.match_item(item, brand_id=brand_id, menu_version=menu_version)
    candidates = matched.get("candidates", [])
    match_reason = matched.get("reason")
    if not matched.get("menu_id") and item.get("reference"):
        referenced = sessions._referenced_menus(events, index, brand_id=brand_id,
                                                menu_version=menu_version)
        if len(referenced) == 1 and _solid_reference(referenced[0], events, session):
            # 후보가 하나면 묻지 않는다 — "저도 라떼요"에 앞 주문이 카페 라떼 하나뿐이면 그것이 답이다
            # (2026-11-05 사용자 보고 3 · 5번). 후보가 둘 이상이거나 가리킨 주문이 그 자체로
            # 확정되지 않으면(한 문장에 메뉴가 둘) 그대로 묻는다
            matched = {**matched, "menu_id": referenced[0]["menu_id"], "name": referenced[0]["name"]}
            candidates, match_reason = [], "reference_menu_single"
        elif referenced:
            candidates, match_reason = referenced, "reference_menu"
    row = {**item, "menu_id": matched.get("menu_id"), "menu_name": matched.get("name"),
           "candidates": candidates, "match_reason": match_reason,
           "_message_id": event["message_id"],
           "_reference_source": candidates[0].get("source_message_id") if candidates else None}
    row = sessions._spec_checks(row, brand_id=brand_id, menu_version=menu_version)
    if row.get("menu_id") and "size" not in (row.get("options") or {}) \
            and not (row.get("ambiguous_options") or {}).get("size"):
        default_size = menu_service.default_option(brand_id, menu_version, "size",
                                                   menu_id=row["menu_id"])
        if default_size:
            row["options"] = {**(row.get("options") or {}), "size": default_size}
    return row


# --- N-08 ------------------------------------------------------------------------------
def ignore_node(state: dict) -> dict:
    """N-08 ignore 기록 — 항목을 만들거나 바꾸지 않는다. 후속 LLM · 툴 호출 0건."""
    return {}


# --- N-06 ------------------------------------------------------------------------------
def target_node(state: dict) -> dict:
    """N-06 대상 연결 — update · cancel · confirm 이 가리키는 항목을 규칙으로 찾는다. 모호하면 확정하지 않는다."""
    session, source = state["session"], state["source"]
    events = state["targets"]["ordered"]
    if source == "chat":
        actionable = next((e for e in events if e["route"] != "ignore"), None)
        route = actionable["route"] if actionable else _chat_route(state["text"], events)
        # 새 주문(add)은 가리킬 앞 주문이 없다 — 대상을 찾으면 **남의 행을 고르게 된다**(4차 보고 1번)
        target = (edits._find_target(session["session_id"], state["text"])
                  if route not in ("ignore", "add") else None)
        return {"targets": {"ordered": events, "event_type": route, "target": target}}
    if source == "reply":
        by_seq = {}
        for e in events:
            seq = int(e["message_id"].removeprefix("reply")) - 1
            if e["route"] != "ignore" and seq not in by_seq:
                by_seq[seq] = e["route"]
        return {"targets": {"ordered": events, "event_types": by_seq}}

    # snapshot — 메시지 순서대로 같은 대화 안의 변경 · 취소를 반영하고, 끝에 항목 번호를 매긴다
    entries = [(m["index"], m["k"], m["row"]) for m in state.get("matched") or []]
    cancels = []
    for index, event in enumerate(events):
        if event["route"] not in ("update", "cancel"):
            continue                    # confirm — 상태 변화 없음(P-03 3절) · 툴 0건(D-10 5절)
        earlier = [row for i, _, row in sorted(entries, key=lambda e: (e[0], e[1])) if i < index]
        if event["route"] == "update":
            if not _apply_update(earlier, event, events, index, session):
                # 바꿀 앞 주문이 없다 — 버리지 않고 새 주문으로 받는다(주문이 조용히 사라지지 않게)
                entries += [(index, k, match_one(item, event, events, index, session))
                            for k, item in enumerate(event.get("items", []))]
        else:
            hit = _narrow_by_options(_candidates(earlier, event), event)
            if len(hit) == 1 and not event.get("target_unclear"):
                hit[0]["_removed"] = True
                cancels.append({"event": event, "row": hit[0]})
            elif hit:
                _mark_target_unclear(hit, "어느 잔을 취소할지 확인해 주세요", event)
    rows = [row for _, _, row in sorted(entries, key=lambda e: (e[0], e[1]))]
    for seq, row in enumerate(rows, start=1):
        row["item_id"] = ids.item_id(session["session_id"], seq)
    return {"rows": rows, "cancels": cancels, "targets": {"ordered": events}}


def _chat_route(text: str, events: list[dict]) -> str:
    """담당자가 친 문장을 LLM 이 잡담으로 봤을 때의 규칙 판정 (2026-11-05 2차 보고 7번).

    system.md 규칙 4 는 **대화 속 지시**를 주문이 아닌 말로 다루라고 한다. 그런데 채팅 입력은
    담당자 본인이 이 시스템에 하는 지시다 — "박서연 과장님은 제외"가 ignore 로 분류되어
    "주문에서 바꿀 것을 찾지 못했어요"만 돌아왔다. 규칙이 변경 · 취소 · 추가로 읽으면 그것을 따른다.

    **add 는 "추가"라고 적은 문장에만 따른다**(4차 보고 1번). 예전에는 "채팅으로 주문을 새로
    만드는 경로는 없다"며 전부 버렸고, 단톡방에 늦게 온 주문을 넣을 길이 없었다. 지금은 N-09 가
    `edits.add_from_chat` 으로 받는다.

    그런데 `intake.classify` 는 **분류되지 않는 말을 add 로 돌려준다** — "오늘 날씨 좋네요"도
    add 다. 규칙만 믿고 열면 잡담이 주문이 된다. 그래서 여기서는 넣으라고 적은 문장만 add 로
    본다. LLM 이 add 로 읽은 경우는 이 함수를 지나지 않는다(그때는 그 판정을 따른다).
    """
    ruled = intake.classify(text)
    # 울타리의 정본은 `edits.ADD_INSTRUCTION` 하나다 — 같은 규칙을 두 곳에 두지 않는다
    if ruled == "add" and not edits.ADD_INSTRUCTION.search(text or ""):
        return "ignore"
    if ruled in ("update", "cancel", "add"):
        for event in events:
            event["route"] = event["event_type"] = ruled      # 출력 가드의 ignore 검사와 어긋나지 않게
        return ruled
    return "ignore"


def _candidates(rows: list[dict], event: dict) -> list[dict]:
    """대상 후보 — LLM 이 가리킨 메시지가 있으면 그 메시지의 항목, 없으면 같은 사람의 최근 항목."""
    live = [r for r in rows if not r.get("_removed")]
    if event.get("target_message_id"):
        hit = [r for r in live if r["_message_id"] == event["target_message_id"]]
        # 가리킨 메시지가 **남의 주문**이면 쓰지 않는다. 해석 서비스가 대상을 잘못 짚는 일이 있다 —
        # 실측 2026-11-07 ORD-0034: 김민수의 "아까 샷 추가한 한 잔 취소할게요" 가 박서연의 m3 을
        # 가리켰고(target_unclear=false), 그 바람에 취소가 반영되지 않고 질문도 뜨지 않았다.
        # 남의 잔을 취소하는 것은 조용히 사라지는 것보다 더 나쁘다. 사람 기준으로 돌아가
        # 문장의 단서(샷 추가 등)로 좁힌다 — _narrow_by_options 가 그 일을 한다
        if event.get("person"):
            hit = [r for r in hit if r["person"] == event["person"]]
        if hit:
            return _distinct(hit)
    hint = (event.get("target_hint") or "").strip()
    mine = [r for r in live if r["person"] == event["person"]]
    if hint:
        named = [r for r in mine if hint in (r.get("menu_text") or "") or hint in (r.get("menu_name") or "")]
        mine = named or mine
    if not mine:
        return []
    last_message = mine[-1]["_message_id"]
    return _distinct([r for r in mine if r["_message_id"] == last_message])


def _narrow_by_options(rows: list[dict], event: dict) -> list[dict]:
    """대상이 둘 이상일 때 문장에 적힌 옵션으로 좁힌다 — "샷 추가한 한 잔 취소"(P-03 INT-007).

    좁혀서 **정확히 하나**가 남을 때만 쓴다. 0개 · 여럿이면 원래 후보를 그대로 돌려 모호로 둔다.
    """
    if len(rows) < 2:
        return rows
    said = intake._options(event.get("_raw_text") or "")
    if not said:
        return rows
    hit = [r for r in rows if all((r.get("options") or {}).get(k) == v for k, v in said.items())]
    return hit if len(hit) == 1 else rows


_CHANGE_VERB = re.compile(r"\s*(바꿀게요|바꿀게|바꿀래요|바꿔\s*주세요|바꿔줘|바꿔요|변경해\s*주세요|변경할게요|"
                          r"할게요|할래요|해\s*주세요|해줘|주세요)[.!~ ]*$")


def _after_instead(text: str) -> str | None:
    """"아아 말고 라떼로 바꿀게요" → "라떼로 바꿀게요". '말고' 앞은 바뀌기 전 값이다."""
    if "말고" not in (text or ""):
        return None
    tail = text.rsplit("말고", 1)[1].strip()
    # 바꾸는 동사는 메뉴 표현이 아니다 — 남기면 엄격 매칭이 "라떼 바꿀게"를 확정하지 않는다(D-16 INT-005)
    tail = _CHANGE_VERB.sub("", tail).strip()
    return tail or None


def _distinct(rows: list[dict]) -> list[dict]:
    """같은 메시지에서 나온 잔이라도 옵션이 다르면 서로 다른 대상이다(P-03 경계 사례 7)."""
    seen, out = set(), []
    for row in rows:
        key = (row.get("menu_id"), tuple(sorted((row.get("options") or {}).items())))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _mark_target_unclear(rows: list[dict], text: str, event: dict | None = None) -> None:
    for row in rows:
        row["_target_unclear_text"] = text
        if event:
            row["_unclear_event"] = event["message_id"]
            row["_unclear_route"] = event["route"]


def _apply_update(rows: list[dict], event: dict, events: list[dict], index: int, session: dict) -> bool:
    """같은 대화 안의 변경("사이즈는 벤티로 할게요") — 새 항목을 만들지 않고 대상 항목을 고친다.

    돌려주는 값: 대상 후보가 있었는가. 없으면 호출부가 새 주문으로 받는다.
    """
    hit = _candidates(rows, event)
    if not hit:
        return False
    # 그 사람의 주문이 하나뿐이면 "어느 주문인지"는 모호할 수 없다 — LLM 의 target_unclear 만으로 묻지 않는다.
    # 물으면 고를 후보가 하나뿐인 "어느 것을 말하는지 골라 주세요"가 뜬다(최유나 "디카페인으로 해주세요" · 2026-11-04)
    only_order = len([r for r in rows if not r.get("_removed") and r["person"] == event["person"]]) == 1
    if len(hit) != 1 or (event.get("target_unclear") and not only_order):
        _mark_target_unclear(hit, "어느 주문을 바꿀지 확인해 주세요", event)
        return True
    target = hit[0]
    raw = event.get("_raw_text") or ""
    # "A 말고 B" — 바뀔 값은 '말고' 뒤에만 있다. 문장 전체로 읽으면 앞의 A(아아 · 2잔)를 새 값으로 읽는다
    tail = _after_instead(raw)
    items = intake.build_items(tail, event["person"]) if tail else event.get("items", [])
    quantity_text = tail or raw
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    for item in items:
        changed = match_one(item, event, events, index, session)
        options = item.get("options") or {}
        # 사양만 바꾸는 변경 — 문장 속 메뉴 이름은 대상을 가리키는 말이지 바꾸려는 값이 아니다
        spec_only = (event.get("consolidated") in consolidate.SPEC_ONLY
                     or consolidate.points_back(raw))
        if not spec_only and changed.get("menu_id") and changed["menu_id"] != target.get("menu_id") \
                and (tail or _names_menu(changed["menu_id"], raw, options, brand_id, menu_version)):
            for key in ("menu_id", "menu_name", "candidates", "match_reason", "menu_text", "match_text"):
                target[key] = changed.get(key)
        if options:
            target["options"] = {**(target.get("options") or {}), **options}
            target["ambiguous_options"] = {k: v for k, v in (target.get("ambiguous_options") or {}).items()
                                           if k not in options}
        if item.get("ambiguous_options"):
            target["ambiguous_options"] = {**(target.get("ambiguous_options") or {}),
                                           **item["ambiguous_options"]}
        if re.search(r"\d+\s*잔|[한두세네]\s*잔", quantity_text):
            target["quantity"] = item.get("quantity") or target.get("quantity")
        target["option_words"] = {**(target.get("option_words") or {}), **(item.get("option_words") or {})}
    # 바꾼 옵션도 처음 만들 때와 같은 검사를 지난다 — 매장에 없는 옵션("오트밀크")은 기타 요청으로 옮긴다.
    # 빠지면 milk=oat 가 옵션에 남아 규칙 실패(failed)가 되고, 엉뚱한 사이즈 질문에 답해도 반영되지 않았다
    # (이지은 대리 · 2026-11-05 5차 보고)
    if set(target.get("options") or {}) - _MENU_OPTION_GROUPS:
        target.update(sessions._spec_checks(target, brand_id=brand_id, menu_version=menu_version))
    target["_updated_by"] = event["message_id"]
    return True


# 메뉴판(DB)이 관리하는 옵션 종류 — 이 밖의 옵션(우유 · 시럽)은 기타 요청으로 옮겨야 한다(11_seed 머리말)
_MENU_OPTION_GROUPS = {"size", "ice", "shot", "bean", "cup"}


def _names_menu(menu_id: str, text: str, options: dict, brand_id: str, menu_version: str) -> bool:
    """변경 문장이 **그 메뉴를 직접 말했는가** — 말하지 않았으면 메뉴를 바꾸지 않는다 (5차 보고).

    "저도 변경 하나만요~ 우유 오트밀크로 바꿔주세요"가 아이스 카페 라떼를 아메리카노로 바꿨다 — 모델이 남의
    메시지(아메리카노)를 참조 근거로 달았고, 규칙 해석에서는 "우유"가 메뉴(우유)로 읽혔다. 우유 옵션을 말한
    문장의 "우유"는 메뉴 이름이 아니다. 온도만 다른 판("라떼" → 아이스 카페 라떼)은 같은 메뉴로 본다.
    """
    named = set(menu_service.mentions(brand_id, menu_version, text))
    catalog = menu_service.catalog(brand_id, menu_version)["by_id"]
    if "milk" in (options or {}) or intake._options(text).get("milk"):
        named = {m for m in named if (catalog.get(m) or {}).get("name") != "우유"}
    if menu_id in named:
        return True
    base = consolidate._base_names(brand_id, menu_version, [menu_id])
    return bool(base & consolidate._base_names(brand_id, menu_version, named))


# --- N-07 ------------------------------------------------------------------------------
def validation_node(state: dict) -> dict:
    """N-07 규칙 검증 (Validation Agent) — 판정은 규칙만. 보조 검토는 판정 권한이 없다."""
    if state["source"] != "snapshot":
        return {}                     # chat · reply 는 N-09 반영 뒤 같은 validate_item 으로 재검증한다
    session = state["session"]
    rows = []
    for row in state.get("rows") or []:
        verdict = validation.validate_item(row, brand_id=session["brand_id"],
                                           menu_version=session["menu_version"])
        verdict = validation.apply_llm_review(verdict, None)
        # 주문자 후보가 있으면(person_check) 후보 버튼이 있는 ambiguous_person 질문이 맡는다 — 여기서 덮지 않는다.
        # 이름 없는 대화는 묻지 않는다 — 주문자를 모른다는 것을 이미 알고 시작한 세션이고, 물어도 고를 이름이 없다
        # (2026-11-05 보고 1번). 이름 없이 메뉴 · 수량 · 옵션만 정리하고 공유도 이름 없이 한다
        nameless = row.get("person") == chat_parse.UNNAMED
        unclear = row.get("_target_unclear_text") or (
            "누구 주문인지 확인해 주세요"
            if row.get("person_unclear") and not row.get("person_check") and not nameless else None)
        if unclear:
            verdict = {**verdict, "rule_verdict": "undecided", "validation_status": "needs_review",
                       "review_reason": "ambiguous_target", "review_reason_text": unclear}
        unit, estimated = settlement.item_price(row, brand_id=session["brand_id"],
                                                menu_version=session["menu_version"])
        rows.append({**row, **verdict, "unit_price": unit, "estimated_price": estimated})
    return {"rows": rows}


def _settlement_for_log(session_id: str) -> dict | None:
    """route_decision 에 붙일 정산 대조 (SC-5). 로그 재료일 뿐이므로 DB 를 못 읽으면 칸을 비우고 넘어간다.

    분석 실패로 번지지 않게 한다 — DB 장애 자체는 db_unavailable 이 따로 남긴다.
    """
    try:
        return settlement.check(sessions.items(session_id))
    except db.DbUnavailable:
        return None


# --- N-09 ------------------------------------------------------------------------------
def apply_node(state: dict) -> dict:
    """N-09 결과 반영 (Settlement 포함) — source 별로 남긴다. 단계마다 체크포인트.

    분석: 이벤트 · chat_read 는 N-04 가 이미 저장했다. 여기서는 규칙 단계 결과(항목 · 취소)를 **한 트랜잭션**으로
    menu_matched · validated 까지 남기고, 정산 → settled 뒤에 보조 검토(LLM · 의견만)를 붙인다.
    LLM 이 도는 구간을 settled 뒤로 둬서, 체크포인트 없이 LLM 을 기다리는 구간이 생기지 않게 한다.
    """
    session, source = state["session"], state["source"]
    if source == "chat":
        t = state["targets"]
        result = edits.apply_chat_event(session, state["text"], t["event_type"], t["target"],
                                        expected_version=state.get("expected_version"))
        return {"result": result}
    if source == "reply":
        result = suggestions.persist(session, state.get("messages_in") or [],
                                     state["targets"]["event_types"])
        return {"result": result}

    session_id = session["session_id"]
    rows = state.get("rows") or []
    with db.transaction() as cur:
        sessions.set_checkpoint(cur, session_id, "menu_matched")
        sessions._insert_items(cur, session_id, rows)
        for cancel in state.get("cancels") or []:
            _cancel(cur, session_id, cancel)
        sessions.set_checkpoint(cur, session_id, "validated")

    live = sessions.items(session_id)
    with db.transaction() as cur:
        settlement.save_lines(session_id, session["version"], settlement.compute_lines(live), cur=cur)
        sessions.set_checkpoint(cur, session_id, "settled")

    # 보조 검토는 의견 · 사유 문구만 바꾼다 — 금액과 무관하므로 정산 뒤에 둬도 결과가 같다
    review_calls = _review_unclear(state, rows)
    sessions._build_questions(session_id, [r for r in rows if not r.get("_removed")],
                              brand_id=session["brand_id"], menu_version=session["menu_version"])
    messages = state.get("messages") or []
    return {"result": {"session_id": session_id,
                       "parsed_counts": {"message_count": len(messages),
                                         "person_count": len({m["sender"] for m in messages})}},
            "llm_calls": review_calls}


def _review_unclear(state: dict, rows: list[dict]) -> int:
    """Validation 보조 검토 — 대상이 모호한 항목만, 저장 직후에(툴 list_order_items · get_order_item 이 DB 를 읽는다).

    의견(agree · disagree)과 사유 문구만 바꾼다. **validation_status 는 건드리지 않는다**(REQ-004 ① — N-10이 다시 본다).
    """
    session, service = state["session"], state.get("service")
    if not service:
        return 0
    targets = [r for r in rows if not r.get("_removed")
               and (r.get("_target_unclear_text") or r.get("person_unclear"))][:config.REVIEW_AGENT_MAX_ITEMS]
    calls = 0
    messages = state.get("messages") or []
    for row in targets:
        seqs = [m["seq"] for m in messages if m["message_id"] in (row.get("_message_id"), row.get("_unclear_event"))]
        lo, hi = (min(seqs) - config.BATCH_CONTEXT, max(seqs)) if seqs else (0, 0)
        window = [{"message_id": m["message_id"], "sender": m["sender"], "text": m["text"]}
                  for m in messages if lo <= m["seq"] <= hi]
        opinion, n = agents.review_agent(session, row, window, route=row.get("_unclear_route") or "add",
                                         service=service, trace_id=state.get("trace_id"))
        calls += n
        if not opinion:
            continue
        row["llm_review_verdict"] = opinion["llm_review_verdict"]
        if opinion["review_reason_text"]:
            row["review_reason_text"] = opinion["review_reason_text"]
        db.execute("UPDATE app.order_item SET llm_review_verdict = %s, "
                   "       review_reason_text = COALESCE(%s, review_reason_text) "
                   " WHERE session_id = %s AND item_id = %s",
                   (opinion["llm_review_verdict"], opinion["review_reason_text"],
                    session["session_id"], row["item_id"]))
    return calls


def _cancel(cur, session_id: str, cancel: dict) -> None:
    """취소 — 물리 삭제하지 않고 removed 로 둔다. 변경 이력에 남는다 (O-6)."""
    target, person = cancel["row"], cancel["event"]["person"]
    cur.execute("UPDATE app.order_item SET removed = true, updated_at = NOW() "
                " WHERE session_id = %s AND item_id = %s", (session_id, target["item_id"]))
    changes.record(cur, session_id, method="chat_cancel", item_id=target["item_id"], who=person,
                   before_text=f'{target.get("menu_name") or target["menu_text"]} '
                               f'{target.get("quantity") or 1}잔',
                   after_text="취소", issue_undo=False)


# --- N-10 ------------------------------------------------------------------------------
def output_guard_node(state: dict) -> dict:
    """N-10 출력 가드 — 단일 출구. 공통 검사 6종을 모든 출처 · 모든 경로에 똑같이 건다(D-12 4.1)."""
    session, source = state["session"], state["source"]
    events = state["targets"]["ordered"]
    guard = guard_items(session)                                        # ① ②

    result = dict(state.get("result") or {})
    for key in ("text", "found_text"):                                  # ③ ④ 바깥으로 나가는 문구
        if isinstance(result.get(key), str):
            result[key] = clean_text(result[key])
    for s in result.get("suggestions") or []:
        for key in ("change_text", "quote"):
            if isinstance(s.get(key), str):
                s[key] = clean_text(s[key])

    # ⑤ 채팅 속 지시 — ignore 이벤트가 항목을 바꾸지 않았다
    ignored = {e["message_id"] for e in events if e["route"] == "ignore"}
    touched = {r["_message_id"] for r in state.get("rows") or []} | \
              {r.get("_updated_by") for r in state.get("rows") or []}
    leaked = sorted(ignored & touched)
    if leaked:
        logging_setup.log("schema_invalid", level="warning", session_id=session["session_id"],
                          agent="output_guard", detail="ignore_changed_items", message_ids=leaked)

    # 규칙 해석으로 대체한 묶음이 있으면 O-6 처리 기록에 남긴다 — 운영자가 "이 주문은 AI 대신 규칙이 읽었다"를 안다.
    # 장애 코드 · 서비스 이름은 담당자 화면에 내지 않는다(F-013). 사건(incident)은 장애 대응 층 Monitor 가 만든다
    degraded = [d for d in state.get("degraded") or [] if d["reason"] in _RECOVERY_TEXT]
    if degraded:
        reasons = sorted({d["reason"] for d in degraded})
        with db.transaction() as cur:
            changes.record(cur, session["session_id"], method="auto_recovery", issue_undo=False,
                           before_text=" · ".join(_RECOVERY_TEXT[r] for r in reasons),
                           after_text=f"규칙 해석으로 이어서 정리 (메시지 {sum(d['messages'] for d in degraded)}개)")

    # ⑥ route_decision 한 줄 · 상태 결정
    by_route: dict[str, int] = {}
    for e in events:
        by_route[e["route"]] = by_route.get(e["route"], 0) + 1
    rows = state.get("rows") or []
    logging_setup.log("route_decision", session_id=session["session_id"], trace_id=state.get("trace_id"),
                      source=source, events_by_type=by_route,
                      needs_review=sessions.review_count(session["session_id"]),
                      matched=sum(1 for r in rows if r.get("menu_id")), items=len(rows),
                      llm_calls=state.get("llm_calls") or 0,
                      degraded=[d["reason"] for d in state.get("degraded") or []],
                      demoted=guard["demoted"],
                      # 처리 계획과 실제 실행의 대조 (PRD 5.9.3) — 계획한 LLM 묶음 중 규칙으로 대체된 수
                      plan_id=(state.get("plan") or {}).get("plan_id"),
                      planned_llm_batches=len((state.get("plan") or {}).get("llm_batches") or []),
                      degraded_batches=len({d.get("batch_no") for d in state.get("degraded") or []
                                            if d.get("batch_no") is not None}),
                      # 정산 대조 (SC-5) — 사람별 금액 합 = 전체 금액. 새 event 를 만들지 않는다(17종 고정)
                      settlement=_settlement_for_log(session["session_id"]))
    if source == "snapshot":
        with db.transaction() as cur:
            pending = sessions.review_count(session["session_id"])
            needs_review = pending or any(d.get("needs_review") for d in state.get("degraded") or [])
            sessions.set_status(cur, session["session_id"],
                                "needs_review" if needs_review else "awaiting_confirmation")
        result["review_count"] = sessions.review_count(session["session_id"])
        if any(d.get("needs_review") for d in state.get("degraded") or []):
            result["needs_review"] = True
    return {"result": result, "guard": guard}


def guard_items(session: dict) -> dict:
    """N-10 ① ② — 그래프 밖(확인 질문 답 · 개별 바꾸기 · 되돌리기)도 **이 함수**를 부른다.

    ① 불변식 — passed 인데 rule_verdict<>passed 인 항목 0건 (REQ-004 ①)
    ② menu_id 가 세션 메뉴판에 있다 (SC-8)
    위반은 그 항목만 확인 필요로 내린다. 재생성하지 않는다.
    """
    session_id = session["session_id"]
    live = sessions.items(session_id)
    catalog = menu_service.catalog(session["brand_id"], session["menu_version"])["by_id"]
    bad_invariant = validation.assert_invariant(live)
    bad_menu = [r["item_id"] for r in live if r.get("menu_id") and r["menu_id"] not in catalog]
    demoted = sorted(set(bad_invariant) | set(bad_menu))
    if demoted:
        with db.transaction() as cur:
            for item_id in bad_menu:
                cur.execute("UPDATE app.order_item SET menu_id = NULL WHERE session_id = %s AND item_id = %s",
                            (session_id, item_id))
            cur.execute(
                "UPDATE app.order_item SET validation_status = 'needs_review', rule_verdict = 'undecided', "
                "       review_reason = COALESCE(review_reason, 'unregistered_menu'), "
                "       review_reason_text = COALESCE(review_reason_text, '메뉴를 확인해 주세요') "
                " WHERE session_id = %s AND item_id = ANY(%s)", (session_id, demoted))
        logging_setup.log("schema_invalid", level="warning", session_id=session_id,
                          agent="output_guard", detail="demoted", item_ids=demoted)
    return {"demoted": len(demoted)}


def clean_text(text: str) -> str:
    """N-10 ③ ④ — 바깥 문구에서 금액 문자열 · 개인 정보 패턴을 지운다."""
    masked, _ = pii.mask_pii(text)
    return _AMOUNT.sub("", masked).strip() if _AMOUNT.search(masked or "") else masked

