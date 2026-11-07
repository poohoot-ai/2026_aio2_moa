"""평가 하네스 — Q-02 평가셋정의서의 65문항을 실제 파이프라인에 태우고 축별로 채점한다 (Q-03).

    python eval/run_eval.py --condition rules                  # LLM 0회 · 비교 기준
    python eval/run_eval.py --condition primary --limit 5       # 1차(gpt-5.4-mini) 5문항만
    python eval/run_eval.py --condition primary --repeat 3      # Q-05 본 실행 (조건마다 3회)
    python eval/run_eval.py --condition primary --judge         # 축 5 Judge 까지
    python eval/run_eval.py --judge-check                       # 5.1 Judge 교차 검증 (G-A5)

**점수 해석은 하지 않는다** — 수치를 내고 저장만 한다. 판정은 Q-05 평가결과보고서가 한다.

만든 세션은 끝나면 지운다(실패해도 지운다). 대화 끝에 무작위 표식 줄을 붙여 FR-011 재사용을 피한다.
결과: eval/results/eval_{조건}_{시각}.json
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import os
import pathlib
import sys
import time
import traceback
import uuid

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("INTERPRETER_SOURCE", "llm")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, interpreter_client  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.services import brand_flow, chat_parse, sessions, suggestions  # noqa: E402
from eval import judge as judge_mod  # noqa: E402
from eval import score as score_mod  # noqa: E402
from interpreter import main as interpreter_app  # noqa: E402

DATASET = BACKEND / "eval" / "dataset.csv"
RESULTS = BACKEND / "eval" / "results"
BRAND_LINE = {"starbucks": "스타벅스로 갈 예정이야", "generic": "동네 카페로 갈 예정이야"}


# --- 조건 ------------------------------------------------------------------
def setup_condition(condition: str) -> None:
    """1차 · 대체 · 규칙 (Q-02 1절 실행 구성).

    해석 서비스는 **같은 프로세스에 띄워** 부른다 — docker 없이 돌기 위해서다. 모델 · 묶음 크기 ·
    계약은 배포와 같은 코드를 지난다(interpreter/main.py).
    """
    logging.disable(logging.INFO)
    if condition == "rules":
        config.ORDER_INTERPRETER_PRIMARY_URL = ""
        config.ORDER_INTERPRETER_BACKUP_URL = ""
        nodes._pick_service = lambda: None
        return

    role = "primary" if condition == "primary" else "backup"
    interpreter_app.ROLE = role
    interpreter_app.SOURCE, interpreter_app.MODE_OVERRIDE = "llm", "normal"
    interpreter_app.MODEL = config.LLM_MODEL if role == "primary" else config.LLM_MODEL_BACKUP
    client = TestClient(interpreter_app.app)
    config.ORDER_INTERPRETER_PRIMARY_URL = "http://primary"
    config.ORDER_INTERPRETER_BACKUP_URL = "http://backup"
    real = interpreter_client.interpret
    interpreter_client.interpret = lambda *a, **k: real(*a, **{**k, "client": client})
    nodes._pick_service = lambda: role          # 데모 시드가 회로를 열어 둘 수 있다


# --- 한 문항 ---------------------------------------------------------------
def run_question(row: dict) -> dict:
    """문항 1건 — 세션을 만들고 경로 · 항목 · 근거 · 비용을 모은다. 세션은 지운다."""
    brand = (row.get("brand") or "starbucks").strip()
    context = [ln for ln in (row.get("context") or "").splitlines() if ln.strip()]
    query_lines = [ln for ln in (row.get("query") or "").splitlines() if ln.strip()]
    source = (row.get("source") or "snapshot").strip()

    analysed = context if source in ("chat", "reply") else context + query_lines
    raw = "\n".join(analysed + [f"eval {uuid.uuid4().hex[:8]}", BRAND_LINE.get(brand, BRAND_LINE["starbucks"])])

    # 문항마다 고유 trace — `llm_history` 는 세션이 지워져도 남고, 세션 ID 는 MAX+1 이라 **재사용된다**.
    # session_id 로만 세면 같은 ID 를 쓴 **지난 실행의 호출 수**가 섞인다(실측 2026-11-04 · 규칙 조건 0회가 4회로 보였다)
    trace_id = f"tr_EVAL{uuid.uuid4().hex[:10]}"
    started = time.monotonic()
    session_id = None
    try:
        created = brand_flow.start(raw, source="paste", trace_id=trace_id)
        if created["kind"] != "running":
            raise RuntimeError(f'분석이 시작되지 않았다: {created["kind"]}')
        session_id = created["session_id"]
        sessions.run_pipeline(session_id, trace_id=trace_id)

        if source == "chat":
            session = _session(session_id)
            from app import graph
            graph.run("chat", session, trace_id=trace_id, text=query_lines[0],
                      expected_version=session["version"])
        elif source == "reply":
            session = _session(session_id)
            parsed = chat_parse.parse("\n".join(query_lines))
            suggestions.analyze(session, "\n".join(query_lines))

        measured = _measure(session_id, trace_id, source=source, analysed_lines=len(analysed),
                            query_lines=len(query_lines))
        measured["latency_sec"] = round(time.monotonic() - started, 2)
        measured["session_id"] = session_id
        return measured
    finally:
        if session_id:
            db.execute("DELETE FROM app.order_session WHERE session_id = %s", (session_id,))


def _session(session_id: str) -> dict:
    return db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s", (session_id,))


def _measure(session_id: str, trace_id: str, *, source: str, analysed_lines: int,
             query_lines: int) -> dict:
    """DB 에 남은 결과를 읽는다 — 화면이 보는 것과 같은 값."""
    events = db.fetch_all(
        "SELECT e.message_id, e.event_type, e.evidence_message_ids, m.seq, m.is_reply "
        "  FROM app.order_event e JOIN app.message m ON m.message_id = e.message_id "
        " WHERE e.session_id = %s ORDER BY m.is_reply, m.seq, e.event_id", (session_id,))
    items = sessions.items(session_id)
    questions = db.fetch_all(
        "SELECT item_id, options FROM app.review_question WHERE session_id = %s ORDER BY seq",
        (session_id,))
    candidates = {}
    for q in questions:
        candidates.setdefault(q["item_id"], []).extend(q["options"] or [])

    # 채점 대상 메시지 — snapshot 은 질문 줄(대화 끝의 표식 2줄 제외), chat · reply 는 그 입력
    target = [e for e in events if (e["is_reply"] if source == "reply" else not e["is_reply"])]
    if source == "snapshot":
        first = analysed_lines - query_lines + 1
        target = [e for e in target if e["seq"] >= first]
    by_message: dict[int, set[str]] = {}
    for e in target:
        by_message.setdefault(e["seq"], set()).add(e["event_type"])
    routes = [by_message[seq] for seq in sorted(by_message)]

    evidence_seqs = sorted({m["seq"] for m in db.fetch_all(
        "SELECT seq FROM app.message WHERE message_id = ANY(%s)",
        ([mid for e in target for mid in (e["evidence_message_ids"] or [])],))}) if target else []

    sheet = [{"person": r["person"], "menu_id": r["menu_id"], "menu_name": r.get("menu_name"),
              "menu_text": r["menu_text"], "size": (r["options"] or {}).get("size"),
              "options": {k: v for k, v in (r["options"] or {}).items() if k != "size"},
              "quantity": r["quantity"],
              "needs_review": r["validation_status"] == "needs_review",
              "candidates": candidates.get(r["item_id"], [])} for r in items]

    cost = db.fetch_one(
        "SELECT COUNT(*) AS calls, "
        "       COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) AS tokens, "
        "       COALESCE(MAX(duration_ms), 0) AS max_ms "
        "  FROM app.llm_history WHERE session_id = %s AND trace_id = %s",
        (session_id, trace_id)) or {}
    degraded = db.fetch_one(
        "SELECT COUNT(*) AS n FROM app.change_log WHERE session_id = %s AND method = 'auto_recovery'",
        (session_id,))

    return {"routes": routes, "items": sheet, "evidence_seqs": evidence_seqs,
            "llm_calls": int(cost.get("calls") or 0), "tokens": int(cost.get("tokens") or 0),
            "max_llm_ms": int(cost.get("max_ms") or 0), "degraded": bool((degraded or {}).get("n")),
            "suggestions": _suggestion_count(session_id)}


def _suggestion_count(session_id: str) -> int:
    row = db.fetch_one("SELECT COUNT(*) AS n FROM app.suggestion WHERE session_id = %s",
                       (session_id,))
    return int((row or {}).get("n") or 0)


# --- 채점 ------------------------------------------------------------------
def score_question(row: dict, measured: dict, *, with_judge: bool) -> dict:
    scored = {
        "id": row["id"], "type": row.get("type"), "intent": row.get("intent"),
        "source": row.get("source"), "brand": row.get("brand"),
        "route": score_mod.route_score(row["expected_route"], measured["routes"]),
        "items": score_mod.items_score(row["expected_items"], measured["items"]),
        "review": score_mod.review_counts(row["expected_items"], measured["items"]),
        "evidence_ok": score_mod.evidence_ok(row.get("expected_evidence"),
                                             measured["evidence_seqs"]),
        "cost": {"llm_calls": measured["llm_calls"], "tokens": measured["tokens"],
                 "latency_sec": measured["latency_sec"],
                 "max_llm_ms": measured["max_llm_ms"], "degraded": measured["degraded"]},
    }
    want_suggestions = (row.get("expected_suggestions") or "").strip()
    if want_suggestions.isdigit():
        scored["suggestions"] = {"expected": int(want_suggestions),
                                 "actual": measured["suggestions"],
                                 "ok": int(want_suggestions) == measured["suggestions"]}
    if with_judge:
        scored["judge"] = judge_mod.judge(row.get("query") or "", measured["items"],
                                          brand_id=row.get("brand") or "starbucks",
                                          menu_version=config.MENU_VERSION)
    return scored


# --- 실행 ------------------------------------------------------------------
def load_dataset(limit: int | None = None, only: list[str] | None = None) -> list[dict]:
    with open(DATASET, encoding="utf-8-sig", newline="") as f:
        rows = [{(k or "").strip(): v for k, v in r.items()} for r in csv.DictReader(f)]
    if only:
        rows = [r for r in rows if r["id"] in set(only)]
    return rows[:limit] if limit else rows


def run(condition: str, *, limit=None, only=None, repeat=1, with_judge=False) -> dict:
    setup_condition(condition)
    dataset = load_dataset(limit, only)
    runs = []
    for attempt in range(1, repeat + 1):
        scored, failures = [], []
        for row in dataset:
            try:
                measured = run_question(row)
                scored.append(score_question(row, measured, with_judge=with_judge))
            except Exception as exc:                       # noqa: BLE001 — 한 문항 실패가 전체를 멈추지 않는다
                failures.append({"id": row["id"], "error": f"{type(exc).__name__}: {exc}",
                                 "trace": traceback.format_exc(limit=3)})
            print(f'  {row["id"]} {"실패" if failures and failures[-1]["id"] == row["id"] else "완료"}',
                  flush=True)
        summary = score_mod.aggregate(scored) if scored else {}
        if with_judge:
            judged = [q["judge"]["score"] for q in scored if q.get("judge", {}).get("score") is not None]
            summary["axis5_evidence_fidelity"] = round(sum(judged) / len(judged), 4) if judged else None
        runs.append({"attempt": attempt, "summary": summary, "questions": scored,
                     "failures": failures})
    return {"condition": condition, "model": config.LLM_MODEL if condition != "rules" else None,
            "questions_in_run": len(dataset), "runs": runs}


def save(payload: dict, name: str) -> pathlib.Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M")
    path = RESULTS / f"eval_{name}_{stamp}.json"
    payload["checked_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="평가 하네스 (Q-02 축 1 ~ 7)")
    ap.add_argument("--condition", choices=("primary", "backup", "rules"), default="rules")
    ap.add_argument("--limit", type=int, default=None, help="앞에서 N 문항만")
    ap.add_argument("--only", nargs="*", help="문항 ID 지정 (EV-001 ...)")
    ap.add_argument("--repeat", type=int, default=1, help="LLM 조건은 3회가 기본 구성(Q-02)")
    ap.add_argument("--judge", action="store_true", help="축 5 Judge 까지 잰다")
    ap.add_argument("--judge-check", action="store_true", help="5.1 Judge 교차 검증만 돌린다")
    args = ap.parse_args()

    if args.judge_check:
        payload = judge_check(limit=args.limit or 10)
        # **저장을 먼저 한다** — 콘솔 인코딩(cp949)에서 출력이 막혀도 결과가 남아야 한다
        path = save(payload, "judge_check")
        numbers = {k: v for k, v in payload["summary"].items() if k != "note"}
        print(json.dumps(numbers, ensure_ascii=False, indent=1))
        print(f"결과: {path.relative_to(BACKEND)}")
        return

    payload = run(args.condition, limit=args.limit, only=args.only, repeat=args.repeat,
                  with_judge=args.judge)
    path = save(payload, args.condition)
    for r in payload["runs"]:
        print(f'\n[{args.condition} {r["attempt"]}회차] 실패 {len(r["failures"])}건')
        print(json.dumps(r["summary"], ensure_ascii=False, indent=1))
    print(f"결과: {path.relative_to(BACKEND)}")


def judge_check(limit: int = 10) -> dict:
    """5.1 교차 검증 — 1차 조건으로 주문서를 만들고, 그 주문서로 (a) 정상 · (b) 오답을 짝지어 본다.

    (a) 10건은 **사람이 정답과 일치한다고 확인한** 것이어야 한다(Q-02 5.1). 여기서는 자동으로
    "정답과 일치한 문항"만 골라 (a) 로 쓰고, 사람 확인은 Q-05 가 기록한다.
    """
    setup_condition("primary")
    dataset = [r for r in load_dataset() if r["source"] == "snapshot"][:limit * 2]
    cases, kinds = [], list(judge_mod.INJECTIONS)
    for row in dataset:
        if len(cases) >= limit:
            break
        try:
            measured = run_question(row)
        except Exception:                                  # noqa: BLE001
            continue
        scored = score_mod.items_score(row["expected_items"], measured["items"])
        if not scored["ok"] or not measured["items"]:
            continue                                       # 정답과 어긋난 주문서는 (a) 로 쓰지 않는다
        cases.append({"id": row["id"], "conversation": row.get("query") or "",
                      "items": measured["items"], "brand_id": row.get("brand") or "starbucks",
                      "menu_version": config.MENU_VERSION})

    paired = list(cases)
    for n, case in enumerate(cases):
        kind = kinds[n % len(kinds)][0]
        paired.append({**case, "id": f'{case["id"]}-{kind}', "injection": kind})

    out = judge_mod.cross_check(paired)
    return {"summary": {k: v for k, v in out.items() if k not in ("normal", "injected")},
            "cases": len(cases), "detail": {"normal": out["normal"], "injected": out["injected"]}}


if __name__ == "__main__":
    main()
