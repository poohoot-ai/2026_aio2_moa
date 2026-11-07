"""공유 완료 기록 — API-034. **공유가 완료다** (P-03 4.2 v1.6).

확정 단계가 따로 없다. 담당자가 텍스트를 복사하거나 이미지를 저장한 순간
세션이 COMPLETED 가 되고, 그 시점 버전이 고정된다.

여러 번 와도 안전하다 (D-02 1.2) — 복사 · 저장할 때마다 완료 시각이 덧붙고
`share_count` 가 올라간다. 행이 여러 개 생기는 것이 정상이다.
"""

from app import db, ids, logging_setup
from app.services import order_sheet, settlement, share

METHODS = {"text": "copy_text", "image": "save_image",
           "copy_text": "copy_text", "save_image": "save_image"}


class NotShareable(Exception):
    """공유 가능 조건을 통과하지 못했다 — 호출자가 422 를 돌려준다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def complete(session: dict, method: str) -> dict:
    """공유 실행 기록. **조건을 다시 확인한다.**

    화면이 버튼을 막고 있더라도 서버가 다시 본다 — 화면 상태가 낡았거나
    다른 창에서 주문이 바뀌었을 수 있고, 그때 완료로 굳으면 틀린 주문서가 확정된다.
    """
    session_id = session["session_id"]
    stored_method = METHODS.get(method)
    if stored_method is None:
        raise ValueError(method)

    gate = order_sheet.can_share(session_id)
    if not gate["can_share"]:
        raise NotShareable(gate["blocked_reason_text"] or "아직 공유할 수 없어요")

    sheet = order_sheet.view(session)
    card = share.view(sheet)
    content_hash = ids.content_hash(card["content"])

    with db.transaction() as cur:
        cur.execute("SELECT version FROM app.order_session WHERE session_id = %s",
                    (session_id,))
        version = cur.fetchone()["version"]
        cur.execute(
            "INSERT INTO app.share_history "
            "  (share_id, session_id, session_version, method, content_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            (ids.share_id(), session_id, version, stored_method, content_hash))
        cur.execute(
            "UPDATE app.order_session "
            "   SET status = 'completed', completed_at = NOW(), "
            "       share_count = share_count + 1, updated_at = NOW() "
            " WHERE session_id = %s", (session_id,))
        # 공유 시점 계산을 그 버전으로 고정한다 (FR-009)
        cur.execute(
            "SELECT i.*, m.name AS menu_name FROM app.order_item i "
            "  LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
            " WHERE i.session_id = %s AND i.removed = false", (session_id,))
        settlement.save_lines(session_id, version, settlement.compute_lines(cur.fetchall()),
                              cur=cur)

    row = db.fetch_one(
        "SELECT share_count, completed_at FROM app.order_session WHERE session_id = %s",
        (session_id,))
    logging_setup.log("share_completed", session_id=session_id, trace_id=session["trace_id"],
                      method=stored_method, version=version, share_count=row["share_count"],
                      # 공유를 허용한 바로 그 판정(v_settlement_check) — 내부 검증 자료(SC-5)
                      settlement=gate.get("integrity"))

    return {
        "user_message": "복사했어요" if stored_method == "copy_text" else "저장했어요",
        "status": "completed",
        "completed_version": version,
        "share_count": row["share_count"],
        "completed_at_text": row["completed_at"].strftime("%H:%M"),
        "status_line": {"tone": "success",
                        "text": "주문서를 공유했어요! 매장에 보여 주며 주문해 주세요"},
        "same_as_shared": True,
    }


def shared_state(session_id: str) -> dict:
    """"공유한 내용과 같아요" 판정 — 지금 내용 해시와 마지막 공유 해시를 비교한다."""
    last = db.fetch_one(
        "SELECT session_version, method, content_hash, shared_at FROM app.share_history "
        " WHERE session_id = %s ORDER BY shared_at DESC LIMIT 1", (session_id,))
    if not last:
        return {"shared": False, "same_as_shared": None, "shared_at_text": None}

    session = db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s",
                           (session_id,))
    current = share.view(order_sheet.view(session))
    return {
        "shared": True,
        "same_as_shared": ids.content_hash(current["content"]) == last["content_hash"],
        "shared_at_text": last["shared_at"].strftime("%H:%M"),
        "shared_version": last["session_version"],
        "share_method": last["method"],
    }
