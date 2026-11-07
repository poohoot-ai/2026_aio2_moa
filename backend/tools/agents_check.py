"""Menu Agent · Validation 보조 검토 확인 — 규칙이 못 정하는 주문을 에이전트 없이 / 있이 그래프에 태워 대조한다.

    python tools/agents_check.py      # .env 의 DB · OpenAI 키. 만든 세션은 끝나면 지운다

보는 것: 에이전트가 툴을 실제로 부르는가(tool_call 로그) · 확인 질문 후보가 어떻게 바뀌는가 ·
**메뉴를 확정하지 않는가**(SC-8 — 확정 항목 수가 에이전트 없을 때와 같아야 한다) · 보조 검토 의견이 남는가.
결과: eval/agents_check.json
"""

import datetime
import json
import logging
import os
import pathlib
import sys
import uuid

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
logging.disable(logging.CRITICAL)
os.environ["INTERPRETER_SOURCE"] = "llm"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, interpreter_client, logging_setup  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.services import brand_flow, sessions  # noqa: E402
from interpreter import main as svc  # noqa: E402

LOG = BACKEND / "eval" / "agents_check.json"
CASES = [
    ("특성 주문", ["[이지연] 나는 달달한 걸로 한잔 부탁해"]),
    ("미등록 수식어", ["[정하준] 헤이즐넛 라떼 하나요"]),
    ("시드에 없는 메뉴", ["[이지연] 녹차 프라푸치노 톨 하나"]),
    ("대리 주문 · 군더더기", ["[박서연] 저는 조금 이따 볼게요", "[김민수] 서연이 것도 라떼 하나 추가요"]),
    ("줄임 표현", ["[최유나] 민초 블렌디드 하나요"]),
    ("대상 모호 취소 (보조 검토)", ["[김민수] 아아 톨 2잔, 하나는 샷 추가요.", "[김민수] 하나 취소할게요."]),
]
tool_calls: list[dict] = []
_real_log = logging_setup.log


def capture(event, **fields):
    if event == "tool_call":
        tool_calls.append({"tool": fields.get("tool"), "agent": fields.get("agent"), "route": fields.get("route")})
    _real_log(event, **fields)


def setup():
    client = TestClient(svc.app)
    svc.SOURCE, svc.MODE_OVERRIDE = "llm", "normal"
    config.ORDER_INTERPRETER_PRIMARY_URL = "http://primary"
    real = interpreter_client.interpret
    interpreter_client.interpret = lambda *a, **k: real(*a, **{**k, "client": client})
    nodes._pick_service = lambda: "primary"             # 데모 시드가 1차를 circuit_open 에 둔다
    logging_setup.log = capture


def run(lines, agents_on):
    config.MENU_AGENT_MAX_ITEMS = 20 if agents_on else 0
    config.REVIEW_AGENT_MAX_ITEMS = 10 if agents_on else 0
    tool_calls.clear()
    raw = "\n".join(lines + [f"agents-check {uuid.uuid4().hex[:8]}", "스타벅스로 갈 예정이야"])
    started = brand_flow.start(raw, source="paste", trace_id="tr_AGENTCHK")
    sid = started["session_id"]
    sessions.run_pipeline(sid, trace_id="tr_AGENTCHK")
    items = [r for r in sessions.items(sid) if not r["removed"]]
    qs = db.fetch_all("SELECT item_id, question_text, options FROM app.review_question WHERE session_id = %s "
                      " ORDER BY seq", (sid,))
    out = {"items": [{"person": r["person"], "menu": r.get("menu_name") or r["menu_text"], "confirmed": bool(r["menu_id"]),
                      "status": r["validation_status"], "reason": r.get("review_reason_text"),
                      "llm_review": r.get("llm_review_verdict")} for r in items],
           "questions": [{"text": q["question_text"], "options": [o.get("label") for o in q["options"] or []]} for q in qs],
           "tools": list(tool_calls)}
    db.execute("DELETE FROM app.order_session WHERE session_id = %s", (sid,))
    return out


def main():
    setup()
    report = []
    for name, lines in CASES:
        off, on = run(lines, False), run(lines, True)
        same_confirmed = [i["confirmed"] for i in off["items"]] == [i["confirmed"] for i in on["items"]]
        report.append({"case": name, "lines": lines, "rules_only": off, "with_agents": on,
                       "confirmed_unchanged": same_confirmed})
        print(f"\n== {name}: {' / '.join(lines)}")
        for label, r in (("에이전트 없음", off), ("에이전트 있음", on)):
            print(f"  [{label}] 툴 {[t['tool'] for t in r['tools']] or '없음'}")
            for q in r["questions"]:
                print(f"      질문: {q['text']}  →  후보 {q['options'] or '없음'}")
            for i in r["items"]:
                if i["llm_review"] or (label == "에이전트 있음" and i["reason"] and "취소" in (i["reason"] or "")):
                    print(f"      보조 검토: {i['person']} {i['menu']} · 의견 {i['llm_review']} · 사유 '{i['reason']}'")
        print(f"  확정 항목 수 변화 없음(SC-8): {same_confirmed}")
    LOG.write_text(json.dumps({"checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                               "model": config.LLM_MODEL, "cases": report}, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    print(f"\n로그: {LOG.relative_to(BACKEND)} (세션 삭제함)")


if __name__ == "__main__":
    main()
