"""2026-11-07 전 구간 테스트에서 드러난 결함 — **지시 대명사가 조용히 틀린 메뉴로 확정됐다.**

실측(ORD-0038 · 실제 LLM · 스타벅스 기준):

    [박서연] 아아 톨 한 잔이요.            m3
    [최유나] 여기 인기 있는 메뉴 뭐야?       m4  ignore
    [정하준] 민트 초코가 유명하대            m5  ignore   <- "그걸로"가 가리킨 것
    [최유나] 그럼 난 그걸로 하나~            m6  add

    결과: 최유나 = 아이스 아메리카노(sb_004) · validation_status=passed · 확인 필요 없음

무엇이 일어났나: `_referenced_menus` 가 뒤로 훑다가 m5 의 "민트 초코"를 매칭하지 못하자
**그냥 지나쳐** 세 메시지 앞 m3(박서연의 아아)을 집었다. 그리고 그 메시지는 메뉴를 하나만
말하므로 `_solid_reference` 가 참이 되어 **자동 확정**됐다.

맞는 결론: 민트 초코는 스타벅스 기준에 없다(일반 카페 메뉴 gn_013). 그러면 미등록이고,
미등록은 AI 가 임의 확정하지 않고 담당자에게 넘긴다(도메인 용어 "확인 필요").

왜 이게 위험한가: 화면에 오류가 없다. 담당자는 안 시킨 커피를 받고, 잔 수와 금액이 맞아
SC-5 "중복 · 유실 0건"도 통과한 것처럼 보인다. 기존 단위시험은 LLM 을 모킹해
`menu_text="민트 초코"` 를 직접 넣으므로(test_agents.py) 이 경로를 밟지 않는다.
"""
import os

import pytest

from app import config
from app.services import menu as menu_service
from app.services import sessions

pytestmark = pytest.mark.skipif(os.getenv("MOA_LIVE_DB") != "1",
                                reason="메뉴 사전을 실제로 읽어야 한다(MOA_LIVE_DB=1)")

MV = config.MENU_VERSION
SB = "starbucks"

CHAT = [
    {"message_id": "m1", "person": "김민수", "_raw_text": "아이스 아메리카노 톨 2잔, 하나는 샷 추가요."},
    {"message_id": "m2", "person": "이지연", "_raw_text": "바닐라라떼 큰 거 얼음 적게요."},
    {"message_id": "m3", "person": "박서연", "_raw_text": "아아 톨 한 잔이요."},
    {"message_id": "m4", "person": "최유나", "_raw_text": "여기 인기 있는 메뉴 뭐야?"},
    {"message_id": "m5", "person": "정하준", "_raw_text": "민트 초코가 유명하대"},
    {"message_id": "m6", "person": "최유나", "_raw_text": "그럼 난 그걸로 하나~"},
    {"message_id": "m7", "person": "김민수", "_raw_text": "아까 샷 추가한 한 잔 취소할게요."},
    {"message_id": "m8", "person": "정하준", "_raw_text": "나도 민수랑 같은 걸로!"},
]


# =============================================================================
# 1. 결함 자체 — 이것이 실측에서 틀렸던 한 줄이다
# =============================================================================
def test_a_reference_to_an_unavailable_menu_resolves_to_nothing():
    """가리킨 것이 지금 못 내는 메뉴면 **아무것도 집지 않는다.**

    종전에는 `[아이스 아메리카노 @ m3]` 였다 — 세 메시지를 건너뛴 남의 주문이다.
    빈 목록이어야 미등록으로 가고, 미등록이 담당자 확인으로 이어진다.
    """
    got = sessions._referenced_menus(CHAT, 5, brand_id=SB, menu_version=MV)
    assert got == [], [(g.get("name"), g.get("source_message_id")) for g in got]


def test_it_never_reaches_past_the_thing_that_was_pointed_at():
    """건너뛰어 집은 그 메뉴(m3 아이스 아메리카노)가 결과에 없다는 것을 못 박는다."""
    got = sessions._referenced_menus(CHAT, 5, brand_id=SB, menu_version=MV)
    assert "sb_004" not in [g.get("menu_id") for g in got]


# =============================================================================
# 2. 고치면서 깨면 안 되는 것 — 참조 주문은 계속 동작해야 한다
# =============================================================================
def test_a_named_reference_still_works():
    """"나도 민수랑 같은 걸로" — 이름을 가리키면 그 사람 주문을 집는다."""
    got = sessions._referenced_menus(CHAT, 7, brand_id=SB, menu_version=MV)
    assert [g["menu_id"] for g in got] == ["sb_004"], got
    assert got[0]["source_message_id"] == "m1"


def test_a_plain_reference_still_works():
    """"저도 같은 걸로요" — 바로 앞 주문 하나뿐이면 그것이 답이다(2026-11-05 보고 3 · 5번)."""
    simple = [
        {"message_id": "n1", "person": "김민수", "_raw_text": "아아 톨 하나요"},
        {"message_id": "n2", "person": "이지연", "_raw_text": "저도 같은 걸로요"},
    ]
    got = sessions._referenced_menus(simple, 1, brand_id=SB, menu_version=MV)
    assert [g["menu_id"] for g in got] == ["sb_004"], got


def test_chatter_that_names_nothing_is_still_walked_past():
    """메뉴를 말하지 않은 잡담은 여전히 지나친다 — 멈추는 조건이 넓으면 참조가 다 막힌다."""
    chat = [
        {"message_id": "k1", "person": "김민수", "_raw_text": "아아 톨 하나요"},
        {"message_id": "k2", "person": "이지연", "_raw_text": "오늘 날씨 좋네"},
        {"message_id": "k3", "person": "박서연", "_raw_text": "그래 ㅋㅋ"},
        {"message_id": "k4", "person": "최유나", "_raw_text": "저도 같은 걸로요"},
    ]
    got = sessions._referenced_menus(chat, 3, brand_id=SB, menu_version=MV)
    assert [g["menu_id"] for g in got] == ["sb_004"], got


# =============================================================================
# 3. 판정 도구 자체 — unavailable_mentions (G-A0 양방향)
# =============================================================================
def test_an_other_brand_menu_is_reported_as_unavailable():
    """(b) 알려진 실패 케이스 — 일반 카페 메뉴를 스타벅스 기준에서 집어낸다."""
    hits = menu_service.unavailable_mentions(SB, MV, "민트 초코가 유명하대")
    assert [h["text"] for h in hits] == ["민트초코"], hits
    assert hits[0]["brand_id"] == "generic"


@pytest.mark.parametrize("text", [
    "아아 톨 한 잔이요",                    # 지금 낼 수 있는 메뉴
    "아이스 아메리카노 톨 2잔, 하나는 샷 추가요.",
    "바닐라라떼 큰 거 얼음 적게요",
    "여기 인기 있는 메뉴 뭐야?",             # 메뉴 이름이 없는 질문
    "그럼 난 그걸로 하나~",                 # 지시 대명사만
    "오늘 날씨 좋네",
    "나도 민수랑 같은 걸로!",
])
def test_available_or_menuless_text_is_not_flagged(text):
    """(a) 정상 케이스 — 낼 수 있는 메뉴와 메뉴 없는 문장은 걸리지 않는다.

    이쪽이 틀리면 멀쩡한 참조 주문이 전부 확인 필요가 되어 담당자 일이 늘어난다.
    """
    assert menu_service.unavailable_mentions(SB, MV, text) == []


def test_the_same_name_is_available_on_its_own_brand():
    """일반 카페 기준에서는 민트 초코가 낼 수 있는 메뉴다 — 매장 기준에 따라 답이 갈린다."""
    assert menu_service.unavailable_mentions("generic", MV, "민트 초코가 유명하대") == []


def test_short_aliases_do_not_trip_the_check():
    """2글자 별칭(아아 · 라떼 · 모카)으로는 막지 않는다 — 잡담에 우연히 들어갈 수 있다."""
    index = menu_service._all_menu_names()
    assert all(len(k) >= menu_service._MIN_UNAVAILABLE_LEN for k in index), \
        [k for k in index if len(k) < menu_service._MIN_UNAVAILABLE_LEN]
