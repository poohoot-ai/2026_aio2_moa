# 2026-11-05 2차 사용자 보고 7건 — DB 없이 도는 것 (실DB 흐름은 test_user_fixes_1105b_live.py)
#
# 1 이미 주문한 사람의 확인 질문이 주문이 됐다        2 앞 주문을 되짚는 사양 변경이 새 주문이 됐다
# 3 권유 문장을 가리킨 참조가 자동 확정되지 않았다    4 한 사람이 여러 잔 · 수정과 추가의 구분
# 5 "넵" 같은 맞장구가 확인 질문이 됐다              6 기본 사이즈 자동 처리     7 채팅 "제외"
import pytest

from app.services import intake
from app.services import menu as menu_service
from tests import q03_fakes as F
from tests.test_user_fixes_1105 import (_llm_item, install_wide, only_row, rows_after_rules,
                                        run_rules, run_with_llm)


@pytest.fixture
def store():
    return F.Store()


# --- 1번 주문이 아닌 확인 · 권유 문장 -----------------------------------------------------------
@pytest.mark.parametrize("text", [
    "혹시 스타벅스에서 주문하는 거죠?",
    "오늘 회의 끝나고 다 같이 커피 한잔 하실래요?",
    "뭐 드실래요?",
    "스타벅스 맞죠?",
    "거기 아이스 아메리카노 맛있나요?",
])
def test_01_asking_and_proposing_is_not_an_order(text):
    assert intake.classify(text) == "ignore", text
    assert intake.order_pattern(text) is False, text


def test_01_person_who_already_ordered_is_not_asked_again(monkeypatch, store):
    """이미 주문한 사람이 던진 확인 질문 때문에 확인 요청이 생기지 않는다."""
    out = run_with_llm(monkeypatch, store, [
        "[한도윤 과장] 저는 아이스 아메리카노 톨 얼음 적게로 주세요",
        "[한도윤 과장] 혹시 스타벅스에서 주문하는 거죠?",
    ], [("m1", "add", [_llm_item("아이스 아메리카노 톨 얼음 적게")]),
        ("m2", "add", [_llm_item("혹시 스타벅스에서 주문하는 거죠")])])

    assert [e["route"] for e in out["targets"]["ordered"]] == ["add", "ignore"]
    row = only_row(store)
    assert row["menu_name"] == "아이스 아메리카노"
    assert row["validation_status"] == "passed"


def test_01_order_verb_still_wins_when_the_sentence_really_orders():
    assert intake.order_pattern("아이스 아메리카노 두 잔 주문할게요") is True
    assert intake.classify("저는 아이스 아메리카노 마실래요") == "add"


# --- 2번 앞 주문을 되짚는 사양 변경 --------------------------------------------------------------
def test_02_lookback_spec_change_updates_instead_of_adding(monkeypatch, store):
    out = run_with_llm(monkeypatch, store, [
        "[정하준 대리] 저는 아이스 바닐라라떼 하나요",
        "[정하준 대리] 아까 바닐라라떼 주문했는데 샷 하나 더 추가해주세요",
    ], [("m1", "add", [_llm_item("아이스 바닐라라떼")]),
        ("m2", "add", [_llm_item("바닐라라떼 샷 추가", option_texts=["shot=extra"])])])

    assert [e["route"] for e in out["targets"]["ordered"]] == ["add", "update"]
    row = only_row(store)
    assert row["menu_name"] == "아이스 바닐라라떼"
    assert row["options"].get("shot") == "extra"
    assert row["validation_status"] == "passed"


def test_02_menu_name_button_matches_by_name_not_alias():
    """버튼 라벨(메뉴 이름)이 다른 메뉴의 별칭이어도 이름으로 확정된다 — "그 내용은 반영하지 못했어요" 방지."""
    from tests.test_user_fixes_1105 import EXTRA_ALIASES, EXTRA_MENUS   # noqa: F401

    # 같은 별칭을 두 메뉴가 공유하는 상황을 그대로 만든다
    cat = F.catalog()
    cat["menus"] = cat["menus"] + EXTRA_MENUS
    cat["by_id"].update({m["menu_id"]: m for m in EXTRA_MENUS})
    from app.services.text_norm import normalize
    for menu in EXTRA_MENUS:
        cat["alias_index"].setdefault(normalize(menu["name"]), []).append(menu["menu_id"])
    for alias, menu_id in EXTRA_ALIASES:
        cat["alias_index"].setdefault(normalize(alias), []).append(menu_id)
    menu_service._CACHE[(F.BRAND, F.MENU_VERSION)] = cat
    try:
        assert menu_service.match(F.BRAND, F.MENU_VERSION, "카라멜 마키아또").get("menu_id") is None
        assert menu_service.by_name(F.BRAND, F.MENU_VERSION, "카라멜 마키아또")["menu_id"] == "sb_020"
        assert menu_service.by_name(F.BRAND, F.MENU_VERSION, "아이스 카라멜 마키아또")["menu_id"] == "sb_021"
    finally:
        menu_service._CACHE.pop((F.BRAND, F.MENU_VERSION), None)


# --- 3번 권유 문장을 가리킨 참조 ------------------------------------------------------------------
def test_03_reference_to_a_suggestion_is_confirmed(monkeypatch, store):
    """"그러면 아이스 바닐라라떼 어떠세요" → "좋아요 그걸로 주세요" 는 묻지 않고 확정한다."""
    run_rules(monkeypatch, store, [
        "[오세훈 차장] 저는 뭐 마시지",
        "[김민수 대리] 그러면 아이스 바닐라라떼 어떠세요",
        "[오세훈 차장] 좋아요 그걸로 주세요",
    ])
    rows = {r["person"]: r for r in store.live()}
    assert set(rows) == {"오세훈 차장"}, rows          # 권유한 사람의 잔은 생기지 않는다
    assert rows["오세훈 차장"]["menu_name"] == "아이스 바닐라라떼"
    assert rows["오세훈 차장"]["validation_status"] == "passed"


def test_03_mentions_counts_distinct_menus():
    F._install_catalog = None
    menu_service._CACHE[(F.BRAND, F.MENU_VERSION)] = F.catalog()
    try:
        found = menu_service.mentions(F.BRAND, F.MENU_VERSION, "저는 아아 톨 하나랑 카페라떼 하나 주세요")
        assert len(found) == 2
        assert len(menu_service.mentions(F.BRAND, F.MENU_VERSION, "그러면 아이스 바닐라라떼 어떠세요")) == 1
        assert menu_service.mentions(F.BRAND, F.MENU_VERSION, "오늘 날씨 좋네요") == []
    finally:
        menu_service._CACHE.pop((F.BRAND, F.MENU_VERSION), None)


# --- 4번 한 사람이 여러 잔 — 수정과 추가를 가른다 -------------------------------------------------
def test_04_same_person_can_order_two_different_drinks(monkeypatch, store):
    run_rules(monkeypatch, store, [
        "[김민수 대리] 저는 아이스 아메리카노 톨 하나요",
        "[김민수 대리] 그리고 카페 라떼도 하나 주세요",
    ])
    menus = sorted(r["menu_name"] for r in store.live())
    assert menus == ["아이스 아메리카노", "카페 라떼"], menus


def test_04_one_more_cup_is_an_add_not_an_update(monkeypatch, store):
    """"하나 더"는 추가다 — 앞 주문을 되짚는 말(아까 · 방금)이 없으면 변경으로 보지 않는다."""
    run_rules(monkeypatch, store, [
        "[김민수 대리] 저는 아이스 아메리카노 톨 하나요",
        "[김민수 대리] 아이스 아메리카노 하나 더 주세요",
    ])
    assert len(store.live()) == 2, [(r["person"], r["menu_name"]) for r in store.live()]


def test_04_three_cups_in_one_sentence_keep_the_quantity(monkeypatch, store):
    run_rules(monkeypatch, store, ["[김민수 대리] 아이스 아메리카노 톨 3잔 주세요"])
    row = only_row(store)
    assert row["quantity"] == 3 and row["menu_name"] == "아이스 아메리카노"


# --- 5번 맞장구 -----------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["넵", "넵넵", "ㅇㅇ", "좋아요", "오케이", "넹~"])
def test_05_short_acknowledgements_never_become_items(monkeypatch, text):
    store = rows_after_rules(monkeypatch, ["[이지연 사원] 저는 카페 라떼 하나 주세요",
                                           f"[김민수 대리] {text}"])
    assert [r["person"] for r in store.live()] == ["이지연 사원"], \
        [(r["person"], r["menu_text"]) for r in store.live()]


def test_05_ack_with_a_reference_is_still_an_order(monkeypatch, store):
    """"저도요"는 맞장구가 아니라 참조 주문이다 — 군말 규칙이 이것까지 지우지 않는다."""
    run_rules(monkeypatch, store, ["[이지연 사원] 저는 카페 라떼 하나 주세요",
                                   "[김민수 대리] 저도요"])
    assert sorted(r["person"] for r in store.live()) == ["김민수 대리", "이지연 사원"]


# --- 6번 기본 사이즈 --------------------------------------------------------------------------------
def test_06_plain_order_gets_the_default_size_without_asking(monkeypatch, store):
    run_rules(monkeypatch, store, ["[박서연 과장] 저는 따뜻한 카페라떼 하나 부탁드려요~"])
    row = only_row(store)
    assert row["options"].get("size") == "tall"
    assert row["validation_status"] == "passed"
    assert store.questions == []


# --- 7번 채팅으로 빼기 — "제외"는 취소이고, "휘핑 제외"는 요청이다 ---------------------------------
@pytest.mark.parametrize("said, route", [
    ("박서연 과장님은 제외", "cancel"),
    ("박서연 과장 빼줘", "cancel"),
    ("주문 지워줘", "cancel"),
    ("박서연 과장 삭제", "cancel"),
    ("카페라떼 휘핑 제외해주세요", "add"),          # 메뉴 사양 요청이지 취소가 아니다
    ("아이스 라떼 시럽 제외로 주세요", "add"),
])
def test_07_exclusion_words_are_cancels_but_option_requests_are_not(said, route):
    assert intake.classify(said) == route, (said, intake.order_text(said))


def test_07_chat_route_falls_back_to_rules_when_the_llm_says_chatter():
    """담당자 채팅은 이 시스템에 하는 지시다 — LLM 이 잡담으로 봐도 규칙이 변경 · 취소로 읽으면 따른다."""
    from app.graph import nodes

    events = [{"message_id": "chat", "route": "ignore", "event_type": "ignore"}]
    assert nodes._chat_route("박서연 과장님은 제외", events) == "cancel"
    assert [e["route"] for e in events] == ["cancel"]

    events = [{"message_id": "chat", "route": "ignore", "event_type": "ignore"}]
    assert nodes._chat_route("오늘 날씨 좋네요", events) == "ignore"
    assert [e["route"] for e in events] == ["ignore"]


def test_07_chat_creates_a_new_order_only_when_asked_to_add():
    """2026-11-05 4차 보고 1번으로 규칙이 바뀌었다 — 채팅으로 주문을 **넣을 수 있다.**

    종전 규칙은 "채팅으로 주문을 새로 만들지 않는다"였다. 단톡방에 늦게 온 주문을 넣을 길이
    없어 담당자가 막혔다. 다만 `intake.classify` 는 **분류되지 않는 말을 add 로** 돌려주므로
    (잡담 포함) 규칙 분기는 "넣어 달라"고 적은 문장만 add 로 본다 — 그 울타리가 없으면
    "오늘 날씨 좋네요"가 주문이 된다. 넣을 대상 · 메뉴 판정은 `edits.add_from_chat` 이 한다.
    """
    from app.graph import nodes

    events = [{"message_id": "chat", "route": "ignore", "event_type": "ignore"}]
    assert nodes._chat_route("박서연 아이스 아메리카노 한 잔 추가해줘", events) == "add"
    assert [e["route"] for e in events] == ["add"]

    # 넣으라고 적지 않은 말은 그대로 둔다
    for chatter in ("아이스 아메리카노 하나 주세요", "오늘 날씨 좋네요", "다들 뭐 먹을래?"):
        events = [{"message_id": "chat", "route": "ignore", "event_type": "ignore"}]
        assert nodes._chat_route(chatter, events) == "ignore", chatter
