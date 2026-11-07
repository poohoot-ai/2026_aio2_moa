# 실제 DB 로 전체 흐름을 한 번 태운다 — MOA_LIVE_DB=1 일 때만 돈다 (CI 는 건너뛴다)
#
#   MOA_LIVE_DB=1 pytest -q tests/test_live_flow.py
#
# 계약 테스트(test_api_contract)는 모양만 본다. 이 파일은 **라우터와 서비스가 실제로 맞물리는지**
# 본다 — 응답은 200 인데 숫자 · 버튼이 틀리는 "조용히 틀림"은 DB 를 태워야 드러난다.
# 만든 세션은 끝나면 지운다. 대화 끝에 무작위 표식 줄을 붙여 기존 결과 재사용(FR-011)을 피한다.
import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.getenv("MOA_LIVE_DB") != "1",
                                reason="MOA_LIVE_DB=1 일 때만 실제 DB 로 돈다")

STARBUCKS_CHAT = [
    "[김민수] 아이스 아메리카노 톨 2잔, 하나는 샷 추가요.",
    "[이지연] 바닐라라떼 큰 거 얼음 적게요.",
    "[박서연] 아아 톨 한 잔이요. 빨대는 빼 주시고 개인 텀블러에 담아 주세요.",
    "[최유나] 여기 인기 있는 메뉴 뭐야?",
    "[정하준] 민트 초코가 유명하대",
    "[최유나] 그럼 난 그걸로 하나~",
    "스타벅스로 갈 예정이야",
]


@pytest.fixture(scope="module")
def live():
    from app import db
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    made = []
    yield client, made
    for session_id in made:
        db.execute("DELETE FROM app.order_session WHERE session_id = %s", (session_id,))


def _chat(lines):
    """표식 줄은 읽지 못한 줄이라 주문이 아니다. **요청 한 줄(마지막 줄)보다 앞에** 둔다 —
    뒤에 두면 표식이 요청 한 줄로 읽혀 매장 기준이 대화 본문에서 다시 판별된다."""
    marker = f"live-check {uuid.uuid4().hex}"
    if lines and not lines[-1].startswith("["):
        return "\n".join(lines[:-1] + [marker, lines[-1]])
    return "\n".join(lines + [marker])


def _ok(res, status=200):
    body = res.json()
    assert res.status_code == status, body
    assert "error" not in body, body
    return body["data"]


def test_health_connected(live):
    client, _ = live
    body = client.get("/health").json()
    assert body["checks"]["db"] is True, body
    assert body["checks"]["menu_rows"] > 0


def test_full_owner_flow(live):
    client, made = live
    raw = _chat(STARBUCKS_CHAT)

    created = _ok(client.post("/api/sessions", json={"raw_text": raw}), 202)
    made.append(created["session_id"])
    sid, auth = created["session_id"], {"X-Session-Token": created["session_token"]}
    assert created["brand"]["id"] == "starbucks"

    # TestClient 는 백그라운드 작업을 응답 뒤에 끝까지 돌린다 — 여기서는 분석이 끝나 있다
    progress = _ok(client.get(f"/api/sessions/{sid}/progress", headers=auth))
    assert progress["status"] in ("needs_review", "awaiting_confirmation"), progress
    assert [s["state"] for s in progress["steps"]] == ["done"] * 4

    result = _ok(client.get(f"/api/sessions/{sid}", headers=auth))
    assert result["rows"], "항목이 만들어져야 한다"
    assert "NEEDS_REVIEW" not in str(result)               # 내부 값을 내보내지 않는다
    version = result["version"]

    # 소유권 — 남의 토큰으로는 404
    other = _ok(client.post("/api/sessions", json={"raw_text": _chat(["[한소희] 아이스티 한 잔",
                                                                      "동네카페에서 사올게"])}), 202)
    made.append(other["session_id"])
    res = client.get(f"/api/sessions/{sid}", headers={"X-Session-Token": other["session_token"]})
    assert res.status_code == 404

    # 같은 담당자가 같은 대화를 다시 넣으면 새로 분석하지 않는다 (FR-011)
    reused = _ok(client.post("/api/sessions", json={"raw_text": raw}, headers=auth))
    assert reused["reused"] is True and reused["session_id"] == sid
    # 다른 사람(토큰 없음)이 같은 원문을 넣으면 **새로 분석**한다 — 남의 세션을 알려 주지 않고,
    # 열 수 없는 세션을 돌려줘 "이 링크는 더 이상 쓸 수 없어요"에 막히지 않게 (D-05 확인에서 발견)
    stranger = _ok(client.post("/api/sessions", json={"raw_text": raw}), 202)
    made.append(stranger["session_id"])
    assert stranger["session_id"] != sid and stranger["session_token"]

    # 확인 질문 — 후보가 있어야 답할 수 있다 (결함 "후보 0개" 재발 방지)
    pack = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))
    if pack["question"]:
        question = pack["question"]
        assert question["options"], question
        answered = _ok(client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                                   json={"answer": question["options"][0]["value"]},
                                   headers={**auth, "If-Match": str(version)}))
        version = answered["version"]

        # 낡은 버전으로 쓰면 409
        stale = client.post(f"/api/sessions/{sid}/messages", json={"text": "김민수 취소"},
                            headers={**auth, "If-Match": str(version - 1)})
        assert stale.status_code in (409, 200)
        if stale.status_code == 409:
            assert stale.json()["error"]["code"] == "STALE_VERSION"

        undone = _ok(client.post(f"/api/sessions/{sid}/undo",
                                 json={"undo_token": answered["undo_token"]}, headers=auth))
        assert undone["user_message"] == "되돌렸어요"
        again = client.post(f"/api/sessions/{sid}/undo",
                            json={"undo_token": answered["undo_token"]}, headers=auth)
        assert again.status_code == 422

    evidence = _ok(client.get(f"/api/sessions/{sid}/evidence", headers=auth))
    # 보낸 사람이 없는 줄("스타벅스로 갈 예정이야")은 메시지가 아니다 — 읽지 못한 줄로 간다
    assert len(evidence["messages"]) == sum(1 for line in STARBUCKS_CHAT if line.startswith("["))

    menus = _ok(client.get("/api/menus/search", params={"q": "라떼"}, headers=auth))
    assert menus["menus"] and menus["scope_notice"].startswith("스타벅스")

    sheet = _ok(client.get(f"/api/sessions/{sid}/order-sheet", headers=auth))
    assert sheet["totals"]["notice"]
    if not sheet["can_share"]:
        blocked = client.get(f"/api/sessions/{sid}/share", headers=auth)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["user_message"] == sheet["blocked_reason_text"]

    replies = _ok(client.post(f"/api/sessions/{sid}/replies/analyze",
                              json={"raw_text": "[이지연] 얼음 많이로 바꿔요"}, headers=auth))
    assert "suggestions" in replies

    # 매장 기준 바꾸기 — 202 뒤 재분석이 끝나 있어야 한다
    current = _ok(client.get(f"/api/sessions/{sid}", headers=auth))["version"]
    changed = _ok(client.post(f"/api/sessions/{sid}/brand-change", json={"brand_id": "generic"},
                              headers={**auth, "If-Match": str(current)}), 202)
    assert changed["brand"]["id"] == "generic"
    after = _ok(client.get(f"/api/sessions/{sid}/progress", headers=auth))
    assert after["status"] in ("needs_review", "awaiting_confirmation"), after


def test_generic_session_resolves_and_shares(live):
    client, made = live
    raw = _chat(["[한소희] 아이스 아메리카노 레귤러 한 잔", "[오태민] 아이스 라떼 라지 얼음 많이요",
                 "동네카페에서 사올게"])
    created = _ok(client.post("/api/sessions", json={"raw_text": raw}), 202)
    made.append(created["session_id"])
    sid, auth = created["session_id"], {"X-Session-Token": created["session_token"]}

    for _ in range(6):
        pack = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))
        if not pack["question"]:
            break
        _ok(client.post(f"/api/sessions/{sid}/questions/{pack['question']['question_id']}/answer",
                        json={"answer": pack["question"]["options"][0]["value"]}, headers=auth))

    sheet = _ok(client.get(f"/api/sessions/{sid}/order-sheet", headers=auth))
    assert sheet["can_share"], sheet["blocked_reason_text"]
    share = _ok(client.get(f"/api/sessions/{sid}/share", headers=auth))
    # 이미지 공유 가능 여부는 한글 폰트가 있는지로 정한다 — 실행 환경마다 다르므로 환경 판정과 같은지만 본다
    from app.services import share_image
    assert share["content"] and share["image_available"] is share_image.available()
    done = _ok(client.post(f"/api/sessions/{sid}/share/complete", json={"method": "text"},
                           headers=auth))
    assert done["status"] == "completed"
    # 여러 번 와도 안전하다 (D-02 1.2)
    twice = _ok(client.post(f"/api/sessions/{sid}/share/complete", json={"method": "text"},
                            headers=auth))
    assert twice["share_count"] == done["share_count"] + 1


def test_proxy_order_person_is_linked_only_after_confirmation(live):
    """대리 주문 — "서연이 것도 라떼"는 질문에서 "박서연 님 맞아요"를 눌러야 박서연 주문이 된다 (2026-11-04)."""
    client, made = live
    raw = _chat(["[박서연] 아아 톨 한 잔이요", "[김민수] 서연이 것도 라떼 하나 추가요.",
                 "스타벅스로 갈 예정이야"])
    created = _ok(client.post("/api/sessions", json={"raw_text": raw}), 202)
    made.append(created["session_id"])
    sid, auth = created["session_id"], {"X-Session-Token": created["session_token"]}

    def latte():
        rows = _ok(client.get(f"/api/sessions/{sid}", headers=auth))["rows"]
        return next(r for r in rows if "라떼" in r["menu_text"])

    before = latte()
    assert before["person"] != "박서연" and before["needs_review"]   # 확인 전에는 잇지 않는다
    assert before["menu_text"] == "카페 라떼"                        # 앞머리를 떼서 메뉴는 확정

    question = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))["question"]
    assert question["text"].endswith("박서연 님 주문이 맞나요?"), question
    assert [o["value"] for o in question["options"]][0] == "박서연"
    answered = _ok(client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                               json={"answer": "박서연"}, headers=auth))
    assert answered["text"] == "박서연 님 주문으로 정했어요."
    after = latte()
    assert after["person"] == "박서연" and not after["needs_review"]

    _ok(client.post(f"/api/sessions/{sid}/undo", json={"undo_token": answered["undo_token"]}, headers=auth))
    assert latte()["person"] == before["person"]                     # 되돌리면 다시 확인 전으로
    reopened = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))["question"]
    assert reopened["question_id"] == question["question_id"]
    # 버튼에 없는 이름은 받지 않는다
    bad = client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                      json={"answer": "최유나"}, headers=auth)
    assert bad.status_code == 422


def test_reply_twice_then_apply_and_share_without_names(live):
    """#6 같은 세션에서 답장 분석을 두 번 눌러도 된다(전에는 두 번째가 500) · #8 이름 없이 공유 (2026-11-04)."""
    client, made = live
    raw = _chat(["[김민수] 아아 톨 한 잔이요", "[이지연] 바닐라라떼 톨 한 잔", "스타벅스로 갈 예정이야"])
    created = _ok(client.post("/api/sessions", json={"raw_text": raw}), 202)
    made.append(created["session_id"])
    sid, auth = created["session_id"], {"X-Session-Token": created["session_token"]}

    first = _ok(client.post(f"/api/sessions/{sid}/replies/analyze",
                            json={"raw_text": "[이지연] 그란데로 바꿔주세요"}, headers=auth))
    second = _ok(client.post(f"/api/sessions/{sid}/replies/analyze",
                             json={"raw_text": "[김민수] [오후 2:31] 벤티로 바꿔요"}, headers=auth))
    ids_ = [s["suggestion_id"] for s in first["suggestions"] + second["suggestions"]]
    assert len(ids_) == 2 and len(set(ids_)) == 2                  # 번호가 겹치지 않는다

    for suggestion in first["suggestions"] + second["suggestions"]:
        version = _ok(client.get(f"/api/sessions/{sid}", headers=auth))["version"]
        _ok(client.post(f"/api/sessions/{sid}/suggestions/{suggestion['suggestion_id']}/decision",
                        json={"decision": "apply"}, headers={**auth, "If-Match": str(version)}))
    rows = {r["person"]: r for r in _ok(client.get(f"/api/sessions/{sid}", headers=auth))["rows"]}
    assert "그란데" in str(rows["이지연"]["options"]) and "벤티" in str(rows["김민수"]["options"])

    named = _ok(client.get(f"/api/sessions/{sid}/share", headers=auth))
    hidden = _ok(client.get(f"/api/sessions/{sid}/share", params={"names": "false"}, headers=auth))
    assert "김민수" in named["content"] and named["show_names"] is True
    assert "김민수" not in hidden["content"] and "이지연" not in hidden["content"]
    assert hidden["show_names"] is False and "× 1잔" in hidden["content"]
    image = client.get(f"/api/sessions/{sid}/share", params={"format": "image", "names": "false"}, headers=auth)
    assert image.status_code in (200, 422)                           # 폰트가 없으면 422(IMAGE_UNAVAILABLE)


def test_brand_question_then_answer(live):
    client, made = live
    raw = _chat(["[서지후] 스타벅스 아아 한 잔", "[임나래] 나는 메가커피 아이스티"])
    asked = _ok(client.post("/api/sessions", json={"raw_text": raw}))
    made.append(asked["session_id"])
    assert asked["status"] == "awaiting_brand_answer" and asked["brand_question"]["options"]
    sid, auth = asked["session_id"], {"X-Session-Token": asked["session_token"]}

    answered = _ok(client.post(f"/api/sessions/{sid}/brand-answer",
                               json={"answer": "other", "store_name": "메가커피"}, headers=auth), 202)
    assert answered["brand"]["id"] == "generic"
    again = client.post(f"/api/sessions/{sid}/brand-answer", json={"answer": "starbucks"},
                        headers=auth)
    assert again.status_code == 409                        # 되묻기는 1회


def test_ops_dashboard(live):
    client, made = live
    token = _ok(client.post("/api/ops/login", json={"password": "test-ops"}))["ops_token"]
    auth = {"Authorization": f"Bearer {token}"}
    incidents = _ok(client.get("/api/ops/incidents", headers=auth))
    assert "approvals" in incidents
    for path in ("/api/ops/status", "/api/ops/metrics", "/api/ops/orders"):
        _ok(client.get(path, headers=auth))
    if incidents["incidents"]:
        first = incidents["incidents"][0]["incident_id"]
        _ok(client.get(f"/api/ops/incidents/{first}", headers=auth))
    if made:
        _ok(client.get(f"/api/ops/orders/{made[0]}", headers=auth))


def test_quantity_size_temperature_checks(live):
    """수량 · 사이즈 · 온도 점검 4건 — 조용히 확정하지 않고 묻는다, 답하면 풀린다."""
    client, made = live
    raw = _chat(["[김민수] 아이스 아메리카노 톨 50잔",       # 과다 수량
                 "[이지연] 카페 라떼 톨 0잔",                 # 0잔
                 "[박서연] 따뜻한 아이스 아메리카노 톨",       # 온도 충돌
                 "[최유나] 아이스 카페 라떼 라지",            # 다른 매장 사이즈 용어
                 "[정하준] 바닐라라떼 중간 사이즈 30잔",       # 사이즈 + 수량 둘 다
                 "[한소희] 차가운 라떼 톨",                   # 정상 — 아이스로 읽혀야 한다
                 "[오태민] 아이스 아메리카노 톨 2잔",          # 정상 — 묻지 않아야 한다
                 "스타벅스로 갈 예정이야"])
    created = _ok(client.post("/api/sessions", json={"raw_text": raw}), 202)
    made.append(created["session_id"])
    sid, auth = created["session_id"], {"X-Session-Token": created["session_token"]}

    rows = {r["person"]: r for r in _ok(client.get(f"/api/sessions/{sid}", headers=auth))["rows"]}
    assert rows["한소희"]["menu_text"].startswith("아이스"), rows["한소희"]
    assert rows["오태민"]["needs_review"] is False
    assert rows["한소희"]["needs_review"] is False
    for person in ("김민수", "이지연", "박서연", "최유나", "정하준"):
        assert rows[person]["needs_review"] is True, rows[person]
        assert rows[person]["estimated_price"] is None          # 확인 전에는 금액을 내지 않는다

    asked = []
    for _ in range(12):
        pack = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))
        question = pack["question"]
        if not question:
            break
        assert question["options"], question                  # 후보 0개 질문은 답할 수 없다
        asked.append(question["text"])
        _ok(client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                        json={"answer": question["options"][0]["value"]}, headers=auth))

    text = "\n".join(asked)
    assert '"50잔"' in text and '"0잔"' in text
    assert "따뜻한 것과 아이스" in text
    assert text.count("어떤 사이즈인가요") == 2                 # 최유나(라지) · 정하준(중간 사이즈)
    assert '"30잔"' in text                                      # 사이즈 답 뒤 후속 질문

    after = {r["person"]: r for r in _ok(client.get(f"/api/sessions/{sid}", headers=auth))["rows"]}
    assert all(not r["needs_review"] for r in after.values()), after
    sheet = _ok(client.get(f"/api/sessions/{sid}/order-sheet", headers=auth))
    assert sheet["can_share"], sheet["blocked_reason_text"]


def test_undo_question_answer_reopens_the_question(live):
    """되돌리기는 **그 질문을 다시 묻는 상태로** 돌려놓아야 한다 (API-024 · U-5).

    항목만 확인 필요로 되돌리고 질문을 `answered` 로 남기면, 상태 줄은 "1건만 답해 주시면
    돼요"인데 물을 질문이 없어 담당자가 공유까지 막힌 채 빠져나갈 길이 없다 — 되돌리기가
    "처리된 상태로 지나가 버리는" 결함이다.
    """
    client, made = live
    created = _ok(client.post("/api/sessions", json={"raw_text": _chat(STARBUCKS_CHAT)}), 202)
    made.append(created["session_id"])
    sid, auth = created["session_id"], {"X-Session-Token": created["session_token"]}

    pack = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))
    question = pack["question"]
    assert question, "확인 질문이 있어야 이 검사가 뜻을 가진다"
    answered = _ok(client.post(f"/api/sessions/{sid}/questions/{question['question_id']}/answer",
                               json={"answer": question["options"][0]["value"]}, headers=auth))

    _ok(client.post(f"/api/sessions/{sid}/undo",
                    json={"undo_token": answered["undo_token"]}, headers=auth))

    again = _ok(client.get(f"/api/sessions/{sid}/questions", headers=auth))
    assert again["question"], "되돌린 뒤에는 그 질문을 다시 물어야 한다"
    assert again["question"]["question_id"] == question["question_id"]
    assert again["question"]["text"] == question["text"]

    # 남은 건수(질문 큐)와 상태 줄(확인 필요 항목 수)이 어긋나면 하단 바와 상태 줄이 모순된다
    progress = _ok(client.get(f"/api/sessions/{sid}/progress", headers=auth))
    assert progress["status"] == "needs_review"
    assert again["remaining"] == progress["review_count"]
