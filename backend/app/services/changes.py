"""변경 이력과 되돌리기 — 직전 1회만 보장한다 (D-07 D-4 · P-02 F-006).

되돌리기 유효성은 "최신 행인가"로 판정한다. 중간 행을 되돌리면 그 뒤의 변경이
어긋난다 — 그래서 토큰 일치만으로 허용하지 않는다.
"""

import json

from app import db, ids

# O-6 방법 6종 (P-06) — 화면 문구를 서버가 정한다 (chat_add 는 4차 보고 1번)
METHOD_LABEL = {
    "chat_cancel": "대화에서 취소",
    "question_answer": "직접 선택",
    "chat_edit": "채팅으로 수정",
    "chat_add": "채팅으로 추가",
    "reply_applied": "답장 반영",
    "auto_recovery": "자동 복구",
}


def record(cur, session_id: str, *, method: str, item_id: str | None = None,
           who: str | None = None, before_text: str | None = None,
           after_text: str | None = None, before_json: dict | None = None,
           after_json: dict | None = None, suggestion_id: str | None = None,
           incident_id: str | None = None, issue_undo: bool = True) -> str | None:
    """변경 1건을 남기고 되돌리기 토큰을 발급한다.

    새 토큰을 발급하면 **그 세션의 이전 토큰을 모두 지운다** — 직전 1회만 유효하다는
    규칙을 DB 상태로 강제한다. 부분 UNIQUE 인덱스(uq_undo_token)가 중복을 막는다.
    """
    token = ids.undo_token() if issue_undo else None
    if token:
        cur.execute(
            "UPDATE app.change_log SET undo_token = NULL "
            " WHERE session_id = %s AND undo_token IS NOT NULL",
            (session_id,),
        )
    cur.execute(
        "INSERT INTO app.change_log "
        "  (change_id, session_id, item_id, who, method, method_label, before_text, after_text, "
        "   before_json, after_json, suggestion_id, incident_id, undo_token) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (ids.change_id(), session_id, item_id, who, method,
         METHOD_LABEL.get(method, method), before_text, after_text,
         json.dumps(before_json, ensure_ascii=False) if before_json else None,
         json.dumps(after_json, ensure_ascii=False) if after_json else None,
         suggestion_id, incident_id, token),
    )
    return token


def latest(session_id: str) -> dict | None:
    return db.fetch_one(
        "SELECT change_id, item_id, undo_token, undone_at, before_json, after_json, "
        "       before_text, after_text, method "
        "  FROM app.change_log WHERE session_id = %s "
        " ORDER BY created_at DESC, change_id DESC LIMIT 1",
        (session_id,),
    )


def find_undoable(session_id: str, undo_token: str) -> dict | None:
    """되돌릴 수 있는 행. 조건 3개를 모두 만족해야 한다.

    토큰 일치 · 아직 되돌리지 않음 · **그 세션의 가장 최근 변경**.
    하나라도 어긋나면 None 이고, 호출자가 422 UNDO_UNAVAILABLE 을 돌려준다.
    """
    row = latest(session_id)
    if not row:
        return None
    if row["undo_token"] != undo_token or row["undone_at"] is not None:
        return None
    return row


def mark_undone(cur, change_id: str) -> None:
    cur.execute(
        "UPDATE app.change_log SET undone_at = NOW(), undo_token = NULL WHERE change_id = %s",
        (change_id,),
    )


def history(session_id: str) -> list[dict]:
    """O-6 상세 — 시각 · 누구 · 전 → 후 · 방법 (P-06 케이스 23)."""
    rows = db.fetch_all(
        "SELECT created_at, who, before_text, after_text, method_label, incident_id "
        "  FROM app.change_log WHERE session_id = %s ORDER BY created_at",
        (session_id,),
    )
    out = []
    for row in rows:
        before, after = row["before_text"], row["after_text"]
        if before and after:
            before_after = f"{before} → {after}"
        else:
            before_after = after or before or "-"
        out.append({
            "at_text": row["created_at"].strftime("%H:%M"),
            "who": row["who"] or "-",
            "before_after": before_after,
            "method_text": row["method_label"],
            "incident_id": row["incident_id"],
        })
    return out


def counts(session_id: str) -> dict:
    row = db.fetch_one(
        "SELECT COUNT(*)                                            AS change_count, "
        "       COUNT(*) FILTER (WHERE method = 'auto_recovery')     AS recovery_count "
        "  FROM app.change_log WHERE session_id = %s",
        (session_id,),
    )
    return {"change_count": int(row["change_count"]), "recovery_count": int(row["recovery_count"])}
