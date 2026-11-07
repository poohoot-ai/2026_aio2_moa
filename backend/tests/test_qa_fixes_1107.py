"""2026-11-07 QA 재테스트 요청 — 꼬리말 사전 · 로그 안전성 · 기본 비밀번호.

시험 전용 합성 문자열만 쓴다. 실제 API 키 · 비밀번호 · 사용자 대화를 시험 데이터로 쓰지 않는다.
"""
import importlib
import re

import pytest

from app import config, logging_setup
from app.api import deps
from app.services import intake, menu, ops_auth, sessions
from tests import seed_catalog

SENTINEL = "SYNTHETIC_SECRET_CHAT_PROMPT_SENTINEL"


# --- 공손 어미 "부탁합니다" (QA 1번) ----------------------------------------------------------
# 실측 2026-11-06: "아이스 아메리카노 벤티 1잔 부탁합니다" 가 미등록으로 떨어져 되묻기가 떴다.
# 꼬리말 사전에 `부탁드립니다` · `부탁해요` 는 있는데 `부탁합니다` 가 없었고, `부탁` 단독은
# 한글 사이에서 `\b` 가 성립하지 않아 걸리지 않았다. 엄격 매칭(strict)은 남은 글자 때문에 확정을 거부한다.
@pytest.mark.parametrize("said", [
    "아이스 아메리카노 벤티 1잔 부탁합니다.",
    "아이스 아메리카노 벤티 1잔 부탁해요.",
    "아이스 아메리카노 벤티 1잔 부탁드립니다.",
    "아이스 아메리카노 벤티 1잔 부탁할게요.",
    "아이스 아메리카노 벤티 1잔 부탁드릴게요.",
    "아이스 아메리카노 벤티 1잔 부탁해.",
])
def test_polite_order_endings_leave_only_the_menu(said):
    assert intake._menu_text(said) == "아이스 아메리카노", said


@pytest.mark.parametrize("said", [
    "아이스 아메리카노 벤티 1잔 부탁합니다.",
    "아이스 아메리카노 벤티 1잔 부탁할게요.",
])
def test_a_clear_order_is_not_asked_back(said, monkeypatch):
    seed_catalog.install(monkeypatch)
    """엄격 매칭이 확정해야 한다 — 후보만 남으면 담당자에게 불필요한 되묻기가 간다."""
    hit = menu.match("starbucks", "mv_20261029", intake._menu_text(said), strict=True)
    assert hit.get("menu_id") == "sb_004", hit.get("candidates")


def test_a_real_unregistered_menu_is_still_blocked(monkeypatch):
    seed_catalog.install(monkeypatch)
    """꼬리말을 더 떼도 미등록 차단(SC-8)은 유지된다 — 메뉴판에 없는 말은 확정하지 않는다."""
    hit = menu.match("starbucks", "mv_20261029",
                     intake._menu_text("유니콘 라떼 톨 1잔 부탁합니다"), strict=True)
    assert hit.get("menu_id") is None


# --- H-33 예외 로그의 민감정보 (RETEST-002) ----------------------------------------------------
def test_pipeline_failure_log_keeps_tracing_without_the_exception_text(monkeypatch):
    logged = []
    monkeypatch.setattr(logging_setup, "log", lambda event, **f: logged.append((event, f)))
    monkeypatch.setattr(deps, "_last_checkpoint", lambda _sid: "chat_read")
    monkeypatch.setattr(deps.db, "execute", lambda *a, **k: 0)

    def boom(*a, **k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(sessions, "run_pipeline", boom)
    deps.run_pipeline_background("ORD-9999", "tr_qa1107")

    (event, fields), = [x for x in logged if x[0] == "checkpoint_save_failed"]
    assert event == "checkpoint_save_failed"
    # 예외 전문은 남기지 않는다 — 대화 · 프롬프트 · 비밀값이 섞여 들어오는 경로다
    assert SENTINEL not in repr(fields)
    # 추적 정보는 유지한다
    assert fields["session_id"] == "ORD-9999" and fields["trace_id"] == "tr_qa1107"
    assert fields["step"] == "chat_read"
    assert "RuntimeError" in repr(fields), "오류 유형은 남아야 추적할 수 있다"


# --- H-32 로그 시각 형식 (RETEST-001) ---------------------------------------------------------
def test_log_timestamp_is_utc_with_milliseconds(monkeypatch):
    seen = {}
    monkeypatch.setattr(logging_setup._logger, "info", lambda line, *a, **k: seen.setdefault("line", line))
    logging_setup.log("incident_detected", incident_id="INC-QA1107")
    ts = __import__("json").loads(seen["line"])["ts"]
    # 2026-11-07T01:23:45.678+00:00 — UTC · 밀리초 3자리
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(?:\+00:00|Z)", ts), ts


# --- Q-07 실습 기본 비밀번호 (RETEST-003) -----------------------------------------------------
# PRD R-17 이 "코드의 대체 기본값을 배포 전 제거하거나 .env 설정을 필수로 한다"로 정해 두었다.
def test_no_default_password_when_the_environment_variable_is_missing(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "lab")
    monkeypatch.setenv("OPS_DASHBOARD_PASSWORD", "")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.OPS_DASHBOARD_PASSWORD == ""
        assert reloaded.OPS_PASSWORD_MISSING is True
        monkeypatch.setattr(ops_auth.config, "OPS_DASHBOARD_PASSWORD", "")
        assert ops_auth.login("moa-ops") is None       # 코드 기본값으로 들어오지 못한다
        assert ops_auth.login("") is None
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_a_password_from_the_environment_still_logs_in(monkeypatch):
    monkeypatch.setattr(ops_auth.config, "OPS_DASHBOARD_PASSWORD", "qa-1107-synthetic-pw")
    assert ops_auth.login("qa-1107-synthetic-pw") is not None
    assert ops_auth.login("wrong-value") is None
