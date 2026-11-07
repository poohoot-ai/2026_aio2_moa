# 2026-11-07 전 구간 테스트에서 드러난 결함 — **해결된 항목의 확인 질문이 안 닫힌다** (실제 DB)
#
#   MOA_LIVE_DB=1 ../.venv/Scripts/python.exe -m pytest -q tests/test_stale_question_1107_live.py
#
# 실측(ORD-0038): 이지연의 "바닐라라떼 큰 거"가 사이즈 모호로 확인 필요가 됐고 질문 q1 이 열렸다.
# 그 뒤 단톡방 답장 "나 그란데 말고 벤티로 바꿔줘"를 반영해 사이즈가 벤티로 정해졌는데,
# q1 은 `open` 으로 남아 질문 큐가 **이미 정해진 사이즈를 다시 물었다**.
#
# 왜 가짜 저장소로는 못 잡나: 항목 판정(validation_status)과 질문 테이블이 **다른 테이블**이고
# 어긋남이 둘을 함께 읽을 때만 보인다. 화면에는 오류가 없다 — 질문이 하나 더 뜨는 것뿐이어서,
# 담당자는 자기가 뭘 잘못 눌렀다고 생각한다.
#
# 고친 곳: `edits._revalidate` — 판정이 passed 로 바뀌면 그 항목의 열린 질문을 닫는다.
# 경로마다 닫지 않고 **모든 수정 경로가 지나는 한 곳**에 둔다(G-A4 와 같은 이유).
import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.getenv("MOA_LIVE_DB") != "1",
                                reason="MOA_LIVE_DB=1 일 때만 실제 DB 로 돈다")

# 이지연의 "큰 거"가 사이즈 모호 -> 확인 필요를 만든다
AMBIGUOUS = [
    "[김민수] 아이스 아메리카노 톨 한 잔이요",
    "[이지연] 바닐라라떼 큰 거 얼음 적게요",
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


def _start(client, made):
    """표식 줄로 기존 결과 재사용(FR-011)을 피한다. 요청 한 줄보다 앞에 둔다."""
    raw = "\n".join(AMBIGUOUS + [f"live-1107q {uuid.uuid4().hex}", "스타벅스로 갈 예정이야"])
    data = _ok(client.post("/api/sessions", json={"raw_text": raw, "source": "paste"}), 202)
    made.append(data["session_id"])
    return data["session_id"], {"X-Session-Token": data["session_token"]}


def _rows(client, sid, token):
    session = _ok(client.get(f"/api/sessions/{sid}", headers=token))
    return session["version"], session["rows"]


def _ambiguous_item(client, sid, token):
    """사이즈가 모호해 확인 필요가 된 행. 해석이 흔들려 없으면 그 시험은 건너뛴다."""
    _, rows = _rows(client, sid, token)
    hit = [r for r in rows if r.get("needs_review")]
    if not hit:
        pytest.skip("이번 해석에서는 확인 필요 항목이 생기지 않았다(LLM 비결정)")
    return hit[0]


def _open_questions(sid, item_id):
    from app import db
    return db.fetch_all(
        "SELECT question_id, status FROM app.review_question "
        " WHERE session_id = %s AND item_id = %s AND status = 'open'", (sid, item_id))


def _status_of(sid, item_id):
    from app import db
    return db.fetch_one("SELECT validation_status FROM app.order_item WHERE item_id = %s",
                        (item_id,))["validation_status"]


# =============================================================================
# 1. 답장 반영으로 풀린 항목
# =============================================================================
def test_a_reply_that_resolves_the_item_also_closes_its_question(live):
    """답장으로 사이즈가 정해지면 그 사이즈를 다시 묻지 않는다.

    이것이 실측에서 깨진 경로다 — `suggestions.decide` 의 반영 분기가 항목만 고쳤다.
    """
    client, made = live
    sid, token = _start(client, made)
    item = _ambiguous_item(client, sid, token)

    # 질문 큐를 한 번 읽어 질문이 만들어지게 한다(화면이 하는 일 · reconcile)
    _ok(client.get(f"/api/sessions/{sid}/questions", headers=token))
    assert _open_questions(sid, item["item_id"]), "확인 필요 항목에 질문이 열려 있어야 한다"

    res = client.post(f"/api/sessions/{sid}/replies/analyze",
                      json={"raw_text": "[이지연] 나 벤티로 해줘", "apply": True}, headers=token)
    data = _ok(res)
    if not data.get("applied"):
        pytest.skip("이번 해석에서는 답장이 바로 반영되지 않았다(LLM 비결정)")

    assert _status_of(sid, item["item_id"]) == "passed", "사이즈가 정해졌으면 판정이 풀려야 한다"
    assert _open_questions(sid, item["item_id"]) == [], \
        "풀린 항목의 질문이 열려 있으면 담당자에게 같은 것을 다시 묻는다"


# =============================================================================
# 2. 개별 바꾸기(행 편집)로 풀린 항목 — 같은 결함이 있던 두 번째 경로
# =============================================================================
def test_b_row_edit_that_resolves_the_item_also_closes_its_question(live):
    """행을 직접 고쳐 풀어도 질문이 닫힌다. 답장만 고치고 이 경로를 빠뜨리면 같은 일이 난다."""
    client, made = live
    sid, token = _start(client, made)
    item = _ambiguous_item(client, sid, token)
    _ok(client.get(f"/api/sessions/{sid}/questions", headers=token))
    assert _open_questions(sid, item["item_id"])

    version, _ = _rows(client, sid, token)
    res = client.patch(f"/api/sessions/{sid}/items/{item['item_id']}",
                       json={"options": {"size": "venti"}},
                       headers={**token, "If-Match": str(version)})
    _ok(res)

    assert _status_of(sid, item["item_id"]) == "passed"
    assert _open_questions(sid, item["item_id"]) == []


# =============================================================================
# 3. 아직 안 풀린 항목의 질문은 그대로 둔다 — 닫는 조건이 넓으면 확인이 사라진다
# =============================================================================
def test_c_an_unresolved_item_keeps_its_question(live):
    """판정이 여전히 needs_review 면 질문은 열려 있어야 한다.

    이 시험이 없으면 "질문이 0개"를 통과 신호로 읽게 되고, 확인 필요가 남은 채
    질문만 사라져 공유 게이트에서 처음 막히는 더 나쁜 상태가 된다.
    """
    client, made = live
    sid, token = _start(client, made)
    item = _ambiguous_item(client, sid, token)
    _ok(client.get(f"/api/sessions/{sid}/questions", headers=token))

    # 얼음만 바꾼다 — 사이즈 모호는 그대로다
    version, _ = _rows(client, sid, token)
    _ok(client.patch(f"/api/sessions/{sid}/items/{item['item_id']}",
                     json={"options": {"ice": "normal"}},
                     headers={**token, "If-Match": str(version)}))

    if _status_of(sid, item["item_id"]) == "passed":
        pytest.skip("이번 해석에서는 얼음 변경이 모호를 함께 해소했다")
    assert _open_questions(sid, item["item_id"]), "안 풀린 항목의 질문을 닫으면 확인이 사라진다"
