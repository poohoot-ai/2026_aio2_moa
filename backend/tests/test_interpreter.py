# 주문 해석 서비스(DEP-001) 와 호출부 — 모드 4종 · 계약 검증 · 장애 코드 (D-02 6.1)
#
# 서비스 앱을 TestClient 로 띄우고, 호출부(interpreter_client)가 그 앱을 부르게 바꿔 끼운다.
# 양방향으로 본다(G-A0): 정상 모드는 계약을 통과하고, invalid 모드는 **반드시** F-5 로 잡힌다.
import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, interpreter_client
from app.contracts import OrderEvents
from interpreter import main as svc

MESSAGES = [
    {"message_id": "m1", "order": 1, "sender": "김민수", "text": "아이스 아메리카노 톨 2잔"},
    {"message_id": "m2", "order": 2, "sender": "이지연", "text": "여기 인기 메뉴 뭐야?"},
    {"message_id": "m3", "order": 3, "sender": "박서연", "text": "나도 같은 걸로"},
]
CONTEXT = {"brand_id": "starbucks", "menu_version": "mv_20261029"}


@pytest.fixture
def service(monkeypatch):
    """모드를 바꿀 수 있는 1차 서비스. 호출부의 httpx.post 를 이 앱으로 돌린다."""
    app_client = TestClient(svc.app, raise_server_exceptions=False)
    monkeypatch.setattr(svc, "SOURCE", "rules")
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(url)
        return app_client.post(url.replace("http://primary", ""), json=json)

    monkeypatch.setattr(interpreter_client.httpx, "post", post)

    def set_mode(mode):
        monkeypatch.setattr(svc, "MODE_OVERRIDE", mode)

    set_mode("normal")
    return app_client, set_mode, calls


def test_normal_mode_returns_contract_valid_events(service):
    _, _, calls = service
    parsed = interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    assert isinstance(parsed, OrderEvents)
    assert [e.event_type for e in parsed.events] == ["add", "ignore", "add"]
    assert parsed.events[0].items[0].quantity == 2
    assert parsed.events[0].items[0].size_text == "톨"
    assert parsed.events[2].is_reference is True
    assert len(calls) == 1                                  # 재시도 · 대체 전환을 하지 않는다


def test_rules_source_is_deterministic(service):
    first = interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    second = interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    assert first == second                                  # 같은 입력에 같은 결과 (NFR-006)


def test_error_mode_is_dep_http_5xx(service):
    app_client, set_mode, calls = service
    set_mode("error")
    with pytest.raises(interpreter_client.InterpreterError) as caught:
        interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    assert caught.value.fault_code == "DEP_HTTP_5XX"
    assert len(calls) == 1
    assert app_client.get("/health").status_code == 503      # 감시자가 볼 신호


def test_invalid_mode_is_agent_output_invalid(service):
    _, set_mode, _ = service
    set_mode("invalid")
    with pytest.raises(interpreter_client.InterpreterError) as caught:
        interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    assert caught.value.fault_code == "AGENT_OUTPUT_INVALID"
    assert "events.0.event_type" in caught.value.missing_fields   # FR-025 "빠진 필드"


def test_delay_mode_sleeps_past_timeout(service, monkeypatch):
    _, set_mode, _ = service
    set_mode("delay")
    slept = []
    monkeypatch.setattr(svc.time, "sleep", slept.append)   # 대기를 0 으로 (4.16 규약 3)
    interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    assert slept == [svc.DELAY_SEC] and svc.DELAY_SEC > config.ORDER_INTERPRETER_TIMEOUT_SEC


def test_client_timeout_is_dep_timeout(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")

    def slow(url, json=None, timeout=None):
        assert timeout == config.ORDER_INTERPRETER_TIMEOUT_SEC == 5
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(interpreter_client.httpx, "post", slow)
    with pytest.raises(interpreter_client.InterpreterError) as caught:
        interpreter_client.interpret("extract_events", MESSAGES, CONTEXT)
    assert caught.value.fault_code == "DEP_TIMEOUT"


def test_unset_url_is_unknown(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "")
    with pytest.raises(interpreter_client.InterpreterError) as caught:
        interpreter_client.interpret("extract_events", MESSAGES, CONTEXT, service="backup")
    assert caught.value.fault_code == "UNKNOWN"


def test_unknown_task_rejected(service):
    app_client, _, _ = service
    assert app_client.post("/interpret", json={"task": "nope"}).status_code == 400


def test_llm_error_maps_to_status(service, monkeypatch):
    app_client, _, _ = service
    from app import llm

    monkeypatch.setattr(svc, "SOURCE", "llm")

    def boom(*a, **k):
        raise llm.LLMError("한도", code="limit_reached")

    monkeypatch.setattr(svc.llm, "generate_structured", boom)
    res = app_client.post("/interpret", json={"task": "extract_events", "messages": MESSAGES,
                                              "context": CONTEXT})
    assert res.status_code == 503 and res.json()["llm_error_code"] == "limit_reached"


def test_health_reports_interpreters(monkeypatch):
    from app.api import health
    from app.main import app

    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "")
    monkeypatch.setattr(health, "_ping", lambda url: "down" if url else "unset")
    body = TestClient(app).get("/health").json()
    assert body["checks"]["order_interpreter"]["primary"] == "down"
    assert body["checks"]["order_interpreter"]["backup"] == "unset"
    assert body["checks"]["llm"]["provider"] == config.LLM_PROVIDER
    assert body["status"] == "degraded"                       # 의존 서비스 장애는 503 이 아니다


# --- PII — LLM 으로 나가는 요청 본문에 원본 식별정보 0건 (D-13 · 양방향) -------------------------
def test_request_body_has_no_raw_pii(monkeypatch):
    sent = {}
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")

    def post(url, json=None, timeout=None):
        sent.update(json)
        return httpx.Response(200, json={"result": {"events": []}, "model": "rules"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(interpreter_client.httpx, "post", post)
    messages = [
        {"message_id": "m1", "sender": "김민수", "text": "아아 톨 2잔, 010-1234-5678 로 연락줘"},
        {"message_id": "m2", "sender": "이지연", "text": "국민 123-45-678901 로 보낼게요"},
        {"message_id": "m3", "sender": "박서연", "text": "2026-11-03 오후 2시 픽업, 라떼 그란데 1잔"},
    ]
    context = {"brand_id": "starbucks", "menu_version": "mv_20261029",
               "existing_items": [{"item_id": "ORD-0001-it_01", "custom_request": "a.b@example.com"}]}
    interpreter_client.interpret("extract_events", messages, context)

    body = str(sent)
    for raw in ("010-1234-5678", "123-45-678901", "a.b@example.com"):
        assert raw not in body                                               # 가릴 것은 가렸다
    # 가리지 않아야 할 것 — 주문 · 일정 · 근거 연결 · 매장 기준
    texts = [m["text"] for m in sent["messages"]]
    assert texts[2] == "2026-11-03 오후 2시 픽업, 라떼 그란데 1잔"
    assert "아아 톨 2잔" in texts[0]
    assert [m["message_id"] for m in sent["messages"]] == ["m1", "m2", "m3"]
    assert sent["context"]["brand_id"] == "starbucks"
    assert sent["context"]["existing_items"][0]["item_id"] == "ORD-0001-it_01"
    assert "010-1234-5678" in messages[0]["text"]                            # 호출부의 원본은 그대로


# --- 회로 차단은 냉각 뒤 시험 요청 1회를 허용한다 (PRD 5.6 F-4) ------------------------------
# 실측 결함 2026-11-06: `_circuit_blocked` 가 `cooldown_until` 을 읽고도 쓰지 않아,
# state='circuit_open' 이면 냉각이 지난 뒤에도 영구히 막았다. 회로를 닫는 코드는 healing 의
# `_probe_circuit` 하나이고 그것도 F-4 분기에서만 불리므로, 1차 해석 서비스가 한 번 열리면
# F-4 시험을 돌릴 때까지 **모든 주문 해석이 대체 서비스로** 갔다(실측: 1시간 반 동안 열린 채).
# 그 상태에서는 F-1 시험도 1차를 아예 부르지 않아 "5xx 감지 → 전환"을 검증하지 못한다.
# PRD 5.6 F-4 정책은 "회로 차단 · 자동 복구 중단 → 60초 후 시험 요청 1회 → 실패 시 운영자 승인"이다.
@pytest.mark.parametrize("state,cooldown_sec,blocked", [
    ("up", None, False),
    ("degraded", None, False),
    ("circuit_open", 30, True),      # 냉각 중 — 막는다
    ("circuit_open", -1, False),     # 냉각이 지났다 — 시험 요청 1회를 통과시킨다
    ("circuit_open", None, True),    # 냉각 값이 없으면 막아 둔다 (_circuit_record 는 열 때 항상 넣는다)
    ("down", None, True),
    ("probing", None, True),         # healing 이 시험 요청 중 — 업무 층은 끼어들지 않는다
])
def test_circuit_blocked_lets_one_trial_through_after_cooldown(monkeypatch, state, cooldown_sec, blocked):
    import datetime as _dt

    from app import db

    cooldown = (None if cooldown_sec is None else
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=cooldown_sec))
    monkeypatch.setattr(config, "db_configured", lambda: True)
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: {"state": state, "cooldown_until": cooldown})
    assert interpreter_client._circuit_blocked("primary") is blocked
