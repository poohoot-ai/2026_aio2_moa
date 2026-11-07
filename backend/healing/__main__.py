"""Long-running worker entry point for the existing Compose `healing` service."""

import json
import logging
import os
import secrets
import socket
import time
from datetime import datetime, timezone

import httpx
import psycopg

from app import config, db, ids, logging_setup
from app.contracts import TASK_CONTRACTS
from app.interpreter_client import InterpreterError, interpret_resilient
from app.services import incidents, menu, metrics, monitor, workflows

LOG = logging.getLogger("moa.healing")
WORKER_ID = os.getenv("HOSTNAME") or socket.gethostname()
POLL_SEC = 1.0
LEASE_SEC = 30
F_CODE = {"F-1": "DEP_HTTP_5XX", "F-2": "DEP_TIMEOUT", "F-3": "WORKER_DOWN",
          "F-4": "REPEATED_FAILURE", "F-5": "AGENT_OUTPUT_INVALID", "F-6": "DB_UNAVAILABLE"}
INCIDENT_LABELS = {
    "DEP_HTTP_5XX": "주문 해석 오류 응답", "DEP_TIMEOUT": "응답 지연",
    "WORKER_DOWN": "처리 프로세스 중단", "REPEATED_FAILURE": "반복 장애",
    "AGENT_OUTPUT_INVALID": "잘못된 결과 형식", "DB_UNAVAILABLE": "메뉴 저장소 연결 끊김",
}


def _allowlist() -> list[str]:
    return sorted({v.strip() for v in os.getenv("HEALING_ALLOWLIST", "").split(",") if v.strip()})


def _docker_client() -> httpx.Client:
    host = os.getenv("DOCKER_HOST", "tcp://socket-proxy:2375")
    if host.startswith("tcp://"):
        host = "http://" + host.removeprefix("tcp://")
    return httpx.Client(base_url=host, timeout=4.0)


def _heartbeat() -> None:
    proxy_ok = False
    detail = ""
    try:
        with _docker_client() as client:
            response = client.get("/_ping")
            proxy_ok = response.status_code == 200 and response.text.strip() == "OK"
            if not proxy_ok:
                detail = f"proxy HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        detail = f"proxy {type(exc).__name__}"
    db.execute(
        "INSERT INTO healing.workflow_worker(worker_id,last_seen,proxy_ok,allowlist,detail) "
        "VALUES (%s,NOW(),%s,%s::jsonb,%s) ON CONFLICT(worker_id) DO UPDATE SET "
        "last_seen=NOW(),proxy_ok=EXCLUDED.proxy_ok,allowlist=EXCLUDED.allowlist,detail=EXCLUDED.detail",
        (WORKER_ID, proxy_ok, json.dumps(_allowlist()), detail or None), role="healing")


def _event(run_id: str, kind: str, summary: str, iteration: int | None = None, details: dict | None = None) -> None:
    db.execute("INSERT INTO healing.workflow_event(run_id,iteration_no,event_type,summary,details) "
               "VALUES (%s,%s,%s,%s,%s::jsonb)",
               (run_id, iteration, kind, summary, json.dumps(details or {}, ensure_ascii=False)), role="healing")


def _record_verification(result: dict) -> None:
    incident_id = result.get("incident_id")
    if not incident_id:
        return
    fault = result.get("fault_type")
    check_name = {"F-1": "request_sample", "F-3": "health_consecutive",
                  "F-6": "cache_db_match"}.get(fault, "function_test")
    expected = {"F-1": "5 successful synthetic requests", "F-3": "3 consecutive backend, frontend, and Gateway checks",
                "F-5": "invalid output ends in NEEDS_REVIEW after 3 attempts",
                "F-6": "stale cache checksum matches preloaded checksum"}.get(fault, "fault-specific behavior verified")
    actual = result.get("detail", "")
    expected_safe_outcome = (actual == "NEEDS_REVIEW" or "approval" in actual.lower())
    verification_passed = bool(result.get("passed") or expected_safe_outcome)
    db.execute("INSERT INTO healing.verification_result(incident_id,check_name,expected,actual,passed,verdict) "
               "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(incident_id,check_name) DO UPDATE SET "
               "expected=EXCLUDED.expected,actual=EXCLUDED.actual,passed=EXCLUDED.passed,"
               "verdict=EXCLUDED.verdict,verified_at=NOW()",
               (incident_id, check_name, expected, actual, verification_passed,
                "pass" if verification_passed else "fail"), role="healing")


def _update_run(run_id: str, *, status: str | None = None, phase: str | None = None,
                iteration: int | None = None, summary: dict | None = None) -> None:
    db.execute("UPDATE healing.workflow_run SET status=COALESCE(%s,status),phase=COALESCE(%s,phase),"
               "current_iteration=COALESCE(%s,current_iteration),summary=COALESCE(%s::jsonb,summary),"
               "updated_at=NOW(), started_at=COALESCE(started_at,CASE WHEN %s='running' THEN NOW() END),"
               "finished_at=CASE WHEN %s IN ('completed','stopped','failed','needs_operator_action','invalid') "
               "THEN NOW() ELSE finished_at END WHERE run_id=%s",
               (status, phase, iteration, json.dumps(summary) if summary is not None else None,
                status, status, run_id), role="healing")


def _cancelled(run_id: str) -> bool:
    row = db.fetch_one("SELECT cancel_requested FROM healing.workflow_run WHERE run_id=%s",
                       (run_id,), role="healing")
    return bool(row and row["cancel_requested"])


def _sleep_or_stop(run_id: str, seconds: float) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if _cancelled(run_id):
            return True
        time.sleep(min(0.5, deadline - time.monotonic()))
    return _cancelled(run_id)


def _claim() -> dict | None:
    with db.transaction(role="healing") as cur:
        # A dead worker's lease is never replayed blindly: end its injections and surface operator action.
        cur.execute("SELECT run_id FROM healing.workflow_lease WHERE lease_name='active' "
                    "AND lease_until < NOW() FOR UPDATE")
        expired = cur.fetchone()
        if expired:
            stale_id = expired["run_id"]
            cur.execute("SELECT target,fault,mode FROM healing.fault_injection "
                        "WHERE run_id=%s AND action='inject' AND cleared_at IS NULL", (stale_id,))
            stale_injections = cur.fetchall()
            restart_failures = []
            for injection in stale_injections:
                if injection["fault"] == "F-3" and injection["mode"] == "kill":
                    recovered, detail = _docker_action(injection["target"], "restart")
                    if not recovered:
                        restart_failures.append(detail)
            cur.execute("UPDATE healing.fault_injection SET cleared_at=COALESCE(cleared_at,NOW()) "
                        "WHERE run_id=%s AND action='inject' AND cleared_at IS NULL", (stale_id,))
            cur.execute("UPDATE healing.workflow_run SET status='needs_operator_action',phase='cleanup',"
                        "finished_at=NOW(),updated_at=NOW() WHERE run_id=%s "
                        "AND status IN ('running','stop_requested','cleaning')", (stale_id,))
            summary = "expired lease cleanup; operator verification required"
            if restart_failures:
                summary += "; backend restart failed: " + ",".join(restart_failures)
            cur.execute("INSERT INTO healing.workflow_event(run_id,event_type,summary) "
                        "VALUES (%s,'lease_recovered',%s)", (stale_id, summary))
            cur.execute("DELETE FROM healing.workflow_lease WHERE lease_name='active'")
        cur.execute("SELECT run_id FROM healing.workflow_run WHERE status='queued' "
                    "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        run_id = row["run_id"]
        cur.execute("UPDATE healing.workflow_run SET status='running',phase='preflight',started_at=NOW(),updated_at=NOW() "
                    "WHERE run_id=%s AND status='queued' RETURNING *", (run_id,))
        run = cur.fetchone()
        if not run:
            return None
        cur.execute("INSERT INTO healing.workflow_lease(lease_name,run_id,worker_id,lease_until) "
                    "VALUES ('active',%s,%s,NOW() + (%s * INTERVAL '1 second')) "
                    "ON CONFLICT(lease_name) DO UPDATE SET run_id=EXCLUDED.run_id,worker_id=EXCLUDED.worker_id,"
                    "lease_until=EXCLUDED.lease_until,updated_at=NOW()",
                    (run_id, WORKER_ID, LEASE_SEC))
        return run


def _renew_lease(run_id: str) -> None:
    db.execute("UPDATE healing.workflow_lease SET lease_until=NOW() + (%s * INTERVAL '1 second'),updated_at=NOW() "
               "WHERE lease_name='active' AND run_id=%s AND worker_id=%s",
               (LEASE_SEC, run_id, WORKER_ID), role="healing")


def _inject(run_id: str, no: int, fault: str, target: str, mode: str) -> str:
    injection_id = ids.injection_id()
    with db.transaction(role="healing") as cur:
        cur.execute("INSERT INTO healing.fault_injection "
                    "(injection_id,fault,action,target,mode,environment,run_id,iteration_no) "
                    "VALUES (%s,%s,'inject',%s,%s,'lab',%s,%s)",
                    (injection_id, fault, target, mode, run_id, no))
        cur.execute("UPDATE healing.workflow_iteration SET status='running',phase='injecting',"
                    "injection_id=%s,started_at=NOW() WHERE run_id=%s AND iteration_no=%s",
                    (injection_id, run_id, no))
        cur.execute("UPDATE healing.workflow_run SET phase='injecting',current_iteration=%s,updated_at=NOW() "
                    "WHERE run_id=%s", (no, run_id))
    _event(run_id, "injection_started", f"{fault} 장애 주입", no,
           {"injection_id": injection_id, "target": target, "mode": mode})
    return injection_id


def _clear(run_id: str, injection_id: str) -> bool:
    db.execute("UPDATE healing.fault_injection SET cleared_at=COALESCE(cleared_at,NOW()) "
               "WHERE injection_id=%s AND run_id=%s AND action='inject'",
               (injection_id, run_id), role="healing")
    row = db.fetch_one("SELECT cleared_at FROM healing.fault_injection WHERE injection_id=%s AND run_id=%s",
                       (injection_id, run_id), role="healing")
    return bool(row and row["cleared_at"])


def _create_incident(run_id: str, iteration: int, fault: str, injection_id: str,
                     evidence: str) -> str:
    incident_id = "INC-WF-" + secrets.token_hex(6).upper()
    code = F_CODE[fault]
    trace_id = f"tr_{secrets.token_hex(6).upper()}"
    db.execute("INSERT INTO healing.incident(incident_id,trace_id,fault_code,target,status,status_label,"
               "injected_at,detected_at,detection_metric,detection_evidence,is_verification_run,run_id) "
               "SELECT %s,%s,%s,target,'detected',%s,injected_at,NOW(),'workflow_signal',ARRAY[%s],true,%s "
               "FROM healing.fault_injection WHERE injection_id=%s",
               (incident_id, trace_id, code, INCIDENT_LABELS[code], evidence, run_id, injection_id), role="healing")
    db.execute("UPDATE healing.workflow_iteration SET incident_id=%s WHERE run_id=%s AND iteration_no=%s",
               (incident_id, run_id, iteration), role="healing")
    db.execute("UPDATE healing.fault_injection SET incident_id=%s WHERE injection_id=%s AND run_id=%s",
               (incident_id, injection_id, run_id), role="healing")
    return incident_id


def _service_for(target: str) -> str:
    return "backup" if target.endswith("backup") else "primary"


def _cooldown_remaining(target: str, cap: float = 90.0) -> float:
    """회로 냉각이 끝날 때까지 남은 초 (+1초 여유).

    회차 시작부터 60초를 세면 안 된다 — 회로는 연속 3회 실패가 **다 난 뒤에** 열리므로
    그만큼 냉각이 늦게 끝난다. 실측 2026-11-06: 60초에 probe 해 `cooldown_until<=NOW()` 에
    걸려 "cooldown 또는 circuit_open 상태가 준비되지 않아" 로 F-4 가 늘 FAIL 이었다.
    """
    key = f"order_interpreter_{_service_for(target)}"
    row = db.fetch_one(
        "SELECT GREATEST(0, EXTRACT(EPOCH FROM (cooldown_until - NOW()))) AS s "
        "  FROM healing.circuit_state WHERE dependency_key=%s", (key,), role="healing")
    return min(float((row or {}).get("s") or 0) + 1.0, cap)


def _probe_circuit(target: str, run_id: str, incident_id: str) -> tuple[bool, str]:
    service = _service_for(target)
    key = f"order_interpreter_{service}"
    row = db.fetch_one(
        "UPDATE healing.circuit_state SET state='probing',updated_at=NOW() "
        "WHERE dependency_key=%s AND state='circuit_open' AND cooldown_until<=NOW() "
        "RETURNING dependency_key", (key,), role="healing")
    if not row:
        return False, "cooldown 또는 circuit_open 상태가 준비되지 않아 probe하지 않았습니다"
    url = config.ORDER_INTERPRETER_BACKUP_URL if service == "backup" else config.ORDER_INTERPRETER_PRIMARY_URL
    try:
        response = httpx.get(f"{url}/health", timeout=2.0)
        success = response.status_code == 200
    except httpx.HTTPError:
        success = False
    if success:
        db.execute("UPDATE healing.circuit_state SET state='up',consecutive_failures=0,"
                   "cooldown_until=NULL,healthy_streak=0,updated_at=NOW() WHERE dependency_key=%s",
                   (key,), role="healing")
        return True, "단일 probe 성공; circuit을 up으로 복귀했습니다"
    db.execute("UPDATE healing.circuit_state SET state='down',cooldown_until=NULL,updated_at=NOW() "
               "WHERE dependency_key=%s", (key,), role="healing")
    db.execute("UPDATE healing.incident SET fault_code='REPEATED_FAILURE',status='awaiting_approval',"
               "status_label='승인 대기',detection_metric='circuit_probe',"
               "detection_evidence=ARRAY['cooldown 후 단일 probe 실패'],strategy='escalate',updated_at=NOW() "
               "WHERE incident_id=%s AND run_id=%s", (incident_id, run_id), role="healing")
    return False, f"probe 실패; 자동 재시도 없이 승인 대기 사건 {incident_id}을 만들었습니다"


def _synthetic_interpreter_request(fault: str, target: str, samples: int,
                                  run_id: str, attempt_observer=None) -> tuple[bool, str, int]:
    success = 0
    errors: list[str] = []
    for index in range(samples):
        context = {"brand_id": "generic", "menu_version": config.MENU_VERSION}
        attempts = 1 + config.MAX_REGENERATE if fault == "F-5" else 1
        for attempt in range(attempts):
            try:
                interpret_resilient(
                    "extract_events", [{"message_id": f"synthetic-{run_id}-{index}", "order": index + 1,
                                        "sender": "lab-run", "text": "iced americano one"}],
                    context, service=_service_for(target), trace_id=f"workflow-{run_id}-{fault}-{index}-{attempt}",
                    session_id=None, attempt_observer=attempt_observer)
                success += 1
                break
            except InterpreterError as exc:
                errors.append(exc.fault_code)
                if fault == "F-5" and exc.fault_code == "AGENT_OUTPUT_INVALID":
                    if attempt + 1 < attempts:
                        context["validation_feedback"] = {
                            "error": exc.fault_code, "attempt": attempt + 1,
                            "instruction": "Regenerate complete output matching the required schema.",
                        }
                        continue
                    return False, "NEEDS_REVIEW", success
                break
        if success <= index:
            break
    return success == samples, ",".join(errors) or "synthetic_request_ok", success

def _docker_action(target: str, action: str) -> tuple[bool, str]:
    if target not in _allowlist() or target not in ("moa-a-backend-1", "moa-b-backend-1"):
        return False, "target is not in healing allowlist"
    try:
        with _docker_client() as client:
            response = client.post(f"/containers/{target}/{action}", params={"t": 2})
            if response.status_code not in (204, 304):
                return False, f"Docker {action} HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"Docker proxy {type(exc).__name__}"
    return True, f"Docker {action} accepted"


_STATUS_COL = 17          # HAProxy CSV 의 status 칸 (pxname,svname,... 다음 17번째)


def _gateway_status_from_csv(csv: str, letter: str) -> str | None:
    """`chk_{letter}/backend` 서버의 상태 문자열. 줄을 못 찾으면 None("모른다")."""
    prefix = f"chk_{letter},backend,"
    for line in csv.splitlines():
        if line.startswith(prefix):
            cells = line.split(",")
            if len(cells) > _STATUS_COL:
                return cells[_STATUS_COL]
    return None


def _gateway_excluded_from_csv(csv: str, letter: str) -> bool:
    """게이트웨이가 이 스택을 사용자 트래픽에서 제외했는가.

    컨테이너가 사라지면 HAProxy 는 DOWN 이 아니라 **MAINT (resolution)** 으로 간다
    (`resolvers docker` · `init-addr libc,none` — DNS 레코드가 없어진다).
    "DOWN" 문자열만 보면 영원히 못 찾는다 — 실측 2026-11-06: 60초 동안 DOWN 이 한 번도 안 나와
    F-3 검증이 "예상과 다름"으로 늘 실패했다. 상태가 UP 으로 시작하지 않으면 제외된 것으로 본다.
    줄을 못 찾았으면 "모른다"이므로 제외됐다고 하지 않는다 — 모르는 것을 근거로 통과시키지 않는다.
    """
    status = _gateway_status_from_csv(csv, letter)
    return bool(status) and not status.startswith("UP")


def _gateway_csv() -> str:
    return httpx.get(os.getenv("GATEWAY_STATS_URL", "http://gateway:8404/stats;csv"), timeout=1.0).text


def _restart_backend(target: str, run_id: str, seconds: int, attempt_observer=None) -> tuple[bool, str, int | None]:
    started = time.monotonic()
    action_started = time.monotonic()
    stopped, detail = _docker_action(target, "stop")
    if attempt_observer:
        attempt_observer({"action": "backend 중지 요청", "outcome": "성공" if stopped else "실패",
                          "duration_ms": int((time.monotonic() - action_started) * 1000)})
    if not stopped:
        return False, detail, None
    # MAINT(resolution) 로 넘어가는 데 실측 약 9초가 걸린다 — 10초는 너무 빡빡했다
    down_deadline = time.monotonic() + 20
    observed_down = False
    gateway_down = False
    detected_ms = None
    letter = "a" if target.startswith("moa-a-") else "b"
    while time.monotonic() < down_deadline:
        try:
            httpx.get(f"http://{target}:8000/ready", timeout=0.5)
        except httpx.HTTPError:
            observed_down = True
        try:
            gateway_down = _gateway_excluded_from_csv(_gateway_csv(), letter)
        except httpx.HTTPError:
            pass
        if observed_down and gateway_down:
            detected_ms = int((time.monotonic() - started) * 1000)
            break
        if _cancelled(run_id):
            break
        time.sleep(0.5)
    cancelled = _sleep_or_stop(run_id, max(0, seconds - (time.monotonic() - started)))
    # Restart is unconditional after an accepted stop, including cancellation.
    action_started = time.monotonic()
    restarted, restart_detail = _docker_action(target, "restart")
    if attempt_observer:
        attempt_observer({"action": "backend 재시작 요청", "outcome": "성공" if restarted else "실패",
                          "duration_ms": int((time.monotonic() - action_started) * 1000)})
    if not restarted:
        return False, f"backend stopped but restart failed: {restart_detail}", detected_ms

    verification_started = time.monotonic()
    verification_polls = 0
    deadline = time.monotonic() + 30
    consecutive = 0
    while time.monotonic() < deadline:
        verification_polls += 1
        try:
            backend = httpx.get(f"http://{target}:8000/ready", timeout=1.0)
            # 프런트엔드는 A/B 격리 네트워크에만 있어 healing 에서 이름으로 닿지 않는다.
            # 게이트웨이에 sticky 쿠키를 붙여 그 스택의 프런트엔드를 고른다(preflight 와 같은 방식)
            frontend = httpx.get("http://gateway:8501/_stcore/health", timeout=1.0,
                                 headers={"Cookie": f"MOA_STACK={letter}"})
            gateway_up = (_gateway_status_from_csv(_gateway_csv(), letter) or "").startswith("UP")
            consecutive = consecutive + 1 if backend.status_code == 200 and frontend.status_code == 200 and gateway_up else 0
            if consecutive >= 3:
                passed = observed_down and gateway_down and not cancelled
                if attempt_observer:
                    attempt_observer({"action": "backend·frontend·Gateway 독립 확인",
                                      "outcome": "성공" if passed else "예상과 다름",
                                      "poll_count": verification_polls,
                                      "duration_ms": int((time.monotonic() - verification_started) * 1000)})
                return passed, "3 consecutive backend/frontend/Gateway checks passed; exclusion observed=" + str(gateway_down), detected_ms
        except httpx.HTTPError:
            consecutive = 0
        time.sleep(2)
    if attempt_observer:
        attempt_observer({"action": "backend·frontend·Gateway 독립 확인",
                          "outcome": "시간 초과", "poll_count": verification_polls,
                          "duration_ms": int((time.monotonic() - verification_started) * 1000)})
    return False, "backend/frontend independent verification failed within 30 seconds", detected_ms

INJECTION_WAIT_SEC = 6.0     # 해석 서비스 모드 캐시(2초)보다 넉넉히 둔다


def _interpreter_url(target: str) -> str | None:
    url = (config.ORDER_INTERPRETER_BACKUP_URL if target.endswith("backup")
           else config.ORDER_INTERPRETER_PRIMARY_URL)
    return url or None


def _wait_injection_live(target: str, mode: str, run_id: str, no: int) -> bool:
    """주입이 **실제로 적용될 때까지** 기다린다. 적용된 시각부터 재야 MTTD 가 정직하다.

    해석 서비스는 장애 모드를 2초간 캐시한다(interpreter/main.py `_MODE_TTL_SEC`).
    그 전에 보낸 요청은 정상으로 응답하므로 주입 직후부터 재면 **장애가 없던 구간이 MTTD 에 섞인다**.
    실측 2026-11-06: F-2 · F-5 의 첫 합성 요청이 성공해 시도 기록이 "primary:성공" 하나뿐이었고,
    F-4 는 연속 3회 실패 전제를 못 채워 FAIL 로 끝났다.
    """
    url = _interpreter_url(target)
    if not url:
        return True
    deadline = time.monotonic() + INJECTION_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2.0).json().get("mode") == mode:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.3)
    _event(run_id, "injection_not_applied", f"주입 적용을 {INJECTION_WAIT_SEC:.0f}초 안에 확인하지 못함",
           no, {"target": target, "mode": mode})
    return False


def _execute_case(run_id: str, no: int, case: dict) -> dict:
    fault, target, seconds = case["fault_type"], case["target"], int(case["seconds"])
    default_target, mode = workflows.FAULT_DEFAULTS[fault]
    if fault == "F-3":
        mode = "kill"
    elif fault == "F-6":
        mode = "block"
    elif target == "order_interpreter_backup":
        mode = {"F-1": "error", "F-2": "delay", "F-4": "error", "F-5": "invalid"}[fault]

    preloaded_checksum = None
    if fault == "F-6":
        # Warm this exact brand/version cache before injecting the menu read fault.
        preloaded_checksum = menu.catalog("generic", config.MENU_VERSION).get("checksum")
    attempts: list[dict] = []
    first_fault_at: float | None = None      # 장애를 **처음 관측한** 시각 (MTTD 의 끝점)

    def record_attempt(item: dict) -> None:
        nonlocal first_fault_at
        entry = dict(item)
        entry["request_attempt_no"] = entry.get("attempt_no")
        entry["attempt_no"] = len(attempts) + 1
        attempts.append(entry)
        if first_fault_at is None and entry.get("outcome") == "실패":
            first_fault_at = time.monotonic()
        _event(run_id, "recovery_attempt",
               f"시도 {entry.get('attempt_no')}: "
               f"{entry.get('service') or entry.get('action') or '해석 요청'} · "
               f"{entry.get('outcome') or entry.get('result') or '기록'}", no, entry)

    # 회차는 서로 독립이어야 한다 — 앞 회차가 열어 둔 회로를 물려받으면 1차를 건너뛰어
    # 실패를 관측하지 못하고 MTTD · MTTR 이 뒤바뀐다(실측 2026-11-06 F-2 · F-5)
    monitor.reset_circuits()
    injection_id = _inject(run_id, no, fault, target, mode)
    # 주입이 적용된 뒤부터 잰다 — 해석 서비스의 모드 캐시 때문에 직후 요청은 아직 정상이다
    if target.startswith("order_interpreter_"):
        _wait_injection_live(target, mode, run_id, no)
    start = time.monotonic()
    _update_run(run_id, phase="detecting", iteration=no)
    detected_at = None
    verdict, detail, request_count = False, "미검증", 0
    incident_id = None
    try:
        if fault == "F-3":
            _update_run(run_id, phase="recovering", iteration=no)
            ok, detail, detect_offset_ms = _restart_backend(target, run_id, seconds, record_attempt)
            _update_run(run_id, phase="verifying", iteration=no)
            if ok:
                function_ok, function_detail, request_count = _synthetic_interpreter_request(
                    fault, target, 1, run_id, record_attempt)
                ok = ok and function_ok
                detail += f"; synthetic function check: {function_detail}"
            verdict = ok
            detected_at = start + detect_offset_ms / 1000 if detect_offset_ms is not None else time.monotonic()
            incident_id = _create_incident(run_id, no, fault, injection_id, detail)
        elif fault == "F-6":
            from app.services.menu import catalog

            try:
                attempt_started = time.monotonic()
                recovered = catalog("generic", config.MENU_VERSION)
                verdict = bool(recovered.get("stale") and recovered.get("checksum") == preloaded_checksum)
                record_attempt({"action": "동일 버전 캐시 조회", "outcome": "성공" if verdict else "예상과 다름",
                                "duration_ms": int((time.monotonic() - attempt_started) * 1000)})
                detail = "동일 브랜드·버전 캐시 stale fallback" if verdict else "메뉴 조회 fault가 적용되지 않음"
            except db.DbUnavailable as exc:
                detail = f"cache fallback 실패: {type(exc).__name__}"
            detected_at = time.monotonic()
            incident_id = _create_incident(run_id, no, fault, injection_id, detail)
        else:
            # F-4 needs three observations to exercise the breaker; F-1 verifies five synthetic reads.
            _update_run(run_id, phase="recovering", iteration=no)
            samples = 3 if fault == "F-4" else 5 if fault == "F-1" else 1
            verdict, detail, request_count = _synthetic_interpreter_request(fault, target, samples, run_id, record_attempt)
            _update_run(run_id, phase="verifying", iteration=no)
            # 탐지 시각은 **첫 관측 실패**다. 복구 관측이 다 끝난 뒤에 찍으면 MTTD 가 복구 시간까지
            # 흡수해 과대해지고 MTTR 이 0 에 가까워진다 — 실측 2026-11-06: mttd 12,611ms · mttr 13ms.
            # SC-3(복구 60초)이 늘 통과하는 거짓 합격이 되므로 판정이 무의미해진다.
            # 실패를 한 번도 관측하지 못했으면(대체 경로로 바로 성공) 지금을 쓴다 — 그 회차는
            # 애초에 1차 장애를 거치지 않았다는 뜻이고, attempts 기록에 그 사실이 남는다
            detected_at = first_fault_at or time.monotonic()
            incident_id = _create_incident(run_id, no, fault, injection_id, detail)
            if fault == "F-4":
                _clear(run_id, injection_id)
                _update_run(run_id, phase="cooldown_probe", iteration=no)
                if _sleep_or_stop(run_id, _cooldown_remaining(target)):
                    verdict, detail = False, "중지 요청으로 cooldown/probe를 중단했습니다"
                else:
                    probe_started = time.monotonic()
                    probe_ok, probe_detail = _probe_circuit(target, run_id, incident_id)
                    record_attempt({"action": "cooldown 후 단일 probe",
                                    "outcome": "성공" if probe_ok else "승인 대기/실패",
                                    "duration_ms": int((time.monotonic() - probe_started) * 1000)})
                    verdict = verdict and probe_ok
                    detail = f"{detail}; {probe_detail}"
            if fault == "F-5" and detail == "NEEDS_REVIEW":
                verdict = True  # Safe manual-review outcome is the expected F-5 test result.
        recovery_verified_at = time.monotonic()
        mttd_ms = int(max(0, (detected_at or recovery_verified_at) - start) * 1000)
        mttr_ms = int(max(0, recovery_verified_at - (detected_at or start)) * 1000)
        if _sleep_or_stop(run_id, max(0, seconds - (recovery_verified_at - start))):
            verdict, detail = False, "중지 요청으로 회차 종료"
        expected = {
            "F-1": "오류 감지 → 반대 해석기로 전환 → 합성 요청 5건 확인",
            "F-2": "5초 초과 지연 감지 → 대체 해석기 전환 → 총 15초 이내 응답",
            "F-3": "backend 제외 → 재시작 → backend/frontend/Gateway 연속 확인",
            "F-4": "3회 실패 → 60초 차단 → 단일 probe 또는 승인 대기",
            "F-5": "최대 3회 생성·검증 → 필요 시 NEEDS_REVIEW",
            "F-6": "동일 버전·checksum 캐시 폴백",
        }[fault]
        steps = [
            {"name": "장애 감지와 사건 기록", "expected": "주입한 장애를 감지하고 사건을 기록",
             "actual": "사건 기록됨" if incident_id else "사건 기록 없음",
             "duration_ms": mttd_ms, "status": "PASS" if incident_id else "FAIL"},
            {"name": "복구 시도와 독립 확인", "expected": expected, "actual": detail,
             "duration_ms": mttr_ms, "status": "PASS" if verdict else "FAIL"},
        ]
        return {"fault_type": fault, "target": target, "injection_id": injection_id,
                "incident_id": incident_id, "passed": verdict, "detail": detail,
                "synthetic_requests": request_count, "mttd_ms": mttd_ms, "mttr_ms": mttr_ms,
                "attempts": attempts, "steps": steps}
    finally:
        cleanup_started = time.monotonic()
        cleared = _clear(run_id, injection_id)
        _event(run_id, "injection_cleared" if cleared else "cleanup_failed",
               "주입 해제 확인" if cleared else "주입 해제를 확인하지 못함", no,
               {"injection_id": injection_id, "duration_ms": int((time.monotonic() - cleanup_started) * 1000),
                "status": "PASS" if cleared else "FAIL"})


def _run(run: dict) -> None:
    run_id = run["run_id"]
    cases = run["cases"]
    if isinstance(cases, str):
        cases = json.loads(cases)
    results = []
    try:
        _update_run(run_id, status="running", phase="preflight")
        first = cases[0]
        check = workflows.preflight(mode=run["mode"], fault_type=first["fault_type"],
                                    target=first["target"])
        if not check.get("ready"):
            summary = {"preflight_ready": False, "checks": check.get("checks", []),
                       "pass": 0, "fail": 0, "total": len(cases)}
            _update_run(run_id, status="invalid", phase="preflight", summary=summary)
            _event(run_id, "preflight_failed", "실행 전 점검 실패. 장애를 주입하지 않았습니다", details=summary)
            return
        for no, case in enumerate(cases, start=1):
            _renew_lease(run_id)
            if _cancelled(run_id):
                break
            fault, target = case["fault_type"], case["target"]
            db.execute("INSERT INTO healing.workflow_iteration(run_id,iteration_no,fault_type,target,status,phase) "
                       "VALUES (%s,%s,%s,%s,'queued','queued') ON CONFLICT DO NOTHING",
                       (run_id, no, fault, target), role="healing")
            _update_run(run_id, phase="fault_injection", iteration=no)
            _event(run_id, "iteration_started", f"{no}/{len(cases)}회차 · {fault} 시작", no,
                   {"target": target})
            try:
                result = _execute_case(run_id, no, case)
            except Exception as exc:  # worker must always attempt cleanup and keep the run auditable
                result = {"fault_type": fault, "target": target, "passed": False,
                          "detail": f"실행 오류: {type(exc).__name__}"}
                LOG.exception("workflow iteration failed run_id=%s iteration=%s", run_id, no)
            _record_verification(result)
            iteration_status = "PASS" if result.get("passed") else "FAIL"
            if result.get("detail") == "NEEDS_REVIEW":
                iteration_status = "NEEDS_REVIEW"
            elif ("approval" in (result.get("detail") or "").lower()
                  or "승인 대기" in (result.get("detail") or "")):
                iteration_status = "AWAITING_APPROVAL"
            result["status"] = iteration_status
            # 시험 사건도 닫는다. 닫지 않으면 대시보드 E 블록에 "주문 해석 오류 응답"으로 영원히 남고
            # (실측 2026-11-06: 시험 사건 2건이 detected 로 방치), 같은 장애의 열린 사건이 있다는
            # 이유로 다음 사건 생성까지 막는다. AWAITING_APPROVAL 은 사람 승인을 기다리는 정상 상태라 둔다
            if result.get("incident_id") and iteration_status in ("PASS", "FAIL", "NEEDS_REVIEW"):
                monitor.close_incident(result["incident_id"],
                                       recovered=(iteration_status == "PASS"),
                                       manual_check=(iteration_status == "NEEDS_REVIEW"))
            results.append(result)
            db.execute("UPDATE healing.workflow_iteration SET status=%s,phase='verified',incident_id=%s,"
                       "synthetic_requests=%s,result=%s::jsonb,finished_at=NOW(),mttd_ms=%s,mttr_ms=%s "
                       "WHERE run_id=%s AND iteration_no=%s",
                       (iteration_status, result.get("incident_id"), result.get("synthetic_requests", 0),
                        json.dumps(result, ensure_ascii=False), result.get("mttd_ms"), result.get("mttr_ms"),
                        run_id, no), role="healing")
            _event(run_id, "iteration_result", f"{no}/{len(cases)}회차 · {iteration_status}", no,
                   {"detail": result.get("detail"), "incident_id": result.get("incident_id")})
            _renew_lease(run_id)

        db.execute("UPDATE healing.fault_injection SET cleared_at=COALESCE(cleared_at,NOW()) "
                   "WHERE run_id=%s AND action='inject' AND cleared_at IS NULL", (run_id,), role="healing")
        uncleared = db.fetch_one("SELECT COUNT(*) AS n FROM healing.fault_injection "
                                 "WHERE run_id=%s AND action='inject' AND cleared_at IS NULL",
                                 (run_id,), role="healing")["n"]
        if uncleared:
            final_status = "needs_operator_action"
        elif _cancelled(run_id):
            final_status = "stopped"
        else:
            final_status = "completed"  # Means injection workflow ended, never recovery success.
        statuses = [r.get("status") or ("PASS" if r.get("passed") else "FAIL") for r in results]
        mttd_values = [r["mttd_ms"] for r in results if r.get("mttd_ms") is not None]
        mttr_values = [r["mttr_ms"] for r in results if r.get("mttr_ms") is not None]
        summary = {"pass": sum(1 for value in statuses if value == "PASS"),
                   "fail": sum(1 for value in statuses if value == "FAIL"),
                   "needs_review": sum(1 for value in statuses if value == "NEEDS_REVIEW"),
                   "awaiting_approval": sum(1 for value in statuses if value == "AWAITING_APPROVAL"),
                   "invalid": sum(1 for value in statuses if value == "INVALID"),
                   "total": len(cases), "completed_iterations": len(results),
                   "avg_mttd_ms": round(sum(mttd_values) / len(mttd_values)) if mttd_values else None,
                   "avg_mttr_ms": round(sum(mttr_values) / len(mttr_values)) if mttr_values else None}
        _update_run(run_id, status=final_status, phase="cleanup", summary=summary)
        _event(run_id, "run_finished", f"workflow {final_status}", details=summary)
    except Exception:
        LOG.exception("workflow run failed run_id=%s", run_id)
        try:
            db.execute("UPDATE healing.fault_injection SET cleared_at=COALESCE(cleared_at,NOW()) "
                       "WHERE run_id=%s AND action='inject' AND cleared_at IS NULL", (run_id,), role="healing")
            _update_run(run_id, status="failed", phase="cleanup")
            _event(run_id, "run_failed", "실행 중 오류. 남은 주입을 정리했습니다")
        except Exception:
            LOG.exception("workflow emergency cleanup failed run_id=%s", run_id)
    finally:
        db.execute("DELETE FROM healing.workflow_lease WHERE lease_name='active' AND run_id=%s AND worker_id=%s",
                   (run_id, WORKER_ID), role="healing")


CODE_FAULT = {code: fault for fault, code in F_CODE.items()}

# F-3 상시 감시 (PRD 5.6.1 "헬스체크 연속 2회 실패" · 5.6 "준비 대기 최대 30초")
HEALTH_EVERY_SEC = 5.0
HEALTH_FAIL_NEEDED = 2
READY_WAIT_SEC = 30
_HEALTH_FAILS: dict[str, int] = {}
_RESTARTS: dict[str, list[float]] = {}
_HEALTH_AT = 0.0


def _ready(target: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"http://{target}:8000/ready", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


DB_EVERY_SEC = 5.0
_DB_AT = 0.0
_DB_OUTAGE = monitor.DbOutage()


def _db_watch() -> None:
    """F-6 상시 감시 (필수 FT-03) — DB 를 직접 찔러 보고, 끊긴 구간을 메모리에 보류한다.

    사건 기록이 같은 DB 에 있으므로 끊긴 동안에는 적을 수 없다. 복구된 순간에 보류한
    탐지 · 복구 시각으로 완결된 사건을 한 번에 적는다. 서비스는 그 사이 메뉴 캐시 폴백으로 산다.
    여기서는 예외를 밖으로 내지 않는다 — 이 단계가 멈추면 F-6 을 영영 알 수 없다.
    """
    global _DB_AT
    if time.monotonic() - _DB_AT < DB_EVERY_SEC:
        return
    _DB_AT = time.monotonic()
    try:
        db.fetch_one("SELECT 1 AS ok", role="healing")
        ok = True
    except Exception:      # noqa: BLE001 — 어떤 실패든 "지금 못 쓴다"로 본다
        ok = False
    if not ok:
        logging_setup.log("db_unavailable", level="warning", detail="healing 저장소 조회 실패")
    record = _DB_OUTAGE.observe(ok=ok, now=time.monotonic())
    if not record:
        return
    seconds = record["resolved_at"] - record["detected_at"]
    try:
        incident_id = monitor.report_db_outage(
            record, evidence=[f"healing 저장소 조회 연속 {record['failures']}회 실패",
                              f"끊긴 구간 약 {seconds:.0f}초"])
    except Exception:      # noqa: BLE001 — 막 돌아온 DB 가 다시 끊길 수 있다. 다음 순회에 다시 본다
        LOG.exception("db outage report failed")
        return
    if incident_id:
        logging_setup.log("incident_detected", incident_id=incident_id, fault_code="DB_UNAVAILABLE",
                          target="menu_store", detection_metric="db_connection_error")
        logging_setup.log("recovery_verified", incident_id=incident_id,
                          detail=f"저장소 재연결 확인 · 끊긴 구간 약 {seconds:.0f}초")


def _safe(step) -> None:
    """한 단계의 실패가 나머지를 멈추지 않게 한다.

    DB 장애 중에는 _heartbeat · _monitor · _claim 이 모두 깨진다. 한 줄로 묶어 두면
    그때 _db_watch 까지 건너뛰어 **F-6 을 탐지할 수 없다**(실측에서 가장 먼저 깨지던 지점).
    """
    try:
        step()
    except (db.DbUnavailable, psycopg.Error):
        pass
    except Exception:      # noqa: BLE001
        LOG.exception("worker step failed: %s", getattr(step, "__name__", step))


_HEALTH_FIRST: dict[str, float] = {}      # 대상별 첫 헬스체크 실패 시각(단조 시계)


def _health_watch() -> None:
    """F-3 상시 감시 — 헬스체크 연속 2회 실패를 처리 프로세스 중단으로 보고 재시작한다.

    1회 실패로 움직이지 않는다(순간 오류 오탐 방지, PRD 5.6.1 "5초 주기 / 연속 2회").
    3회 이상에서도 다시 만들지 않는다 — 그때는 사건이 이미 열려 있다.
    """
    global _HEALTH_AT
    if time.monotonic() - _HEALTH_AT < HEALTH_EVERY_SEC:
        return
    _HEALTH_AT = time.monotonic()
    for target in (name for name in _allowlist() if "backend" in name):
        if monitor.test_injection_active(target):
            _HEALTH_FAILS[target] = 0          # 시험이 복구를 맡는다 — 끼어들면 둘이 같이 재시작한다
            continue
        if _ready(target):
            _HEALTH_FAILS[target] = 0
            _HEALTH_FIRST.pop(target, None)
            continue
        fails = _HEALTH_FAILS[target] = _HEALTH_FAILS.get(target, 0) + 1
        if fails == 1:
            # MTTD 의 시작점 — 탐지(연속 2회)보다 한 주기 앞선다. 적어 두지 않으면
            # "탐지에 얼마나 걸렸나"를 잴 방법이 없다(저절로 난 장애에는 주입 시각이 없다)
            _HEALTH_FIRST[target] = time.monotonic()
        metrics.record("conn_fail_count", target, fails, threshold=HEALTH_FAIL_NEEDED,
                       breached=fails >= HEALTH_FAIL_NEEDED)
        if fails == HEALTH_FAIL_NEEDED:
            _recover_worker(target)


def _lab_kill_watch() -> None:
    """도구줄(실습) 장애 주입 중 **프로세스 중단(F-3)** 을 실제로 수행한다.

    해석기(F-1 · F-2 · F-5)와 메뉴 저장소(F-6)는 대상 서비스가 `fault_injection` 을 직접 읽어
    스스로 모드를 바꾸지만, 프로세스 중단은 **실행하는 주체가 없으면 행만 남는다**
    (2026-11-07 확인 — 버튼은 눌리고 토스트도 떴지만 컨테이너는 살아 있었다).
    업무 backend 에는 Docker 권한이 없으므로(socket-proxy 는 healing 전용) 여기서 집행한다.

    시험 회차(`run_id` 있음)는 `_run` 이 맡는다 — 여기서는 run_id 가 없는 행만 본다.
    멈춘 뒤 주입을 바로 닫는다. **중단은 한 번 일어나는 사건**이지 유지되는 모드가 아니고,
    되살리는 일은 상시 감시(`_health_watch`)가 평소 경로로 해야 측정이 성립한다.
    """
    if config.ENVIRONMENT != "lab":
        return
    rows = db.fetch_all(
        "SELECT injection_id,target FROM healing.fault_injection "
        " WHERE run_id IS NULL AND action='inject' AND mode='kill' AND cleared_at IS NULL "
        " ORDER BY injected_at", role="healing")
    for row in rows:
        target, injection_id = row["target"], row["injection_id"]
        other = "moa-b-backend-1" if target == "moa-a-backend-1" else "moa-a-backend-1"
        if monitor.test_injection_active(target):
            continue          # 시험이 같은 대상을 쥐고 있다. 다음 순회에 다시 본다
        if not _ready(other):
            # 양쪽을 동시에 내리지 않는다 — 서비스가 통째로 멈추면 복구를 관측할 수 없다
            detail = f"{other} 가 정상이 아니어서 {target} 중지를 건너뛴다"
            ok = False
        else:
            ok, detail = _docker_action(target, "stop")
        db.execute("UPDATE healing.fault_injection SET cleared_at=NOW() WHERE injection_id=%s",
                   (injection_id,), role="healing")
        # 성공은 따로 적지 않는다 — 이어서 상시 감시가 `incident_detected` 를 남긴다.
        # 실패만 남긴다(로그 event 는 D-03 1.5 고정 목록에서만 쓴다 — 새 이름을 늘리지 않는다)
        if not ok:
            logging_setup.log("escalated", level="warning", fault_code="WORKER_DOWN",
                              target=target, detail=detail)
        else:
            LOG.info("lab kill applied: %s (%s)", target, detail)


def _recover_worker(target: str) -> None:
    first = _HEALTH_FIRST.pop(target, None)
    incident_id = monitor.open_incident_for(
        "WORKER_DOWN", target, label=INCIDENT_LABELS["WORKER_DOWN"],
        detection_metric="conn_fail_count",
        evidence=[f"{target} /ready 연속 {HEALTH_FAIL_NEEDED}회 실패"],
        first_signal_sec_ago=(time.monotonic() - first) if first else None)
    if not incident_id:
        return
    logging_setup.log("incident_detected", incident_id=incident_id, fault_code="WORKER_DOWN",
                      target=target, detection_metric="conn_fail_count")
    stamps = _RESTARTS.setdefault(target, [])
    now = time.monotonic()
    if not monitor.restart_allowed(stamps, now=now):
        # 상한을 넘으면 사람에게 넘긴다 — 올라오지 않는 컨테이너를 영원히 재시작하지 않는다
        monitor.close_incident(incident_id, recovered=False)
        logging_setup.log("escalated", level="warning", incident_id=incident_id,
                          detail=f"{target} 재시작 상한 초과(10분 내 {monitor.RESTART_LIMIT}회)")
        return
    logging_setup.log("recovery_started", incident_id=incident_id, fault_code="WORKER_DOWN")
    restart_started = time.monotonic()
    ok, detail = _docker_action(target, "restart")
    stamps.append(now)
    metrics.record("restart_count", target, 1, threshold=1, breached=True)
    monitor.record_attempt(incident_id, action="restart", result="ok" if ok else "fail",
                           tool="docker", tool_args={"target": target, "action": "restart"},
                           error=None if ok else detail,
                           duration_ms=int((time.monotonic() - restart_started) * 1000))
    verified = False
    if ok:
        deadline = time.monotonic() + READY_WAIT_SEC
        probe_started = time.monotonic()
        while time.monotonic() < deadline and not verified:
            verified = _ready(target)
            if not verified:
                time.sleep(1.0)
        # 준비 확인은 복구와 다른 일이다 — 재시작이 성공해도 안 올라오는 경우가 있어 따로 남긴다
        monitor.record_attempt(incident_id, action="probe",
                               result="ok" if verified else "timeout",
                               tool="ready_check", tool_args={"target": target},
                               error=None if verified else f"{READY_WAIT_SEC}초 안에 준비되지 않았다",
                               duration_ms=int((time.monotonic() - probe_started) * 1000))
    # 준비 확인은 **독립 확인**이기도 하다 — 복구한 쪽(재시작)이 스스로 성공을 판정하지 않고
    # /ready 를 따로 물어 본 결과다(PRD 5.3 Verification 독립). 남기지 않으면 F-3 사건의
    # "검증"이 늘 "아직 검증하지 않음"으로 보인다 — 실은 확인했는데 기록이 없는 것이다
    _record_verification({"incident_id": incident_id, "fault_type": "F-3",
                          "detail": f"{target} /ready 응답" if verified
                                    else f"{target} /ready 무응답",
                          "passed": verified})
    monitor.close_incident(incident_id, recovered=verified)
    if verified:
        _HEALTH_FAILS[target] = 0
        logging_setup.log("recovery_verified", incident_id=incident_id,
                          detail=f"{target} 재시작 후 준비 확인")
    else:
        logging_setup.log("escalated", level="warning", incident_id=incident_id, detail=detail)


def _approval_watch() -> None:
    """승인받은 사건의 복구를 **실제로 이어서** 한다.

    `healing.approval` 을 읽는 코드가 API 쪽에만 있어서, 운영자가 [승인]을 눌러도
    상태만 `recovering` 으로 바뀌고 아무도 복구를 시도하지 않았다(2026-11-07 확인).
    그 사건은 영영 닫히지 않고, 열린 사건으로 세어져 같은 장애의 새 탐지까지 막았다.

    **살아 있는 시험이 쥐고 있는 사건은 건드리지 않는다** — 그쪽은 `_run` 이 주인이다.
    승인 직후 몇 초는 기다린다(운영자가 거부로 바꿔 누를 수 있다).
    """
    rows = db.fetch_all(
        "SELECT incident_id, fault_code, target FROM healing.incident "
        " WHERE status = 'recovering' AND updated_at < NOW() - INTERVAL '5 seconds' "
        "   AND (run_id IS NULL OR run_id NOT IN "
        "        (SELECT run_id FROM healing.workflow_run "
        "          WHERE status IN ('queued','running','stop_requested','cleaning'))) "
        " ORDER BY updated_at LIMIT 5", role="healing")
    for row in rows:
        incident_id, code, target = row["incident_id"], row["fault_code"], row["target"]
        logging_setup.log("recovery_started", incident_id=incident_id, fault_code=code)
        ok, detail = _retry_after_approval(code, target, incident_id)
        _close_incident(incident_id, recovered=ok, detail=detail)


def _retry_after_approval(code: str, target: str | None, incident_id: str) -> tuple[bool, str]:
    """승인이 뜻하는 것은 "다시 해 봐"다. 대상 종류마다 다시 해 보는 방법이 다르다."""
    if target and target.startswith("order_interpreter_"):
        fault = CODE_FAULT.get(code)
        if fault:
            ok, detail, _ = _synthetic_interpreter_request(
                fault, target, 1, incident_id,
                attempt_observer=_attempt_recorder(incident_id, target))
            return ok, detail
        return False, f"{code} 는 합성 요청으로 확인할 수 없다"
    if target and target.endswith("-backend-1"):
        started = time.monotonic()
        ok, detail = _docker_action(target, "restart")
        monitor.record_attempt(incident_id, action="restart", result="ok" if ok else "fail",
                               tool="docker", tool_args={"target": target, "action": "restart"},
                               error=None if ok else detail,
                               duration_ms=int((time.monotonic() - started) * 1000))
        if not ok:
            return False, detail
        deadline = time.monotonic() + READY_WAIT_SEC
        probe_started = time.monotonic()
        while time.monotonic() < deadline:
            if _ready(target):
                monitor.record_attempt(incident_id, action="probe", result="ok",
                                       tool="ready_check", tool_args={"target": target},
                                       duration_ms=int((time.monotonic() - probe_started) * 1000))
                return True, f"{target} 재시작 후 준비 확인"
            time.sleep(1.0)
        monitor.record_attempt(incident_id, action="probe", result="timeout",
                               tool="ready_check", tool_args={"target": target},
                               error=f"{READY_WAIT_SEC}초 안에 준비되지 않았다",
                               duration_ms=int((time.monotonic() - probe_started) * 1000))
        return False, f"{target} 재시작 뒤 준비되지 않았다"
    if target == "menu_store":
        started = time.monotonic()
        try:
            db.fetch_one("SELECT 1 AS ok", role="healing")
        except (db.DbUnavailable, psycopg.Error) as exc:
            monitor.record_attempt(incident_id, action="reconnect", result="fail",
                                   tool="menu_store", error=type(exc).__name__,
                                   duration_ms=int((time.monotonic() - started) * 1000))
            return False, f"메뉴 저장소 재조회 실패: {type(exc).__name__}"
        monitor.record_attempt(incident_id, action="reconnect", result="ok",
                               tool="menu_store",
                               duration_ms=int((time.monotonic() - started) * 1000))
        return True, "메뉴 저장소 재조회 성공"
    # 대상을 모르면 복구했다고 적지 않는다 — 지어낸 성공이 무인 복구율을 부풀린다
    return False, f"다시 시도할 대상을 알 수 없다(target={target})"


def _attempt_recorder(incident_id: str, target: str):
    """해석기 호출 시도를 `healing.recovery_attempt` 행으로 옮기는 관측자.

    `interpret_resilient` 가 시도마다 `{attempt_no, service, outcome, fault_code, duration_ms}` 를
    넘겨 준다. 복구 시험은 이것을 회차 JSON 에 담았지만 **실제 사건에는 아무도 붙이지 않아서**
    사건 상세의 "복구 시도"가 비어 있었다. 같은 관측자를 양쪽에 쓴다.

    처음 부른 쪽이면 `retry`, 반대 해석기로 넘어갔으면 `switch_backup` 이다 —
    둘을 구분해야 "전환이 실제로 일어났나"를 사후에 볼 수 있다(SC-12 · F-1 복구 전략).
    """
    preferred = "backup" if target.endswith("_backup") else "primary"

    def observe(attempt: dict) -> None:
        service = attempt.get("service")
        monitor.record_attempt(
            incident_id,
            action="retry" if service == preferred else "switch_backup",
            result="ok" if attempt.get("outcome") == "성공" else "fail",
            tool="order_interpreter", tool_args={"service": service},
            error=attempt.get("fault_code"), duration_ms=attempt.get("duration_ms"))

    return observe


def _close_incident(incident_id: str, *, recovered: bool, detail: str) -> None:
    monitor.close_incident(incident_id, recovered=recovered)
    if recovered:
        logging_setup.log("recovery_verified", incident_id=incident_id, detail=detail)
    else:
        logging_setup.log("escalated", level="warning", incident_id=incident_id, detail=detail)


def _monitor() -> None:
    """상시 감시 — 업무 층이 남긴 메트릭으로 **실제** 사건을 만들고 복구를 독립 검증한다.

    요청 안 복구(재시도 · 대체 전환 · 폴백)는 업무 층이 이미 했다(PRD 5.3 v1.1 복구 경계).
    여기서 하는 일은 사건을 남기고, 지금 정상인지를 합성 요청으로 **독립 검증**한 뒤 닫는 것이다 —
    복구한 쪽이 스스로 "성공"이라고 판정하지 못하게 한다(5.3 Verification 독립).
    """
    for signal in monitor.pending_signals():
        incident_id = monitor.open_incident(signal)
        logging_setup.log("incident_detected", incident_id=incident_id,
                          fault_code=signal["fault_code"], target=signal["target"],
                          detection_metric=signal["metric_key"], trace_id=signal.get("trace_id"))
        fault = CODE_FAULT.get(signal["fault_code"])
        # 합성 요청으로 검증할 수 있는 것은 주문 해석 서비스뿐이다. 그 밖(예외 · 저장소)은
        # 복구했다고 적지 않는다 — 사건을 먼저 남기고 담당자 확인으로 넘긴다
        verifiable = fault is not None and signal["target"].startswith("order_interpreter_")
        if not verifiable or signal["fault_code"] not in monitor.AUTO_RECOVERABLE:
            # 자동 복구 경로가 없는 장애는 복구했다고 적지 않는다 — 기록만 하고 사람에게 넘긴다
            monitor.close_incident(incident_id, recovered=False, manual_check=True)
            logging_setup.log("escalated", level="warning", incident_id=incident_id,
                              detail=f"{signal['fault_code']} 자동 복구 · 검증 경로 없음 — 담당자 확인")
            continue
        logging_setup.log("recovery_started", incident_id=incident_id, fault_code=signal["fault_code"])
        ok, detail, _ = _synthetic_interpreter_request(
            fault, signal["target"], 1, incident_id,
            attempt_observer=_attempt_recorder(incident_id, signal["target"]))
        # 실제 사건에도 독립 확인 결과를 남긴다 — 종전에는 시험 회차만 남아서
        # 사건 상세의 "독립 확인"이 늘 비어 있었다(2026-11-07 실측)
        _record_verification({"incident_id": incident_id, "fault_type": fault,
                              "detail": detail, "passed": ok})
        _close_incident(incident_id, recovered=ok, detail=detail)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if config.ENVIRONMENT != "lab" and os.getenv("HEALING_ENABLE_PRODUCTION_RECOVERY", "0") != "1":
        LOG.warning("lab workflow runner disabled outside ENVIRONMENT=lab")
    while True:
        try:
            _db_watch()                   # 맨 먼저 — DB 가 죽으면 아래 단계가 전부 깨진다
            _safe(_heartbeat)
            _safe(_monitor)               # 상시 감시는 시험 워크플로와 **나란히** 돈다
            _safe(_approval_watch)        # 승인받은 사건의 복구를 이어 간다
            _safe(_lab_kill_watch)        # 도구줄 F-3 집행 — 탐지보다 먼저 둔다
            _safe(_health_watch)          # F-3 은 메트릭이 아니라 헬스체크로 직접 관측한다
            run = _claim() if config.ENVIRONMENT == "lab" else None
            if run:
                _run(run)
            else:
                time.sleep(POLL_SEC)
        except (db.DbUnavailable, psycopg.Error):
            LOG.exception("worker database operation failed")
            time.sleep(2)
        except Exception:
            LOG.exception("worker loop failed")
            time.sleep(2)


if __name__ == "__main__":
    main()
