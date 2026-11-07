"""축 5 근거 충실도 — LLM Judge (Q-02 평가셋정의서 5절).

항목마다 ① supported ② unsupported ③ misattributed 를 고른다.
**확인 필요로 둔 항목은 "확정하지 않았다"는 사실로 supported** 다 — 모르는 것을 모른다고 둔 것이다.

제품 프롬프트가 아니다 — `prompts/`(D-11)와 별개로 여기 둔다. 생성과 같은 모델이라
자기 선호 위험이 있으므로 5.1 교차 검증(정상 10 + 의도적 오답 10)으로 Judge 자체를 잰다(G-A0 · G-A5).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app import config, llm
from app.services import menu as menu_service

VERDICTS = ("supported", "unsupported", "misattributed")

SYSTEM = """너는 주문서 검수자다. 대화 원문과 그 매장의 메뉴판만 근거로, 주문서 항목 하나하나가
대화로 설명되는지 판정한다.

판정 값
  supported       대화와 메뉴판으로 설명된다. **확인 필요로 남겨 둔 항목도 supported** 다
                  (확정하지 않은 것은 지어낸 것이 아니다)
  unsupported     대화에 없는 메뉴 · 수량 · 옵션 · 사이즈가 들어 있다
  misattributed   다른 사람의 주문으로 붙었다

규칙
  - 메뉴판에 있는 메뉴라도 그 대화에서 말하지 않았다면 unsupported 다
  - 대화가 말하지 않은 사이즈 · 옵션이 항목에 적혀 있으면 unsupported 다.
    단 **매장 기본값**으로 적힌 사이즈는 supported 로 본다(대화에 사이즈 단서가 없을 때)
  - 수량이 대화와 다르면 unsupported 다
  - 판정보다 근거를 먼저 쓴다"""


class JudgeItem(BaseModel):
    item_ref: str = Field(description="판정 대상 항목의 번호(입력에 적힌 그대로)")
    reason: str = Field(description="대화 · 메뉴판의 어디를 보고 그렇게 판정했는가. 판정보다 먼저 쓴다")
    verdict: str = Field(description="supported · unsupported · misattributed 중 하나")


class JudgeResult(BaseModel):
    items: list[JudgeItem]


def _menu_table(brand_id: str, menu_version: str, items: list[dict]) -> str:
    """그 세션 매장 기준의 해당 메뉴 · 옵션만 보여 준다 — 메뉴판 전체를 넣지 않는다(토큰 · 유도)."""
    catalog = menu_service.catalog(brand_id, menu_version)
    lines = []
    for menu_id in sorted({i.get("menu_id") for i in items if i.get("menu_id")}):
        menu = catalog["by_id"].get(menu_id)
        if menu:
            lines.append(f'- {menu["name"]} ({menu_id}) {menu["base_price"]}원')
    groups = {}
    for opt in catalog["options"]:
        groups.setdefault(opt["option_group"], []).append(
            f'{opt["option_value"]}={opt["value_label"]}' + ("(기본)" if opt["is_default"] else ""))
    for group, values in groups.items():
        lines.append(f"- 옵션 {group}: {' · '.join(values)}")
    return "\n".join(lines) or "- (해당 메뉴 없음)"


def sheet_text(items: list[dict]) -> str:
    """판정 대상 주문서 — 항목마다 번호를 붙인다. Judge 가 그 번호로 답한다."""
    lines = []
    for n, item in enumerate(items, start=1):
        opts = " ".join(f"{k}={v}" for k, v in sorted((item.get("options") or {}).items()))
        lines.append(f'{n}. {item.get("person") or "?"} · {item.get("menu_name") or item.get("menu_text")} '
                     f'· {item.get("size") or "사이즈 미정"} · {item.get("quantity") or 1}잔'
                     f'{" · " + opts if opts else ""}'
                     f'{" · 확인 필요" if item.get("needs_review") else ""}')
    return "\n".join(lines)


def judge(conversation: str, items: list[dict], *, brand_id: str, menu_version: str,
          model: str | None = None) -> dict:
    """항목별 판정 + 점수(supported 비율). LLM 1회."""
    if not items:
        return {"items": [], "score": None, "llm_calls": 0}

    user = (f"[대화 원문]\n{conversation}\n\n"
            f"[그 매장 메뉴 · 옵션]\n{_menu_table(brand_id, menu_version, items)}\n\n"
            f"[주문서]\n{sheet_text(items)}\n\n"
            f"항목 {len(items)}건 전부를 판정해라.")
    result = llm.generate_structured([{"role": "user", "content": user}], JudgeResult,
                                     system=SYSTEM, temperature=0,
                                     model=model or config.LLM_MODEL)
    parsed = JudgeResult.model_validate(result.data)
    verdicts = [v for v in parsed.items if v.verdict in VERDICTS]
    supported = sum(1 for v in verdicts if v.verdict == "supported")
    return {"items": [v.model_dump() for v in parsed.items],
            "score": round(supported / len(verdicts), 4) if verdicts else None,
            "invalid_verdicts": len(parsed.items) - len(verdicts),
            "llm_calls": 1}


# --- 5.1 교차 검증 — Judge 자체를 잰다 (G-A0 · G-A5) ---------------------------
INJECTIONS = (
    ("quantity", "수량을 바꾼다"),
    ("option", "대화에 없는 옵션을 붙인다"),
    ("size", "사이즈를 바꾼다"),
    ("person", "주문자를 바꾼다"),
    ("extra_person", "대화에 없는 사람의 항목을 더한다"),
    ("similar_menu", "유사 메뉴로 바꾼다"),
)


def inject(items: list[dict], kind: str, *, brand_id: str, menu_version: str) -> tuple[list[dict], int]:
    """(b) 의도적 오답 — **한 군데만** 틀린 그럴듯한 주문서를 만든다. 돌려주는 값: (주문서, 틀린 항목 번호).

    가드(N-10)를 통과하는 값으로만 만든다 — 메뉴판 안의 메뉴 · 옵션. 그래야 Judge 의 판별력만 남는다.
    """
    sheet = [dict(i) for i in items]
    if not sheet:
        raise ValueError("빈 주문서에는 오답을 삽입할 수 없다")
    target = 0
    row = sheet[target]
    if kind == "quantity":
        row["quantity"] = int(row.get("quantity") or 1) + 1
    elif kind == "option":
        row["options"] = {**(row.get("options") or {}), "shot": "extra"}
    elif kind == "size":
        row["size"] = "venti" if row.get("size") != "venti" else "tall"
    elif kind == "person":
        others = [i.get("person") for i in sheet if i.get("person") != row.get("person")]
        row["person"] = others[0] if others else "박서연"
    elif kind == "extra_person":
        sheet.append({**row, "person": "정하준", "quantity": 1, "needs_review": False})
        target = len(sheet) - 1
    elif kind == "similar_menu":
        catalog = menu_service.catalog(brand_id, menu_version)
        name = (row.get("menu_name") or "")
        swap = next((m for m in catalog["menus"]
                     if m["name"] != name and name and (name in m["name"] or m["name"] in name)), None)
        if swap is None:
            raise ValueError(f"유사 메뉴를 찾지 못했다: {name}")
        row["menu_id"], row["menu_name"] = swap["menu_id"], swap["name"]
    else:
        raise ValueError(kind)
    return sheet, target + 1


def cross_check(cases: list[dict], *, model: str | None = None) -> dict:
    """정상 10 + 의도적 오답 10 을 Judge 에 넣어 양방향을 본다.

    cases: {conversation, items, brand_id, menu_version, injection} — injection 이 없으면 (a) 정상.
    통과 조건(Q-02 5.1): (a) 전부 supported · (b) **삽입 항목을 10건 전부** 잡아야 한다.
    """
    normal, injected, calls = [], [], 0
    for case in cases:
        if not case.get("injection"):
            out = judge(case["conversation"], case["items"], brand_id=case["brand_id"],
                        menu_version=case["menu_version"], model=model)
            calls += out["llm_calls"]
            normal.append({"id": case.get("id"), "score": out["score"],
                           "all_supported": out["score"] == 1.0, "items": out["items"]})
            continue
        sheet, bad_ref = inject(case["items"], case["injection"], brand_id=case["brand_id"],
                                menu_version=case["menu_version"])
        out = judge(case["conversation"], sheet, brand_id=case["brand_id"],
                    menu_version=case["menu_version"], model=model)
        calls += out["llm_calls"]
        caught = any(str(v["item_ref"]).strip().startswith(str(bad_ref))
                     and v["verdict"] in ("unsupported", "misattributed") for v in out["items"])
        injected.append({"id": case.get("id"), "injection": case["injection"],
                         "bad_ref": bad_ref, "caught": caught, "score": out["score"],
                         "items": out["items"]})

    caught_n = sum(1 for r in injected if r["caught"])
    return {
        "normal": normal, "injected": injected, "llm_calls": calls,
        "normal_all_supported": sum(1 for r in normal if r["all_supported"]),
        "injected_caught": caught_n,
        "passes": bool(normal) and bool(injected)
        and all(r["all_supported"] for r in normal) and caught_n == len(injected),
        "note": "(b) 없이 낸 일치율은 보고하지 않는다 — 전부 supported 라고 답하는 Judge 도 (a)만으로는 100% 다",
    }
