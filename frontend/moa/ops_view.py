# SCR-006 운영 대시보드 — 블록 A ~ F 의 화면 자료 만들기와 HTML
#
# 출처: 정본 시안 `Moa_운영대시보드.svg`(24 · 25-1 ~ 25-3 · 26) · 05.리포트/운영대시보드_고도화안.md v0.2
#
# 이 모듈은 **Streamlit 을 부르지 않는 순수 함수**만 둔다. 서버 응답(API-042 ~ 046 + 시험 결과)을
# 받아 화면이 그릴 자료로 바꾸고, 그 자료를 HTML 로 만든다. 버튼 · 펼침 상태는 screens/ops.py 의 몫이다.
#
# 판정(통과 · 미달)과 상태 문장은 **같은 숫자에서 한 번만** 만든다. 화면 여러 곳에서 따로 계산하면
# 배너는 "정상"인데 카드는 "미달"인 식으로 어긋난다.
#
# 서버가 아직 주지 않는 칸(G-1 앱 복제본 · G-3 회차 · G-4 유형별 결과 · 무결성 건수)은 없으면
# `미측정` · `상태 미수집` 으로 그린다. 0 과 미측정을 섞지 않는다(고도화안 6절 원칙 7).
import datetime
import math

from moa import components as c, copy, icons

# 목표치 — PRD 9절 SC-2 · SC-3 · SC-4 · SC-5 (고도화안 5절 B)
MTTD_TARGET_SEC = 15
MTTR_TARGET_SEC = 60
MTTR_TARGET_BY_FAULT = {"F-3": 90}
AUTO_RATE_TARGET = 80
INTEGRITY_TARGET = 0
PROBE_NEED_DEFAULT = 3

_OPEN_STATES = ("detected", "recovering")
_FAIL_STATES = ("failed", "manual_check")


# --- 공통 -------------------------------------------------------------------
def fault_of(row: dict) -> str:
    """사건 · 승인 행의 시험 유형(F-n). 서버가 `fault` 를 주면 그것을, 아니면 장애 코드에서 찾는다."""
    if row.get("fault"):
        return row["fault"]
    code = row.get("fault_code") or ""
    return copy.FAULT_CODE_TO_F.get(code, code)


def fault_name(row: dict) -> str:
    code = fault_of(row)
    return copy.FAULT_NAMES.get(code) or row.get("fault_label") or row.get("summary") or code


def duration_text(sec) -> str:
    """41 → `41초`, 64 → `64초`, 148 → `2분 28초`, 120 → `2분`. 없으면 `-`.

    100초 미만은 초로만 쓴다 — 목표(60초 · F-3 90초)와 바로 견줄 수 있게(시안 `64초`).
    """
    if sec is None:
        return "-"
    sec = int(round(sec))
    if sec < 100:
        return f"{sec}초"
    minutes, rest = divmod(sec, 60)
    return f"{minutes}분 {rest}초" if rest else f"{minutes}분"


def _clock_sec(text) -> int | None:
    """`14:02:11` → 그날 0시부터의 초. 형식이 다르면 None."""
    try:
        h, m, s = (int(x) for x in str(text).split(":"))
    except (TypeError, ValueError):
        return None
    return h * 3600 + m * 60 + s


def _now_sec(now: datetime.datetime | None) -> int:
    now = now or datetime.datetime.now()
    return now.hour * 3600 + now.minute * 60 + now.second


def ago_text(clock_text, now: datetime.datetime | None = None) -> str:
    """시각 글자를 지난 시간으로 — `32초 전` 대신 분 단위까지만(고도화안 6절 원칙 6)."""
    at = _clock_sec(clock_text)
    if at is None:
        return "-"
    diff = (_now_sec(now) - at) % 86400
    if diff < 60:
        return copy.OE_JUST_NOW
    if diff < 3600:
        return f"{diff // 60}분 전"
    return f"{diff // 3600}시간 전"


# --- A 상태 배너 --------------------------------------------------------------
def banner(status: dict, incidents: dict) -> dict:
    """우선순위 표(고도화안 5절 A)의 위에서부터 **하나만** 고른다.

    1 승인 대기 → 주황 · 2 자동 복구 실패(진행 중) → 빨강 · 3 탐지 · 복구 중 → 주황
    · 3' 주입 후 탐지 대기 → 회색 · 4 대체 경로 사용 → 초록 · 5 정상 → 초록
    """
    rows = incidents.get("incidents") or []
    by_id = {r.get("incident_id"): r for r in rows}
    approvals = [{**by_id.get(a.get("incident_id"), {}), **a} for a in incidents.get("approvals") or []]
    alerts = [a.get("text") if isinstance(a, dict) else str(a) for a in status.get("alerts") or []]
    out = {"approvals": approvals, "alert": alerts[0] if alerts else None}

    if approvals:
        first = approvals[0]
        waited = (duration_text(first["waiting_sec"]) if first.get("waiting_sec") is not None
                  else (first.get("waiting_text") or "").replace("째 대기", "").replace(" 대기", ""))
        return {**out, "tone": "amber", "title": copy.OA_APPROVAL,
                "sub": copy.OA_APPROVAL_SUB.format(name=fault_name(first), code=fault_of(first),
                                                   waited=waited)}

    failed = [r for r in rows if r.get("state") in _FAIL_STATES and r.get("open")]
    if failed:
        return {**out, "tone": "red", "title": copy.OA_FAILED, "sub": fault_name(failed[0])}

    running = [r for r in rows if r.get("state") in _OPEN_STATES]
    if running:
        r = running[0]
        return {**out, "tone": "amber", "title": copy.OA_RECOVERING,
                "sub": copy.OA_RECOVERING_SUB.format(name=fault_name(r), code=fault_of(r),
                                                     sec=int(r.get("elapsed_sec") or 0))}

    inj = status.get("active_injection")
    if inj and inj.get("code"):
        sub = copy.OA_INJECTED_SUB.format(name=copy.FAULT_NAMES.get(inj["code"], inj["code"]),
                                          code=inj["code"])
        if inj.get("run_no"):
            sub += " · " + copy.OA_RUN_SUFFIX.format(n=inj["run_no"])
        return {**out, "tone": "gray", "title": copy.OA_INJECTED.format(sec=int(inj.get("elapsed_sec") or 0)),
                "sub": sub}

    degraded = _degraded_reason(status)
    if degraded:
        return {**out, "tone": "green", "title": copy.OA_DEGRADED, "sub": degraded}
    return {**out, "tone": "green", "title": copy.OA_NORMAL, "sub": None}


def _service(status: dict, key: str) -> dict | None:
    return next((s for s in status.get("services") or [] if s.get("key") == key), None)


def _active_interpreter(status: dict) -> str | None:
    if "active_interpreter" in status:
        return status.get("active_interpreter")
    active = [s.get("key") for s in status.get("services") or []
              if s.get("active") and str(s.get("key", "")).startswith("order_interpreter")]
    return active[0] if active else None


def _degraded_reason(status: dict) -> str | None:
    active = _active_interpreter(status)
    if active == "order_interpreter_backup":
        return copy.OA_DEGRADED_BACKUP
    has_interpreters = any(str(s.get("key", "")).startswith("order_interpreter")
                           for s in status.get("services") or [])
    if has_interpreters and active is None:
        return copy.OA_DEGRADED_RULE
    if status.get("cache_fallback_active"):
        return copy.OA_DEGRADED_CACHE
    return None


# --- B 핵심 수치 --------------------------------------------------------------
def _verdict(value, ok: bool) -> str:
    if value is None:
        return "none"
    return "pass" if ok else "fail"


def kpis(metrics: dict, trial_sum: dict | None) -> list[dict]:
    """4칸 — 큰 글자 · 작은 글자 · 목표 · 판정. 측정 전은 `미측정`이다(0 이 아니다)."""
    detail = metrics.get("detail") or {}
    mttd = detail.get("mttd_sec")
    mttr = detail.get("mttr_sec")
    mttr_max = detail.get("mttr_max_sec")
    if mttr_max is None and trial_sum:
        mttr_max = trial_sum.get("max_sec")
    integrity = detail.get("integrity_issue_count")

    ok = valid = None
    if trial_sum and trial_sum.get("valid"):
        ok, valid = trial_sum["ok"], trial_sum["valid"]
    pct = round(ok / valid * 100) if valid else None

    def card(key, value, unit, extra, verdict):
        label, abbr = copy.OB_CARDS[key]
        return {"key": key, "label": label, "abbr": abbr, "value": value, "unit": unit,
                "extra": extra, "target": copy.OB_TARGET[key], "verdict": verdict}

    return [
        card("mttd", _sec_text(mttd), "초", None, _verdict(mttd, mttd is not None and mttd <= MTTD_TARGET_SEC)),
        card("mttr", _sec_text(mttr), "초", f"최대 {round(mttr_max)}초" if mttr_max is not None else None,
             _verdict(mttr, mttr is not None and mttr <= MTTR_TARGET_SEC)),
        card("auto", f"{ok}/{valid}" if valid else None, None, f"({pct}%)" if pct is not None else None,
             _verdict(pct, pct is not None and pct >= AUTO_RATE_TARGET)),
        card("integrity", _int_text(integrity), "건", None,
             _verdict(integrity, integrity is not None and integrity <= INTEGRITY_TARGET)),
    ]


def _int_text(value) -> str | None:
    return None if value is None else f"{round(value):,}"


def _sec_text(value) -> str | None:
    """측정 원본은 ms다. 목표(15초 · 60초)와 바로 견주도록 **소수 1자리 초**로 쓴다.

    `8` 로 반올림해 버리면 8.4 와 8.0 이 같아 보이고, 8.2 가 ms 기록과 맞는지 확인할 수 없다.
    """
    return None if value is None else f"{float(value):.1f}"


# --- C 지금 요청 경로 -----------------------------------------------------------
def route(status: dict) -> dict:
    """세 줄 묶음(게이트웨이 · 해석기 · 메뉴 저장소) + 아래 안내 한 줄."""
    need = status.get("probe_need") or PROBE_NEED_DEFAULT
    active = _active_interpreter(status)

    replicas = status.get("replicas")
    if replicas:
        gateway = [_replica_row(r) for r in replicas]
    else:
        # G-1 — 서버가 복제본 상태를 아직 주지 않는다. 지어내지 않고 미수집으로 둔다
        gateway = [{"name": n, "tone": "unknown", "text": copy.OC_UNKNOWN} for n in ("앱 A", "앱 B")]

    interp = []
    for key, name in (("order_interpreter_primary", "1차"), ("order_interpreter_backup", "대체")):
        svc = _service(status, key)
        if svc is not None:
            interp.append(_interpreter_row(svc, name, active == key or bool(svc.get("active")), need))

    store_svc = _service(status, "menu_store")
    store = []
    if store_svc is not None:
        if status.get("cache_fallback_active") or "캐시" in str(store_svc.get("state_text", "")):
            store.append({"name": "주 저장소", "tone": "probing", "text": copy.OC_CACHE})
        elif store_svc.get("state") == "up":
            store.append({"name": "주 저장소", "tone": "active", "text": copy.OC_ACTIVE})
        else:
            store.append({"name": "주 저장소", "tone": "blocked",
                          "text": store_svc.get("state_text") or copy.OC_BLOCKED_DEAD})

    note = None
    primary = next((r for r in interp if r["name"] == "1차"), None)
    if interp and not any(r["tone"] == "active" for r in interp):
        note = ("amber", copy.OC_NOTE_RULE)
    elif primary and primary["tone"] == "probing":
        note = ("amber", copy.OC_NOTE_PROBING.format(need=need))
    elif store and store[0]["tone"] == "probing":
        note = ("amber", copy.OC_NOTE_CACHE)

    groups = [(copy.OC_GROUPS["gateway"], gateway), (copy.OC_GROUPS["interpreter"], interp),
              (copy.OC_GROUPS["store"], store)]
    return {"groups": [g for g in groups if g[1]], "note": note}


def _replica_row(r: dict) -> dict:
    name = r.get("name") or r.get("key")
    # SC-12 — 전환에 걸린 초. 서버가 주지 않으면 칸을 비운다(지어내지 않는다)
    switch = r.get("switch_sec")
    note = copy.OC_SWITCH.format(sec=int(switch)) if switch is not None else None
    if r.get("state") == "up":
        return {"name": name, "tone": "active" if r.get("active", True) else "standby",
                "text": copy.OC_ACTIVE if r.get("active", True) else copy.OC_STANDBY, "note": note}
    return {"name": name, "tone": "blocked", "text": r.get("state_text") or "중단", "note": note}


def _interpreter_row(svc: dict, name: str, is_active: bool, need: int) -> dict:
    state = svc.get("state")
    if state == "probing":
        return {"name": name, "tone": "probing", "streak": int(svc.get("healthy_streak") or 0), "need": need,
                "text": copy.OC_PROBING.format(n=int(svc.get("healthy_streak") or 0), need=need)}
    if state in ("blocked", "down", "degraded"):
        probe = svc.get("probe_in_sec")
        text = svc.get("state_text") or ""
        if probe:
            text = copy.OC_BLOCKED.format(sec=int(probe))
        elif "초 후" not in text:
            text = copy.OC_BLOCKED_DEAD
        return {"name": name, "tone": "blocked", "text": text}
    if is_active:
        return {"name": name, "tone": "active", "text": copy.OC_ACTIVE}
    return {"name": name, "tone": "standby", "text": copy.OC_STANDBY}


# --- D 장애 시험 결과 -----------------------------------------------------------
def trial_summary(trials: dict | None) -> dict | None:
    """유형별 성공 수와 전체 합계. 무효 · 아직 안 함은 분모에서 뺀다(고도화안 7절 집계 제외)."""
    if not trials or not trials.get("rows"):
        return None
    rows, ok_all, valid_all, done, secs = [], 0, 0, 0, []
    for row in trials["rows"]:
        cells = row.get("cells") or []
        ok = sum(1 for x in cells if x.get("result") == "auto")
        valid = sum(1 for x in cells if x.get("result") in ("auto", "approved", "failed"))
        done += sum(1 for x in cells if x.get("result") not in ("pending", None))
        secs += [x["sec"] for x in cells if x.get("result") == "auto" and x.get("sec") is not None]
        ok_all, valid_all = ok_all + ok, valid_all + valid
        rows.append({**row, "ok": ok, "valid": valid})
    f4 = trials.get("f4")
    if f4:
        cells = f4.get("cells") or []
        f4 = {**f4, "ok": sum(1 for x in cells if x.get("result") == "switched"),
              "valid": sum(1 for x in cells if x.get("result") in ("switched", "not_switched"))}
    total = trials.get("total_runs") or 5 * len(rows)
    return {"rows": rows, "f4": f4, "ok": ok_all, "valid": valid_all, "done": done, "total": total,
            "pct": round(ok_all / valid_all * 100) if valid_all else None,
            "max_sec": max(secs) if secs else None,
            # 어느 실행의 숫자인지 — 없으면 25회 측정을 캡처로 증빙할 수 없다
            "run": trials.get("latest_run")}


def _trial_cell(cell: dict, fault: str) -> str:
    result = cell.get("result") or "pending"
    sec = cell.get("sec")
    if result == "auto":
        over = sec is not None and sec > MTTR_TARGET_BY_FAULT.get(fault, MTTR_TARGET_SEC)
        body = f'<b>{int(sec)}</b>' if over else (str(int(sec)) if sec is not None else "")
        return f'<span class="moa-tc moa-tc-auto{" moa-tc-over" if over else ""}">{body}</span>'
    text = {"approved": copy.OD_CELL_APPROVED, "failed": copy.OD_CELL_FAILED, "invalid": copy.OD_CELL_INVALID,
            "running": copy.OD_CELL_RUNNING, "switched": copy.OD_CELL_SWITCHED,
            "not_switched": copy.OD_CELL_NOT_SWITCHED}.get(result, copy.OD_CELL_PENDING)
    return f'<span class="moa-tc moa-tc-{c.esc(result)}">{c.esc(text)}</span>'


def _trial_name(fault: str, note: str | None = None) -> str:
    """행 이름 = 장애 이름 + F 코드 + (작게) 평가 유형 · 감지 메트릭."""
    metric = copy.FAULT_METRIC.get(fault)
    small = " · ".join(x for x in (note, metric) if x)
    tail = f"<small>{c.esc(small)}</small>" if small else ""
    return (f'<span class="moa-tname"><b>{c.esc(copy.FAULT_NAMES.get(fault, fault))}</b>'
            f'<i>{c.esc(fault)}</i>{tail}</span>')


def _trial_strategy(fault: str) -> str:
    return f'<span class="moa-tstrat">{c.esc(copy.FAULT_STRATEGY.get(fault, ""))}</span>'


def trials_html(trial_sum: dict | None) -> str:
    head = (f'<div class="moa-ops-title"><u class="moa-bbadge">D</u>'
            f'<b>{c.esc(copy.OD_TITLE)}</b>'
            f'<span>{c.esc(copy.OD_HINT)}</span>')
    if not trial_sum:
        return head + f'</div><div class="moa-ops-empty">{c.esc(copy.OD_EMPTY)}</div>'
    run = trial_sum.get("run") or {}
    if run.get("run_id"):
        # 실행 ID · 격자 진행 칸 · 예상 흐름 일치(= 예상대로 통과한 회차 / 판정이 난 회차)
        valid = int(run.get("valid") or 0)
        template = copy.OD_RUN_LINE if valid else copy.OD_RUN_LINE_SHORT
        label = template.format(run_id=run["run_id"], done=trial_sum["done"],
                                total=trial_sum["total"], ok=int(run.get("ok") or 0), valid=valid)
    else:
        label = copy.OD_RUNS.format(done=trial_sum["done"], total=trial_sum["total"])
    head += f'<em>{c.esc(label)}</em></div>'
    cols = max([len(r.get("cells") or []) for r in trial_sum["rows"]] + [5])
    th = "".join(f"<span>{i}회</span>" for i in range(1, cols + 1))
    lines = [f'<div class="moa-trow moa-trow-head"><span></span>'
             f'<span class="moa-tstrat">{c.esc(copy.OD_STRATEGY)}</span>{th}'
             f'<span>{c.esc(copy.OD_SUCCESS)}</span></div>']
    for row in trial_sum["rows"]:
        fault = row.get("fault", "")
        cells = "".join(_trial_cell(x, fault) for x in row.get("cells") or [])
        lines.append(f'<div class="moa-trow">{_trial_name(fault)}{_trial_strategy(fault)}{cells}'
                     f'<span class="moa-tok">{row["ok"]}/{row["valid"]}</span></div>')
    f4 = trial_sum.get("f4")
    if f4:
        cells = "".join(_trial_cell(x, "F-4") for x in f4.get("cells") or [])
        lines.append(f'<div class="moa-trow moa-trow-f4">{_trial_name("F-4", copy.OD_F4_NOTE)}'
                     f'{_trial_strategy("F-4")}{cells}'
                     f'<span class="moa-tok">{f4["ok"]}/{f4["valid"]}</span></div>')
    legend = "".join(f'<span><i class="moa-lg moa-lg-{k}"></i>{c.esc(t)}</span>' for k, t in copy.OD_LEGEND)
    total = (copy.OD_TOTAL.format(ok=trial_sum["ok"], valid=trial_sum["valid"], pct=trial_sum["pct"])
             if trial_sum["valid"] else "")
    return (head + f'<div class="moa-trials" style="--cols:{cols}">{"".join(lines)}</div>'
            f'<div class="moa-tlegend"><div>{legend}</div><b>{c.esc(total)}</b></div>')


# --- E 최근 사건 ----------------------------------------------------------------
_TONE = {"awaiting_approval": "amber", "detected": "amber", "recovering": "amber",
         "recovered": "green", "failed": "red", "manual_check": "red"}


def incident_rows(incidents: dict, now: datetime.datetime | None = None) -> list[dict]:
    out = []
    for r in incidents.get("incidents") or []:
        state = r.get("state")
        tone = _TONE.get(state, "gray")
        if state == "recovered" and r.get("approved"):
            tone = "amber"   # 승인 후 복구 — 사람 손을 거친 복구는 초록으로 칠하지 않는다
        out.append({
            "id": r.get("incident_id"), "name": fault_name(r), "code": fault_of(r),
            "state_text": r.get("state_text") or state, "tone": tone,
            "ago": r.get("ago_text") or ago_text(r.get("detected_at_text"), now),
            "elapsed": _elapsed_text(r, now),
        })
    return out


def _elapsed_text(r: dict, now) -> str:
    state = r.get("state")
    if state == "awaiting_approval":
        return copy.OE_WAITING
    sec = r.get("elapsed_sec")
    if state in _OPEN_STATES:
        if sec is None:
            at = _clock_sec(r.get("detected_at_text"))
            sec = None if at is None else (_now_sec(now) - at) % 86400
        return copy.OE_RUNNING.format(sec=int(sec or 0))
    if sec is None:
        start, end = _clock_sec(r.get("detected_at_text")), _clock_sec(r.get("resolved_at_text"))
        sec = None if start is None or end is None else (end - start) % 86400
    return duration_text(sec)


def incident_row_html(row: dict, opened: bool) -> str:
    chev = icons.icon("chevron-down", 16)
    return (f'<div class="moa-irow{" moa-irow-open" if opened else ""}">'
            f'<span class="moa-iname"><b>{c.esc(row["name"])}</b><i>{c.esc(row["code"])} · {c.esc(row["id"])}</i></span>'
            f'<span>{c.pill("● " + str(row["state_text"]), row["tone"])}</span>'
            f'<span class="moa-iago">{c.esc(row["ago"])}</span>'
            f'<span class="moa-iel">{c.esc(row["elapsed"])}</span>'
            f'<span class="moa-chev{" moa-chev-up" if opened else ""}">{chev}</span></div>')


def normalize_detail(detail: dict) -> dict:
    """서버 응답 모양을 D-02 API-043 모양으로 맞춘다 (2026-11-05).

    서버(services/incidents.incident_detail)는 `timeline` · `verification` 을 **목록**으로, `attempts` 를
    최상위로 돌려준다. 두 모양을 모두 받는다 — 서버를 바꾸지 않아 다른 호출부에 영향이 없다.
    """
    detail = dict(detail)
    timeline = detail.get("timeline")
    if isinstance(timeline, list):
        keys = {"injected": "injected_at", "detected": "detected_at",
                "recovery_started": "recovery_started_at", "resolved": "resolved_at", "failed": "failed_at"}
        detail["timeline"] = {keys.get(t.get("key"), t.get("key")): t.get("at_text") for t in timeline}
    verification = detail.get("verification")
    if isinstance(verification, list):
        verdicts = {v.get("verdict") for v in verification if v.get("verdict")}
        detail["verification"] = {
            "checks": [{"name": v.get("check_name"), "result": "pass" if v.get("passed") else "fail"}
                       for v in verification],
            "verdict": ("fail" if "failed" in verdicts else "pass") if verification else None}
    recovery = dict(detail.get("recovery") or {})
    if "attempts" not in recovery and isinstance(detail.get("attempts"), list):
        recovery["attempts"] = [{"no": a.get("attempt_no"), "action": a.get("action"),
                                 "wait_ms": a.get("wait_ms"), "result": a.get("result")}
                                for a in detail["attempts"]]
    if "strategy" not in recovery and detail.get("strategy"):
        recovery["strategy"] = detail["strategy"]
    detail["recovery"] = recovery
    if detail.get("elapsed_sec") is None and isinstance(detail.get("elapsed_ms"), (int, float)):
        detail["elapsed_sec"] = round(detail["elapsed_ms"] / 1000, 1)
    return detail


def timeline(detail: dict) -> dict:
    """주입 0초 기준 구간 — 탐지 전(회색) · 사람 확인(주황) · 복구 중(연초록) + 표시점 + 기준선."""
    detail = normalize_detail(detail)
    secs = detail.get("timeline_sec")
    if not secs:
        tl = detail.get("timeline") or {}
        base = _clock_sec(tl.get("injected_at"))
        secs = {}
        if base is not None:
            for key in ("detected_at", "recovery_started_at", "resolved_at", "failed_at"):
                at = _clock_sec(tl.get(key))
                if at is not None:
                    secs[key.replace("_at", "")] = (at - base) % 86400
    det = secs.get("detected")
    start = secs.get("recovery_started", det)
    end = secs.get("resolved", secs.get("failed"))
    wait = (detail.get("approval") or {}).get("waiting_sec")
    if wait is None:
        wait = (detail.get("escalation") or {}).get("waiting_sec")
    fault = fault_of(detail)
    target = MTTR_TARGET_BY_FAULT.get(fault, MTTR_TARGET_SEC)
    last = max([v for v in (det, start, end) if v is not None] + [0])
    scale = max(90, int(math.ceil(last * 1.1 / 30.0) * 30))
    human = None
    if wait and det is not None:
        human = (det, det + wait)
        if start is None or start < det + wait:
            start = det + wait
    return {"scale": scale, "det": det, "start": start, "end": end, "human": human,
            "failed": "failed" in secs and "resolved" not in secs,
            "lines": [MTTD_TARGET_SEC, target]}


def timeline_html(tl: dict) -> str:
    scale = tl["scale"]

    def pct(v):
        return f"{max(0.0, min(100.0, v / scale * 100)):.2f}%"

    segs = []
    if tl["det"] is not None:
        segs.append(f'<i class="moa-tl-seg moa-tl-pre" style="left:0;width:{pct(tl["det"])}"></i>')
    if tl["human"]:
        a, b = tl["human"]
        segs.append(f'<i class="moa-tl-seg moa-tl-human" style="left:{pct(a)};width:{pct(b - a)}"></i>')
    if tl["start"] is not None and tl["end"] is not None:
        cls = "moa-tl-fail" if tl["failed"] else "moa-tl-rec"
        segs.append(f'<i class="moa-tl-seg {cls}" style="left:{pct(tl["start"])};'
                    f'width:{pct(tl["end"] - tl["start"])}"></i>')
    marks = []
    for key, cls in (("det", "moa-tl-mark-det"), ("start", "moa-tl-mark-start"), ("end", "moa-tl-mark-end")):
        if tl[key] is not None:
            marks.append(f'<i class="moa-tl-mark {cls}" style="left:{pct(tl[key])}"></i>')
    lines = "".join(f'<i class="moa-tl-line" style="left:{pct(v)}"></i>' for v in tl["lines"])
    labels = (f'<span style="left:0">{c.esc(copy.OE_TL_START)}</span>'
              + "".join(f'<span class="moa-tl-tgt" style="left:{pct(v)}">{v}초</span>' for v in tl["lines"])
              + f'<span style="right:0">{scale}초</span>')
    legend = "".join(f'<span><i class="moa-lg moa-tl-lg-{k}"></i>{c.esc(t)}</span>' for k, t in copy.OE_TL_LEGEND)
    return (f'<div class="moa-tl"><div class="moa-tl-scale">{labels}</div>'
            f'<div class="moa-tl-bar">{"".join(segs)}{lines}{"".join(marks)}</div>'
            f'<div class="moa-tl-legend">{legend}</div></div>')


def detail_cards(detail: dict) -> list[tuple[str, str]]:
    """진단 · 전략 · 시도 · 검증 — 운영자가 읽는 쉬운 말 한 줄씩. 원시 JSON 은 쏟지 않는다."""
    detail = normalize_detail(detail)
    summary = detail.get("summary") or {}
    diagnosis = detail.get("diagnosis") or {}
    recovery = detail.get("recovery") or {}
    verification = detail.get("verification") or {}

    strategy = recovery.get("strategy") or detail.get("strategy")
    attempts = recovery.get("attempts") or []
    if attempts:
        ok_no = next((a.get("no") for a in attempts if a.get("result") in ("ok", "success", "pass")), None)
        tried = f"시도 {len(attempts)}회"
        attempt_text = f"{tried} → {ok_no}회째 성공" if ok_no else f"{tried} · 모두 실패"
    else:
        attempt_text = "-"
    checks = verification.get("checks") or []
    if checks:
        passed = sum(1 for ch in checks if ch.get("result") in ("pass", "통과"))
        verify_text = f"검증 {passed}/{len(checks)} 통과"
    else:
        verify_text = "아직 검증하지 않음"

    values = [summary.get("diagnosis") or diagnosis.get("reason") or "-",
              summary.get("strategy") or copy.OE_STRATEGY.get(strategy, strategy) or "-",
              summary.get("attempts") or attempt_text,
              summary.get("verification") or verify_text]
    return list(zip(copy.OE_CARDS, values))


def incident_detail_html(detail: dict) -> str:
    cards = "".join(f'<div class="moa-icard"><span>{c.esc(k)}</span><b>{c.esc(v)}</b></div>'
                    for k, v in detail_cards(detail))
    return f'<div class="moa-idetail">{timeline_html(timeline(detail))}<div class="moa-icards">{cards}</div></div>'


# --- A · B · C HTML --------------------------------------------------------------
def banner_head_html(b: dict) -> str:
    sub = f'<span class="moa-ban-sub">— {c.esc(b["sub"])}</span>' if b.get("sub") else ""
    # 블록 배지 — 시안 2026-11-07 판부터 A ~ F · T1 · T2 · T2-R 이 모든 블록 머리에 붙는다.
    # "결과가 가는 곳" 칩이 이 글자를 가리키므로, 칩과 블록이 같은 이름이어야 읽힌다
    return (f'<div class="moa-ban-head"><u class="moa-bbadge moa-bbadge-a">A</u>'
            f'<i class="moa-ban-dot"></i>'
            f'<b class="moa-ban-title">{c.esc(b["title"])}</b>{sub}</div>')


def approval_html(a: dict) -> str:
    desc = a.get("description_text")
    return (f'<div class="moa-appr-card"><div class="moa-appr-name"><b>{c.esc(fault_name(a))}</b>'
            f'<i>{c.esc(fault_of(a))} · {c.esc(a.get("incident_id"))}</i></div>'
            + (f'<div class="moa-appr-desc">{c.esc(desc)}</div>' if desc else "")
            + f'<div class="moa-appr-rec">{c.esc(a.get("recommendation_text") or "")}</div></div>')


def alert_html(text: str) -> str:
    """`최근 5분 … 12% (기준 10%)` — 괄호 앞(넘은 값)만 굵게. 기준은 보통 굵기(시안 24)."""
    head, sep, tail = str(text).partition(" (")
    body = f"<b>{c.esc(head)}</b> ({c.esc(tail)}" if sep else f"<b>{c.esc(head)}</b>"
    return f'<div class="moa-ban-alert">{icons.icon("alert", 14)}<span>{body}</span></div>'


def kpis_html(cards: list[dict]) -> str:
    out = []
    for k in cards:
        verdict = k["verdict"]
        mark = {"pass": copy.OB_PASS, "fail": copy.OB_FAIL, "none": copy.OB_UNMEASURED}[verdict]
        check = icons.icon("check", 12) if verdict == "pass" else ""
        if k["value"] is None:
            value = f'<b class="moa-kpi-none">{c.esc(copy.OB_UNMEASURED)}</b>'
        else:
            unit = f'<small>{c.esc(k["unit"])}</small>' if k["unit"] else ""
            extra = f'<em>{c.esc(k["extra"])}</em>' if k["extra"] else ""
            value = f'<b>{c.esc(k["value"])}</b>{unit}{extra}'
        out.append(f'<div class="moa-kpi"><div class="moa-kpi-label">'
                   f'<u class="moa-bbadge">B</u><b>{c.esc(k["label"])}</b>'
                   f'<span>{c.esc(k["abbr"])}</span></div><div class="moa-kpi-value">{value}</div>'
                   f'<div class="moa-kpi-foot"><span>{c.esc(k["target"])}</span>'
                   f'<span class="moa-verdict moa-verdict-{verdict}">{check}{c.esc(mark)}</span></div></div>')
    return f'<div class="moa-kpis">{"".join(out)}</div>'


def route_html(r: dict) -> str:
    groups = []
    for label, rows in r["groups"]:
        items = []
        for row in rows:
            dots = ""
            if row["tone"] == "probing" and row.get("need"):
                dots = '<span class="moa-rdots">' + "".join(
                    f'<i class="{"on" if i < row["streak"] else ""}"></i>' for i in range(row["need"])) + "</span>"
            # SC-12 전환 초처럼 이름 옆에 덧붙는 작은 글자. 없으면 칸을 비운다
            sub = f'<em>{c.esc(row["note"])}</em>' if row.get("note") else ""
            items.append(f'<div class="moa-rrow moa-rrow-{row["tone"]}"><i class="moa-rdot"></i>'
                         f'<b>{c.esc(row["name"])}</b>{sub}{dots}<span>{c.esc(row["text"])}</span></div>')
        groups.append(f'<div class="moa-rgroup"><span class="moa-rlabel">{c.esc(label)}</span>'
                      f'<div>{"".join(items)}</div></div>')
    note = ""
    if r.get("note"):
        note = f'<div class="moa-rnote">{c.esc(r["note"][1])}</div>'
    return (f'<div class="moa-ops-title"><u class="moa-bbadge">C</u>'
            f'<b>{c.esc(copy.OC_TITLE)}</b></div>'
            f'<div class="moa-route">{"".join(groups)}</div>{note}')
