"""상시 감시 — 업무 층이 남긴 메트릭을 Monitor 가 읽어 사건을 만든다 (PRD 5.6.1 · FR-013).

지금까지 healing.incident 는 **장애 시험 워크플로 안에서만** 만들어졌다(is_verification_run=true).
운영 중 저절로 난 장애는 탐지도 기록도 되지 않아 대시보드 E 블록과 지표가 비어 있었다.
판정 조건은 PRD 5.6.1 정본이다 — 5xx 연속 2회 · 호출 5초 초과 1회 · 스키마 검증 실패 1회.
"""
import pytest

from app.services import monitor


def _metric(metric_key, target, mid, breached=True):
    return {"id": mid, "metric_key": metric_key, "target": target, "value": 1,
            "breached": breached, "measured_at": "2026-11-06T13:00:00+00:00", "trace_id": f"tr_{mid}"}


@pytest.fixture
def fake_db(monkeypatch):
    state = {"rows": [], "open": set(), "executed": []}
    state["sqls"] = []

    def fetch_all(sql, params=None, **k):
        state["sqls"].append(sql)
        return list(state["rows"])

    monkeypatch.setattr(monitor.db, "fetch_all", fetch_all)
    def fetch_one(sql, params=None, **k):
        state["sqls"].append(sql)
        if "fault_injection" in sql:
            # 실습 주입 조회 — 이 테스트들은 저절로 난 장애를 다룬다(주입 행 없음)
            return state.get("injection")
        return {"n": 1} if params and params[0] in state["open"] else {"n": 0}

    monkeypatch.setattr(monitor.db, "fetch_one", fetch_one)
    monkeypatch.setattr(monitor.db, "execute",
                        lambda sql, params=None, **k: state["executed"].append((sql, params)))
    return state


# --- 판정 조건 (PRD 5.6.1) ---------------------------------------------------------------------
def test_five_hundred_needs_two_in_a_row(fake_db):
    fake_db["rows"] = [_metric("dep_5xx_ratio", "order_interpreter_primary", 1)]
    assert monitor.pending_signals() == []                 # 1회로는 사건이 아니다

    fake_db["rows"].append(_metric("dep_5xx_ratio", "order_interpreter_primary", 2))
    (signal,) = monitor.pending_signals()
    assert signal["fault_code"] == "DEP_HTTP_5XX"
    assert signal["target"] == "order_interpreter_primary"
    assert signal["metric_ids"] == [1, 2]


def test_timeout_fires_on_the_first_observation(fake_db):
    # PRD 5.6.1 API 판정 조건 "호출 5초 초과" — 연속 조건이 없다
    fake_db["rows"] = [_metric("timeout_count", "order_interpreter_primary", 7)]
    (signal,) = monitor.pending_signals()
    assert signal["fault_code"] == "DEP_TIMEOUT" and signal["metric_ids"] == [7]


def test_schema_failure_fires_on_the_first_observation(fake_db):
    fake_db["rows"] = [_metric("schema_error_rate", "order_interpreter_backup", 9)]
    (signal,) = monitor.pending_signals()
    assert signal["fault_code"] == "AGENT_OUTPUT_INVALID"


def test_latency_alone_is_not_an_incident(fake_db):
    # dep_latency_ms 는 추세용이다. 넘었더라도 timeout_count 가 따로 남는다
    fake_db["rows"] = [_metric("dep_latency_ms", "order_interpreter_primary", 3),
                       _metric("dep_latency_ms", "order_interpreter_primary", 4)]
    assert monitor.pending_signals() == []


def test_targets_are_counted_apart(fake_db):
    fake_db["rows"] = [_metric("dep_5xx_ratio", "order_interpreter_primary", 1),
                       _metric("dep_5xx_ratio", "order_interpreter_backup", 2)]
    assert monitor.pending_signals() == []                 # 대상마다 1회씩 — 연속 2회가 아니다


def test_an_already_open_incident_is_not_duplicated(fake_db):
    fake_db["rows"] = [_metric("timeout_count", "order_interpreter_primary", 1)]
    fake_db["open"].add("DEP_TIMEOUT")
    assert monitor.pending_signals() == []                 # 같은 장애가 열려 있으면 증거만 쌓는다


# --- 사건 생성 -------------------------------------------------------------------------------
def test_created_incident_is_a_real_one_not_a_verification_run(fake_db):
    fake_db["rows"] = [_metric("timeout_count", "order_interpreter_primary", 5)]
    (signal,) = monitor.pending_signals()
    incident_id = monitor.open_incident(signal)

    assert incident_id.startswith("INC-")
    inserts = [sql for sql, _ in fake_db["executed"] if "INSERT INTO healing.incident" in sql]
    assert len(inserts) == 1
    # is_verification_run=false 여야 지표 뷰(04_views.sql)와 E 블록이 실제 사건으로 센다.
    # true 로 들어가면 시험 사건과 섞여 복구율이 오염된다
    assert "false" in inserts[0]
    # 주입이 아니라 실제 장애다 — injected_at 은 없다
    assert "injected_at" not in inserts[0]


def test_consumed_metrics_are_tied_to_the_incident(fake_db):
    fake_db["rows"] = [_metric("dep_5xx_ratio", "order_interpreter_primary", 11),
                       _metric("dep_5xx_ratio", "order_interpreter_primary", 12)]
    (signal,) = monitor.pending_signals()
    monitor.open_incident(signal)
    updates = [(sql, params) for sql, params in fake_db["executed"]
               if "UPDATE healing.runtime_metric" in sql]
    assert len(updates) == 1
    # 쓴 행에 사건을 묶어 둬야 다음 순회에서 같은 관측을 또 세지 않는다
    assert [11, 12] == list(updates[0][1][1])


# --- 사건 닫기 — 칸 이름을 고정한다 -------------------------------------------------------------
# 실측 결함 2026-11-06: healing.incident 에 `recovered_at` 이 있다고 가정해 UPDATE 가 깨졌다
# (UndefinedColumn). 실제 칸은 resolved_at(복구 완료) · failed_at · escalated_at 이다.
# 사건 생성은 됐는데 닫히지 않아 대시보드에 "복구 중"으로 영원히 남았다.
def test_closing_a_recovered_incident_sets_resolved_at(fake_db):
    monitor.close_incident("INC-1", recovered=True)
    (sql, params), = [x for x in fake_db["executed"] if "UPDATE healing.incident" in x[0]]
    flat = sql.replace(" ", "")
    assert "status='recovered'" in flat and "resolved_at=NOW()" in flat
    assert "failed_at" not in flat and "recovered_at=NOW()" not in flat
    assert params[-1] == "INC-1"


def test_closing_a_failed_incident_records_failure_and_escalation(fake_db):
    monitor.close_incident("INC-2", recovered=False)
    (sql, _), = [x for x in fake_db["executed"] if "UPDATE healing.incident" in x[0]]
    flat = sql.replace(" ", "")
    assert "status='failed'" in flat and "failed_at=NOW()" in flat and "escalated_at=NOW()" in flat
    assert "resolved_at=NOW()" not in flat      # 실패한 사건에 복구 시각을 넣지 않는다


def test_closing_keeps_the_first_recovery_start(fake_db):
    # 복구 시작 시각은 처음 값을 지킨다 — MTTR 구간이 뒤로 밀리면 안 된다
    monitor.close_incident("INC-3", recovered=True)
    (sql, _), = [x for x in fake_db["executed"] if "UPDATE healing.incident" in x[0]]
    assert "COALESCE(recovery_started_at" in sql


# --- 담당자 확인으로 넘긴 사건 ----------------------------------------------------------------
# F-5 가 NEEDS_REVIEW 로 끝나는 것은 **안전한 기대 결과**다(AI 가 임의 확정하지 않고 사람에게 넘김).
# 복구도 실패도 아니므로 manual_check 로 닫고 에스컬레이션 시각만 남긴다.
def test_closing_to_manual_check_does_not_claim_recovery_or_failure(fake_db):
    monitor.close_incident("INC-4", recovered=False, manual_check=True)
    (sql, _), = [x for x in fake_db["executed"] if "UPDATE healing.incident" in x[0]]
    flat = sql.replace(" ", "")
    assert "status='manual_check'" in flat and "escalated_at=NOW()" in flat
    assert "resolved_at=NOW()" not in flat and "failed_at=NOW()" not in flat


# --- 시험 구간의 관측은 실제 사건이 아니다 -----------------------------------------------------
# 실측 결함 2026-11-06: F-1 장애 시험을 돌리자 그 주입으로 생긴 5xx 관측이 **실제 사건**으로
# 기록됐다(is_verification_run=false). 시험 사건과 실제 사건이 섞이면 SC-4 무인 복구율과
# 사건 수가 오염된다 — R-14(측정 오염)와 같은 종류다.
# 걸러내는 기준은 `healing.fault_injection` 의 run_id 다 — 시험 주입에만 run_id 가 있다.
def test_signals_exclude_observations_made_during_a_test_injection(fake_db):
    fake_db["rows"] = [_metric("timeout_count", "order_interpreter_primary", 1)]
    monitor.pending_signals()
    sql = fake_db["sqls"][0]
    assert "healing.fault_injection" in sql and "run_id IS NOT NULL" in sql, sql
    # 주입 구간과 겹치는 관측만 빼야 한다 — 주입 전후의 실제 장애는 그대로 사건이 된다
    assert "injected_at" in sql and "cleared_at" in sql


# --- 폴링으로 직접 관측한 장애 (F-3) ----------------------------------------------------------
# 메트릭을 세어 판정하는 경로와 달리, 헬스체크 실패는 Monitor 가 **직접 관측**한다.
# 사건은 같은 역할이 만들지만 입구가 둘이다 — pending_signals(메트릭) · open_incident_for(폴링).
def test_open_incident_for_a_polled_fault_is_a_real_incident(fake_db):
    incident_id = monitor.open_incident_for(
        "WORKER_DOWN", "moa-a-backend-1", label="처리 프로세스 중단",
        detection_metric="conn_fail_count", evidence=["/ready 연속 2회 실패"])
    assert incident_id and incident_id.startswith("INC-")
    (sql, _), = [x for x in fake_db["executed"] if "INSERT INTO healing.incident" in x[0]]
    assert "false" in sql and "injected_at" not in sql


def test_open_incident_for_does_not_duplicate_an_open_one(fake_db):
    fake_db["open"].add("WORKER_DOWN")
    assert monitor.open_incident_for("WORKER_DOWN", "moa-a-backend-1", label="x",
                                     detection_metric="conn_fail_count", evidence=[]) is None
    assert [x for x in fake_db["executed"] if "INSERT INTO healing.incident" in x[0]] == []


def test_test_injection_active_looks_only_at_workflow_injections(fake_db):
    monitor.test_injection_active("moa-a-backend-1")
    sql = fake_db["sqls"][-1] if fake_db["sqls"] else ""
    # 대시보드 주입(run_id IS NULL)과 시험 주입(run_id IS NOT NULL)을 가른다
    assert "healing.fault_injection" in sql and "run_id IS NOT NULL" in sql
    assert "cleared_at IS NULL" in sql


# --- 재시작 상한 (PRD 5.6 F-3 "10분 내 재시작 2회") --------------------------------------------
# 상한이 없으면 죽은 채로 올라오지 않는 컨테이너를 영원히 재시작한다 — 폭주 방어의 일부다.
def test_restart_is_allowed_until_the_limit(monkeypatch):
    assert monitor.restart_allowed([], now=1000.0) is True
    assert monitor.restart_allowed([990.0], now=1000.0) is True
    assert monitor.restart_allowed([980.0, 990.0], now=1000.0) is False


def test_restarts_outside_the_window_do_not_count(monkeypatch):
    old = 1000.0 - monitor.RESTART_WINDOW_SEC - 1
    assert monitor.restart_allowed([old, old], now=1000.0) is True


# --- 자동 복구를 했다고 거짓으로 적지 않는다 ----------------------------------------------------
def test_agent_exception_is_not_claimed_as_auto_recovered():
    """AGENT_EXCEPTION 정책은 "체크포인트부터 1회 재실행"인데 업무 층에 그 재실행이 없다.

    자동 복구했다고 적으면 거짓이고 SC-4 무인 복구율이 부풀려진다 — 담당자 확인으로 넘긴다.
    """
    assert "AGENT_EXCEPTION" not in monitor.AUTO_RECOVERABLE
    assert {"DEP_HTTP_5XX", "DEP_TIMEOUT", "AGENT_OUTPUT_INVALID", "WORKER_DOWN"} <= monitor.AUTO_RECOVERABLE


# --- F-6 저장소 장애 (필수 FT-03) — DB 가 죽은 동안은 사건을 적을 수 없다 -----------------------
# 사건 기록이 같은 DB 에 있으므로 F-6 중에는 INSERT 자체가 불가능하다.
# 그래서 탐지 시각을 메모리에 들고 있다가 **DB 가 돌아온 뒤에** 완결된 사건을 한 번에 적는다.
# F-6 은 캐시 폴백으로 서비스가 살아 있으므로 실시간 표시보다 기록의 정확성이 중요하다.
def test_db_outage_needs_two_failures_before_it_counts():
    out = monitor.DbOutage()
    assert out.observe(ok=False, now=100.0) is None      # 1회는 순간 오류일 수 있다
    assert out.detected_at is None
    assert out.observe(ok=False, now=105.0) is None      # 2회째에 탐지로 본다
    assert out.detected_at == 105.0


def test_db_outage_reports_the_incident_only_when_it_comes_back():
    out = monitor.DbOutage()
    out.observe(ok=False, now=100.0)
    out.observe(ok=False, now=105.0)
    assert out.observe(ok=False, now=110.0) is None      # 아직 돌아오지 않았다
    record = out.observe(ok=True, now=118.0)
    # first_failure_at 이 더해졌다 — MTTD 의 시작점(2026-11-07)
    assert record == {"detected_at": 105.0, "resolved_at": 118.0, "failures": 3,
                      "first_failure_at": 100.0}
    assert out.detected_at is None                       # 보고한 뒤에는 비운다


def test_a_single_blip_leaves_nothing_to_report():
    out = monitor.DbOutage()
    out.observe(ok=False, now=100.0)
    assert out.observe(ok=True, now=101.0) is None       # 연속 2회에 못 미쳤으면 사건이 아니다
    assert out.detected_at is None


def test_db_outage_stays_quiet_while_healthy():
    out = monitor.DbOutage()
    assert out.observe(ok=True, now=1.0) is None
    assert out.observe(ok=True, now=2.0) is None


# --- 시험은 정상 상태에서 시작한다 -------------------------------------------------------------
# 실측 결함 2026-11-06: F-1 시험이 primary 회로를 열어 놓자 뒤따른 F-2 · F-5 시험이
# 냉각(60초)이 끝나기 전에 돌아 **primary 를 아예 부르지 않았다**.
#   시도 기록이 backup:성공 하나뿐 -> 실패를 관측하지 못해 탐지 시각이 복구 후로 밀림
#   -> MTTR 9ms · 15ms 라는 거짓 합격 (F-1 에서 고친 것과 같은 증상이 시험 간 간섭으로 재발)
# 회차마다 회로를 정상으로 되돌려 회차를 서로 독립시킨다. preflight 가 이미 두 해석기의
# 정상을 확인하므로 되돌리는 것이 실제 상태와 어긋나지 않는다.
def test_resetting_circuits_puts_every_dependency_back_to_up(fake_db):
    monitor.reset_circuits()
    (sql, _), = [x for x in fake_db["executed"] if "healing.circuit_state" in x[0]]
    flat = sql.replace(" ", "")
    assert "state='up'" in flat and "consecutive_failures=0" in flat
    assert "cooldown_until=NULL" in flat
    assert "WHERE" not in sql.upper(), "회차마다 전부 되돌린다 — 한 대상만 남기면 다음 회차가 또 간섭한다"


# --- 네트워크 격리는 compose 불변식이다 --------------------------------------------------------
# 실측 결함 2026-11-06: F-3 검증이 프런트엔드를 컨테이너 이름으로 직접 불렀다
# (http://moa-a-frontend-1:8501). 프런트엔드는 A/B 격리 네트워크에만 있고 healing 은
# moa_core · ctl 에만 있어 DNS 가 풀리지 않는다 -> 매 순회 예외 -> 30초 시간 초과로 F-3 이 늘 FAIL.
# preflight 는 같은 제약을 게이트웨이 경유(sticky 쿠키)로 이미 우회하고 있었다.
def test_healing_never_probes_a_frontend_by_container_name():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "healing" / "__main__.py").read_text(encoding="utf-8")
    assert "frontend-1:8501" not in source, (
        "프런트엔드는 healing 에서 이름으로 닿지 않는다 — 게이트웨이 경유로 확인한다")
    assert "MOA_STACK" in source, "게이트웨이 경유 확인은 sticky 쿠키로 스택을 고른다"


# --- 2026-11-07: 대상별 중복 판정 · 첫 징후 시각 -----------------------------------------------
# 둘 다 "사건이 하나 남아 있으면 그 장애를 영영 못 본다" · "MTTD 를 낼 수 없다" 로 이어지던 것이다.
@pytest.fixture
def spy(monkeypatch):
    calls = {"fetch_one": [], "execute": [], "count": 0}

    def fetch_one(sql, params=None, **k):
        calls["fetch_one"].append((sql, params))
        if "fault_injection" in sql:
            return None
        return {"n": calls["count"]}

    monkeypatch.setattr(monitor.db, "fetch_one", fetch_one)
    monkeypatch.setattr(monitor.db, "execute",
                        lambda sql, params=None, **k: calls["execute"].append((sql, params)))
    return calls


def test_the_same_fault_on_another_target_is_a_new_incident(spy):
    monitor._open_incident_count("WORKER_DOWN", "moa-b-backend-1")
    sql, params = spy["fetch_one"][0]
    assert "target IS NOT DISTINCT FROM" in sql     # 대상을 조건에 쓴다
    assert params[1] == "moa-b-backend-1"


def test_an_escalated_incident_does_not_block_new_detection():
    """manual_check 는 자동화가 사람에게 넘긴 상태다 — 진행 중이 아니다.

    열린 것으로 세면 담당자가 정리할 때까지 그 장애 코드가 침묵한다.
    """
    assert "manual_check" not in monitor._OPEN_STATUSES
    assert "recovering" in monitor._OPEN_STATUSES     # 복구 중은 여전히 중복을 막는다


def test_a_spontaneous_fault_records_when_the_signal_started(spy):
    signal = {"fault_code": "DEP_TIMEOUT", "target": "order_interpreter_primary",
              "label": "응답 지연", "metric_key": "timeout_count", "metric_ids": [1],
              "detected_at": "2026-11-07T00:00:00+00:00", "trace_id": "tr_1",
              "evidence": ["timeout_count=1"]}
    monitor.open_incident(signal)
    sql, params = spy["execute"][0]
    assert "first_signal_at" in sql
    assert "detected_at" in sql and "NOW()" in sql          # 알아챈 시각은 지금이다
    assert "2026-11-07T00:00:00+00:00" in params            # 첫 관측은 신호 시각
    assert "order_interpreter_primary" in params            # 대상도 적는다


def test_a_db_outage_remembers_the_first_failure():
    out = monitor.DbOutage()
    assert out.observe(ok=False, now=100.0) is None     # 첫 실패
    assert out.observe(ok=False, now=105.0) is None     # 탐지(연속 2회)
    record = out.observe(ok=True, now=120.0)
    assert record["first_failure_at"] == 100.0
    assert record["detected_at"] == 105.0               # MTTD 는 5초로 나온다
