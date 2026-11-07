"""PRD v1.9 반영 (2026-11-05) — 처리 계획(5.9.3) · F-5 재생성 한도 · 운영 비밀번호 · 스키마 문자열.

장애 대응 층(6역할)은 대상이 아니다. 업무 층 · 스키마 · 설정에서 바뀐 것만 본다.
"""

import importlib
import pathlib

import pytest

from app import config, interpreter_client, logging_setup
from app.graph import nodes
from tests.test_graph import _batch, _contract, interpreter, msgs  # noqa: F401 — fixture 재사용

SQL = pathlib.Path(__file__).resolve().parent.parent / "sql"


# --- 처리 계획 (Planner · N-02) ---------------------------------------------------------------
def test_plan_records_what_batch_node_decided(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(config, "MAX_LLM_CALLS_PER_RUN", 6)                # 2묶음까지 LLM
    monkeypatch.setattr(nodes, "_pick_service", lambda: "primary")
    logged = []
    monkeypatch.setattr(logging_setup, "log", lambda event, **f: logged.append((event, f)))

    out = nodes.batch_node({"messages": msgs(12), "source": "chat", "trace_id": "tr_P"})
    plan = out["plan"]

    assert plan["plan_id"].startswith("pl_")
    assert plan["service"] == "primary" and plan["resumed"] is False
    assert plan["batch_count"] == 3 and plan["llm_batches"] == [1, 2] and plan["rule_batches"] == [3]
    assert plan["done_when"] == ["every_message_has_event", "schema_valid", "checkpoint_chat_read"]
    # 계획은 batch_node 의 결정을 바꾸지 않는다 — 묶음 배정이 계획과 같다
    assert [b["use_llm"] for b in out["batches"]] == [True, True, False]
    # 로그는 route_decision(stage=plan) · source 키 없음 — 분류 결과 로그와 섞이지 않는다
    event, fields = logged[-1]
    assert event == "route_decision" and fields["stage"] == "plan" and "source" not in fields


def test_plan_without_interpreter_is_rules_only(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "")
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "")
    plan = nodes.batch_node({"messages": msgs(3)})["plan"]
    assert plan["service"] is None and plan["llm_batches"] == [] and plan["rule_batches"] == [1]


def test_plan_key_is_in_run_state():
    # State 에 없는 키는 LangGraph 가 조용히 버린다(CLAUDE.md "에러 없이 반쪽")
    from app.graph.state import RunState
    assert "plan" in RunState.__annotations__


# --- F-5 형식 오류 — 재생성 2회 → 대체 모델 1회 → NEEDS_REVIEW (승인 PRD FR-026 · FT-04) -------
# 저장소 docs/PRD.md v1.3 의 F-5 행은 "재생성 2회 → NEEDS_REVIEW"만 적어 대체 모델 단계가 빠져 있다.
# 승인 PRD(루트 PRD.md) FR-026 은 "같은 입력으로 최대 2회 재생성 → 대체 모델로 1회 → 넘으면 규칙 해석
# 폴백 또는 NEEDS_REVIEW"를 P0 필수로 적는다. 정본을 따라 대체 모델 재요청 1회를 되살렸다(2026-11-06).
def test_invalid_output_switches_to_backup_once_after_regenerate_exhausted(interpreter, monkeypatch):
    monkeypatch.setattr(nodes, "_backup_ready", lambda: True)
    attempted_services = []
    original = interpreter_client.interpret_resilient

    def record_service(*args, **kwargs):
        attempted_services.append(kwargs.get("service", "primary"))
        return original(*args, **kwargs)

    monkeypatch.setattr(interpreter_client, "interpret_resilient", record_service)
    bad = interpreter_client.InterpreterError("AGENT_OUTPUT_INVALID", "x", service="primary")
    interpreter.extend([bad, bad, bad, bad])
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 4                        # 최초 1 + 재생성 2 + 대체 모델 1 (FR-026)
    assert attempted_services == ["primary"] * 3 + ["backup"]
    assert out["events"][0]["event_type"] == "ignore"   # 대체 모델까지 실패하면 담당자에게 넘긴다
    assert out["degraded"][0]["needs_review"] is True


def test_backup_model_retry_result_is_used_when_it_passes(interpreter, monkeypatch):
    monkeypatch.setattr(nodes, "_backup_ready", lambda: True)
    bad = interpreter_client.InterpreterError("AGENT_OUTPUT_INVALID", "x", service="primary")
    interpreter.extend([bad, bad, bad, _contract("m1", event_type="ignore")])
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 4
    assert not out.get("degraded")                      # 대체 모델 해석이 통과하면 그 결과를 쓴다


def test_backup_model_retry_is_logged(interpreter, monkeypatch):
    monkeypatch.setattr(nodes, "_backup_ready", lambda: True)
    logged = []
    monkeypatch.setattr(logging_setup, "log", lambda event, **f: logged.append((event, f)))
    bad = interpreter_client.InterpreterError("AGENT_OUTPUT_INVALID", "x", service="primary")
    interpreter.extend([bad, bad, bad, bad])
    nodes.intake_node(_batch(msgs(1)))
    # 로그 event 는 고정 목록이다 — 새 이름 대신 schema_invalid 에 detail 로 구분한다(D-03 결정 6)
    assert any(e == "schema_invalid" and f.get("detail") == "backup_model_retry" for e, f in logged)


def test_invalid_output_after_regeneration_is_not_replaced_by_rules(interpreter, monkeypatch):
    monkeypatch.setattr(nodes, "_backup_ready", lambda: True)
    bad = interpreter_client.InterpreterError("AGENT_OUTPUT_INVALID", "x", service="primary")
    interpreter.extend([bad, bad, bad, bad])
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 4
    assert out["degraded"][0]["reason"] == "regenerate_exhausted"
    assert out["events"][0]["event_type"] == "ignore"
    assert out["degraded"][0]["needs_review"] is True


def test_invalid_output_without_backup_still_marks_review(interpreter, monkeypatch):
    monkeypatch.setattr(nodes, "_backup_ready", lambda: False)
    bad = interpreter_client.InterpreterError("AGENT_OUTPUT_INVALID", "x", service="primary")
    interpreter.extend([bad, bad, bad])
    out = nodes.intake_node(_batch(msgs(1)))
    assert out["llm_calls"] == 3
    assert out["events"][0]["event_type"] == "ignore"
    assert out["degraded"][0]["needs_review"] is True


def test_backup_ready_needs_address(monkeypatch):
    monkeypatch.setattr(config, "ORDER_INTERPRETER_BACKUP_URL", "")
    assert nodes._backup_ready() is False


# --- 운영 대시보드 비밀번호 기본값 (R-17) -------------------------------------------------------
# 종전에는 lab 에서 코드의 짧은 고정 문자열로 로그인됐다. PRD R-17 이 "배포 전 제거하거나
# .env 설정을 필수로 한다"로 정해 두었으므로 없앴다 — 환경과 무관하게 값이 없으면 막는다
@pytest.mark.parametrize("env, value, expected, missing", [
    ("lab", "", "", True),
    ("lab", "long-enough-pw", "long-enough-pw", False),
    ("prod", "", "", True),
    ("prod", "long-enough-pw", "long-enough-pw", False),
])
def test_ops_password_comes_only_from_the_environment(monkeypatch, env, value, expected, missing):
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("OPS_DASHBOARD_PASSWORD", value)
    try:
        reloaded = importlib.reload(config)
        assert reloaded.OPS_DASHBOARD_PASSWORD == expected
        assert reloaded.OPS_PASSWORD_MISSING is missing
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# --- 스키마 문자열 (DB 없이 파일로 확인) -------------------------------------------------------
def test_healing_schema_has_v19_codes_and_metrics():
    text = (SQL / "03_schema_healing.sql").read_text(encoding="utf-8")
    for code in ("AGENT_EXCEPTION", "PROMPT_INJECTION"):
        assert text.count(code) >= 2                                       # CREATE + 기존 DB 용 ALTER
    for key in ("timeout_count", "agent_error_count", "restart_count", "db_connection_error",
                "slow_query_count", "schema_error_rate", "validation_fail_rate", "injection_detect_count"):
        assert text.count(f"'{key}'") >= 2
    assert "ADD COLUMN IF NOT EXISTS recovery_plan JSONB" in text


def test_metrics_view_mttr_follows_sc3_and_appends_new_column_last():
    text = (SQL / "04_views.sql").read_text(encoding="utf-8")
    view = text[text.index("CREATE OR REPLACE VIEW healing.v_metrics"):]
    view = view[:view.index(";")]
    # 시작점에 first_signal_at 이 끼어들었다(2026-11-07) — 저절로 난 장애의 **첫 징후** 시각이다.
    # SC-3 의 뜻(복구까지 = 장애 시작부터)은 그대로고, 주입 시각이 없을 때 더 이른 관측을 쓴다.
    assert "COALESCE(i.injected_at, f.injected_at, i.first_signal_at, i.detected_at)" in view
    # MTTD 도 같은 기준이어야 한다 — 주입 시각이 없으면 첫 징후부터 잰다
    assert "COALESCE(i.injected_at, f.injected_at, i.first_signal_at)" in view
    # CREATE OR REPLACE VIEW 는 기존 칸 순서를 바꿀 수 없다 — 새 칸은 맨 끝
    select_part = view[:view.index("FROM healing.incident")]
    assert select_part.rstrip().endswith("AS mttr_excl_pause_sec")


def test_brand_seed_has_gongcha():
    text = (SQL / "10_seed_brand.sql").read_text(encoding="utf-8")
    assert "('공차'," in text
