"""상시 감시 — 업무 층이 남긴 메트릭을 읽어 **실제 장애 사건**을 만든다 (PRD 5.6.1 · FR-013).

지금까지 `healing.incident` 는 장애 시험 워크플로 안에서만 만들어졌다(`is_verification_run=true`).
운영 중 저절로 난 장애는 탐지도 기록도 되지 않아 대시보드 E 블록과 지표 뷰가 비어 있었다.

경계(PRD 5.3 v1.1): 요청 안 복구(재시도 · 대체 전환 · 폴백)는 업무 층이 이미 했다.
여기서 하는 일은 **사건을 만들고 증거를 묶는 것**이고, 프로세스 수준 조치와 검증은 healing 워커가 한다.

판정 조건은 PRD 5.6.1 정본이다.

    dep_5xx_ratio      5xx 연속 2회      -> DEP_HTTP_5XX
    timeout_count      호출 5초 초과 1회  -> DEP_TIMEOUT
    schema_error_rate  스키마 실패 1회    -> AGENT_OUTPUT_INVALID

`dep_latency_ms` 는 추세용이라 그것만으로는 사건이 되지 않는다 — 넘은 호출은 `timeout_count` 로 따로 남는다.
"""

import json
import secrets

import psycopg

from app import db

# metric_key -> (장애 코드, 사건이 되는 최소 관측 수, 사람이 읽는 이름)
_RULES = {
    "dep_5xx_ratio": ("DEP_HTTP_5XX", 2, "주문 해석 오류 응답"),
    "timeout_count": ("DEP_TIMEOUT", 1, "응답 지연"),
    "schema_error_rate": ("AGENT_OUTPUT_INVALID", 1, "잘못된 결과 형식"),
    # 처리되지 않은 예외 1회로 사건이 된다 (PRD 5.6.1 에이전트 실행 FT-02)
    "agent_error_count": ("AGENT_EXCEPTION", 1, "처리 중 예외"),
    # 쿼리 2초 초과 1회 (PRD 5.6.1 DB FT-03). 연결 오류는 여기 두지 않는다 —
    # DB 가 죽은 구간은 기록 자체가 안 되므로 DbOutage 가 메모리에 보류해 복구 후 적는다
    "slow_query_count": ("DB_UNAVAILABLE", 1, "메뉴 저장소 응답 지연"),
}
# F-3(agent_error_count · restart_count)과 F-6(db_connection_error · slow_query_count)은
# 수집 훅이 들어오는 단계에서 함께 넣는다. 수집이 없는 규칙을 미리 두면 사건이 만들어지지 않거나,
# 만들어져도 닫을 검증 경로가 없어 열린 채 쌓인다

# 아직 닫히지 않은 사건 — 같은 장애가 **같은 대상에** 열려 있으면 새로 만들지 않는다(PRD 5.6.1 Monitor 책임).
#
# `manual_check` 를 뺐다. 그것은 자동화가 포기하고 **사람에게 넘긴 상태**이지 진행 중이 아니다.
# 넣어 두면 담당자가 정리할 때까지 그 장애 코드의 새 사건이 영영 열리지 않는다 —
# 2026-11-07 실측에서 `manual_check` 로 남은 F-5 사건 하나가 F-5 탐지를 통째로 막고 있었다.
# 같은 장애가 계속 나면 사건이 다시 열려야 한다. 그래야 횟수와 MTTD 가 침묵하지 않는다.
_OPEN_STATUSES = ("detected", "recovering", "awaiting_approval")

WINDOW_SEC = 60


def pending_signals(window_sec: int = WINDOW_SEC) -> list[dict]:
    """사건이 될 만한 관측 묶음. 이미 사건에 묶인 행과 열린 사건은 제외한다."""
    rows = db.fetch_all(
        "SELECT id,metric_key,target,value,breached,measured_at,trace_id "
        "  FROM healing.runtime_metric m "
        " WHERE breached = true AND incident_id IS NULL "
        "   AND measured_at > NOW() - (%s * INTERVAL '1 second') "
        # 장애 시험 구간의 관측은 실제 사건이 아니다. 시험 주입에만 run_id 가 있다 —
        # 섞으면 SC-4 무인 복구율과 사건 수가 오염된다(R-14 측정 오염과 같은 종류).
        # 주입 구간과 **겹치는 동안의** 관측만 뺀다 — 주입 전후의 실제 장애는 그대로 사건이 된다
        "   AND NOT EXISTS (SELECT 1 FROM healing.fault_injection fi "
        "                    WHERE fi.run_id IS NOT NULL AND fi.target = m.target "
        "                      AND m.measured_at >= fi.injected_at "
        "                      AND (fi.cleared_at IS NULL OR m.measured_at <= fi.cleared_at)) "
        " ORDER BY measured_at, id", (window_sec,), role="healing")

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        rule = _RULES.get(row["metric_key"])
        if not rule:
            continue                                   # 추세 메트릭(dep_latency_ms 등)은 사건 재료가 아니다
        grouped.setdefault((rule[0], row["target"]), []).append(row)

    signals = []
    for (fault_code, target), group in grouped.items():
        # 같은 장애 코드로 묶되 최소 관측 수는 메트릭마다 다르다 — 가장 느슨한 것을 쓴다
        need = min(_RULES[r["metric_key"]][1] for r in group)
        if len(group) < need:
            continue
        if _open_incident_count(fault_code, target):
            continue
        label = _RULES[group[0]["metric_key"]][2]
        signals.append({
            "fault_code": fault_code, "target": target, "label": label,
            "metric_key": group[0]["metric_key"],
            "metric_ids": [r["id"] for r in group],
            "detected_at": group[0]["measured_at"],
            "trace_id": next((r["trace_id"] for r in group if r["trace_id"]), None),
            "evidence": [f"{r['metric_key']}={r['value']}" for r in group],
        })
    return signals


def _open_incident_count(fault_code: str, target: str) -> int:
    """같은 장애가 **같은 대상에** 열려 있는가.

    `target` 을 받아 놓고 조건에 쓰지 않던 동안, 앱 A 의 F-3 이 열려 있으면 앱 B 의 F-3 을
    탐지하지 못했다. 복제본 전환(SC-12)은 한쪽이 죽은 동안 다른 쪽을 보는 일이라 치명적이다.
    대상을 적지 않고 만든 지난 사건(target IS NULL)은 어느 대상과도 겹치지 않는 것으로 본다.
    """
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM healing.incident "
        " WHERE fault_code = %s AND target IS NOT DISTINCT FROM %s "
        "   AND status = ANY(%s) AND is_verification_run = false",
        (fault_code, target, list(_OPEN_STATUSES)), role="healing")
    return (row or {}).get("n", 0)


def open_incident(signal: dict) -> str:
    """사건을 만들고 쓴 관측을 묶는다.

    `injected_at` 은 **실습 주입이 있을 때만** 채운다(`link_lab_injection`). 저절로 난 장애에는
    주입 시각이 없으므로 비워 두고, MTTD 평균에서 빠진다.
    """
    incident_id = "INC-" + secrets.token_hex(6).upper()
    trace_id = signal.get("trace_id") or f"tr_{secrets.token_hex(6).upper()}"
    # `detected_at` 은 **알아챈 시각**(지금)이고, `first_signal_at` 은 **첫 위반 관측 시각**이다.
    # 종전에는 detected_at 에 첫 관측 시각을 넣어 둘이 같았고, 그래서 탐지에 걸린 시간을 잴 수 없었다.
    db.execute(
        "INSERT INTO healing.incident(incident_id,trace_id,fault_code,target,status,status_label,"
        "first_signal_at,detected_at,detection_metric,detection_evidence,is_verification_run) "
        "VALUES (%s,%s,%s,%s,'detected',%s,%s,NOW(),%s,%s,false)",
        (incident_id, trace_id, signal["fault_code"], signal.get("target"), signal["label"],
         signal["detected_at"], signal["metric_key"], signal["evidence"]), role="healing")
    # 쓴 행에 사건을 묶어 둔다 — 안 하면 다음 순회에서 같은 관측을 또 세어 사건이 쏟아진다
    db.execute("UPDATE healing.runtime_metric SET incident_id = %s WHERE id = ANY(%s)",
               (incident_id, signal["metric_ids"]), role="healing")
    link_lab_injection(incident_id, signal["target"])
    return incident_id



# `healing.recovery_attempt.action` 의 CHECK 와 같아야 한다 — 어긋나면 INSERT 가 조용히 실패한다
ATTEMPT_ACTIONS = ("retry", "switch_backup", "restart", "resume",
                   "regenerate", "reconnect", "cache_fallback", "probe")


def record_attempt(incident_id: str | None, *, action: str, result: str,
                   tool: str | None = None, tool_args: dict | None = None,
                   error: str | None = None, duration_ms: int | None = None) -> None:
    """복구로 **무엇을 했는지** 한 줄 남긴다 (D-07 4.15).

    왜 필요한가: 사건은 "정상 복구"로 닫히는데 무엇을 해서 복구됐는지가 어디에도 없었다.
    `healing.recovery_attempt` 는 스키마 · 뷰 · 화면이 모두 읽는데 **INSERT 하는 코드가
    아예 없어서** 사건 29건에 0행이었다(2026-11-07 실측). 그래서 사건 상세의 "복구 시도"가
    늘 비고, `v_metrics.avg_attempts` 가 늘 0.00 이었다 — 숫자가 0 이면 "시도 없이 복구됐다"로
    읽히는데 실은 기록이 없는 것이다. 둘은 전혀 다른 뜻이다.

    복구 시험(T2)은 회차 결과 JSON 에 시도를 남기므로 **시험만 증거가 있고 실제 장애는 없었다.**
    여기가 그 비대칭을 없애는 한 곳이다.

    기록이 실패해도 복구를 멈추지 않는다 — 기록은 복구의 전제가 아니다.
    """
    if not incident_id:
        return
    if action not in ATTEMPT_ACTIONS:
        raise ValueError(f"action={action} 은 recovery_attempt 의 허용값이 아니다")
    if result not in ("ok", "fail", "timeout"):
        raise ValueError(f"result={result} 은 recovery_attempt 의 허용값이 아니다")
    try:
        db.execute(
            "INSERT INTO healing.recovery_attempt"
            "(incident_id,attempt_no,action,wait_ms,tool,tool_args,result,error,started_at,ended_at) "
            "SELECT %s, COALESCE(MAX(attempt_no),0)+1, %s, 0, %s, %s::jsonb, %s, %s,"
            "       NOW() - (COALESCE(%s,0) * INTERVAL '1 millisecond'), NOW() "
            "  FROM healing.recovery_attempt WHERE incident_id = %s",
            (incident_id, action, tool,
             json.dumps(tool_args or {}, ensure_ascii=False), result,
             (error or None), duration_ms, incident_id), role="healing")
    except (db.DbUnavailable, psycopg.Error):
        # 사건이 아직 없거나(외래키) DB 가 흔들리는 중이면 기록만 포기한다
        pass


def close_incident(incident_id: str, *, recovered: bool, manual_check: bool = False) -> None:
    """사건을 닫는다. 칸 이름은 `healing.incident` 그대로다 — `recovered_at` 은 없다.

    복구 완료는 `resolved_at`, 자동 복구 실패는 `failed_at` + `escalated_at` 이다.
    `recovery_started_at` 은 처음 값을 지킨다 — MTTR 구간이 뒤로 밀리면 안 된다.

    `manual_check` 는 담당자 확인으로 넘긴 경우다 — F-5 가 NEEDS_REVIEW 로 끝나는 것은
    안전한 기대 결과이지 복구도 실패도 아니다. 복구 시각 · 실패 시각을 지어내지 않는다.
    """
    if manual_check:
        db.execute("UPDATE healing.incident SET status='manual_check',status_label=%s,"
                   "recovery_started_at=COALESCE(recovery_started_at,detected_at),"
                   "escalated_at=NOW() WHERE incident_id=%s",
                   ("담당자 확인 필요", incident_id), role="healing")
        return
    if recovered:
        db.execute("UPDATE healing.incident SET status='recovered',status_label=%s,"
                   "recovery_started_at=COALESCE(recovery_started_at,detected_at),"
                   "resolved_at=NOW() WHERE incident_id=%s",
                   ("정상 복구", incident_id), role="healing")
        return
    db.execute("UPDATE healing.incident SET status='failed',status_label=%s,"
               "recovery_started_at=COALESCE(recovery_started_at,detected_at),"
               "failed_at=NOW(),escalated_at=NOW() WHERE incident_id=%s",
               ("자동 복구 실패", incident_id), role="healing")


# --- 폴링으로 직접 관측한 장애 (F-3) ----------------------------------------------------------
# 사건을 만드는 입구가 둘이다. 메트릭을 세는 pending_signals 와, 헬스체크처럼 **직접 관측**하는 이쪽.
# 둘 다 Monitor 역할이고, 중복 방지 기준(열린 사건)도 같다.
def open_incident_for(fault_code: str, target: str, *, label: str, detection_metric: str,
                      evidence: list[str], trace_id: str | None = None,
                      first_signal_sec_ago: float | None = None) -> str | None:
    """직접 관측한 장애의 사건. 같은 장애가 같은 대상에 열려 있으면 만들지 않고 None 을 돌려준다.

    `first_signal_sec_ago` 는 **첫 징후를 본 지 몇 초 지났는가**다(예: 헬스체크 첫 실패).
    호출부가 단조 시계로 재므로 초 차이로 받아 DB 시각으로 환산한다 — 시계가 두 벌이 되지 않게.
    """
    if _open_incident_count(fault_code, target):
        return None
    incident_id = "INC-" + secrets.token_hex(6).upper()
    db.execute(
        "INSERT INTO healing.incident(incident_id,trace_id,fault_code,target,status,status_label,"
        "first_signal_at,detection_metric,detection_evidence,is_verification_run) "
        "VALUES (%s,%s,%s,%s,'detected',%s,"
        "        CASE WHEN %s IS NULL THEN NULL ELSE NOW() - (%s * INTERVAL '1 second') END,"
        "        %s,%s,false)",
        (incident_id, trace_id or f"tr_{secrets.token_hex(6).upper()}", fault_code, target, label,
         first_signal_sec_ago, first_signal_sec_ago, detection_metric, evidence), role="healing")
    link_lab_injection(incident_id, target)
    return incident_id


def link_lab_injection(incident_id: str, target: str, *, window_sec: int = 180) -> bool:
    """실습 주입으로 생긴 사건이면 그 주입과 묶고 `injected_at` 을 채운다.

    **MTTD 가 측정되는 유일한 경로다.** `v_metrics.mttd_sec` 은 `detected_at - 주입 시각`인데,
    주입과 사건을 묶는 코드가 시험 워크플로에만 있었다. 그 사건들은 `is_verification_run = true`
    라 뷰에서 빠지므로, 대시보드 B 의 "탐지까지" 칸이 **구조적으로 항상 비어 있었다**
    (2026-11-07 실측 `mttd_sec: null` · 사건 11건). SC-2(15초 이하)를 재려면 이 연결이 있어야 한다.

    저절로 난 실제 장애에는 주입 행이 없다 — 그쪽은 그대로 `injected_at` 없이 남고
    MTTD 평균에서 빠진다. 지어낸 시각을 채우지 않는다.
    """
    row = db.fetch_one(
        "SELECT injection_id, injected_at FROM healing.fault_injection "
        " WHERE run_id IS NULL AND action = 'inject' AND incident_id IS NULL AND target = %s "
        "   AND injected_at > NOW() - (%s * INTERVAL '1 second') "
        " ORDER BY injected_at DESC LIMIT 1", (target, window_sec), role="healing")
    if not row:
        return False
    db.execute("UPDATE healing.fault_injection SET incident_id = %s WHERE injection_id = %s",
               (incident_id, row["injection_id"]), role="healing")
    db.execute("UPDATE healing.incident SET injected_at = %s WHERE incident_id = %s",
               (row["injected_at"], incident_id), role="healing")
    return True


def test_injection_active(target: str) -> bool:
    """이 대상에 **시험 주입**이 걸려 있는가. 시험 주입에만 run_id 가 있다.

    걸려 있으면 상시 감시는 비켜선다 — 시험이 만든 장애를 실제 사건으로 또 세면
    SC-4 무인 복구율이 오염되고, 시험이 복구하는 중에 둘이 같이 재시작할 수도 있다.
    """
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM healing.fault_injection "
        " WHERE target = %s AND action = 'inject' AND run_id IS NOT NULL AND cleared_at IS NULL",
        (target,), role="healing")
    return bool((row or {}).get("n", 0))


# 재시작 상한 (PRD 5.6 F-3 "같은 세션 재실행 1회 · 10분 내 재시작 2회")
# 상한이 없으면 올라오지 않는 컨테이너를 영원히 재시작한다 — 폭주 방어의 일부다.
RESTART_LIMIT = 2
RESTART_WINDOW_SEC = 600


def restart_allowed(stamps: list[float], *, now: float) -> bool:
    """최근 창 안의 재시작 수가 상한 미만인가. 넘으면 사람에게 넘긴다(에스컬레이션)."""
    recent = [t for t in stamps if now - t < RESTART_WINDOW_SEC]
    return len(recent) < RESTART_LIMIT

# 자동 복구가 가능한 장애 (PRD 5.6 복구정책)
#
# AGENT_EXCEPTION 의 정책은 "요청 단위로 잡아 마지막 체크포인트부터 1회 재실행"이지만
# 업무 층에 그 재실행이 아직 없다. 자동 복구했다고 적으면 거짓이고 SC-4 무인 복구율이
# 부풀려지므로, 기록만 하고 담당자 확인(manual_check)으로 넘긴다.
AUTO_RECOVERABLE = frozenset({"DEP_HTTP_5XX", "DEP_TIMEOUT", "AGENT_OUTPUT_INVALID",
                              "WORKER_DOWN", "DB_UNAVAILABLE"})


# --- F-6 저장소 장애 (필수 FT-03) -------------------------------------------------------------
DB_FAIL_NEEDED = 2          # PRD 5.6.1 DB 판정 조건 "연결 오류 연속 2회"


class DbOutage:
    """DB 가 죽은 동안의 사건을 **메모리에 보류**하고 복구된 순간에 한 번에 보고한다.

    사건 기록이 같은 DB 에 있으므로 F-6 중에는 INSERT 자체가 불가능하다. 실시간으로 적으려 하면
    기록이 통째로 사라지고 MTTD · MTTR 을 낼 수 없다. F-6 은 캐시 폴백으로 서비스가 살아 있으니
    실시간 표시보다 기록의 정확성이 중요하다 — 복구 후 완결된 사건을 적는다.

    1회 실패로 사건을 만들지 않는다(순간 오류 오탐 방지). 연속 2회에서 탐지로 본다.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.detected_at: float | None = None
        self.first_failure_at: float | None = None      # 첫 실패 — MTTD 의 시작점

    def observe(self, *, ok: bool, now: float) -> dict | None:
        """관측 한 번. **복구된 순간에만** 적을 사건을 돌려주고, 그 밖에는 None 이다."""
        if not ok:
            self.failures += 1
            if self.failures == 1:
                self.first_failure_at = now   # 탐지(연속 2회)보다 한 주기 앞선다
            if self.failures == DB_FAIL_NEEDED:
                self.detected_at = now
            return None
        if self.detected_at is None:
            self.failures = 0                 # 연속 2회에 못 미친 깜빡임은 사건이 아니다
            self.first_failure_at = None
            return None
        record = {"detected_at": self.detected_at, "resolved_at": now, "failures": self.failures,
                  "first_failure_at": self.first_failure_at or self.detected_at}
        self.failures = 0
        self.detected_at = None
        self.first_failure_at = None
        return record


def report_db_outage(record: dict, *, evidence: list[str]) -> str | None:
    """보류했던 F-6 사건을 복구 후에 적는다. 탐지 · 복구 시각은 보류한 값 그대로 쓴다."""
    if _open_incident_count("DB_UNAVAILABLE", "menu_store"):
        return None
    incident_id = "INC-" + secrets.token_hex(6).upper()
    since_detected = record["resolved_at"] - record["detected_at"]
    since_first = record["resolved_at"] - record.get("first_failure_at", record["detected_at"])
    db.execute(
        "INSERT INTO healing.incident(incident_id,trace_id,fault_code,target,status,status_label,"
        "first_signal_at,detected_at,recovery_started_at,resolved_at,detection_metric,"
        "detection_evidence,is_verification_run) "
        "VALUES (%s,%s,'DB_UNAVAILABLE','menu_store','recovered',%s,"
        "        NOW() - (%s * INTERVAL '1 second'),"
        "        NOW() - (%s * INTERVAL '1 second'), NOW() - (%s * INTERVAL '1 second'), NOW(),"
        "        'db_connection_error',%s,false)",
        (incident_id, f"tr_{secrets.token_hex(6).upper()}", "메뉴 저장소 연결 끊김",
         since_first, since_detected, since_detected, evidence), role="healing")
    # 이 사건은 **이미 복구된 뒤에** 적힌다 — 복구는 재연결이 성공한 것이고, 그 사실을 남긴다.
    # 사건만 적고 시도를 비워 두면 화면의 "복구 시도"가 비어 "무엇을 해서 살아났나"를 알 수 없다
    record_attempt(incident_id, action="reconnect", result="ok", tool="menu_store",
                   tool_args={"failures": record.get("failures")},
                   duration_ms=int(max(since_detected, 0) * 1000))
    return incident_id


def reset_circuits() -> None:
    """회로를 모두 정상으로 되돌린다 — **장애 시험 회차를 시작하기 전에만** 쓴다.

    왜 필요한가(실측 2026-11-06): F-1 시험이 1차 회로를 열어 놓자 뒤따른 F-2 · F-5 시험이
    냉각(60초)이 끝나기 전에 돌아 1차를 아예 부르지 않았다. 실패를 관측하지 못하니 탐지 시각이
    복구 뒤로 밀려 MTTR 이 9ms · 15ms 로 나왔다 — 늘 통과하는 거짓 합격이다.

    되돌리는 것이 실제 상태와 어긋나지 않는 이유: preflight 가 직전에 두 해석기 · 두 backend ·
    게이트웨이의 정상을 확인한다. 시험은 정상 상태에서 주입해야 측정이 유효하다.
    운영 중에는 쓰지 않는다 — 회로는 업무 층의 관측으로만 열리고 닫혀야 한다.
    """
    db.execute("UPDATE healing.circuit_state "
               "SET state='up',consecutive_failures=0,cooldown_until=NULL,updated_at=NOW()",
               role="healing")
