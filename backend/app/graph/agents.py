"""툴을 쓰는 에이전트 2개 — Menu Agent(N-05) · Validation 보조 검토(N-09 저장 직후). PRD 5.1 · D-11 4.2 · 4.3.

둘 다 **판정 권한이 없다** — 결과는 확인 질문의 후보 · 사유에만 들어간다.
  Menu Agent      규칙이 메뉴를 정하지 못한 항목만 받는다. 툴로 메뉴판을 찾아 **후보를 채우고 순서를 정한다**.
                  menu_id 를 확정하지 않는다 — 확정은 담당자가 질문에서 한다(PRD 5.1 Menu "허용: 후보 제안 · 정렬" · SC-8)
  보조 검토       대상이 모호한(어느 잔 · 어느 주문) 항목만 받는다. 툴로 주문 항목을 보고 **의견과 사유**를 낸다.
                  validation_status 를 바꾸지 않는다(REQ-004 ① · `validation.apply_llm_review`)

한 왕복 = 해석 서비스(DEP-001) 한 번. LLM 이 "툴을 하나 더 부른다 / 결론을 낸다"를 고르고, 툴 실행은 `tools.call`(세션 주입 ·
소유권 · 경로별 허용)이 한다. 왕복 상한 3(`config.MAX_TOOL_ROUNDS`) · 호출한 툴 이름을 남긴다(교안코드규약 4.7).
해석 서비스가 없거나 실패하면 **아무것도 바꾸지 않는다** — 규칙 결과가 그대로 남는다.
"""

import json

from app import config, interpreter_client, tools
from app.services import menu as menu_service
from app.services.text_norm import normalize
from app.tools.budget import AGENT_TOOLS
from app.tools.pii import mask_pii


def _tool_specs(agent: str) -> list[dict]:
    allowed = AGENT_TOOLS[agent]
    out = []
    for spec in tools.schemas():
        if spec["name"] in allowed:
            props = spec["parameters"].get("properties", {})
            params = ", ".join(f"{k}({v.get('description', v.get('type', ''))})" for k, v in props.items()) or "없음"
            out.append({"name": spec["name"], "description": spec["description"], "params": params})
    return out


def _short(result: dict) -> str:
    """툴 결과를 다음 왕복 입력에 붙일 한 줄 — 메뉴 id · 이름 · 옵션 값 · 항목 요약만. 금액 칸은 넣지 않는다(D-11 금지 5)."""
    if "menus" in result:
        return "; ".join(f"{m['menu_id']} {m['name']}" + (f" (특성 {', '.join(m.get('reason_tags') or m.get('trait_tags') or [])})"
                                                          if m.get("reason_tags") or m.get("trait_tags") else "")
                         for m in result["menus"]) or "결과 없음"
    if "menu" in result:
        if not result["menu"]:
            return "이 매장 메뉴판에 없음"
        groups = "; ".join(f"{g['group']}: {', '.join(v['value'] for v in g['values'])}"
                           for g in result.get("options") or [])
        return f"{result['menu']['menu_id']} {result['menu']['name']} · {groups or '옵션 없음'}"
    if "items" in result or "item" in result:
        items = result.get("items") or ([result["item"]] if result.get("item") else [])
        return "; ".join(f"{i['item_id']} {i['person']} {i['menu']} {i['quantity']}잔 {i.get('options') or ''}"
                         for i in items) or "결과 없음"
    if "error" in result:
        return f"거부됨: {result['error']}"
    return "결과 없음"


def _run(task: str, agent: str, route: str, ctx, items: list[dict], messages: list[dict], *, service: str,
         trace_id: str | None) -> tuple[dict | None, list[str], int, list[dict]]:
    """전문가 루프 1회. (결론, 호출한 툴 이름, LLM 호출 수, 툴 결과 원본 목록)."""
    budget = tools.ToolBudget(agent=agent, route=route)
    specs = _tool_specs(agent)
    calls, raw_results = [0], []

    def step(transcript: list[dict]) -> dict:
        done = transcript[1:]
        history = [{"tool": tc["name"], "args": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                    "result": _short(r["result"])}
                   for turn in done for tc, r in zip(turn["tool_calls"], turn["results"])]
        raw_results[:] = [r["result"] for turn in done for r in turn["results"]]
        context = {"brand_id": ctx.brand_id, "menu_version": ctx.menu_version, "menu_as_of": config.MENU_AS_OF,
                   "agent": {"items": items, "tools": specs, "transcript": history,
                             "remaining": config.MAX_TOOL_ROUNDS - len(done) - 1}}
        calls[0] += 1
        reply = interpreter_client.interpret_resilient(task, messages, context, service=service, trace_id=trace_id,
                                             session_id=ctx.session_id).model_dump()
        if reply["action"] == "call_tool" and reply.get("tool"):
            try:
                args = json.loads(reply.get("args_json") or "{}")
            except (TypeError, ValueError):
                args = {"args_json": reply.get("args_json")}      # 스키마 밖 인자 → 툴이 거부 → 사유가 LLM 에 돌아간다
            return {"tool_calls": [{"name": reply["tool"], "args": args if isinstance(args, dict) else {}}]}
        return {"final": reply}

    try:
        result = tools.run_specialist(ctx, budget, {"items": items}, step)
    except interpreter_client.InterpreterError:
        return None, list(budget.used), calls[0], raw_results
    return result.final, result.used, calls[0], raw_results


# --- Menu Agent ---------------------------------------------------------------------------------
def menu_agent(session: dict, row: dict, messages: list[dict], *, route: str, service: str,
               trace_id: str | None) -> tuple[dict, int]:
    """규칙이 못 정한 항목 하나 → 후보 · 사유를 채운 항목(menu_id 는 그대로 None), LLM 호출 수."""
    ctx = tools.context_from_session(session)
    rule_candidates = ", ".join(c["name"] for c in row.get("candidates") or []) or "없음"
    original = (messages[-1].get("text") if messages else "") or row.get("menu_text") or ""
    summary = (f"{row.get('person')} — 원문 '{original}' · 메뉴 표현 '{row.get('match_text') or row.get('menu_text')}'"
               f" · 수량 {row.get('quantity') or 1}"
               + (f" · 특성 {', '.join(row['traits'])}" if row.get("traits") else "")
               + (f" · 모호 {', '.join(row['ambiguous_options'])}" if row.get("ambiguous_options") else "")
               + f" · 규칙 후보: {rule_candidates}")
    final, used, n, raw = _run("normalize_menu", "menu", route, ctx, [{"item_ref": "i1", "summary": summary}],
                               messages, service=service, trace_id=trace_id)
    out = {**row, "agent_tools": used}
    if not final or not final.get("matches"):
        return out, n
    catalog = menu_service.catalog(session["brand_id"], session["menu_version"])["by_id"]
    # 툴이 이번에 돌려준 menu_id 만 받는다 — 기억 속 메뉴 · 다른 매장 메뉴는 버린다(D-11 menu.md 규칙 1)
    seen = {m["menu_id"] for r in raw for m in r.get("menus") or []} | \
           {r["menu"]["menu_id"] for r in raw if isinstance(r.get("menu"), dict) and r["menu"].get("menu_id")}
    match = final["matches"][0]
    picks = [match["menu_id"]] if match.get("menu_id") else []
    picks += [c["menu_id"] for c in match.get("candidates") or []]
    ordered, keep = [], set()
    for menu_id in picks + [c["menu_id"] for c in row.get("candidates") or []]:
        if menu_id in catalog and (menu_id in seen or menu_id in {c["menu_id"] for c in row.get("candidates") or []}) \
                and menu_id not in keep:
            keep.add(menu_id)
            hit = catalog[menu_id]
            ordered.append({"menu_id": menu_id, "name": hit["name"], "base_price": hit.get("base_price"),
                            "trait_tags": hit.get("trait_tags", [])})
    if ordered:
        out["candidates"] = ordered[:5]
        out["candidates_by"] = "menu_agent"
    reason, _ = mask_pii(match.get("reason") or "")
    out["agent_reason"] = reason[:120] if reason else None
    split = request_split(original, match)
    if split:
        out["llm_split"] = split
    return out, n


# 요청 구절이라고 볼 만한 말 — 맛 이름만 떼어 낸 것("헤이즐넛")은 요청이 아니라 다른 메뉴다(menu.md 규칙 8)
_REQUEST_CUE = ("시럽", "우유", "휘핑", "드리즐", "폼", "펌프", "컵", "리드", "샷", "적게", "조금", "많이",
                "빼", "없이", "추가", "넣", "올려", "바꿔", "변경", "말고", "따로")


def request_split(original: str, match: dict) -> dict | None:
    """LLM 이 나눈 (메뉴 부분, 요청 부분)을 받아도 되는가. 받으면 {"menu_text", "custom_request"}.

    LLM 은 **나누기만** 한다. 메뉴는 호출자가 menu_text 로 규칙(엄격 매칭)에 다시 맞춘다 — 확정 권한은 그대로 규칙에 있다(SC-8).
    받지 않는 경우: 둘 중 하나가 비었다 · 원문에 없는 글자를 지어냈다 · 요청 부분에 요청이라고 볼 말이 없다.
    """
    menu_text = (match.get("menu_text") or "").strip()
    custom = (match.get("custom_request") or "").strip()
    if not menu_text or not custom:
        return None
    source = normalize(original)
    if not source or normalize(menu_text) not in source or normalize(custom) not in source:
        return None
    if not any(cue in custom for cue in _REQUEST_CUE):
        return None
    return {"menu_text": menu_text, "custom_request": custom}


# --- Validation 보조 검토 --------------------------------------------------------------------------
_VERDICT_DB = {"agree": "agree", "disagree": "disagree", "unsure": None}   # D-07 CHECK — unsure 는 저장 값이 없다


def review_agent(session: dict, row: dict, messages: list[dict], *, route: str, service: str,
                 trace_id: str | None) -> tuple[dict | None, int]:
    """대상이 모호한 항목 하나 → {llm_review_verdict, review_reason_text} 또는 None, LLM 호출 수. 판정 권한 없음."""
    ctx = tools.context_from_session(session)
    summary = (f"{row['item_id']} {row.get('person')} {row.get('menu_name') or row.get('menu_text')} "
               f"{row.get('quantity') or 1}잔 · 확인 사유: {row.get('review_reason_text') or row.get('_target_unclear_text')}")
    final, used, n, _ = _run("review", "validation", route, ctx, [{"item_ref": row["item_id"], "summary": summary}],
                             messages, service=service, trace_id=trace_id)
    if not final or not final.get("verdicts"):
        return None, n
    verdict = final["verdicts"][0]
    reason, _ = mask_pii(verdict.get("reason") or "")
    from app.graph.nodes import clean_text                                    # 금액 문자열 제거 (D-11 금지 5)

    return {"llm_review_verdict": _VERDICT_DB.get(verdict.get("verdict")),
            "review_reason_text": clean_text(reason)[:120] if reason else None,
            "tools": used}, n
