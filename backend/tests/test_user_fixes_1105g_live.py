# 2026-11-06 5차 사용자 보고 (실제 DB · MOA_LIVE_DB=1)
#
#   MOA_LIVE_DB=1 ../.venv/Scripts/python.exe -m pytest -q tests/test_user_fixes_1105g_live.py
#
# 1 확인 질문을 **버튼이 아니라 채팅으로** 답했더니 반영은 됐는데 다음 단계로 넘어가지 못했다
# 2 주문 정리에서 사이즈를 그란데로 골라 반영됐는데 **주문서 화면에서는 톨**로 보였다
import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.getenv("MOA_LIVE_DB") != "1",
                                reason="MOA_LIVE_DB=1 일 때만 실제 DB 로 돈다")

BASE = [
    "[김민수] 아이스 아메리카노 톨 한 잔이요",
    "[이지연] 아이스 카페 라떼 톨 한 잔이요",
]


@pytest.fixture
def live():
    from app import db
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    made = []
    yield client, made
    for session_id in made:
        db.execute("DELETE FROM app.order_session WHERE session_id = %s", (session_id,))


def _ok(res, status=200):
    body = res.json()
    assert res.status_code == status, body
    assert "error" not in body, body
    return body["data"]


def _start(client, made, lines):
    raw = "\n".join(lines + [f"live-1105g {uuid.uuid4().hex}", "스타벅스로 갈 예정이야"])
    data = _ok(client.post("/api/sessions", json={"raw_text": raw, "source": "paste"}), 202)
    made.append(data["session_id"])
    return data["session_id"], {"X-Session-Token": data["session_token"]}


def _version(client, sid, token):
    return _ok(client.get(f"/api/sessions/{sid}", headers=token))["version"]


def _say(client, sid, token, text):
    res = client.post(f"/api/sessions/{sid}/messages", json={"text": text},
                      headers={**token, "If-Match": str(_version(client, sid, token))})
    return res, res.json().get("data") or res.json().get("error") or {}


def _answer_by_text(client, sid, token, question_id, text):
    """화면이 채팅으로 답할 때 쓰는 경로 — API-021 (text)."""
    res = client.post(f"/api/sessions/{sid}/questions/{question_id}/answer",
                      json={"text": text},
                      headers={**token, "If-Match": str(_version(client, sid, token))})
    return res, res.json().get("data") or res.json().get("error") or {}


def _open_question(client, sid, token):
    return _ok(client.get(f"/api/sessions/{sid}/questions", headers=token))


# 측정 도구 자기 검증 (G-A0) — 아래 두 함수는 **서로 다른 화면의 서로 다른 필드**를 읽는다.
#   주문 정리: rows[].options  = [{"label": "사이즈", "value": "그란데"}]       (칩 — 고른 값만)
#   주문서   : groups[].options = 같은 모양 · groups[].option_choices[].current  (고치기 칩)
# 처음 쓴 판정기는 주문서에서 `option_choices[].selected` 를 찾아 **없는 키라 늘 빈손**이었고,
# 멀쩡한 서버를 결함으로 보고했다. 그래서 두 함수 모두 정상 케이스(톨)로 한 번 재어 본다.
def _size_of(client, sid, token, person):
    """주문 정리 화면이 보는 값 (API-015 세션 투영)."""
    rows = _ok(client.get(f"/api/sessions/{sid}", headers=token))["rows"]
    return [[o["value"] for o in (r.get("options") or []) if o.get("label") == "사이즈"]
            for r in rows if r["person"] == person]


def _sheet_size_of(client, sid, token, person):
    """주문서 화면이 보는 값 (API-030) — 주문 정리와 달라서는 안 된다.

    `group_html` 이 그리는 `options` 와 고치기 칩이 쓰는 `option_choices[].current` 를 **함께** 본다.
    둘 중 하나만 맞으면 화면 한쪽에서만 틀린 사이즈가 보인다.
    """
    sheet = _ok(client.get(f"/api/sessions/{sid}/order-sheet", headers=token))
    found = []
    for group in sheet.get("groups") or []:
        if person not in (group.get("people") or []):
            continue
        chips = [o["value"] for o in (group.get("options") or []) if o.get("label") == "사이즈"]
        current = [o["current"] for o in (group.get("option_choices") or [])
                   if o.get("label") == "사이즈"]
        found.append(sorted(set(chips + current)))
    return found


def test_the_size_readers_agree_on_an_untouched_order(live):
    """판정기 자기 검증 — 손대지 않은 주문에서 두 화면이 같은 값(톨)을 보여야 한다 (G-A0)."""
    client, made = live
    sid, token = _start(client, made, BASE)
    assert _size_of(client, sid, token, "김민수") == [["톨"]]
    assert _sheet_size_of(client, sid, token, "김민수") == [["톨"]]


# =============================================================================
# 2번 주문 정리와 주문서가 같은 사이즈를 보여야 한다
# =============================================================================
def test_size_answered_by_button_shows_the_same_in_the_order_sheet(live):
    client, made = live
    sid, token = _start(client, made, BASE)
    _, data = _say(client, sid, token, "박서연 아이스 아메리카노 큰 거 한 잔 추가해줘")
    assert data.get("kind") == "applied", data

    queue = _open_question(client, sid, token)
    question = queue["question"]
    assert question and "박서연" in question["text"], queue
    grande = next(o for o in question["options"] if o["label"] == "그란데")
    res = client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                      json={"answer": grande["value"]},
                      headers={**token, "If-Match": str(_version(client, sid, token))})
    assert res.status_code == 200, res.json()

    assert _size_of(client, sid, token, "박서연") == [["그란데"]], _size_of(client, sid, token, "박서연")
    assert _sheet_size_of(client, sid, token, "박서연") == [["그란데"]], \
        _sheet_size_of(client, sid, token, "박서연")


# =============================================================================
# 1번 채팅으로 답해도 질문이 닫히고 다음 단계로 넘어갈 수 있어야 한다
# =============================================================================
def test_answering_by_chat_closes_the_question_and_unblocks_the_next_step(live):
    client, made = live
    sid, token = _start(client, made, BASE)
    _, data = _say(client, sid, token, "박서연 아이스 아메리카노 큰 거 한 잔 추가해줘")
    assert data.get("kind") == "applied", data

    question = _open_question(client, sid, token)["question"]
    assert question, "질문이 있어야 이 시험이 뜻을 가진다"
    res, answered = _answer_by_text(client, sid, token, question["question_id"], "그란데로 해줘")
    assert res.status_code == 200, answered

    # 큐가 비고, 확인 필요 건수도 0 이어야 한다 — 둘 중 하나만 0 이면 [주문서 보기]가 막힌다
    queue = _open_question(client, sid, token)
    progress = _ok(client.get(f"/api/sessions/{sid}/progress", headers=token))
    assert queue["question"] is None, queue
    assert queue["remaining"] == 0, queue
    assert progress["review_count"] == 0, progress
    assert int(progress.get("pending_suggestions") or 0) == 0, progress
    assert progress["status"] == "awaiting_confirmation", progress

    # 반영 결과도 주문서까지 같은 값이어야 한다
    assert _size_of(client, sid, token, "박서연") == [["그란데"]], _size_of(client, sid, token, "박서연")
    assert _sheet_size_of(client, sid, token, "박서연") == [["그란데"]], \
        _sheet_size_of(client, sid, token, "박서연")
    assert _ok(client.get(f"/api/sessions/{sid}/order-sheet", headers=token))["can_share"], "공유가 막혔다"


def test_group_order_is_stable_across_reloads(live):
    """같은 메뉴 · 같은 잔 수로 비겼을 때 묶음 순서가 흔들리면 안 된다.

    `group_id` 는 이 순서의 일련번호이고, 이름 칩 편집이 (group_id, 순번)으로 사람을 가리킨다 —
    순서가 뒤집히면 **다른 사람 주문을 고치게 된다.**
    """
    client, made = live
    sid, token = _start(client, made, BASE)
    _say(client, sid, token, "박서연 아이스 아메리카노 큰 거 한 잔 추가해줘")
    question = _open_question(client, sid, token)["question"]
    grande = next(o for o in question["options"] if o["label"] == "그란데")
    client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                json={"answer": grande["value"]},
                headers={**token, "If-Match": str(_version(client, sid, token))})

    seen = set()
    for _ in range(5):
        sheet = _ok(client.get(f"/api/sessions/{sid}/order-sheet", headers=token))
        seen.add(tuple((g["group_id"], tuple(g["people"])) for g in sheet["groups"]))
    assert len(seen) == 1, seen
