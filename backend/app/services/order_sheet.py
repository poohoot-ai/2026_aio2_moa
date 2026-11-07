"""주문서 — 묶음은 저장하지 않고 질의 시 집계한다 (D-07 D-1 · v_order_group).

공유 가능 여부를 **서버가 판정한다.** 화면이 조건을 따지면 조건이 두 곳에 생기고,
한쪽만 고쳤을 때 조용히 어긋난다 (P-06 프론트엔드 원칙).
"""

from app import db
from app.services import brand as brand_service
from app.services import projection, settlement

TOTALS_NOTICE = "참고용 금액이에요. 실제 결제 금액과 다를 수 있어요"

STATUS_LINE_SHARE = ("info", "단톡방에 공유해 확인받고 매장에는 이 주문서를 보여 주세요")
STATUS_LINE_SHARED = ("success", "주문서를 공유했어요! 매장에 보여 주며 주문해 주세요")


def groups(session_id: str, session: dict) -> list[dict]:
    """v_order_group 을 읽어 묶음 목록을 만든다.

    묶는 기준은 "메뉴 + 사양 + 옵션 + 기타 요청이 모두 같을 때"다 (D-02 API-030).
    뷰가 그 기준으로 GROUP BY 하고 있으므로 여기서 다시 묶지 않는다.
    """
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    rows = db.fetch_all(
        "SELECT g.menu_id, g.menu_text, g.options, g.custom_request, g.quantity, "
        "       g.estimated_amount, g.people, g.item_ids, g.has_undecided, m.name AS menu_name "
        "  FROM app.v_order_group g "
        "  LEFT JOIN app.menu m ON m.menu_id = g.menu_id "
        " WHERE g.session_id = %s "
        # 동점(같은 메뉴 이름 · 같은 잔 수)에서 순서가 흔들리지 않게 항목 번호로 마지막 비김을 푼다.
        # `group_id` 는 이 순서의 일련번호이고 화면의 이름 칩 편집이 (group_id, 순번)으로 사람을
        # 가리킨다 — 순서가 뒤집히면 **다른 사람 주문을 고치게 된다** (2026-11-06)
        " ORDER BY g.quantity DESC, m.name NULLS LAST, g.item_ids[1]",
        (session_id,),
    )
    out = []
    for index, row in enumerate(rows, start=1):
        out.append({
            "group_id": f"g{index}",
            "menu_text": row["menu_name"] or row["menu_text"],
            "options": projection.options_block(brand_id, menu_version, row["options"], {}),
            # 이름 칩 편집(케이스 19) — 화면이 이 칸을 읽는데 비어 있어 기타 요청 칸만 떴다(2026-11-04)
            "option_choices": projection.option_choices(brand_id, menu_version,
                                                        {"menu_id": row["menu_id"], "options": row["options"]}),
            "custom_request": row["custom_request"],
            "quantity": int(row["quantity"] or 0),
            "estimated_amount": None if row["has_undecided"] else row["estimated_amount"],
            "people": list(row["people"] or []),
            "item_ids": list(row["item_ids"] or []),
        })
    return out


def can_share(session_id: str) -> dict:
    """공유 가능 조건 — 셋 다 통과해야 한다 (FR-009).

      1 확인 필요 항목이 0건
      2 반영할지 정하지 않은 제안(pending)이 0건
      3 정산 정합성 통과 (v_settlement_check)
    """
    counts = db.fetch_one(
        "SELECT (SELECT COUNT(*) FROM app.order_item "
        "         WHERE session_id = %s AND removed = false "
        "           AND validation_status <> 'passed')                       AS review_count, "
        "       (SELECT COUNT(*) FROM app.suggestion "
        # 확인 질문으로 올린 제안(question_id 있음)은 질문 큐가 맡는다 — 여기서 또 세면 질문을 다 답해도
        # "반영할지 정하지 않은 제안이 N건 있어요"가 남아 주문서가 영영 막힌다(2026-11-05 3차 보고 7번)
        "         WHERE session_id = %s AND status = 'pending' "
        "           AND question_id IS NULL)                                 AS pending_count",
        (session_id, session_id),
    )
    review_count = int(counts["review_count"])
    pending_count = int(counts["pending_count"])
    check = settlement.integrity(session_id)

    if review_count:
        return {"can_share": False, "review_count": review_count,
                "blocked_reason_text": f"확인이 필요한 주문이 {review_count}건 있어요",
                "integrity": check}
    if pending_count:
        return {"can_share": False, "review_count": 0,
                "blocked_reason_text": f"반영할지 정하지 않은 제안이 {pending_count}건 있어요",
                "integrity": check}
    if not check["integrity_ok"]:
        return {"can_share": False, "review_count": 0,
                "blocked_reason_text": check["reason"], "integrity": check}
    return {"can_share": True, "review_count": 0, "blocked_reason_text": None,
            "integrity": check}


def view(session: dict) -> dict:
    """API-030 — 산출물 조회."""
    session_id = session["session_id"]
    group_rows = groups(session_id, session)
    gate = can_share(session_id)

    cups = sum(g["quantity"] for g in group_rows)
    people = settlement.count_people(p for g in group_rows for p in g["people"])
    has_undecided = any(g["estimated_amount"] is None for g in group_rows)
    total = None if has_undecided else sum(g["estimated_amount"] for g in group_rows)

    tone, text = (STATUS_LINE_SHARED if session["status"] == "completed"
                  else STATUS_LINE_SHARE)
    return {
        "session_id": session_id,
        "status": session["status"],
        "version": session["version"],
        "brand": brand_service.projection(session["brand_id"], session["brand_source"],
                                          session.get("brand_evidence_text")),
        "summary": {"people": people, "cups": cups},
        "groups": group_rows,
        "totals": {
            "cups": cups,
            "estimated_total": total,
            "notice": TOTALS_NOTICE,
            "integrity_ok": gate["integrity"]["integrity_ok"],
        },
        "status_line": {"tone": tone, "text": text},
        "can_share": gate["can_share"],
        "blocked_reason_text": gate["blocked_reason_text"],
        "ai_notice": projection.AI_NOTICE_SHEET,
    }
