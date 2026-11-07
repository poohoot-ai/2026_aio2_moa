"""도구줄(실습) 장애 주입 — F-3 이 실제로 컨테이너를 멈추는지 (2026-11-07).

여기가 비어 있던 동안 F-3 버튼은 `fault_injection` 에 행만 쓰고 아무도 읽지 않았다.
버튼은 눌리고 토스트도 떴으므로 **화면만으로는 정상과 구분되지 않았다** — 회귀로 잠근다.

도구줄과 시험 층의 경계도 함께 잠근다. 도구줄 주입은 run_id 가 없고(= 실제 사건으로 센다),
시험 회차는 run_id 가 있다(= 상시 감시가 비켜선다).
"""
import pytest

from app.services import incidents
from healing import __main__ as worker


def test_toolbar_f3_targets_a_real_container():
    target, mode = incidents.FAULT_TARGETS["F-3"]
    assert mode == "kill"
    # 상징적 이름(agent_process)은 집행할 주체가 없다. 허용목록 안의 실제 컨테이너여야 한다
    assert target in ("moa-a-backend-1", "moa-b-backend-1")


@pytest.fixture
def lab(monkeypatch):
    state = {"rows": [], "actions": [], "cleared": [], "ready": True, "test_run": False}

    monkeypatch.setattr(worker.config, "ENVIRONMENT", "lab")
    monkeypatch.setattr(worker.db, "fetch_all", lambda *a, **k: list(state["rows"]))
    monkeypatch.setattr(worker.db, "execute",
                        lambda sql, params=None, **k: state["cleared"].append(params[0]))
    monkeypatch.setattr(worker, "_ready", lambda target, timeout=2.0: state["ready"])
    monkeypatch.setattr(worker.monitor, "test_injection_active", lambda t: state["test_run"])

    def action(target, act):
        state["actions"].append((target, act))
        return True, f"Docker {act} accepted"

    monkeypatch.setattr(worker, "_docker_action", action)
    return state


def _row(target="moa-a-backend-1"):
    return {"injection_id": "fi_1", "target": target}


def test_kill_injection_stops_the_container(lab):
    lab["rows"] = [_row()]
    worker._lab_kill_watch()
    assert lab["actions"] == [("moa-a-backend-1", "stop")]


def test_kill_injection_is_closed_right_away(lab):
    """중단은 유지되는 모드가 아니라 한 번 일어나는 사건이다.

    닫지 않으면 `active_injections` 에 남아 [해제]를 누를 때까지 표시가 붙고,
    되살아난 뒤에도 '주입 중'으로 보인다.
    """
    lab["rows"] = [_row()]
    worker._lab_kill_watch()
    assert lab["cleared"] == ["fi_1"]


def test_both_backends_are_never_down_together(lab):
    lab["ready"] = False          # 반대쪽이 이미 죽어 있다
    lab["rows"] = [_row()]
    worker._lab_kill_watch()
    assert lab["actions"] == []
    assert lab["cleared"] == ["fi_1"]      # 집행하지 않아도 행은 닫는다 — 영원히 재시도하지 않는다


def test_a_trial_run_keeps_its_own_target(lab):
    """시험이 같은 대상을 쥐고 있으면 끼어들지 않는다 — 둘이 같이 재시작하면 측정이 깨진다."""
    lab["test_run"] = True
    lab["rows"] = [_row()]
    worker._lab_kill_watch()
    assert lab["actions"] == []
    assert lab["cleared"] == []            # 다음 순회에 다시 본다


def test_nothing_runs_outside_lab(lab, monkeypatch):
    monkeypatch.setattr(worker.config, "ENVIRONMENT", "prod")
    lab["rows"] = [_row()]
    worker._lab_kill_watch()
    assert lab["actions"] == [] and lab["cleared"] == []


# --- MTTD 가 측정되는 경로 -------------------------------------------------------------
# 주입과 사건을 묶는 코드가 시험 워크플로에만 있었고, 그 사건은 v_metrics 에서 빠진다.
# 그래서 대시보드 B "탐지까지" 칸이 구조적으로 항상 비어 있었다(실측 mttd_sec: null · 사건 11건).
from app.services import monitor as monitor_service     # noqa: E402


@pytest.fixture
def fake_link_db(monkeypatch):
    state = {"row": {"injection_id": "fi_9", "injected_at": "2026-11-07T00:00:00+00:00"},
             "updates": []}
    monkeypatch.setattr(monitor_service.db, "fetch_one", lambda *a, **k: state["row"])
    monkeypatch.setattr(monitor_service.db, "execute",
                        lambda sql, params=None, **k: state["updates"].append((sql.split()[1], params)))
    return state


def test_a_toolbar_injection_fills_the_detection_start(fake_link_db):
    assert monitor_service.link_lab_injection("INC-1", "moa-a-backend-1") is True
    updated = [u[1] for u in fake_link_db["updates"]]
    assert ("INC-1", "fi_9") in updated                      # 주입 → 사건
    assert ("2026-11-07T00:00:00+00:00", "INC-1") in updated  # 사건 → 주입 시각


def test_a_spontaneous_fault_keeps_an_empty_detection_start(fake_link_db):
    """저절로 난 장애에는 주입 행이 없다. 시각을 지어내지 않고 MTTD 평균에서 빠진다."""
    fake_link_db["row"] = None
    assert monitor_service.link_lab_injection("INC-2", "order_interpreter_primary") is False
    assert fake_link_db["updates"] == []


# --- 화면이 [해제] 를 그릴 수 있는가 ----------------------------------------------
# 주입은 됐는데 상태 API 가 그 사실을 알려주지 않으면 T1 에 해제 버튼이 나오지 않는다.
# 2026-11-07 실측에서 실제로 그랬다 — 주입 1건이 걸린 채 화면에는 안내 줄만 떠 있었다.
def test_status_tells_the_screen_about_a_toolbar_injection(monkeypatch):
    row = {"fault": "F-1", "target": "order_interpreter_primary", "mode": "error",
           "injected_at": None, "elapsed": 12.7}
    monkeypatch.setattr(incidents.db, "fetch_one", lambda *a, **k: row)
    assert incidents.active_injection() == {
        "code": "F-1", "target": "order_interpreter_primary", "mode": "error", "elapsed_sec": 12}


def test_no_injection_is_not_an_empty_injection(monkeypatch):
    monkeypatch.setattr(incidents.db, "fetch_one", lambda *a, **k: None)
    assert incidents.active_injection() is None


def test_a_broken_store_does_not_blank_the_status_block(monkeypatch):
    def boom(*a, **k):
        raise incidents.db.DbUnavailable("down")
    monkeypatch.setattr(incidents.db, "fetch_one", boom)
    assert incidents.active_injection() is None


# --- 승인 뒤 복구가 이어지는가 (2026-11-07) -------------------------------------------
# `healing.approval` 을 읽는 코드가 API 에만 있어, [승인]을 눌러도 상태만 recovering 으로
# 바뀌고 아무도 복구를 시도하지 않았다. 그 사건은 닫히지 않고 같은 장애의 새 탐지까지 막았다.
@pytest.fixture
def approved(monkeypatch):
    state = {"rows": [], "closed": [], "actions": [], "synthetic": (True, "합성 요청 성공", 0),
             "sql": []}

    def fetch_all(sql, params=None, **k):
        state["sql"].append(sql)
        return list(state["rows"])

    monkeypatch.setattr(worker.db, "fetch_all", fetch_all)
    monkeypatch.setattr(worker, "_close_incident",
                        lambda iid, *, recovered, detail: state["closed"].append((iid, recovered)))
    monkeypatch.setattr(worker, "_synthetic_interpreter_request",
                        lambda *a, **k: state["synthetic"])
    monkeypatch.setattr(worker, "_docker_action",
                        lambda t, a: (state["actions"].append((t, a)), (True, "ok"))[1])
    monkeypatch.setattr(worker, "_ready", lambda t, timeout=2.0: True)
    return state


def test_an_approved_interpreter_fault_is_retried_and_closed(approved):
    approved["rows"] = [{"incident_id": "INC-1", "fault_code": "DEP_HTTP_5XX",
                         "target": "order_interpreter_primary"}]
    worker._approval_watch()
    assert approved["closed"] == [("INC-1", True)]


def test_an_approved_backend_fault_restarts_the_container(approved):
    approved["rows"] = [{"incident_id": "INC-2", "fault_code": "WORKER_DOWN",
                         "target": "moa-a-backend-1"}]
    worker._approval_watch()
    assert approved["actions"] == [("moa-a-backend-1", "restart")]
    assert approved["closed"] == [("INC-2", True)]


def test_an_unknown_target_is_not_reported_as_recovered(approved):
    """대상을 모르면 복구했다고 적지 않는다 — 지어낸 성공이 무인 복구율을 부풀린다."""
    approved["rows"] = [{"incident_id": "INC-3", "fault_code": "UNKNOWN", "target": None}]
    worker._approval_watch()
    assert approved["closed"] == [("INC-3", False)]


def test_a_live_trial_keeps_its_own_incident(approved):
    """살아 있는 시험이 쥔 사건은 건드리지 않는다 — 그쪽 주인은 `_run` 이다."""
    worker._approval_watch()
    sql = approved["sql"][0]
    assert "workflow_run" in sql and "'running'" in sql
    assert "INTERVAL '5 seconds'" in sql      # 거부로 바꿔 누를 틈을 준다
