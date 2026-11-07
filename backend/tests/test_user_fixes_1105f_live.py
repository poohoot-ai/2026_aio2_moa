# 2026-11-05 4차 사용자 보고 — 주문 정리 단계의 채팅 수정 (실제 DB · MOA_LIVE_DB=1)
#
#   MOA_LIVE_DB=1 ../.venv/Scripts/python.exe -m pytest -q tests/test_user_fixes_1105f_live.py
#
# 1-가 기존 인원의 메뉴를 채팅으로 바꾸기 — 한 사람이 **여러 잔**이면 "어느 주문을 말하는지 찾지 못했어요"로 막혔다
# 1-나 새 인물의 주문을 채팅으로 추가 — 채팅으로 주문을 만드는 경로가 아예 없었다. 게다가
#      `_find_target` 이 **세션에 없는 사람 이름을 무시하고** 남의 행을 대상으로 골라 422 가 났다
#
# 항목 생성 · 판정 · 금액 · 질문은 DB 가 함께 맞물려야 드러난다 — 가짜 저장소로는 "응답 200 인데
# 주문이 안 생김"을 재현할 수 없다.
import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.getenv("MOA_LIVE_DB") != "1",
                                reason="MOA_LIVE_DB=1 일 때만 실제 DB 로 돈다")

ONE_EACH = [
    "[김민수] 아이스 아메리카노 톨 한 잔이요",
    "[이지연] 아이스 카페 라떼 톨 한 잔이요",
]
TWO_CUPS = [
    "[김민수] 아이스 아메리카노 톨 2잔, 하나는 샷 추가요.",
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
    """표식 줄은 기존 결과 재사용(FR-011)을 피한다. 요청 한 줄보다 **앞에** 둔다."""
    raw = "\n".join(lines + [f"live-1105f {uuid.uuid4().hex}", "스타벅스로 갈 예정이야"])
    data = _ok(client.post("/api/sessions", json={"raw_text": raw, "source": "paste"}), 202)
    made.append(data["session_id"])
    return data["session_id"], {"X-Session-Token": data["session_token"]}


def _rows(client, sid, token):
    session = _ok(client.get(f"/api/sessions/{sid}", headers=token))
    return session["version"], session["rows"]


def _say(client, sid, token, text):
    version, _ = _rows(client, sid, token)
    res = client.post(f"/api/sessions/{sid}/messages", json={"text": text},
                      headers={**token, "If-Match": str(version)})
    return res, res.json().get("data") or res.json().get("error") or {}


def _menu_of(rows, person):
    return [r.get("menu_name") or r.get("menu_text") for r in rows if r["person"] == person]


# =============================================================================
# 1-가 기존 인원의 메뉴 바꾸기
# =============================================================================
def test_single_cup_person_menu_changes(live):
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    res, data = _say(client, sid, token, "김민수 아이스 카페 라떼로 바꿔줘")
    assert res.status_code == 200, data
    assert data["kind"] == "applied", data
    assert data["undo_token"]
    _, rows = _rows(client, sid, token)
    assert _menu_of(rows, "김민수") == ["아이스 카페 라떼"], rows


def test_a_person_with_two_cups_is_not_a_dead_end(live):
    """사람은 하나로 정해졌고 **어느 잔인지만** 모른다 — 그러면 그 사실을 말한다.

    예전에는 "어느 주문을 말하는지 찾지 못했어요"였다. 사람을 못 찾은 것처럼 들려서
    담당자가 이름을 다시 써 보고, 같은 답을 또 받았다(채팅으로 고치는 길이 막힌다).
    """
    client, made = live
    sid, token = _start(client, made, TWO_CUPS)
    _, rows = _rows(client, sid, token)
    assert len(_menu_of(rows, "김민수")) == 2, rows

    res, data = _say(client, sid, token, "김민수 아이스 카페 라떼로 바꿔줘")
    assert res.status_code == 200, data
    assert data["kind"] == "ask_which", data
    assert "김민수" in data["text"]
    assert len(data["options"]) == 2, data          # 고를 수 있게 잔을 적어 준다


def test_two_cups_resolve_when_the_sentence_points_at_one(live):
    """옵션으로 한 잔을 가리키면 그 잔만 바꾼다 — "샷 추가한 거"."""
    client, made = live
    sid, token = _start(client, made, TWO_CUPS)
    res, data = _say(client, sid, token, "김민수 샷 추가한 거 아이스 카페 라떼로 바꿔줘")
    assert res.status_code == 200, data
    assert data["kind"] == "applied", data
    _, rows = _rows(client, sid, token)
    assert sorted(_menu_of(rows, "김민수")) == ["아이스 아메리카노", "아이스 카페 라떼"], rows


# =============================================================================
# 1-나 새 인물의 주문 추가
# =============================================================================
def test_a_new_person_order_is_added(live):
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    res, data = _say(client, sid, token, "박서연 아이스 아메리카노 톨 한 잔 추가해줘")
    assert res.status_code == 200, data
    assert data["kind"] == "applied", data
    assert "박서연" in data["text"]
    assert data["undo_token"], data
    _, rows = _rows(client, sid, token)
    assert _menu_of(rows, "박서연") == ["아이스 아메리카노"], rows
    assert len(rows) == 3, rows


def test_adding_a_new_person_never_touches_someone_elses_row(live):
    """세션에 없는 이름을 무시하고 남의 행을 고치지 않는다 — 가장 위험한 쪽이다."""
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    _, before = _rows(client, sid, token)
    _say(client, sid, token, "박서연 아이스 아메리카노 톨 한 잔 추가해줘")
    _, after = _rows(client, sid, token)
    assert _menu_of(after, "김민수") == _menu_of(before, "김민수")
    assert _menu_of(after, "이지연") == _menu_of(before, "이지연")


def test_an_added_order_can_be_undone(live):
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    _, data = _say(client, sid, token, "박서연 아이스 아메리카노 톨 한 잔 추가해줘")
    undone = _ok(client.post(f"/api/sessions/{sid}/undo",
                             json={"undo_token": data["undo_token"]}, headers=token))
    assert undone["user_message"]
    _, rows = _rows(client, sid, token)
    assert _menu_of(rows, "박서연") == [], rows


def test_adding_another_cup_for_an_existing_person(live):
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    res, data = _say(client, sid, token, "김민수 아이스 카페 라떼 톨 한 잔 더 추가해줘")
    assert res.status_code == 200, data
    assert data["kind"] == "applied", data
    _, rows = _rows(client, sid, token)
    assert sorted(_menu_of(rows, "김민수")) == ["아이스 아메리카노", "아이스 카페 라떼"], rows


def test_an_add_without_a_name_says_so(live):
    """누구 주문인지 모르면 임의로 고르지 않는다 — 무엇이 더 필요한지 말한다."""
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    res, data = _say(client, sid, token, "아이스 아메리카노 한 잔 추가해줘")
    assert res.status_code == 200, data
    assert data["kind"] == "no_change", data
    assert "누구" in data["text"], data
    _, rows = _rows(client, sid, token)
    assert len(rows) == 2, rows


def test_an_add_without_any_menu_words_says_so(live):
    """메뉴로 읽을 말이 아예 없으면 빈 주문을 만들지 않는다."""
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    res, data = _say(client, sid, token, "박서연 추가해줘")
    assert res.status_code == 200, data
    assert data["kind"] == "no_change", data
    assert "메뉴" in data["text"], data
    _, rows = _rows(client, sid, token)
    assert len(rows) == 2, rows


def test_an_unregistered_menu_is_added_as_needs_review(live):
    """메뉴판에 없는 이름은 **버리지 않고** 확인 필요로 올린다 — 분석 경로와 같은 규칙이다."""
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    res, data = _say(client, sid, token, "박서연 우주라떼 한 잔 추가해줘")
    assert res.status_code == 200, data
    assert data["kind"] == "applied", data
    _, rows = _rows(client, sid, token)
    added = [r for r in rows if r["person"] == "박서연"]
    assert len(added) == 1 and added[0]["needs_review"], rows


def test_an_added_review_item_gets_a_question(live):
    """추가한 주문이 확인 필요로 남으면 질문이 생긴다 — 주문서에서 막히지 않게(보고 10번)."""
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    _say(client, sid, token, "박서연 헤이즐넛 라떼 한 잔 추가해줘")
    progress = _ok(client.get(f"/api/sessions/{sid}/progress", headers=token))
    queue = _ok(client.get(f"/api/sessions/{sid}/questions", headers=token))
    assert queue["remaining"] == progress["review_count"], (queue, progress)


def test_the_same_idempotency_key_applies_an_add_once(live):
    """화면이 같은 키로 다시 보낼 수 있다(4차 보고 1번) — 두 번 반영되면 안 된다."""
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    version, _ = _rows(client, sid, token)
    headers = {**token, "If-Match": str(version), "Idempotency-Key": f"chat-{uuid.uuid4().hex}"}
    body = {"text": "박서연 아이스 아메리카노 톨 한 잔 추가해줘"}
    first = _ok(client.post(f"/api/sessions/{sid}/messages", json=body, headers=headers))
    again = _ok(client.post(f"/api/sessions/{sid}/messages", json=body, headers=headers))
    assert again["text"] == first["text"]
    _, rows = _rows(client, sid, token)
    assert _menu_of(rows, "박서연") == ["아이스 아메리카노"], rows


def test_chatter_is_not_treated_as_an_add(live):
    """잡담은 주문 추가 시도가 아니다 — `intake.classify` 가 분류되지 않는 말을 add 로 돌려준다.

    울타리가 없으면 "오늘 날씨 좋네요"에 "메뉴 이름을 적어 주세요"가 돌아갔다(실측).
    """
    client, made = live
    sid, token = _start(client, made, ONE_EACH)
    for chatter in ("오늘 날씨 좋네요", "다들 뭐 먹을래?", "ㅋㅋㅋ 고민되네"):
        res, data = _say(client, sid, token, chatter)
        assert res.status_code == 200, (chatter, data)
        assert data["kind"] == "no_change", (chatter, data)
        assert "바꿀 것을 찾지 못했어요" in data["text"], (chatter, data)
    _, rows = _rows(client, sid, token)
    assert len(rows) == 2, rows
