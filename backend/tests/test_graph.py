# 업무 층 그래프 — D-12 노드 · 상한 · 경로 분기를 DB 없이 고정한다 (D-15)
#
# 실제 DB · LLM 으로 전체를 태우는 확인은 tests/test_live_flow.py(MOA_LIVE_DB=1)와 tools/d15_graph_check.py 다.
import inspect

import pytest

from app import config, interpreter_client
from app.contracts import OrderEvents
from app.graph import build, nodes

SESSION = {"session_id": "ORD-0001", "brand_id": "starbucks", "menu_version": "mv_20261029",
           "status": "running", "version": 1}


def msgs(n):
    return [{"message_id": f"m{i}", "seq": i, "sender": "김민수", "text": f"아아 톨 {i}잔"}
            for i in range(1, n + 1)]


# --- 상한 3종 · 보조 상한 — D-12 7절과 같은 값 ---------------------------------------------
def test_limits_match_d12():
    assert config.GRAPH_RECURSION_LIMIT == 25
    assert config.MAX_REASK == 3
    assert (config.MAX_REGENERATE, config.MAX_TOOL_ROUNDS, config.MAX_LLM_CALLS_PER_RUN) == (2, 3, 300)
    assert (config.BATCH_SIZE, config.BATCH_CONTEXT, config.BATCH_CONCURRENCY) == (4, 4, 4)
    assert config.BATCH_SIZE_BACKUP == 2


def test_graph_has_the_ten_d12_nodes():
    graph = build.build()
    assert set(graph.get_graph().nodes) - {"__start__", "__end__"} == {
        "input_guard", "batch", "intake", "route", "menu", "target", "validation", "ignore",
        "apply", "output_guard"}


@pytest.mark.parametrize("fn,node_id", [
    (nodes.input_guard_node, "N-01"), (nodes.batch_node, "N-02"), (nodes.intake_node, "N-03"),
    (nodes.route_node, "N-04"), (nodes.menu_node, "N-05"), (nodes.target_node, "N-06"),
    (nodes.validation_node, "N-07"), (nodes.ignore_node, "N-08"), (nodes.apply_node, "N-09"),
    (nodes.output_guard_node, "N-10")])
def test_node_docstring_starts_with_its_id(fn, node_id):
    assert inspect.getdoc(fn).startswith(node_id)                         # D-16 대조 기준


def test_single_entry_and_exit():
    g = build.build().get_graph()
    starts = [e.target for e in g.edges if e.source == "__start__"]
    ends = [e.source for e in g.edges if e.target == "__end__"]
    assert starts == ["input_guard"] and ends == ["output_guard"]           # G-A4


# --- N-01 ----------------------------------------------------------------------------------
def test_input_guard_rejects_unanswered_brand_question():
    with pytest.raises(nodes.GraphRejected):
        nodes.input_guard_node({"session": {**SESSION, "status": "awaiting_brand_answer"},
                                "source": "chat", "text": "x"})


# --- N-02 ----------------------------------------------------------------------------------
def test_batches_of_four_with_four_previous(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(nodes, "_pick_service", lambda: "primary")
    batches = nodes.batch_node({"messages": msgs(9)})["batches"]
    assert [len(b["targets"]) for b in batches] == [4, 4, 1]
    assert [len(b["prev"]) for b in batches] == [0, 4, 4]
    assert batches[1]["prev"][0]["message_id"] == "m1" and batches[2]["prev"][0]["message_id"] == "m5"


def test_backup_service_uses_smaller_batches(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "http://backup")
    monkeypatch.setattr(nodes, "_pick_service", lambda: "backup")
    batches = nodes.batch_node({"messages": msgs(5)})["batches"]
    assert [len(b["targets"]) for b in batches] == [2, 2, 1]               # 5초 제한 (D-12 10.2)
    assert [len(b["prev"]) for b in batches] == [0, 2, 4]


def test_without_interpreter_one_rules_batch(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "")
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "")
    batches = nodes.batch_node({"messages": msgs(9)})["batches"]
    assert len(batches) == 1 and batches[0]["use_llm"] is False


def test_llm_call_cap_turns_extra_batches_into_rules(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(config, "MAX_LLM_CALLS_PER_RUN", 6)                # 묶음당 최악 3회 → 2묶음까지
    monkeypatch.setattr(nodes, "_pick_service", lambda: "primary")
    batches = nodes.batch_node({"messages": msgs(12)})["batches"]
    assert [b["use_llm"] for b in batches] == [True, True, False]


# --- N-03 — 재생성 · 누락 · 장애 · 규칙 대체 -------------------------------------------------
def _batch(targets, prev=()):
    return {"session": SESSION, "trace_id": "tr_T", "existing": [],
            "batch": {"batch_no": 1, "prev": list(prev), "targets": targets, "use_llm": True,
                      "service": "primary"}}


def _contract(*ids, event_type="add"):
    return OrderEvents.model_validate({"events": [
        {"message_id": i, "evidence_message_ids": [i], "target_message_id": None, "reason": "r",
         "event_type": event_type, "person": None, "person_unclear": False, "target_unclear": False,
         "is_reference": False, "items": []} for i in ids]})


@pytest.fixture
def interpreter(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    replies = []

    def fake(task, messages, context, **k):
        assert "session_id" not in context                                 # 식별자는 컨텍스트에 없다
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(interpreter_client, "interpret", fake)
    # 누락 메시지를 규칙으로 채운 뒤의 메뉴 근거 판정(_demote_unsupported)은 메뉴판을 읽는다 — 이 테스트들의
    # 메시지는 "아아 톨 N잔"이라 메뉴가 있다. DB 없이 돌도록 그 판정만 고정한다(강등 자체는 test_user_fixes_1105e)
    from app.services import consolidate
    monkeypatch.setattr(consolidate, "menu_signal", lambda item, **k: "menu")
    return replies


def test_intake_uses_llm_route(interpreter):
    interpreter.append(_contract("m1", "m2", event_type="ignore"))
    out = nodes.intake_node(_batch(msgs(2)))
    assert [e["event_type"] for e in out["events"]] == ["ignore", "ignore"]
    assert out["llm_calls"] == 1 and not out.get("degraded")


def test_intake_regenerates_twice_then_marks_invalid_output_for_review(interpreter):
    bad = interpreter_client.InterpreterError("AGENT_OUTPUT_INVALID", "x", service="primary")
    interpreter.extend([bad, bad, bad])
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 3                                           # 1 + 재생성 2
    assert out["degraded"][0]["reason"] == "regenerate_exhausted"
    assert out["events"][0]["event_type"] == "ignore"                      # 미검증 출력은 주문으로 확정하지 않는다
    assert out["degraded"][0]["needs_review"] is True


def test_missing_event_is_filled_by_rules_without_regenerating(interpreter):
    """누락된 메시지는 **그 메시지만** 규칙으로 채운다 — 묶음 전체를 다시 만들지 않는다 (2026-11-05 3차 보고 2번).

    예전에는 누락을 계약 위반과 같게 보고 재생성 2회를 썼다(호출 3회). 모델이 주문 아닌 말의 이벤트를
    자주 빠뜨려 분석이 느려지고, 잘 읽은 메시지의 해석까지 버려졌다.
    """
    interpreter.extend([_contract("m1")])
    out = nodes.intake_node(_batch(msgs(2)))
    assert out["llm_calls"] == 1
    assert out["degraded"][0]["reason"] == "missing_events"
    assert {e["message_id"] for e in out["events"]} == {"m1", "m2"}


def test_whole_line_message_id_is_restored(interpreter):
    # 대체 모델이 실제로 낸 모양 — message_id 칸에 줄 전체
    bad = _contract("m1").model_dump()
    bad["events"][0]["message_id"] = "m1 [김민수] 아아 톨 1잔"
    interpreter.append(OrderEvents.model_validate(bad))
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 1 and not out.get("degraded")                # 누락으로 재생성하지 않는다
    assert out["events"][0]["message_id"] == "m1"


def test_single_message_batch_owns_its_events(interpreter):
    # 대체 모델이 실제로 낸 모양 — message_id · target 칸에 [이미 정리된 주문]의 항목 id
    bad = _contract("m1", event_type="update").model_dump()
    bad["events"][0].update(message_id="ORD-0008-it_01", target_message_id="ORD-0008-it_01")
    interpreter.append(OrderEvents.model_validate(bad))
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 1 and not out.get("degraded")
    assert out["events"][0]["message_id"] == "m1" and out["events"][0]["target_message_id"] is None


def test_unknown_message_id_is_not_invented(interpreter):
    # 묶음에 메시지가 둘 이상이면 모르는 id 를 어느 메시지에 붙일지 알 수 없다 — 누락으로 둔다
    bad = _contract("m1", "m2").model_dump()
    bad["events"][0]["message_id"] = "m9 [누군가] 없는 메시지"
    interpreter.extend([OrderEvents.model_validate(bad)] * 3)
    out = nodes.intake_node(_batch(msgs(2)))
    assert out["degraded"][0]["reason"] == "missing_events"               # 모르는 id 는 그대로 — 누락


def test_dependency_failure_is_not_retried_here(interpreter):
    interpreter.append(interpreter_client.InterpreterError("DEP_TIMEOUT", "x", service="primary"))
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 1 and out["degraded"][0]["reason"] == "DEP_TIMEOUT"  # 복구는 장애 대응 층


def test_previous_messages_are_not_turned_into_events(interpreter):
    interpreter.append(_contract("m1", "m2"))                              # m1 은 앞 메시지다
    out = nodes.intake_node(_batch(msgs(2)[1:], prev=msgs(1)))
    assert [e["message_id"] for e in out["events"]] == ["m2"]             # SC-5 중복 0


def test_circuit_open_skips_llm(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(nodes, "_pick_service", lambda: None)
    called = []
    monkeypatch.setattr(interpreter_client, "interpret", lambda *a, **k: called.append(1))
    batch = nodes.batch_node({"messages": msgs(1)})["batches"][0]          # 스위치는 N-02 가 한 번 읽는다
    out = nodes.intake_node({**_batch(msgs(1)), "batch": batch})
    assert called == [] and out["degraded"][0]["reason"] == "circuit_open"


# --- N-04 ----------------------------------------------------------------------------------
def test_route_edge_sends_add_to_menu_and_ignore_to_ignore():
    events = [{"message_id": "m1", "route": "add"}, {"message_id": "m2", "route": "ignore"},
              {"message_id": "m3", "route": "cancel"}]
    sends = nodes.route_edge({"source": "snapshot", "session": SESSION, "targets": {"ordered": events}})
    assert sorted(s.node for s in sends) == ["ignore", "menu"]
    assert nodes.route_edge({"source": "snapshot", "session": SESSION,
                             "targets": {"ordered": [events[2]]}}) == "target"


# --- N-06 — 같은 대화 안의 변경 · 취소 ------------------------------------------------------
def _row(mid, person, menu_id, options=None, qty=1):
    return {"_message_id": mid, "person": person, "menu_id": menu_id, "menu_name": menu_id,
            "menu_text": menu_id, "options": options or {}, "ambiguous_options": {}, "quantity": qty}


def _target(matched, events, monkeypatch):
    monkeypatch.setattr(nodes, "match_one", lambda item, *a: {**item, "menu_id": item.get("menu_id")})
    state = {"session": SESSION, "source": "snapshot", "targets": {"ordered": events},
             "matched": [{"index": i, "k": 0, "row": r} for i, r in matched]}
    return nodes.target_node(state)


def test_update_changes_the_earlier_item_instead_of_adding(monkeypatch):
    out = _target([(0, _row("m1", "이지연", "vanilla", {"size": "tall"}))],
                  [{"message_id": "m1", "route": "add", "person": "이지연"},
                   {"message_id": "m2", "route": "update", "person": "이지연", "_raw_text": "벤티로",
                    "items": [{"options": {"size": "venti"}}]}], monkeypatch)
    assert len(out["rows"]) == 1 and out["rows"][0]["options"]["size"] == "venti"   # 경계 사례 3


def test_update_without_earlier_order_becomes_new_order(monkeypatch):
    out = _target([], [{"message_id": "m1", "route": "update", "person": "이지연", "_raw_text": "라떼로",
                        "items": [{"menu_id": "latte", "person": "이지연"}]}], monkeypatch)
    assert len(out["rows"]) == 1                                          # 주문이 조용히 사라지지 않는다


def test_cancel_one_of_two_different_cups_is_not_guessed(monkeypatch):
    rows = [(0, _row("m1", "김민수", "americano", {"size": "tall"})),
            (0, _row("m1", "김민수", "americano", {"size": "tall", "shot": "1"}))]
    out = _target(rows, [{"message_id": "m1", "route": "add", "person": "김민수"},
                         {"message_id": "m2", "route": "cancel", "person": "김민수"}], monkeypatch)
    assert out["cancels"] == []                                           # 경계 사례 7 — 임의 선택 금지
    assert all(r.get("_target_unclear_text") for r in out["rows"])


def test_cancel_single_target_is_applied(monkeypatch):
    out = _target([(0, _row("m1", "김민수", "americano"))],
                  [{"message_id": "m1", "route": "add", "person": "김민수"},
                   {"message_id": "m2", "route": "cancel", "person": "김민수"}], monkeypatch)
    assert len(out["cancels"]) == 1 and out["rows"][0]["_removed"]


def test_cancel_only_looks_at_earlier_messages(monkeypatch):
    out = _target([(1, _row("m2", "김민수", "americano"))],
                  [{"message_id": "m1", "route": "cancel", "person": "김민수"},
                   {"message_id": "m2", "route": "add", "person": "김민수"}], monkeypatch)
    assert out["cancels"] == []                                           # 뒤에 한 주문을 앞에서 지우지 않는다


# --- N-10 ----------------------------------------------------------------------------------
@pytest.mark.parametrize("text,gone", [("이지연 5,100원 바꿨어요", "5,100원"),
                                       ("010-1234-5678 로 연락", "010-1234-5678")])
def test_clean_text_strips_amounts_and_pii(text, gone):
    assert gone not in nodes.clean_text(text)


def test_clean_text_keeps_plain_order_text():
    assert nodes.clean_text("이지연 님 주문을 톨에서 그란데로 바꿨어요.") == "이지연 님 주문을 톨에서 그란데로 바꿨어요."


# --- 되묻기 상한 — MAX_REASK 가 실제로 쓰인다 -------------------------------------------------
class _Cur:
    def __init__(self, count):
        self.count, self.sql = count, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return {"n": self.count, "seq": 9}


def test_follow_up_stops_at_reask_limit(monkeypatch):
    from app.services import edits, questions

    inserted = []
    monkeypatch.setattr(questions, "insert", lambda *a, **k: inserted.append(1))
    monkeypatch.setattr(questions, "build", lambda *a, **k: {})
    row = {"item_id": "ORD-0001-it_01", "validation_status": "needs_review",
           "ambiguous_options": {"size": '"큰 거"'}}
    edits._follow_up(_Cur(config.MAX_REASK), SESSION, row)
    assert inserted == []                                                 # 상한에 닿으면 더 묻지 않는다
    edits._follow_up(_Cur(config.MAX_REASK - 1), SESSION, row)
    assert inserted == [1]


@pytest.mark.parametrize("states,primary,backup,expect", [
    ({}, "http://p", "", "primary"),
    ({"order_interpreter_primary": "circuit_open"}, "http://p", "http://b", "backup"),
    ({"order_interpreter_primary": "circuit_open"}, "http://p", "", None),       # 대체 없음 → 규칙
    ({"order_interpreter_primary": "probing", "order_interpreter_backup": "down"}, "http://p", "http://b", None),
])
def test_kill_switch_picks_service_from_circuit_state(monkeypatch, states, primary, backup, expect):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", primary)
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", backup)
    monkeypatch.setattr(config, "db_configured", lambda: True)
    monkeypatch.setattr(nodes.db, "fetch_all", lambda *a, **k: [{"dependency_key": k_, "state": v}
                                                                for k_, v in states.items()])
    assert nodes._pick_service() == expect


# --- D-05 · D-06 확장 — 진행 지연 안내 · 규칙 대체 기록 -------------------------------------------
def test_progress_says_slow_only_after_threshold():
    import datetime

    from app.services import projection

    now = datetime.datetime.now(datetime.timezone.utc)
    base = {"session_id": "ORD-0001", "status": "running", "checkpoint": "created"}
    fresh = projection.progress_view({**base, "checkpoint_at": now, "updated_at": now}, review_count=0)
    old = now - datetime.timedelta(seconds=config.SLOW_NOTICE_SEC + 1)
    slow = projection.progress_view({**base, "checkpoint_at": old, "updated_at": old}, review_count=0)
    assert fresh["status_line"]["text"] != projection.SLOW_TEXT
    assert slow["status_line"]["text"] == projection.SLOW_TEXT
    done = projection.progress_view({**base, "status": "needs_review", "checkpoint_at": old,
                                     "updated_at": old}, review_count=2)
    assert done["status_line"]["text"] != projection.SLOW_TEXT              # 끝난 분석에는 안 띄운다


@pytest.mark.parametrize("reason,recorded", [("DEP_TIMEOUT", True), ("circuit_open", True),
                                             ("interpreter_unset", False)])
def test_degraded_batches_are_recorded_for_ops(monkeypatch, reason, recorded):
    from contextlib import contextmanager

    from app.services import changes, sessions

    calls = []
    monkeypatch.setattr(nodes, "guard_items", lambda s: {"demoted": 0})
    monkeypatch.setattr(changes, "record", lambda cur, sid, **k: calls.append(k))
    monkeypatch.setattr(sessions, "review_count", lambda sid: 0)

    @contextmanager
    def tx(*a, **k):
        yield object()

    monkeypatch.setattr(nodes.db, "transaction", tx)
    monkeypatch.setattr(sessions, "set_status", lambda *a, **k: None)
    nodes.output_guard_node({"session": SESSION, "source": "chat", "targets": {"ordered": []},
                             "degraded": [{"batch_no": 1, "reason": reason, "messages": 3}]})
    assert bool(calls) is recorded
    if recorded:
        assert calls[0]["method"] == "auto_recovery" and "규칙 해석" in calls[0]["after_text"]
        assert reason not in calls[0]["before_text"]                        # 장애 코드는 문구에 넣지 않는다


# --- D-16 인텐트 확인에서 드러난 규칙 빈틈 3건 ---------------------------------------------------
def _match_by_text(monkeypatch):
    def fake(item, *a):
        text = item.get("match_text") or ""
        return {**item, "menu_id": "latte" if "라떼" in text else ("americano" if "아아" in text else None),
                "menu_name": "카페 라떼" if "라떼" in text else None}
    monkeypatch.setattr(nodes, "match_one", fake)


def test_update_reads_value_after_malgo(monkeypatch):
    _match_by_text(monkeypatch)
    rows = [(0, _row("m1", "박서연", "americano", {"size": "tall"}))]
    state = {"session": SESSION, "source": "snapshot", "matched": [{"index": i, "k": 0, "row": r} for i, r in rows],
             "targets": {"ordered": [{"message_id": "m1", "route": "add", "person": "박서연"},
                                     {"message_id": "m2", "route": "update", "person": "박서연",
                                      "_raw_text": "아아 말고 라떼로 바꿀게요", "items": []}]}}
    out = nodes.target_node(state)
    assert len(out["rows"]) == 1 and out["rows"][0]["menu_id"] == "latte"      # INT-005


def test_update_quantity_after_malgo(monkeypatch):
    _match_by_text(monkeypatch)
    rows = [(0, _row("m1", "김민수", "americano", {"size": "tall"}, qty=2))]
    state = {"session": SESSION, "source": "snapshot", "matched": [{"index": i, "k": 0, "row": r} for i, r in rows],
             "targets": {"ordered": [{"message_id": "m1", "route": "add", "person": "김민수"},
                                     {"message_id": "m2", "route": "update", "person": "김민수",
                                      "_raw_text": "2잔 말고 1잔만요", "items": []}]}}
    assert nodes.target_node(state)["rows"][0]["quantity"] == 1             # INT-006 — 앞의 2잔을 읽지 않는다


@pytest.mark.parametrize("text,tail", [("아아 말고 라떼로 바꿀게요", "라떼로"), ("2잔 말고 1잔만요", "1잔만요"),
                                       ("톨 말고 그란데로 해 주세요", "그란데로"), ("라떼로 바꿔줘", None)])
def test_after_instead_strips_change_verbs(text, tail):
    assert nodes._after_instead(text) == tail


def test_cancel_narrowed_by_option_in_sentence(monkeypatch):
    rows = [(0, _row("m1", "김민수", "americano", {"size": "tall"})),
            (0, _row("m1", "김민수", "americano", {"size": "tall", "shot": "extra"}))]
    out = _target(rows, [{"message_id": "m1", "route": "add", "person": "김민수"},
                         {"message_id": "m2", "route": "cancel", "person": "김민수",
                          "_raw_text": "아까 샷 추가한 한 잔 취소할게요"}], monkeypatch)
    assert len(out["cancels"]) == 1 and out["cancels"][0]["row"]["options"].get("shot") == "extra"   # INT-007
    # 옵션을 말하지 않으면 여전히 고르지 않는다 (경계 사례 7) — 앞 호출이 행에 취소 표시를 남겼으므로 새 행으로
    rows = [(0, _row("m1", "김민수", "americano", {"size": "tall"})),
            (0, _row("m1", "김민수", "americano", {"size": "tall", "shot": "extra"}))]
    out = _target(rows, [{"message_id": "m1", "route": "add", "person": "김민수"},
                         {"message_id": "m2", "route": "cancel", "person": "김민수",
                          "_raw_text": "하나 취소할게요"}], monkeypatch)
    assert out["cancels"] == []


def test_trait_reason_wins_over_unregistered(monkeypatch):
    from app.services import menu, validation

    monkeypatch.setattr(menu, "is_valid_option", lambda *a, **k: True)
    verdict = validation.validate_item({"menu_id": None, "traits": ["단맛"], "quantity": 1,
                                        "ambiguous_options": {}, "options": {}},
                                       brand_id="starbucks", menu_version="mv")
    assert verdict["review_reason"] == "trait_expression"                    # INT-011 — 특성 후보로 묻는다


# --- LLM 이 읽은 기타 요청을 변환에서 잃지 않는다 (05.리포트 멀티에이전트 구조 분석 갱신 중 발견) ----------
def test_llm_custom_request_survives_conversion():
    from app.services import intake

    event = _contract("m1").model_dump()["events"][0]
    event["items"] = [{"menu_text": "자바칩 프라푸치노", "temperature_text": None, "size_text": None, "quantity": 1,
                       "option_texts": [], "custom_request": "휘핑 빼주세요"}]
    message = {"message_id": "m1", "seq": 1, "sender": "이지연", "text": "자바칩 프라푸치노 휘핑 빼주세요"}
    item = intake.from_contract([event], [message])[0]["items"][0]
    assert item["custom_request"] == "휘핑 빼주세요"                       # 전에는 None — 요청이 사라졌다
    assert "휘핑" not in item["match_text"]                                # 메뉴는 LLM 이 떼어 준 표현으로 맞춘다
    plain = _contract("m1").model_dump()["events"][0]
    plain["items"] = [{**event["items"][0], "custom_request": None}]
    assert intake.from_contract([plain], [{**message, "text": "자바칩 프라푸치노 하나"}])[0]["items"][0]["custom_request"] is None



def test_menu_send_carries_trace_id_and_service():
    events = [{"message_id": "m1", "route": "add"}]
    sends = nodes.route_edge({"source": "snapshot", "session": SESSION, "trace_id": "tr_X", "service": "primary",
                              "targets": {"ordered": events}})
    assert sends[0].arg["trace_id"] == "tr_X" and sends[0].arg["service"] == "primary" and sends[0].arg["agent_ok"]


# --- 킬 스위치도 냉각을 본다 (PRD 5.6 F-4) ----------------------------------------------------
# 실측 결함 2026-11-06: `_pick_service` 가 usable=("up","degraded") 만 보고 cooldown_until 을
# 무시해, 1차가 circuit_open 이면 **냉각이 지난 뒤에도** 대체로만 갔다. 그래서 1차에 시험 요청이
# 가지 않아 회로가 영구히 닫히지 않았다(회로를 닫는 다른 길은 healing 의 F-4 probe 뿐이다).
# 회로 해석을 interpreter_client.circuit_usable 한 곳으로 모아 두 층이 같은 규칙을 쓰게 한다.
@pytest.mark.parametrize("state,cooldown_sec,picked", [
    ("up", None, "primary"),
    ("degraded", None, "primary"),
    ("circuit_open", 30, "backup"),      # 냉각 중 — 대체로 간다
    ("circuit_open", -1, "primary"),     # 냉각이 지났다 — 1차에 시험 요청 1회
    ("down", None, "backup"),
    ("probing", None, "backup"),         # healing 이 시험 중 — 업무 층은 끼어들지 않는다
])
def test_pick_service_sends_one_trial_to_primary_after_cooldown(monkeypatch, state, cooldown_sec, picked):
    import datetime as _dt

    cooldown = (None if cooldown_sec is None else
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=cooldown_sec))
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "http://backup")
    monkeypatch.setattr(config, "db_configured", lambda: True)
    monkeypatch.setattr(nodes.db, "fetch_all", lambda *a, **k: [
        {"dependency_key": "order_interpreter_primary", "state": state, "cooldown_until": cooldown},
        {"dependency_key": "order_interpreter_backup", "state": "up", "cooldown_until": None},
    ])
    assert nodes._pick_service() == picked
