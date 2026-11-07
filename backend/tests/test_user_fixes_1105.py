# 2026-11-05 사용자 보고 11건 수정분 — DB 없이 도는 것만 여기 둔다(실DB 흐름은 tests/test_live_flow.py)
#
# 번호는 사용자 보고 번호다. 각 테스트 이름 앞에 번호를 적어 둔다 — 회귀가 나면 어느 보고가 되돌아갔는지 바로 보인다.
import pytest

from app.services import chat_parse, share
from tests import q03_fakes as F


# --- 1번 이름 같은 특징이 없는 채팅 -------------------------------------------------------------
NAMELESS = """아이스 아메리카노 한 잔요
저는 따뜻한 라떼 주세요"""


def test_01_nameless_chat_is_accepted_as_one_unnamed_person():
    """이름이 한 줄도 없으면 거부하지 않는다 — `이름 없음` 한 사람의 대화로 받는다."""
    parsed = chat_parse.parse(NAMELESS)
    assert parsed["can_proceed"] is True
    assert parsed["unreadable_lines"] == []
    assert [m["sender"] for m in parsed["messages"]] == [chat_parse.UNNAMED] * 2
    assert [m["text"] for m in parsed["messages"]] == NAMELESS.splitlines()


def test_01_nameless_summary_does_not_claim_one_person():
    assert "이름이 없는 대화예요" in chat_parse.parse(NAMELESS)["summary_text"]
    assert "1명" not in chat_parse.parse(NAMELESS)["summary_text"]


def test_01_mixed_format_still_reports_unreadable_lines():
    """이름이 일부라도 있으면 형식이 섞인 것이다 — 예전 동작을 바꾸지 않는다."""
    parsed = chat_parse.parse("[이지연] 라떼요\n아이스 아메리카노 한 잔요")
    assert [m["sender"] for m in parsed["messages"]] == ["이지연"]
    assert len(parsed["unreadable_lines"]) == 1


def test_01_nameless_order_is_shared_without_names():
    """`이름 없음 · 이름 없음`을 단톡방에 올리지 않는다 — 요청 없이도 이름 줄을 뺀다."""
    sheet = {"brand": {"id": "starbucks"}, "summary": {"people": 1, "cups": 2},
             "totals": {"cups": 2, "estimated_total": 9000},
             "groups": [{"menu_text": "아이스 아메리카노", "options": [], "quantity": 2,
                         "people": [chat_parse.UNNAMED, chat_parse.UNNAMED], "custom_request": None}]}
    out = share.view(sheet)
    assert out["show_names"] is False
    assert chat_parse.UNNAMED not in out["content"]
    assert "1. 아이스 아메리카노 × 2잔" in out["content"] and "총 2잔 |" in out["content"]   # 이름 없는 대화는 인원을 말하지 않는다


def test_01_named_order_still_shows_names():
    sheet = {"brand": {"id": "starbucks"}, "summary": {"people": 1, "cups": 1},
             "totals": {"cups": 1, "estimated_total": 4500},
             "groups": [{"menu_text": "카페 라떼", "options": [], "quantity": 1,
                         "people": ["이지연"], "custom_request": None}]}
    out = share.view(sheet)
    assert out["show_names"] is True and "이지연" in out["content"]


# --- 공통 — 사용자 보고와 같은 8줄 대화 ---------------------------------------------------------
LINES = [
    "[김민수 대리] 오늘 회의 끝나고 다 같이 커피 한잔 어떠세요",
    "[최유나 사원] 좋아요~ 어디로 가나요?",
    "[김민수 대리] 네네 스타벅스입니다~",
    "[이지연 사원] 저는 따뜻한 카페라떼 하나 부탁드려요~",
    "[최유나 사원] 저도 라떼요",
    "[정하준 대리] 전 오늘 좀 피곤해서 달달한 거 먹을래요. 바닐라라떼 아이스로 주세요",
    "[이지연 사원] 제 거는 디카페인으로 가능할까요?",
    "[김민수 대리] 저는 시원한 카페라떼 하나 먹을래요",
]


def _llm_item(menu_text, *, quantity=1, option_texts=None):
    return {"menu_text": menu_text, "temperature_text": None, "size_text": None,
            "quantity": quantity, "option_texts": option_texts or [], "custom_request": None}


def run_with_llm(monkeypatch, store, lines, events):
    """해석 서비스가 `events`를 그대로 돌려주게 두고 그래프 전체(N-01 ~ N-10)를 태운다.

    events = [(message_id, event_type, [item, ...])] — 사용자가 본 그 LLM 출력을 재현한다.
    """
    from app import config, graph, interpreter_client
    from app.contracts import MenuStep, OrderEvents, ReviewStep
    from app.graph import nodes

    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(nodes, "_pick_service", lambda: "primary")

    def fake(task, messages, context, **k):
        # Menu Agent · 보조 검토는 "의견 없음"으로 끝낸다 — 이 테스트가 보는 것은 규칙 · 인원별 정리다
        if task == "normalize_menu":
            return MenuStep.model_validate({"reason": "후보 없음", "action": "finish",
                                            "tool": None, "args_json": None, "matches": []})
        if task == "review":
            return ReviewStep.model_validate({"reason": "의견 없음", "action": "finish",
                                              "tool": None, "args_json": None, "verdicts": []})
        wanted = {m["message_id"] for m in messages}
        base = {"evidence_message_ids": [], "target_message_id": None, "reason": "r",
                "person": None, "person_unclear": False, "target_unclear": False,
                "is_reference": False}
        return OrderEvents.model_validate({"events": [
            {**base, "message_id": mid, "evidence_message_ids": [mid],
             "event_type": etype, "items": items}
            for mid, etype, items in events if mid in wanted]})

    monkeypatch.setattr(interpreter_client, "interpret", fake)
    install_wide(monkeypatch, store, messages=F.chat(lines))
    return graph.run("snapshot", dict(F.SESSION), trace_id="tr_1105")


# 사용자가 실제로 본 LLM 출력 — 메시지마다 add, 메뉴 표현은 문장 통째로
AS_REPORTED = [
    ("m1", "add", [_llm_item("오늘 회의 끝나고 다 같이 커피 한잔 어떠세요")]),
    ("m2", "add", [_llm_item("좋아요~ 어디로 가나요")]),
    ("m3", "add", [_llm_item("네네 스타벅스입니다~")]),
    ("m4", "add", [_llm_item("따뜻한 카페라떼 하나")]),
    ("m5", "add", [_llm_item("저도 라떼")]),
    ("m6", "add", [_llm_item("달달한 거")]),
    ("m7", "add", [_llm_item("디카페인", option_texts=["bean=decaf"])]),
    ("m8", "add", [_llm_item("시원한 카페라떼 하나 먹을래요")]),
]


@pytest.fixture
def store():
    return F.Store()


# --- 2번 메뉴를 말하지 않은 채팅은 항목이 되지 않는다 -------------------------------------------
def test_02_chatter_does_not_become_an_order_item(monkeypatch, store):
    out = run_with_llm(monkeypatch, store, LINES, AS_REPORTED)
    routes = {e["message_id"]: e["route"] for e in out["targets"]["ordered"]}

    # 인사 · 장소 확인 · 제안은 주문이 아니다
    assert routes["m1"] == "ignore", "다 같이 커피 한잔 어떠세요 — 제안이다"
    assert routes["m2"] == "ignore", "어디로 가나요 — 주문이 아니다"
    assert routes["m3"] == "ignore", "네네 스타벅스입니다 — 매장 확인이다"
    texts = [r["menu_text"] for r in store.live()]
    assert not any("어떠세요" in t or "스타벅스입니다" in t or "어디로" in t for t in texts)


# --- 7번 인원별 정리 — 한 사람이 말한 횟수만큼 잔이 늘지 않는다 ---------------------------------
def test_07_one_person_gets_one_cup_not_one_per_message(monkeypatch, store):
    run_with_llm(monkeypatch, store, LINES, AS_REPORTED)
    cups = {}
    for row in store.live():
        cups[row["person"]] = cups.get(row["person"], 0) + (row.get("quantity") or 1)

    assert cups.get("김민수 대리") == 1, f"김민수 대리는 1잔이어야 한다: {cups}"
    assert sum(cups.values()) == 4, f"4명 4잔으로 정리돼야 한다: {cups}"
    assert set(cups) == {"김민수 대리", "최유나 사원", "이지연 사원", "정하준 대리"}


def test_07_order_without_a_menu_is_still_asked_not_dropped(monkeypatch, store):
    """주문 문형이 있는데 메뉴를 못 알아들은 것은 버리지 않는다 — 담당자에게 묻는다."""
    out = run_with_llm(monkeypatch, store, ["[최유나 사원] 저는 아무거나 하나 주세요"],
                       [("m1", "add", [_llm_item("아무거나 하나")])])
    assert [e["route"] for e in out["targets"]["ordered"]] == ["add"]
    assert len(store.live()) == 1
    assert store.live()[0]["validation_status"] == "needs_review"


# --- 메뉴판 확장 — 보고 6 · 8번은 시드의 카라멜 마키아또 쌍과 원두(디카페인) 옵션이 필요하다 -----
# q03_fakes 의 메뉴판을 건드리지 않는다(다른 테스트의 기대값이 걸려 있다). 이 파일에서만 늘린다.
EXTRA_MENUS = [
    {"menu_id": "sb_020", "brand_id": F.BRAND, "menu_version": F.MENU_VERSION,
     "category": "에스프레소", "name": "카라멜 마키아또", "base_price": 5900, "trait_tags": ["단맛", "따뜻한"]},
    {"menu_id": "sb_021", "brand_id": F.BRAND, "menu_version": F.MENU_VERSION,
     "category": "에스프레소", "name": "아이스 카라멜 마키아또", "base_price": 5900, "trait_tags": ["단맛"]},
]
EXTRA_ALIASES = [("카라멜마키아또", "sb_020"), ("카라멜마키아또", "sb_021"), ("카마", "sb_020")]
EXTRA_OPTIONS = [("sb_opt_bean_base", "bean", "base", "기본", 0, True),
                 ("sb_opt_bean_decaf", "bean", "decaf", "디카페인", 0, False)]


def install_wide(monkeypatch, store, messages=None):
    """F.install 뒤에 메뉴판을 늘려 끼운다 — 캐시에 직접 넣으므로 DB 는 여전히 쓰지 않는다."""
    from app.services import menu as menu_service
    from app.services.text_norm import normalize

    F.install(monkeypatch, store, messages=messages)
    cat = menu_service._CACHE[(F.BRAND, F.MENU_VERSION)]
    menus = list(cat["menus"]) + EXTRA_MENUS
    alias_index = {k: list(v) for k, v in cat["alias_index"].items()}
    for menu in EXTRA_MENUS:
        alias_index.setdefault(normalize(menu["name"]), []).append(menu["menu_id"])
    for alias, menu_id in EXTRA_ALIASES:
        alias_index.setdefault(normalize(alias), []).append(menu_id)
    options = list(cat["options"]) + [
        {"option_id": oid, "menu_id": None, "category": None, "option_group": group,
         "option_value": value, "value_label": label, "extra_price": price, "is_default": is_default}
        for oid, group, value, label, price, is_default in EXTRA_OPTIONS]
    monkeypatch.setitem(menu_service._CACHE, (F.BRAND, F.MENU_VERSION),
                        {**cat, "menus": menus, "by_id": {m["menu_id"]: m for m in menus},
                         "alias_index": alias_index, "options": options})
    return store


def run_rules(monkeypatch, store, lines):
    """해석 서비스 없이 규칙만으로 그래프 전체를 태운다 (LLM 0회 · 같은 입력 같은 결과).

    돌려주는 값은 그래프 결과다 — 저장된 행은 `store.live()` 로 본다.
    """
    from app import graph

    F.no_interpreter(monkeypatch)
    install_wide(monkeypatch, store, messages=F.chat(lines))
    return graph.run("snapshot", dict(F.SESSION), trace_id="tr_1105")


def rows_after_rules(monkeypatch, lines):
    """규칙만으로 태운 뒤의 저장소 — 행 하나만 보는 테스트가 쓴다."""
    store = F.Store()
    run_rules(monkeypatch, store, lines)
    return store


def only_row(store):
    live = store.live()
    assert len(live) == 1, [(r["person"], r["menu_name"] or r["menu_text"]) for r in live]
    return live[0]


# --- 3번 참조 주문("저도 라떼요")은 후보가 하나면 자동 확정한다 ---------------------------------
def test_03_reference_order_with_one_candidate_is_confirmed(monkeypatch, store):
    out = run_rules(monkeypatch, store, [
        "[이지연 사원] 저는 따뜻한 카페라떼 하나 부탁드려요~",
        "[최유나 사원] 저도요",
    ])
    rows = {r["person"]: r for r in store.live()}
    assert rows["최유나 사원"]["menu_name"] == "카페 라떼"
    assert rows["최유나 사원"]["validation_status"] == "passed"
    assert store.questions == [] or all(q["item_id"] != rows["최유나 사원"]["item_id"]
                                        for q in store.questions)
    assert out["result"]["review_count"] == 0


def test_03_reference_order_with_two_candidates_still_asks(monkeypatch, store):
    """후보가 둘이면 자동 확정하지 않는다 — 임의로 고르지 않는 원칙은 그대로다."""
    run_rules(monkeypatch, store, [
        "[이지연 사원] 저는 아아 톨 하나랑 카페라떼 하나 주세요",
        "[최유나 사원] 저도 같은 걸로요",
    ])
    rows = {r["person"]: r for r in store.live()}
    assert rows["최유나 사원"]["menu_id"] is None
    assert rows["최유나 사원"]["validation_status"] == "needs_review"


# --- 4번 "시원한 카페라떼 하나 먹을래요" → 아이스 카페 라떼 ------------------------------------
@pytest.mark.parametrize("line, menu", [
    ("[김민수 대리] 저는 시원한 카페라떼 하나 먹을래요", "아이스 카페 라떼"),
    ("[김민수 대리] 아이스 아메리카노 두 잔 주문할게요", "아이스 아메리카노"),
    ("[김민수 대리] 저 바닐라라떼 마실래요", "바닐라라떼"),
    ("[김민수 대리] 따뜻한 카페라떼 하나 시킬게요", "카페 라떼"),
])
def test_04_order_verb_endings_do_not_block_the_menu(monkeypatch, line, menu):
    store = rows_after_rules(monkeypatch, [line])
    row = only_row(store)
    assert row["menu_name"] == menu, row["menu_text"]
    assert row["validation_status"] == "passed"


# --- 6번 취향 표현과 메뉴 이름이 한 메시지에 있으면 메뉴 이름이 이긴다 --------------------------
def test_06_explicit_menu_beats_trait_expression(monkeypatch):
    store = rows_after_rules(monkeypatch, [
        "[정하준 대리] 전 오늘 좀 피곤해서 달달한 거 먹을래요. 카라멜 마키아또 아이스로 주세요"])
    row = only_row(store)
    assert row["menu_name"] == "아이스 카라멜 마키아또"
    assert row["validation_status"] == "passed"
    assert row["review_reason"] != "trait_expression"


def test_06_iced_word_after_the_menu_name_picks_the_iced_menu(monkeypatch):
    store = rows_after_rules(monkeypatch, ["[정하준 대리] 카라멜 마키아또 아이스로 주세요"])
    assert only_row(store)["menu_name"] == "아이스 카라멜 마키아또"


def test_06_trait_only_still_asks(monkeypatch):
    """메뉴 이름이 없으면 그대로 묻는다 — 취향만 말한 주문은 사람이 고른다(PRD 5.1)."""
    store = rows_after_rules(monkeypatch, ["[정하준 대리] 전 달달한 거 하나 먹을래요"])
    row = only_row(store)
    assert row["menu_id"] is None and row["review_reason"] == "trait_expression"


def test_06_unregistered_modifier_is_not_auto_confirmed_by_temperature(monkeypatch):
    """온도로 후보를 좁히는 것이 미등록 메뉴 확정에 쓰이지 않는다 (SC-8)."""
    store = rows_after_rules(monkeypatch, ["[정하준 대리] 헤이즐넛 카라멜 마키아또 아이스로 주세요"])
    row = only_row(store)
    assert row["menu_id"] is None
    assert row["review_reason"] == "unregistered_menu"


# --- 8번 메뉴 없이 사양만 말한 메시지는 앞 주문의 변경이다 --------------------------------------
def test_08_decaf_request_updates_the_existing_order(monkeypatch, store):
    out = run_with_llm(monkeypatch, store, [
        "[이지연 사원] 저는 따뜻한 카페라떼 하나 부탁드려요~",
        "[이지연 사원] 제 거는 디카페인으로 가능할까요?",
    ], [("m1", "add", [_llm_item("따뜻한 카페라떼 하나")]),
        ("m2", "add", [_llm_item("디카페인", option_texts=["bean=decaf"])])])

    assert [e["route"] for e in out["targets"]["ordered"]] == ["add", "update"]
    row = only_row(store)
    assert row["menu_name"] == "카페 라떼"
    assert row["options"].get("bean") == "decaf"
    assert row["validation_status"] == "passed"


def test_08_rules_path_also_updates_instead_of_adding(monkeypatch, store):
    """규칙 해석에서는 "가능할까요?"가 질문으로 분류된다 — 그래도 앞 주문의 변경으로 받는다."""
    run_rules(monkeypatch, store, [
        "[이지연 사원] 저는 따뜻한 카페라떼 하나 부탁드려요~",
        "[이지연 사원] 제 거는 디카페인으로 가능할까요?",
    ])
    row = only_row(store)
    assert row["options"].get("bean") == "decaf"


def test_08_chatter_with_an_option_word_does_not_change_an_order(monkeypatch, store):
    """자기 주문을 가리키지 않는 잡담은 앞 주문을 바꾸지 않는다."""
    run_rules(monkeypatch, store, [
        "[이지연 사원] 저는 따뜻한 카페라떼 하나 부탁드려요~",
        "[김민수 대리] 디카페인도 있나요?",
    ])
    row = only_row(store)
    assert row["options"].get("bean") is None
