"""2026-11-07 전 구간 테스트에서 드러난 결함 — **실제 사건의 복구 시도 이력이 0건**이었다.

`healing.recovery_attempt` 는 스키마에도 있고, `v_metrics.avg_attempts` 가 세고,
사건 상세 화면이 읽는데 **INSERT 하는 코드가 어디에도 없었다.** 사건 29건에 0행이었다.

그래서 무엇이 틀렸나:
  - 사건 상세의 "복구 시도"가 늘 비었다 — 사후에 무엇을 해서 복구됐는지 알 수 없다
  - `avg_attempts` 가 늘 `0.00` 이었다. 0 은 "시도 없이 복구됐다"로 읽히는데 실은 **기록이 없다**
  - 복구 시험(T2)만 회차 JSON 에 시도를 남겨, 시험에는 증거가 있고 실제 장애에는 없었다

G-A0 — 이 시험은 양방향을 본다. (a) 정상 기록이 들어가는가 (b) 허용값 밖이 거부되는가.
(b)가 없으면 `action` 오타 하나가 DB CHECK 에서 조용히 막히고 또 0행이 된다.
"""
import pytest

from app.services import monitor


# =============================================================================
# 1. 허용값 — 스키마 CHECK 와 코드가 어긋나면 기록이 통째로 사라진다
# =============================================================================
def test_the_allowed_actions_match_the_table_check():
    """`ATTEMPT_ACTIONS` 는 03_schema_healing.sql 의 CHECK 와 같아야 한다."""
    import pathlib
    import re

    sql = pathlib.Path(__file__).resolve().parents[1] / "sql" / "03_schema_healing.sql"
    text = sql.read_text(encoding="utf-8")
    # 절 머리 주석에도 이름이 나오므로 CREATE TABLE 문에서 자른다
    block = text.split("CREATE TABLE IF NOT EXISTS healing.recovery_attempt")[1].split(");")[0]
    in_check = re.search(r"action\s+TEXT\s+NOT NULL CHECK \(action IN \((.*?)\)\)", block, re.S)
    assert in_check, block
    declared = set(re.findall(r"'([a-z_]+)'", in_check.group(1)))
    assert declared == set(monitor.ATTEMPT_ACTIONS), (declared, monitor.ATTEMPT_ACTIONS)


@pytest.mark.parametrize("action", monitor.ATTEMPT_ACTIONS)
def test_every_declared_action_is_accepted(action, monkeypatch):
    """(a) 정상 케이스 — 허용값은 전부 INSERT 까지 간다."""
    sent = []
    monkeypatch.setattr(monitor.db, "execute",
                        lambda sql, params=None, **kw: sent.append((sql, params)))
    monitor.record_attempt("INC-TEST", action=action, result="ok")
    assert len(sent) == 1, "허용값인데 INSERT 가 나가지 않았다"
    assert "healing.recovery_attempt" in sent[0][0]


def test_an_unknown_action_is_rejected_loudly(monkeypatch):
    """(b) 알려진 실패 케이스 — 허용값 밖은 조용히 넘기지 않고 바로 터뜨린다.

    DB CHECK 에 맡기면 psycopg 오류가 `except` 에 삼켜져 또 0행이 된다. 그러면
    "기록이 안 되는 상태"가 다시 눈에 안 보인다 — 이 결함이 생긴 방식 그대로다.
    """
    monkeypatch.setattr(monitor.db, "execute",
                        lambda *a, **k: pytest.fail("허용값 밖인데 INSERT 를 시도했다"))
    with pytest.raises(ValueError, match="recovery_attempt"):
        monitor.record_attempt("INC-TEST", action="reboot", result="ok")
    with pytest.raises(ValueError, match="recovery_attempt"):
        monitor.record_attempt("INC-TEST", action="restart", result="성공")


def test_no_incident_id_records_nothing(monkeypatch):
    """사건이 없으면 적을 곳이 없다 — 외래키로 터지지 않게 조용히 넘긴다."""
    monkeypatch.setattr(monitor.db, "execute",
                        lambda *a, **k: pytest.fail("사건 없이 INSERT 를 시도했다"))
    monitor.record_attempt(None, action="restart", result="ok")


def test_recording_failure_does_not_break_recovery(monkeypatch):
    """기록이 실패해도 복구를 멈추지 않는다 — 기록은 복구의 전제가 아니다."""
    def boom(*a, **k):
        raise monitor.db.DbUnavailable("DB 없음")

    monkeypatch.setattr(monitor.db, "execute", boom)
    monitor.record_attempt("INC-TEST", action="restart", result="ok")      # 예외가 새지 않는다


# =============================================================================
# 2. 번호 매김 — 같은 사건의 두 번째 시도가 1번을 덮어쓰면 이력이 사라진다
# =============================================================================
def test_attempt_no_comes_from_the_table_not_the_caller(monkeypatch):
    """호출부가 번호를 세지 않는다. (incident_id, attempt_no) 가 PK 라 충돌하면 INSERT 가 깨진다."""
    sent = []
    monkeypatch.setattr(monitor.db, "execute",
                        lambda sql, params=None, **kw: sent.append(sql))
    monitor.record_attempt("INC-TEST", action="retry", result="fail")
    assert "COALESCE(MAX(attempt_no),0)+1" in sent[0].replace(" ", ""), sent[0]


# =============================================================================
# 3. 워커의 복구 경로가 실제로 기록을 부르는가
# =============================================================================
def test_the_worker_records_an_attempt_for_a_restart(monkeypatch):
    """`_recover_worker` 가 재시작과 준비 확인을 각각 남긴다.

    이 둘을 구분하는 이유: 재시작은 성공했는데 컨테이너가 안 올라오는 경우가 있다.
    한 줄로 합치면 "재시작 실패"와 "재시작 뒤 준비 안 됨"이 같은 기록이 된다.
    """
    from healing import __main__ as worker

    calls = []
    monkeypatch.setattr(worker.monitor, "record_attempt",
                        lambda incident_id, **kw: calls.append(kw))
    monkeypatch.setattr(worker.monitor, "open_incident_for", lambda *a, **k: "INC-TEST")
    monkeypatch.setattr(worker.monitor, "close_incident", lambda *a, **k: None)
    monkeypatch.setattr(worker.monitor, "restart_allowed", lambda *a, **k: True)
    monkeypatch.setattr(worker.metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(worker.logging_setup, "log", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_docker_action", lambda t, a: (True, "Docker restart accepted"))
    monkeypatch.setattr(worker, "_ready", lambda t, timeout=2.0: True)
    monkeypatch.setattr(worker, "_record_verification", lambda r: None)

    worker._recover_worker("moa-a-backend-1")

    actions = [c["action"] for c in calls]
    assert actions == ["restart", "probe"], calls
    assert all(c["result"] == "ok" for c in calls), calls


def test_the_ready_check_is_also_recorded_as_an_independent_verification(monkeypatch):
    """/ready 를 따로 물어 본 것은 독립 확인이다 — 남기지 않으면 화면이 "아직 검증하지 않음"이다.

    복구한 쪽(재시작)이 스스로 성공을 판정하지 못하게 하는 것이 PRD 5.3 Verification 독립이고,
    그 증거가 이 한 줄이다.
    """
    from healing import __main__ as worker

    checks = []
    monkeypatch.setattr(worker.monitor, "record_attempt", lambda incident_id, **kw: None)
    monkeypatch.setattr(worker.monitor, "open_incident_for", lambda *a, **k: "INC-TEST")
    monkeypatch.setattr(worker.monitor, "close_incident", lambda *a, **k: None)
    monkeypatch.setattr(worker.monitor, "restart_allowed", lambda *a, **k: True)
    monkeypatch.setattr(worker.metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(worker.logging_setup, "log", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_docker_action", lambda t, a: (True, "ok"))
    monkeypatch.setattr(worker, "_ready", lambda t, timeout=2.0: True)
    monkeypatch.setattr(worker, "_record_verification", lambda r: checks.append(r))

    worker._recover_worker("moa-a-backend-1")

    assert len(checks) == 1, checks
    assert checks[0]["incident_id"] == "INC-TEST"
    assert checks[0]["fault_type"] == "F-3"      # -> check_name = health_consecutive
    assert checks[0]["passed"] is True


def test_a_failed_ready_check_is_recorded_as_a_failed_verification(monkeypatch):
    """확인이 실패하면 실패로 남긴다 — 통과로 적으면 무인 복구율이 부풀려진다."""
    from healing import __main__ as worker

    checks = []
    monkeypatch.setattr(worker.monitor, "record_attempt", lambda incident_id, **kw: None)
    monkeypatch.setattr(worker.monitor, "open_incident_for", lambda *a, **k: "INC-TEST")
    monkeypatch.setattr(worker.monitor, "close_incident", lambda *a, **k: None)
    monkeypatch.setattr(worker.monitor, "restart_allowed", lambda *a, **k: True)
    monkeypatch.setattr(worker.metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(worker.logging_setup, "log", lambda *a, **k: None)
    monkeypatch.setattr(worker, "READY_WAIT_SEC", 0)
    monkeypatch.setattr(worker, "_docker_action", lambda t, a: (True, "ok"))
    monkeypatch.setattr(worker, "_ready", lambda t, timeout=2.0: False)
    monkeypatch.setattr(worker, "_record_verification", lambda r: checks.append(r))

    worker._recover_worker("moa-a-backend-1")

    assert len(checks) == 1 and checks[0]["passed"] is False, checks


def test_a_restart_that_never_comes_up_is_recorded_as_a_timeout(monkeypatch):
    """재시작은 됐는데 준비가 안 되면 probe 가 timeout 으로 남는다 — 성공으로 적지 않는다."""
    from healing import __main__ as worker

    calls = []
    monkeypatch.setattr(worker.monitor, "record_attempt",
                        lambda incident_id, **kw: calls.append(kw))
    monkeypatch.setattr(worker.monitor, "open_incident_for", lambda *a, **k: "INC-TEST")
    monkeypatch.setattr(worker.monitor, "close_incident", lambda *a, **k: None)
    monkeypatch.setattr(worker.monitor, "restart_allowed", lambda *a, **k: True)
    monkeypatch.setattr(worker.metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(worker.logging_setup, "log", lambda *a, **k: None)
    monkeypatch.setattr(worker, "READY_WAIT_SEC", 0)
    monkeypatch.setattr(worker, "_docker_action", lambda t, a: (True, "Docker restart accepted"))
    monkeypatch.setattr(worker, "_ready", lambda t, timeout=2.0: False)
    monkeypatch.setattr(worker, "_record_verification", lambda r: None)

    worker._recover_worker("moa-b-backend-1")

    assert [c["action"] for c in calls] == ["restart", "probe"], calls
    assert calls[1]["result"] == "timeout", calls[1]


# =============================================================================
# 4. 해석기 시도 관측자 — retry 와 switch_backup 을 구분해야 전환을 사후에 볼 수 있다
# =============================================================================
def test_the_observer_tells_a_retry_from_a_switch(monkeypatch):
    from healing import __main__ as worker

    calls = []
    monkeypatch.setattr(worker.monitor, "record_attempt",
                        lambda incident_id, **kw: calls.append(kw))
    observe = worker._attempt_recorder("INC-TEST", "order_interpreter_primary")

    observe({"attempt_no": 1, "service": "primary", "outcome": "실패",
             "fault_code": "DEP_HTTP_5XX", "duration_ms": 59})
    observe({"attempt_no": 2, "service": "backup", "outcome": "성공", "duration_ms": 820})

    assert [c["action"] for c in calls] == ["retry", "switch_backup"], calls
    assert [c["result"] for c in calls] == ["fail", "ok"], calls
    assert calls[0]["error"] == "DEP_HTTP_5XX"


def test_the_observer_is_relative_to_the_faulting_service(monkeypatch):
    """대체 해석기가 장애면 `backup` 호출이 retry 이고 `primary` 가 전환이다."""
    from healing import __main__ as worker

    calls = []
    monkeypatch.setattr(worker.monitor, "record_attempt",
                        lambda incident_id, **kw: calls.append(kw))
    observe = worker._attempt_recorder("INC-TEST", "order_interpreter_backup")

    observe({"attempt_no": 1, "service": "backup", "outcome": "실패", "duration_ms": 10})
    observe({"attempt_no": 2, "service": "primary", "outcome": "성공", "duration_ms": 20})

    assert [c["action"] for c in calls] == ["retry", "switch_backup"], calls
