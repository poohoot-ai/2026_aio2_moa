"""5.6.1 유형별 감지 메트릭 — 업무 층이 적고 Monitor 가 읽는다 (평가 P3-2).

경계(PRD 5.3 v1.1): 업무 층은 **관측한 사실만** 적는다. 사건 생성 · 복구 판단은 장애 대응 층이 한다.
"""
import pytest

from app import config, db, interpreter_client
from app.services import metrics


@pytest.fixture
def written(monkeypatch):
    rows = []
    monkeypatch.setattr(config, "db_configured", lambda: True)
    monkeypatch.setattr(metrics.db, "execute", lambda sql, params=None, **k: rows.append((sql, params)))
    return rows


def _params(rows):
    return [dict(zip(("metric_key", "target", "value", "threshold", "breached", "trace_id"), p))
            for _, p in rows]


# --- 기록 자체 -------------------------------------------------------------------------------
def test_record_writes_one_runtime_metric_row(written):
    metrics.record("timeout_count", "order_interpreter_primary", 1, threshold=1, breached=True,
                   trace_id="tr_1")
    assert len(written) == 1
    sql, _ = written[0]
    assert "healing.runtime_metric" in sql and "INSERT" in sql
    assert _params(written)[0] == {"metric_key": "timeout_count", "target": "order_interpreter_primary",
                                   "value": 1, "threshold": 1, "breached": True, "trace_id": "tr_1"}


def test_record_rejects_a_key_the_schema_does_not_allow(written):
    # DB CHECK 제약에 없는 키는 여기서 걸러야 한다 — 런타임에 조용히 삼켜지면 메트릭이 비어 있는 줄 모른다
    with pytest.raises(ValueError):
        metrics.record("made_up_key", "x", 1)
    assert written == []


def test_record_never_breaks_the_caller(monkeypatch):
    """기록 실패는 삼킨다 — 메트릭 때문에 주문 요청이 깨지면 안 된다. F-6 중에는 DB 가 아예 없다."""
    monkeypatch.setattr(config, "db_configured", lambda: True)

    def boom(*a, **k):
        raise db.DbUnavailable("DB 없음")

    monkeypatch.setattr(metrics.db, "execute", boom)
    metrics.record("db_connection_error", "menu_store", 1, breached=True)   # 예외가 새지 않는다


def test_record_skips_when_database_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "db_configured", lambda: False)
    called = []
    monkeypatch.setattr(metrics.db, "execute", lambda *a, **k: called.append(1))
    metrics.record("timeout_count", "x", 1)
    assert called == []


# --- 해석 서비스 관측 (F-1 · F-2 · F-5) --------------------------------------------------------
def test_interpreter_timeout_is_recorded_as_timeout_count(written):
    metrics.observe_interpreter("DEP_TIMEOUT", "primary", 5200, trace_id="tr_2")
    keys = {p["metric_key"]: p for p in _params(written)}
    assert keys["timeout_count"]["breached"] is True
    assert keys["timeout_count"]["target"] == "order_interpreter_primary"
    assert keys["dep_latency_ms"]["value"] == 5200          # 지연도 함께 남긴다


def test_interpreter_5xx_is_recorded_as_dep_5xx_ratio(written):
    metrics.observe_interpreter("DEP_HTTP_5XX", "backup", 120)
    keys = {p["metric_key"]: p for p in _params(written)}
    assert keys["dep_5xx_ratio"]["breached"] is True
    assert keys["dep_5xx_ratio"]["target"] == "order_interpreter_backup"


def test_interpreter_invalid_output_is_recorded_as_schema_error_rate(written):
    metrics.observe_interpreter("AGENT_OUTPUT_INVALID", "primary", 300)
    assert {p["metric_key"] for p in _params(written)} >= {"schema_error_rate", "dep_latency_ms"}


def test_interpreter_success_records_latency_only(written):
    metrics.observe_interpreter(None, "primary", 800)
    rows = _params(written)
    assert [p["metric_key"] for p in rows] == ["dep_latency_ms"]
    assert rows[0]["breached"] is False                     # 5초 안이면 넘지 않았다


def test_interpreter_success_over_five_seconds_is_breached(written):
    # PRD 5.6.1 API 판정 조건 "호출 5초 초과" — 예외가 안 났어도 넘은 것은 넘은 것이다
    metrics.observe_interpreter(None, "primary", 5001)
    assert _params(written)[0]["breached"] is True


# --- 업무 층 훅이 실제로 걸려 있는가 -----------------------------------------------------------
def test_resilient_call_records_metrics_on_failure_and_success(monkeypatch):
    seen = []
    monkeypatch.setattr(metrics, "observe_interpreter",
                        lambda code, service, ms, **k: seen.append((code, service)))
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "http://backup")
    monkeypatch.setattr(interpreter_client, "_circuit_blocked", lambda s: False)
    monkeypatch.setattr(interpreter_client, "_circuit_record", lambda *a, **k: None)

    calls = []

    def fake_interpret(task, messages, context, *, service="primary", **k):
        calls.append(service)
        if service == "primary":
            raise interpreter_client.InterpreterError("DEP_HTTP_5XX", "x", service=service)
        return object()

    monkeypatch.setattr(interpreter_client, "interpret", fake_interpret)
    interpreter_client.interpret_resilient("extract_events", [], {}, service="primary")

    assert calls == ["primary", "backup"]
    assert seen == [("DEP_HTTP_5XX", "primary"), (None, "backup")]


# --- F-6 저장소 메트릭 (필수 FT-03) ----------------------------------------------------------
def test_slow_query_is_recorded_once_it_passes_two_seconds(monkeypatch, written):
    """PRD 5.6.1 DB 판정 조건 "쿼리 2초 초과"."""
    assert db.SLOW_QUERY_SEC == 2.0
    now = [100.0]
    monkeypatch.setattr(db.time, "monotonic", lambda: now[0])
    now[0] = 102.5
    db._watch_query("SELECT 1 FROM app.menu", started=100.0)
    rows = _params(written)
    assert [r["metric_key"] for r in rows] == ["slow_query_count"]
    assert rows[0]["target"] == "menu_store" and rows[0]["breached"] is True


def test_a_fast_query_is_not_recorded(monkeypatch, written):
    monkeypatch.setattr(db.time, "monotonic", lambda: 100.3)
    db._watch_query("SELECT 1 FROM app.menu", started=100.0)
    assert written == []


def test_writing_a_metric_never_measures_itself(monkeypatch, written):
    """메트릭 기록 자체가 DB 쓰기다 — 메트릭 표를 건드리는 쿼리를 세면 무한 재귀한다."""
    monkeypatch.setattr(db.time, "monotonic", lambda: 110.0)
    db._watch_query("INSERT INTO healing.runtime_metric(metric_key,target) VALUES (%s,%s)",
                    started=100.0)
    assert written == []
