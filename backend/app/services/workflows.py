"""Lab-only, single-flight workflow API support for the existing healing worker."""

import json
import secrets
from datetime import datetime, timezone

import httpx
import psycopg

from app import config, db, ids
from app.services import incidents

MAX_STEPS = 20
MAX_REPETITIONS = 10
MAX_INJECTION_SECONDS = 120
FAULT_DEFAULTS = {
    "F-1": ("order_interpreter_primary", "error"),
    "F-2": ("order_interpreter_primary", "delay"),
    "F-3": ("moa-a-backend-1", "kill"),
    "F-4": ("order_interpreter_primary", "error"),
    "F-5": ("order_interpreter_primary", "invalid"),
    "F-6": ("menu_store", "block"),
}
FAULT_LABELS = {
    "F-1": "해석기 HTTP 오류 전환", "F-2": "해석기 지연 전환",
    "F-3": "backend 재시작·복귀", "F-4": "회로 차단·probe",
    "F-5": "출력 검증·재생성", "F-6": "메뉴 캐시 폴백",
}
RECOVERABLE = ("F-1", "F-2", "F-3", "F-5", "F-6")


def templates() -> list[dict]:
    return [{"template_id": code, "fault_type": code, "label": label,
             "default_target": FAULT_DEFAULTS[code][0],
             "default_seconds": 1, "default_repetitions": 1,
             "description": label}
            for code, label in FAULT_LABELS.items()]


def _http_check(name: str, url: str, timeout: float = 3.0,
                headers: dict[str, str] | None = None) -> dict:
    try:
        response = httpx.get(url, timeout=timeout, headers=headers)
        return {"name": name, "ready": response.status_code == 200,
                "detail": "정상" if response.status_code == 200 else f"HTTP {response.status_code}"}
    except httpx.HTTPError as exc:
        return {"name": name, "ready": False,
                "detail": f"연결할 수 없음 ({type(exc).__name__})"}


def preflight(*, mode: str = "quick", fault_type: str = "F-1", target: str | None = None) -> dict:
    """Check prerequisites without writing an injection or mutating app data."""
    checks = [{"name": "lab 환경", "ready": config.ENVIRONMENT == "lab",
               "detail": "실습 환경" if config.ENVIRONMENT == "lab" else "ENVIRONMENT=lab에서만 실행 가능"}]
    if config.db_configured():
        health = db.health()
        checks.append({"name": "DB 연결", "ready": health["state"] == "connected",
                       "detail": health["text"]})
        try:
            rows = db.fetch_all(
                "SELECT to_regclass('healing.workflow_run') AS run_table, "
                "to_regclass('healing.workflow_worker') AS worker_table", role="healing")
            schema_ok = bool(rows and rows[0]["run_table"] and rows[0]["worker_table"])
            checks.append({"name": "workflow 스키마", "ready": schema_ok,
                           "detail": "준비됨" if schema_ok else "backend/sql/15_schema_workflows.sql 적용 필요"})
            worker = db.fetch_one(
                "SELECT worker_id, last_seen, proxy_ok, allowlist "
                "FROM healing.workflow_worker ORDER BY last_seen DESC LIMIT 1", role="healing")
            fresh = bool(worker and (datetime.now(timezone.utc) - worker["last_seen"]).total_seconds() <= 15)
            checks.append({"name": "healing worker", "ready": fresh,
                           "detail": "실행 중" if fresh else "worker heartbeat 없음"})
            allowlist = worker.get("allowlist") if worker else []
            proxy_ready = fresh and bool(worker and worker["proxy_ok"])
            checks.append({"name": "Docker proxy·allowlist", "ready": proxy_ready,
                           "detail": "연결·허용목록 확인" if proxy_ready else "worker proxy 확인 또는 allowlist 미준비"})
            backend_allowlist_ok = all(name in (allowlist or []) for name in ("moa-a-backend-1", "moa-b-backend-1"))
            checks.append({"name": "backend allowlist", "ready": fresh and backend_allowlist_ok,
                           "detail": "A/B allowed" if fresh and backend_allowlist_ok else "A/B backend missing from allowlist"})
        except (db.DbUnavailable, psycopg.Error) as exc:
            checks.append({"name": "workflow 스키마", "ready": False,
                           "detail": f"스키마/worker 상태 확인 실패 ({type(exc).__name__})"})
            allowlist = []
    else:
        checks.extend([
            {"name": "DB 연결", "ready": False, "detail": "DATABASE_URL 미설정"},
            {"name": "workflow 스키마", "ready": False, "detail": "DB 연결 필요"},
            {"name": "healing worker", "ready": False, "detail": "DB 연결 필요"},
            {"name": "Docker proxy·allowlist", "ready": False, "detail": "DB 연결 필요"},
        ])
        allowlist = []

    checks.append(_http_check("Gateway", "http://gateway:8404/stats;csv"))
    for role in ("primary", "backup"):
        url = {"primary": config.ORDER_INTERPRETER_PRIMARY_URL,
               "backup": config.ORDER_INTERPRETER_BACKUP_URL}[role]
        checks.append(_http_check(f"interpreter {role}", f"{url}/health") if url else
                      {"name": f"interpreter {role}", "ready": False, "detail": "주소 미설정"})

    for letter in ("a", "b"):
        name = f"moa-{letter}-backend-1"
        checks.append(_http_check(f"{letter.upper()} backend", f"http://{name}:8000/ready"))
        # Frontends intentionally stay on their isolated A/B networks, so the
        # ops backend cannot resolve their container DNS names. Probe each one
        # through the shared Gateway using its sticky routing cookie instead.
        checks.append(_http_check(
            f"{letter.upper()} frontend", "http://gateway:8501/_stcore/health",
            headers={"Cookie": f"MOA_STACK={letter}"},
        ))
    if fault_type == "F-3" and target:
        checks.append({"name": "F-3 allowlisted target", "ready": target in allowlist,
                       "detail": "allowlisted" if target in allowlist else "target missing from worker allowlist"})
    return {"ready": bool(checks) and all(c["ready"] for c in checks), "checks": checks,
            "environment": config.ENVIRONMENT, "checked_at": datetime.now(timezone.utc).isoformat()}


def build_cases(*, mode: str, fault_type: str, target: str | None,
                repetitions: int, injection_seconds: int) -> list[dict]:
    if mode not in ("quick", "full", "custom"):
        raise ValueError("mode는 quick, full, custom 중 하나여야 합니다")
    if fault_type not in FAULT_DEFAULTS:
        raise ValueError("지원하지 않는 장애 유형입니다")
    if not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(f"반복 횟수는 1~{MAX_REPETITIONS}회여야 합니다")
    if not 1 <= injection_seconds <= MAX_INJECTION_SECONDS:
        raise ValueError(f"주입 시간은 1~{MAX_INJECTION_SECONDS}초여야 합니다")
    if mode == "quick":
        repetitions = 1
    if mode == "full":
        cases = []
        for code in RECOVERABLE:
            for ix in range(5):
                default_target, _ = FAULT_DEFAULTS[code]
                case_target = (f"moa-{'a' if ix % 2 == 0 else 'b'}-backend-1"
                               if code == "F-3" else default_target)
                cases.append({"fault_type": code, "target": case_target,
                              "seconds": injection_seconds})
        return cases

    default_target, _ = FAULT_DEFAULTS[fault_type]
    chosen = target or default_target
    if fault_type in ("F-1", "F-2", "F-4", "F-5") and chosen not in (
            "order_interpreter_primary", "order_interpreter_backup"):
        raise ValueError("해석기 장애 대상은 primary 또는 backup이어야 합니다")
    if fault_type == "F-3" and chosen not in ("moa-a-backend-1", "moa-b-backend-1"):
        raise ValueError("F-3 대상은 허용목록 backend A 또는 B여야 합니다")
    if fault_type == "F-6" and chosen != "menu_store":
        raise ValueError("F-6 대상은 menu_store여야 합니다")
    return [{"fault_type": fault_type, "target": chosen, "seconds": injection_seconds}
            for _ in range(repetitions)]


def create_run(*, mode: str, fault_type: str, target: str | None,
               repetitions: int, injection_seconds: int, idempotency_key: str | None = None) -> dict:
    if config.ENVIRONMENT != "lab":
        raise PermissionError("장애 시험은 ENVIRONMENT=lab에서만 실행할 수 있습니다")
    cases = build_cases(mode=mode, fault_type=fault_type, target=target,
                        repetitions=repetitions, injection_seconds=injection_seconds)
    if (mode != "full" and len(cases) > MAX_STEPS) or (mode == "full" and len(cases) != 25):
        raise ValueError("workflow 단계 상한을 초과했습니다")
    check = preflight(mode=mode, fault_type=fault_type, target=target)
    if not check["ready"]:
        return {"ready": False, "preflight": check}
    run_id = "run_" + secrets.token_hex(8)
    try:
        with db.transaction(role="healing") as cur:
            cur.execute(
                "INSERT INTO healing.workflow_run "
                "(run_id, mode, template_id, environment, cases, total_iterations, "
                " idempotency_key, preflight) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING run_id",
                (run_id, mode, fault_type if mode != "full" else "full-25", config.ENVIRONMENT,
                 json.dumps(cases), len(cases), idempotency_key, json.dumps(check)))
            row = cur.fetchone()
            if row is None and idempotency_key:
                cur.execute("SELECT run_id FROM healing.workflow_run WHERE idempotency_key=%s",
                            (idempotency_key,))
                row = cur.fetchone()
                return get_run(row["run_id"])
            cur.execute(
                "INSERT INTO healing.workflow_event(run_id,event_type,summary,details) "
                "VALUES (%s,'queued','사전 점검 통과, 실행 대기',%s::jsonb)",
                (run_id, json.dumps({"case_count": len(cases)})))
    except psycopg.errors.UniqueViolation as exc:
        raise RuntimeError("이미 실행 중인 workflow가 있습니다") from exc
    return get_run(run_id)


def get_run(run_id: str) -> dict | None:
    row = db.fetch_one("SELECT * FROM healing.workflow_run WHERE run_id=%s", (run_id,), role="healing")
    if not row:
        return None
    iterations = db.fetch_all(
        "SELECT iteration_no,fault_type,target,status,phase,injection_id,incident_id,"
        "synthetic_requests,result,started_at,finished_at,mttd_ms,mttr_ms "
        "FROM healing.workflow_iteration WHERE run_id=%s ORDER BY iteration_no",
        (run_id,), role="healing")
    events = db.fetch_all(
        "SELECT event_type,summary,details,created_at FROM healing.workflow_event "
        "WHERE run_id=%s ORDER BY event_id DESC LIMIT 500", (run_id,), role="healing")
    return {**row, "iterations": iterations, "events": list(reversed(events))}


def list_runs(limit: int = 20) -> list[dict]:
    rows = db.fetch_all(
        "SELECT run_id,mode,template_id,status,phase,current_iteration,total_iterations,"
        "created_at,started_at,finished_at,summary FROM healing.workflow_run "
        "ORDER BY created_at DESC LIMIT %s", (max(1, min(limit, 100)),), role="healing")
    return rows


def request_stop(run_id: str) -> dict | None:
    with db.transaction(role="healing") as cur:
        cur.execute(
            "UPDATE healing.workflow_run SET cancel_requested=true,status='stop_requested',"
            "phase='stop_requested',updated_at=NOW() WHERE run_id=%s "
            "AND status IN ('queued','preflight','running','stop_requested','cleaning') RETURNING run_id",
            (run_id,))
        row = cur.fetchone()
        if row:
            cur.execute("INSERT INTO healing.workflow_event(run_id,event_type,summary) "
                        "VALUES (%s,'stop_requested','중지 요청. 합성 요청을 멈추고 장애를 정리합니다')",
                        (run_id,))
    return get_run(run_id) if row else None


def export_run(run_id: str) -> dict | None:
    data = get_run(run_id)
    if not data:
        return None
    # Keep export deliberately bounded to this run; environment secrets are never selected.
    return {"run": {k: data[k] for k in ("run_id","mode","template_id","status","phase",
                                             "total_iterations","current_iteration","summary",
                                             "created_at","started_at","finished_at")},
            "iterations": data["iterations"], "events": data["events"]}

# --- D 장애 시험 결과 격자 (운영 대시보드 블록 D · PRD 5.6.1) ---------------------------------
# 유형별 **최신 5회차**를 여러 run 에 걸쳐 모아 `moa/ops_view.trial_summary` 가 먹는 모양으로 낸다.
# 빠른 확인(quick)을 여러 번 돌려도 격자가 채워지게 하려고 run 하나로 묶지 않는다(2026-11-06 결정).
TRIALS_PER_FAULT = 5

# status -> 칸. 어휘는 ops_view.trial_summary 가 정한 것이다
#   auto      사람 없이 복구  분자 · 분모
#   approved  승인 · 수동     분모만 — 사람이 개입해 끝난 회차는 분모에 남기고 실패로 센다(PRD 5.6.1)
#   failed    실패           분모만
#   invalid   무효           분모 밖 — 주입 도구 오류 · 준비 실패
#   running   진행 중        분모 밖 (아직 안 한 회차는 pending)
_CELL_BY_STATUS = {"PASS": "auto", "AWAITING_APPROVAL": "approved", "NEEDS_REVIEW": "approved",
                   "FAIL": "failed", "INVALID": "invalid", "running": "running", "queued": "pending"}
# F-4 는 안전 전환 여부만 보는 별도 줄이다(분모 25회 밖).
# 차단 후 승인 대기도 전환이 일어난 것이다 — healing 의 F-4 기대 결과가 "단일 probe 또는 승인 대기"다
_F4_CELL_BY_STATUS = {"PASS": "switched", "AWAITING_APPROVAL": "switched",
                      "FAIL": "not_switched", "NEEDS_REVIEW": "not_switched"}


def _trial_cell(row: dict) -> dict:
    """칸 하나 — 초는 SC-3 정본 `recovered_at - injected_at` 이다.

    healing 이 적는 mttr_ms 는 **탐지 기준**(`검증 통과 - 탐지`)이라 SC-3 과 다르다(PRD R-20).
    R-20 의 해소 지침대로 정본은 주입 기준(mttd + mttr)으로 내고, 탐지 기준은 `mttr_sec` 로 따로 둔다 —
    한 칸에서 두 정의가 섞이면 60초 목표 초과 판정이 조용히 틀린다.
    """
    mttd, mttr = row.get("mttd_ms"), row.get("mttr_ms")
    total_ms = None if mttr is None else (mttd or 0) + mttr
    return {"result": _CELL_BY_STATUS.get(row.get("status"), "pending"),
            "sec": None if total_ms is None else round(total_ms / 1000),
            "mttr_sec": None if mttr is None else round(mttr / 1000)}


def trials_grid(per_fault: int = TRIALS_PER_FAULT) -> dict:
    """유형별 최신 N회차 격자. 모양은 `{"total_runs", "rows": [{"fault", "cells"}], "f4"}`.

    기록이 아예 없으면 `rows` 를 비워 둔다 — ops_view 가 그때 "아직 없음" 안내를 띄운다.
    하나라도 있으면 복구 대상 5종의 줄을 모두 둔다. 격자에 구멍이 나면 보는 사람이
    "아직 안 한 유형"과 "줄이 빠진 유형"을 구분할 수 없다.
    """
    rows = db.fetch_all(
        "SELECT fault_type,status,mttd_ms,mttr_ms FROM ("
        " SELECT i.fault_type,i.status,i.mttd_ms,i.mttr_ms,"
        "        ROW_NUMBER() OVER (PARTITION BY i.fault_type"
        "          ORDER BY COALESCE(i.finished_at,i.started_at) DESC NULLS LAST,"
        "                   i.run_id DESC, i.iteration_no DESC) AS rn"
        "   FROM healing.workflow_iteration i) t"
        " WHERE rn <= %s ORDER BY fault_type, rn DESC", (per_fault,), role="healing")
    total_runs = per_fault * len(RECOVERABLE)
    if not rows:
        return {"total_runs": total_runs, "rows": [], "f4": None, "latest_run": None}
    by_fault: dict[str, list[dict]] = {}
    for row in rows:
        by_fault.setdefault(row["fault_type"], []).append(row)
    grid = [{"fault": fault, "cells": [_trial_cell(r) for r in by_fault.get(fault, [])]}
            for fault in RECOVERABLE]
    f4_rows = by_fault.get("F-4") or []
    f4 = {"fault": "F-4",
          "cells": [{"result": _F4_CELL_BY_STATUS.get(r.get("status"), "pending")} for r in f4_rows]}
    return {"total_runs": total_runs, "rows": grid, "f4": f4 if f4_rows else None,
            "latest_run": _latest_run()}


def _latest_run() -> dict | None:
    """격자 위에 적을 최신 실행 — 실행 ID와 예상 흐름 일치 수.

    "25회 중 몇 회를 했는가"만으로는 **어느 실행의 숫자인지** 알 수 없어 캡처가 증빙이 되지 않는다.
    일치는 `PASS`(예상대로 통과) ÷ 판정이 난 회차다. 진행 중 · 대기 회차는 분모에서 뺀다.
    """
    try:
        row = db.fetch_one(
            "SELECT run_id, summary FROM healing.workflow_run ORDER BY created_at DESC LIMIT 1",
            role="healing")
    except (db.DbUnavailable, psycopg.Error):
        return None     # 실행 이름을 못 읽었다고 격자를 통째로 비우지 않는다
    if not row:
        return None
    summary = row.get("summary") or {}
    ok = int(summary.get("pass") or 0)
    valid = ok + int(summary.get("fail") or 0) + int(summary.get("needs_review") or 0)         + int(summary.get("awaiting_approval") or 0)
    return {"run_id": row["run_id"], "ok": ok, "valid": valid,
            "avg_mttd_ms": summary.get("avg_mttd_ms"), "avg_mttr_ms": summary.get("avg_mttr_ms")}
