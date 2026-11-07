# API 계약 테스트 — DB 없이 돈다 (CI). 서버를 띄우지 않고 TestClient 로 부른다
#
# 무엇을 지키는가: D-02 1 ~ 3절 — 포장(data · error · meta), 인증 2종, 오류 코드,
# "DB 장애는 503 이 아니다", 소유권 404. 화면(api_client)이 이 모양에 기대어 분기한다.
import pytest
from fastapi.testclient import TestClient

from app import db, envelope
from app.api import deps
from app.main import app
from app.services import completion, edits, order_sheet, sessions

client = TestClient(app, raise_server_exceptions=False)

SESSION = {"session_id": "ORD-0001", "status": "needs_review", "version": 3,
           "brand_id": "starbucks", "brand_source": "request", "brand_evidence_text": None,
           "menu_version": "mv_20261029", "trace_id": "tr_TEST", "checkpoint": "settled"}
TOKEN = {"X-Session-Token": "tok"}


def error_of(res):
    body = res.json()
    assert "data" not in body, "data 와 error 는 함께 오지 않는다"
    assert body["meta"]["trace_id"], "오류 응답에도 trace_id 가 있다"
    return body["error"]


@pytest.fixture
def no_db(monkeypatch):
    """DB 미설정 상태. MOA_LIVE_DB=1 로 함께 돌려도 같은 조건이 되도록 직접 비운다."""
    from app import config

    monkeypatch.setattr(config, "DATABASE_URL", "")


@pytest.fixture
def owned(monkeypatch):
    """토큰이 SESSION 을 가리키는 상태."""
    monkeypatch.setattr(sessions, "load", lambda sid, tok: SESSION if sid == "ORD-0001" else None)


# --- 4.0 공통 · 상태 ---------------------------------------------------------
def test_health_without_db_is_degraded_not_503(no_db):
    # DB 는 재시작으로 고쳐지지 않는다 — 503 이면 감시자가 영원히 재시작한다 (R-14)
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"] is False
    assert body["checks"]["api_key"] in (True, False)        # 값이 아니라 여부만
    assert "data" not in body                                  # /health 는 포장하지 않는다


def test_health_down_when_llm_required_and_key_missing(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "LLM_REQUIRED", True)
    monkeypatch.setattr(config, "llm_key_present", lambda: False)
    assert client.get("/health").status_code == 503


def test_meta_has_envelope_and_trace():
    res = client.get("/api/meta")
    body = res.json()
    assert res.status_code == 200
    assert body["data"]["price_notice"].startswith("메뉴와 가격은")
    assert [b["id"] for b in body["data"]["brands"]] == ["starbucks", "generic"]
    assert body["meta"]["trace_id"].startswith("tr_")
    assert res.headers["X-Trace-Id"] == body["meta"]["trace_id"]


def test_sample_conversation():
    data = client.get("/api/sample-conversation").json()["data"]
    assert data["is_sample"] is True
    assert data["summary_text"] == "5명 · 메시지 8개를 읽었어요"


# --- 4.1 입력 ----------------------------------------------------------------
def test_preview_json_keeps_unreadable_lines():
    res = client.post("/api/conversations/preview",
                      json={"raw_text": "[김민수] 아아 톨\n이건 형식이 아닌 줄입니다 그렇죠\n[이지연] 라떼"})
    data = res.json()["data"]
    assert data["can_proceed"] is True
    assert len(data["messages"]) == 2
    assert data["unreadable_lines"][0]["line_no"] == 2


def test_preview_txt_file_cp949():
    raw = "[김민수] 아이스 아메리카노".encode("cp949")
    data = client.post("/api/conversations/preview",
                       files={"file": ("chat.txt", raw)}).json()["data"]
    assert data["raw_text"] == "[김민수] 아이스 아메리카노"


def test_preview_image_rejected_by_content():
    res = client.post("/api/conversations/preview",
                      files={"file": ("chat.txt", b"\x89PNG\r\n\x1a\n0000")})
    assert res.status_code == 400
    assert error_of(res)["code"] == "IMAGE_NOT_SUPPORTED"


@pytest.mark.parametrize("name", ["chat.csv", "chat.md", "chat.json", "chat"])
def test_preview_non_txt_text_file_rejected(name):
    """첨부는 .txt 만 (PRD v1.1 §1) — 내용이 텍스트여도 확장자가 .txt 가 아니면 받지 않는다."""
    res = client.post("/api/conversations/preview",
                      files={"file": (name, "[김민수] 아아 톨".encode("utf-8"))})
    assert res.status_code == 400
    assert error_of(res)["code"] == "FILE_NOT_SUPPORTED"
    assert ".txt" in error_of(res)["user_message"]


def test_preview_txt_extension_is_case_insensitive():
    data = client.post("/api/conversations/preview",
                       files={"file": ("CHAT.TXT", "[김민수] 아아 톨".encode("utf-8"))}).json()["data"]
    assert data["raw_text"] == "[김민수] 아아 톨"


def test_create_session_empty_conversation():
    res = client.post("/api/sessions", json={"raw_text": "   "})
    assert res.status_code == 400
    assert error_of(res)["code"] == "CONVERSATION_EMPTY"


def test_create_session_too_large():
    res = client.post("/api/sessions", json={"raw_text": "[a] " + "가" * 400_000})
    assert error_of(res)["code"] == "CONVERSATION_TOO_LARGE"


def test_create_session_rejects_bad_source():
    res = client.post("/api/sessions", json={"raw_text": "[a] 아아", "source": "x"})
    assert error_of(res)["code"] == "BAD_REQUEST"


def test_create_session_without_db_is_200_db_unavailable(no_db):
    res = client.post("/api/sessions", json={"raw_text": "[김민수] 아아 톨"})
    assert res.status_code == 200
    err = error_of(res)
    assert err["code"] == "DB_UNAVAILABLE" and err["retryable"] is True
    assert "DATABASE_URL" not in err["user_message"]          # 기술 문구는 detail 에만


# --- 인증 · 소유권 (D-02 1.1 · G-A3) ----------------------------------------
@pytest.mark.parametrize("method,path", [
    ("get", "/api/sessions/ORD-0001"),
    ("get", "/api/sessions/ORD-0001/progress"),
    ("get", "/api/sessions/ORD-0001/questions"),
    ("get", "/api/sessions/ORD-0001/order-sheet"),
    ("get", "/api/menus/search"),
    ("post", "/api/sessions/ORD-0001/undo"),
])
def test_session_api_requires_token(method, path):
    res = getattr(client, method)(path)
    assert res.status_code == 401
    assert error_of(res)["code"] == "SESSION_TOKEN_MISSING"


def test_other_session_is_404_not_403(owned):
    res = client.get("/api/sessions/ORD-0002", headers=TOKEN)
    assert res.status_code == 404
    assert error_of(res)["code"] == "SESSION_NOT_FOUND"


def test_menu_search_takes_brand_from_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: SESSION)

    def fake_search(brand_id, menu_version, q):
        seen.update(brand_id=brand_id, q=q)
        return {"menus": [], "scope_notice": "", "empty_text": None}

    from app.services import menu
    monkeypatch.setattr(menu, "search_view", fake_search)
    res = client.get("/api/menus/search", params={"q": " 라떼 ", "brand_id": "generic"},
                     headers=TOKEN)
    assert res.status_code == 200
    assert seen == {"brand_id": "starbucks", "q": "라떼"}     # 쿼리의 brand_id 는 무시된다


# --- 쓰기 · 잠금 · 되돌리기 ---------------------------------------------------
def test_if_match_must_be_integer(owned):
    res = client.post("/api/sessions/ORD-0001/messages", json={"text": "라떼로"},
                      headers={**TOKEN, "If-Match": "abc"})
    assert error_of(res)["code"] == "BAD_REQUEST"


def test_stale_version_is_409(owned, monkeypatch):
    def conflict(*a, **k):
        raise edits.Conflict(9)

    monkeypatch.setattr(edits, "interpret_message", conflict)
    res = client.post("/api/sessions/ORD-0001/messages", json={"text": "라떼로"},
                      headers={**TOKEN, "If-Match": "3"})
    assert res.status_code == 409
    err = error_of(res)
    assert err["code"] == "STALE_VERSION" and err["current_version"] == 9


def test_if_match_passed_to_service(owned, monkeypatch):
    got = {}

    def interpret(session, text, *, expected_version):
        got.update(text=text, version=expected_version)
        return {"kind": "no_change", "text": "주문에서 바꿀 것을 찾지 못했어요", "undo_token": None}

    monkeypatch.setattr(edits, "interpret_message", interpret)
    res = client.post("/api/sessions/ORD-0001/messages", json={"text": "안녕"},
                      headers={**TOKEN, "If-Match": "3"})
    assert res.json()["data"]["kind"] == "no_change"
    assert got == {"text": "안녕", "version": 3}


def test_undo_unavailable_is_422(owned, monkeypatch):
    def nothing(*a, **k):
        raise edits.NotApplicable("UNDO_UNAVAILABLE")

    monkeypatch.setattr(edits, "undo", nothing)
    res = client.post("/api/sessions/ORD-0001/undo", json={"undo_token": "u1"}, headers=TOKEN)
    assert res.status_code == 422
    assert error_of(res)["code"] == "UNDO_UNAVAILABLE"


def test_answer_accepts_answer_or_value(owned, monkeypatch):
    got = []
    monkeypatch.setattr(edits, "answer_question",
                        lambda s, q, a, expected_version: got.append(a) or {"text": "ok"})
    for body in ({"answer": "톨"}, {"value": "톨"}):
        res = client.post("/api/sessions/ORD-0001/questions/q1/answer", json=body, headers=TOKEN)
        assert res.status_code == 200
    assert got == ["톨", "톨"]


def test_patch_requires_fields(owned):
    res = client.patch("/api/sessions/ORD-0001/items/it_01", json={}, headers=TOKEN)
    assert error_of(res)["code"] == "BAD_REQUEST"


# --- 공유 (FR-009 — 조건 판정은 서버) ----------------------------------------
def test_share_blocked_is_409(owned, monkeypatch):
    monkeypatch.setattr(order_sheet, "can_share",
                        lambda sid: {"can_share": False,
                                     "blocked_reason_text": "아직 확인할 주문이 2개 남았어요"})
    res = client.get("/api/sessions/ORD-0001/share", headers=TOKEN)
    assert res.status_code == 409
    err = error_of(res)
    assert err["code"] == "SHARE_BLOCKED"
    assert err["user_message"] == "아직 확인할 주문이 2개 남았어요"


def test_complete_share_blocked_is_409(owned, monkeypatch):
    def blocked(*a, **k):
        raise completion.NotShareable("아직 확인할 주문이 1개 남았어요")

    monkeypatch.setattr(completion, "complete", blocked)
    res = client.post("/api/sessions/ORD-0001/share/complete", json={"method": "text"},
                      headers=TOKEN)
    assert error_of(res)["code"] == "SHARE_BLOCKED"


PNG_MAGIC = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])


def _demo_sheet():
    return {
        "brand": {"id": "starbucks"},
        "summary": {"people": 2, "cups": 3},
        "totals": {"cups": 3, "estimated_total": 13500},
        "groups": [
            {"menu_text": "아이스 아메리카노", "options": [{"label": "사이즈", "value": "톨"}],
             "quantity": 2, "people": ["김민수", "정하준"], "custom_request": None},
            {"menu_text": "아이스 카페 라떼", "options": [{"label": "사이즈", "value": "톨"}],
             "quantity": 1, "people": ["박서연"],
             "custom_request": "빨대는 빼 주시고 개인 텀블러에 담아 주세요, 얼음은 컵 반만 채워 주세요"},
        ],
    }


def test_share_image_returns_png_bytes_not_json(owned, monkeypatch):
    """PNG 는 포장(envelope)에 넣지 않는다 — 화면이 바이트를 그대로 내려받는다."""
    from app.services import share, share_image

    if not share_image.available():
        import pytest as _pytest
        _pytest.skip("한글 폰트가 없는 환경 — 아래 테스트가 그 경우를 본다")
    monkeypatch.setattr(order_sheet, "can_share", lambda sid: {"can_share": True})
    monkeypatch.setattr(order_sheet, "view", lambda s: _demo_sheet())
    res = client.get("/api/sessions/ORD-0001/share", params={"format": "image"}, headers=TOKEN)
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content[:8] == PNG_MAGIC
    assert "attachment" in res.headers["content-disposition"]


def test_share_image_is_refused_when_there_is_no_korean_font(owned, monkeypatch):
    """폰트가 없으면 그리지 않는다 — 두부(네모)로 찍힌 그림을 내보내지 않기 위해서다."""
    from app.services import share_image

    monkeypatch.setattr(order_sheet, "can_share", lambda sid: {"can_share": True})
    monkeypatch.setattr(order_sheet, "view", lambda s: _demo_sheet())
    monkeypatch.setattr(share_image, "available", lambda: False)
    res = client.get("/api/sessions/ORD-0001/share", params={"format": "image"}, headers=TOKEN)
    assert error_of(res)["code"] == "IMAGE_UNAVAILABLE"


# --- 운영 대시보드 ------------------------------------------------------------
def test_ops_login_wrong_password():
    res = client.post("/api/ops/login", json={"password": "nope"})
    assert res.status_code == 401
    assert error_of(res)["code"] == "OPS_AUTH_FAILED"


def test_ops_requires_bearer_and_session_token_is_not_enough():
    assert client.get("/api/ops/incidents").status_code == 401
    assert client.get("/api/ops/incidents", headers=TOKEN).status_code == 401


def test_ops_login_then_logout():
    token = client.post("/api/ops/login", json={"password": "test-ops"}).json()["data"]["ops_token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/ops/logout", headers=auth).json()["data"] == {"logged_out": True}
    assert client.get("/api/ops/status", headers=auth).status_code == 401


def test_ops_decision_already_decided(monkeypatch):
    from app.services import incidents

    token = client.post("/api/ops/login", json={"password": "test-ops"}).json()["data"]["ops_token"]
    monkeypatch.setattr(incidents, "decide", lambda i, d, note=None:
                        {"ok": False, "already": "approve", "user_message": "x"})
    res = client.post("/api/ops/incidents/INC-0001/decision", json={"decision": "reject"},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 409
    assert error_of(res)["decision"] == "approve"


# --- 공통 오류 포장 -----------------------------------------------------------
def test_unknown_route_is_wrapped():
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert error_of(res)["code"] == "NOT_FOUND"


def test_query_validation_is_400():
    token = client.post("/api/ops/login", json={"password": "test-ops"}).json()["data"]["ops_token"]
    res = client.get("/api/ops/incidents", params={"limit": "abc"},
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 400


def test_debug_api_not_registered_by_default():
    assert client.post("/api/debug/boom").status_code == 404


def test_teaching_leftovers_removed():
    # 교안 잔재(상품 소개문 검사기)가 다시 들어오지 않게 한다
    assert client.post("/check", json={}).status_code == 404
    assert app.title == "Moa 에이전트 서버"


# --- 백그라운드 분석 실패 -> failed (running 으로 남지 않는다) ---------------
def test_background_failure_marks_failed(monkeypatch):
    calls = []

    def boom(session_id, *, trace_id):
        raise RuntimeError("x")

    monkeypatch.setattr(sessions, "run_pipeline", boom)
    monkeypatch.setattr(db, "execute", lambda sql, params=None, **k: calls.append((sql, params)))
    deps.run_pipeline_background("ORD-0001", "tr_X")
    assert calls and "status = 'failed'" in calls[0][0]
    assert envelope.current_trace.get() is None


# --- 매장 요청 한 줄 (PRD 2.4.1) ----------------------------------------------
@pytest.fixture
def store_dictionary(monkeypatch):
    from app.services import brand

    monkeypatch.setattr(brand, "_dictionary", lambda: [
        {"alias": "스타벅스", "_key": "스타벅스", "resolved_brand_id": "starbucks",
         "kind": "supported", "display_name": "스타벅스"}])


def test_last_line_naming_a_store_is_store_request(store_dictionary):
    data = client.post("/api/conversations/preview",
                       json={"raw_text": "[김민수] 아아 톨\n스타벅스로 갈 예정이야"}).json()["data"]
    assert data["store_request"] == "스타벅스로 갈 예정이야"
    assert data["unreadable_lines"] == []                    # "2번째 줄은 읽지 못했어요"가 뜨지 않는다


def test_last_line_without_store_is_still_unreadable(store_dictionary):
    # D-02 API-011 예시 — 매장 이름이 없는 끝줄은 읽지 못한 줄이다
    data = client.post("/api/conversations/preview",
                       json={"raw_text": "[김민수] 아아 톨\n아까 말한 거 그대로요"}).json()["data"]
    assert data["store_request"] is None
    assert [u["line_no"] for u in data["unreadable_lines"]] == [2]


def test_middle_line_naming_a_store_is_still_unreadable(store_dictionary):
    data = client.post("/api/conversations/preview",
                       json={"raw_text": "[김민수] 아아 톨\n스타벅스 가자\n[이지연] 라떼"}).json()["data"]
    assert data["store_request"] is None
    assert [u["line_no"] for u in data["unreadable_lines"]] == [2]


def test_store_request_without_db_falls_back_to_unreadable(no_db):
    data = client.post("/api/conversations/preview",
                       json={"raw_text": "[김민수] 아아 톨\n스타벅스로 갈 예정이야"}).json()["data"]
    assert data["store_request"] is None and len(data["unreadable_lines"]) == 1


def test_sample_conversation_ends_with_store_request():
    data = client.get("/api/sample-conversation").json()["data"]
    assert data["raw_text"].splitlines()[-1] == "스타벅스로 갈 예정이야"
    assert len(data["messages"]) == 8                        # 요청 한 줄은 메시지가 아니다


# --- 요청 로그 — 한 요청 = 정확히 한 줄 (교안코드규약 4.11 · D-03 1.5) ------------------------
@pytest.fixture
def request_lines(monkeypatch):
    from app import logging_setup

    lines = []
    real = logging_setup.log

    def spy(event, **fields):
        if event == "request":
            lines.append(fields)
        real(event, **fields)

    monkeypatch.setattr(logging_setup, "log", spy)
    return lines


@pytest.mark.parametrize("path,status,level", [
    ("/api/meta", 200, "info"),
    ("/api/nope", 404, "warning"),
])
def test_one_request_line_per_request(request_lines, path, status, level):
    res = client.get(path)
    assert res.status_code == status
    assert len(request_lines) == 1
    line = request_lines[0]
    assert line["trace_id"] == res.headers["X-Trace-Id"]
    assert (line["status"], line["level"]) == (status, level)
    assert "duration_ms" in line and line["method"] == "GET"


def test_unhandled_error_is_one_line_with_trace(request_lines, monkeypatch):
    from app.services import meta

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(meta, "meta_view", boom)
    res = client.get("/api/meta")
    assert res.status_code == 500
    assert error_of(res)["code"] == "INTERNAL_ERROR"
    assert len(request_lines) == 1                           # 바깥 처리기가 한 줄 더 쓰지 않는다
    line = request_lines[0]
    assert line["trace_id"] == res.headers["X-Trace-Id"]     # 500 에도 trace_id 가 있다
    assert (line["status"], line["level"]) == (500, "error")
    assert line["error"].startswith("RuntimeError")


def test_background_failure_does_not_write_request_line(request_lines, monkeypatch):
    def boom(session_id, *, trace_id):
        raise RuntimeError("x")

    monkeypatch.setattr(sessions, "run_pipeline", boom)
    monkeypatch.setattr(db, "execute", lambda *a, **k: 1)
    deps.run_pipeline_background("ORD-0001", "tr_X")
    assert request_lines == []                               # 분석 실패는 checkpoint_save_failed 다


# --- 로그 event 목록 — 코드 전체가 D-03 1.5 의 고정 목록만 쓴다 (D-16 대조 항목) ---------------------
def test_every_log_call_uses_fixed_event_names():
    import pathlib
    import re

    from app import logging_setup

    root = pathlib.Path(__file__).resolve().parent.parent
    used = set()
    for path in list((root / "app").rglob("*.py")) + list((root / "interpreter").rglob("*.py")):
        used |= set(re.findall(r'logging_setup\.log\(\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert used, "스캔이 아무것도 못 찾으면 검사가 아니다"                  # G-A0 — 빈 결과는 실패
    assert used <= logging_setup.EVENTS, used - logging_setup.EVENTS
    assert len(logging_setup.EVENTS) == 18


def test_unknown_event_is_rejected():
    from app import logging_setup

    with pytest.raises(ValueError):
        logging_setup.log("intake_done")                                   # v1.1 에서 route_decision 으로 합쳤다


def test_ops_login_logs_result_without_password(monkeypatch):
    from app import logging_setup

    seen = []
    monkeypatch.setattr(logging_setup, "log", lambda event, **f: seen.append((event, f)))
    client.post("/api/ops/login", json={"password": "wrong-secret-123"})
    client.post("/api/ops/login", json={"password": "test-ops"})
    logins = [f for e, f in seen if e == "ops_login"]
    assert [f["result"] for f in logins] == ["fail", "success"]
    assert "wrong-secret-123" not in str(seen) and "test-ops" not in str(seen)


# --- 관리 링크 안내 제거 (2026-11-03 요청) -----------------------------------
def test_chat_intro_has_no_manage_link_bubble():
    """첫 말풍선은 요약 한 줄뿐이다 — 관리 링크 안내를 두지 않는다."""
    from app.services import projection

    rows = [{"person": "김민수", "menu_id": "sb_001", "quantity": 1,
             "estimated_price": 4500, "removed": False, "validation_status": "passed"}]
    chat = projection.chat_intro({"status": "needs_review"}, rows, review_count=1)
    assert [m["kind"] for m in chat] == ["summary"]
    assert "관리 링크" not in str(chat)


def test_session_created_view_has_no_manage_link_notice():
    from app.services import projection

    view = projection.session_created_view(
        {"session_id": "ORD-0001", "status": "running", "version": 1,
         "brand_id": "starbucks", "brand_source": "request", "brand_evidence_text": None,
         "menu_version": "mv"}, session_token="tok")
    assert "manage_link_notice" not in view


# --- 메뉴 미정 행의 원문 표기 (2026-11-03) ------------------------------------
def test_unresolved_row_text_is_quoted():
    """메뉴가 안 정해진 행은 따옴표째 나간다 — 화면이 그 따옴표로 "미정" 행을 가린다."""
    from app.services.projection import _quoted

    assert _quoted("그럼 난 그걸로 하나~") == '"그럼 난 그걸로 하나~"'
    assert _quoted('"이미 따옴표"') == '"이미 따옴표"'
    assert _quoted(None) == "" and _quoted("  ") == ""


# --- 공유 카드 이미지 (FR-009 · FR-010 · API-033 format=image) ----------------
def test_share_image_is_a_png_with_the_same_card():
    """이미지와 텍스트는 **같은 카드 구조**에서 나온다 — 둘이 어긋나면 공유 내용이 갈린다."""
    from app.services import share, share_image

    if not share_image.available():
        import pytest as _pytest
        _pytest.skip("한글 폰트가 없는 환경 — image_available 이 거짓이어야 정상이다")

    sheet = _demo_sheet()
    data = share.render_image(sheet)
    assert data[:8] == PNG_MAGIC                 # 진짜 PNG 인가
    assert len(data) > 2000                      # 빈 캔버스가 아니다
    card = share.build_card(sheet)
    assert "아이스 아메리카노 (톨)" in share.render_text(card)
    assert share.view(sheet)["image_available"] is True


def test_share_view_tells_the_truth_about_images():
    """`image_available` 은 폰트 유무와 **항상 같아야** 한다 — 참이면 깨진 그림이 나간다."""
    from app.services import share, share_image

    assert share.view(_demo_sheet())["image_available"] == share_image.available()
    assert share.view(_demo_sheet())["image_filename"].endswith(".png")


def test_share_image_wraps_long_text_instead_of_overflowing():
    """긴 기타 요청은 줄을 나눠 담는다 — 한글은 공백이 없어도 카드 밖으로 넘친다."""
    from app.services import share_image

    if not share_image.available():
        import pytest as _pytest
        _pytest.skip("한글 폰트가 없는 환경")

    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = share_image._load(share_image._px(12))
    long_korean = "얼음은컵반만채워주시고빨대는빼주시고개인텀블러에담아주세요" * 2
    lines = share_image._wrap(draw, long_korean, font, 400)
    assert len(lines) > 1
    assert all(draw.textlength(line, font=font) <= 400 for line in lines)
    assert "".join(lines) == long_korean              # 글자를 잃지 않는다
