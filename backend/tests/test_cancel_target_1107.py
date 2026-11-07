"""취소가 남의 주문을 가리키면 조용히 사라지던 결함 (2026-11-07 사용자 ORD-0034).

실측 payload:
    route=cancel · person=김민수 · options={"shot":"extra"} · target_unclear=false
    target_message_id="ORD-0034-m3"   <- m3 은 **박서연**의 주문이다

해석 서비스가 대상을 잘못 가리켰다. 단서(샷 추가)는 정확히 들고 있었는데도
취소가 반영되지 않고 확인 질문도 뜨지 않았다 — 담당자는 취소했다고 믿고 주문서에는 남는다
(금액 · 잔 수가 틀리고 SC-5 "중복 · 유실 0건"을 깬다).
"""
from app.graph import nodes


def _row(person, message_id, options, item_id):
    return {"person": person, "_message_id": message_id, "options": options,
            "menu_id": "sb_004", "menu_text": "아이스 아메리카노", "menu_name": "아이스 아메리카노",
            "item_id": item_id, "quantity": 1}


EARLIER = [
    _row("김민수", "ORD-0034-m1", {"size": "tall"}, "it_01"),
    _row("김민수", "ORD-0034-m1", {"size": "tall", "shot": "extra"}, "it_02"),
    _row("박서연", "ORD-0034-m3", {"size": "tall", "cup": "personal"}, "it_04"),
]

CANCEL = {"route": "cancel", "person": "김민수", "target_unclear": False,
          "options": {"shot": "extra"}, "target_hint": "아까 한 취소",
          "_raw_text": "아까 샷 추가한 한 잔 취소할게요.",
          "target_message_id": "ORD-0034-m3"}


def test_a_cancel_never_targets_another_persons_order():
    """가리킨 메시지가 남의 주문이면 그 메시지를 쓰지 않는다 — 남의 잔을 취소하면 더 나쁘다."""
    hit = nodes._candidates(EARLIER, CANCEL)
    assert all(r["person"] == "김민수" for r in hit), [r["person"] for r in hit]


def test_the_option_clue_picks_the_shot_added_cup():
    """사람 기준으로 돌아오면 단서(샷 추가)가 정확히 한 잔을 고른다."""
    hit = nodes._narrow_by_options(nodes._candidates(EARLIER, CANCEL), CANCEL)
    assert [r["item_id"] for r in hit] == ["it_02"]


def test_a_cancel_is_never_lost_without_a_question():
    """후보가 없으면 **조용히 버리지 않는다** — 취소는 담당자가 분명히 말한 뜻이다."""
    orphan = {**CANCEL, "person": "없는사람", "options": {}, "target_message_id": "ORD-0034-m9"}
    assert nodes._candidates(EARLIER, orphan) == []
    # 후보 0건을 어떻게 다룰지는 target_node 의 책임이다 — 여기서는 "비어서 돌아온다"만 고정한다
