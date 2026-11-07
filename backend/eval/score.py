"""평가 채점 — Q-02 평가셋정의서 1절 축 1 · 2 · 3 · 4 · 6을 정답 대조로 잰다.

축 5(근거 충실도)만 Judge 가 잰다(eval/judge.py). 축 7(비용 · 지연)은 실행 중에 모은다.

**이 모듈은 DB · LLM 을 모르고, 순수 함수만 둔다** — 측정 도구를 테스트할 수 있어야 한다(G-A0).
tests/test_q03_eval.py 가 정상 케이스와 알려진 실패 케이스를 양방향으로 넣어 본다.
"""

from __future__ import annotations

SIZE_WORDS = {"tall": "tall", "톨": "tall", "grande": "grande", "그란데": "grande",
              "venti": "venti", "벤티": "venti", "regular": "regular", "레귤러": "regular",
              "large": "large", "라지": "large"}


# --- 축 1 경로 분류 정확도 ---------------------------------------------------
def parse_expected_routes(cell: str) -> list[set[str]]:
    """`expected_route` 한 칸을 메시지별 경로 집합 목록으로.

      add                       메시지 1건 · 경로 1개
      cancel+add                메시지 1건 · 한 메시지에 이벤트 2개(집합 비교)
      add;add;ignore            기준 대화 — 줄마다 하나씩
    """
    cells = [c.strip() for c in (cell or "").split(";") if c.strip()]
    return [{r.strip() for r in c.split("+") if r.strip()} for c in cells]


def route_score(expected: str, actual_by_message: list[set[str]]) -> dict:
    """메시지 단위로 맞았는가. 메시지 수가 다르면 그 자체로 틀린 것이다(누락 · 과생성)."""
    want = parse_expected_routes(expected)
    got = [set(s) for s in actual_by_message]
    hits = sum(1 for w, g in zip(want, got) if w == g)
    return {"messages": len(want), "hits": hits,
            "ok": len(want) == len(got) and hits == len(want),
            "length_mismatch": len(want) != len(got),
            "expected": [sorted(w) for w in want], "actual": [sorted(g) for g in got]}


# --- 축 2 주문서 정답 일치율 -------------------------------------------------
def parse_expected_items(cell: str) -> list[dict]:
    """`사람|메뉴|사이즈|잔|확인|옵션` 을 `;` 로 이은 칸.

    메뉴 `?` 는 "정답 메뉴 없음", 사람 `?` 는 "주문자를 확정하면 안 됨" 이다.
    """
    out = []
    for part in [p for p in (cell or "").split(";") if p.strip()]:
        f = (part.split("|") + [""] * 6)[:6]
        out.append({"person": f[0].strip(), "menu": f[1].strip(),
                    "size": SIZE_WORDS.get(f[2].strip(), f[2].strip()),
                    "cups": int(f[3]) if f[3].strip().isdigit() else 1,
                    "review": f[4].strip().upper() == "Y",
                    "options": _opts(f[5])})
    return out


def _opts(cell: str) -> dict:
    out = {}
    for token in [t for t in (cell or "").replace(",", " ").split() if "=" in t]:
        key, _, value = token.partition("=")
        out[key.strip()] = value.strip()
    return out


def _key(person: str, menu: str, size: str, options: dict) -> tuple:
    """(사람, 메뉴, 사이즈, 옵션) — 한 항목 2잔과 항목 2개 1잔을 같게 본다(Q-02 3절)."""
    return (person, menu, size or "", tuple(sorted((options or {}).items())))


def _cups(groups: dict, key: tuple) -> int:
    return groups.get(key, 0)


def items_score(expected: str, actual_items: list[dict], *, candidates_of=None) -> dict:
    """살아 있는 항목을 정답과 대조한다 — 잔 합계 · 확인 필요 표시까지.

    actual_items: {person, menu_id, menu_name, size, options, quantity, needs_review, candidates}
    확정(`N`)이면 menu_id 일치, 확인 필요(`Y`)면 질문 후보에 정답 메뉴가 있으면 맞은 것으로 센다.
    """
    want = parse_expected_items(expected)
    want_groups: dict[tuple, int] = {}
    for w in want:
        want_groups[_key(w["person"], w["menu"], w["size"], w["options"])] = \
            want_groups.get(_key(w["person"], w["menu"], w["size"], w["options"]), 0) + w["cups"]

    got_groups: dict[tuple, int] = {}
    for a in actual_items:
        menu = a.get("menu_id") or "?"
        got_groups[_key(a.get("person") or "?", menu, a.get("size") or "", a.get("options"))] = \
            got_groups.get(_key(a.get("person") or "?", menu, a.get("size") or "",
                                a.get("options")), 0) + int(a.get("quantity") or 1)

    exact = sum(min(_cups(want_groups, k), _cups(got_groups, k)) for k in want_groups)
    want_cups = sum(want_groups.values())
    got_cups = sum(got_groups.values())

    # 확인 필요 항목은 후보에 정답이 있으면 맞은 것으로 센다(확정하지 않은 것이 정답이므로)
    by_candidate = 0
    for w in want:
        if not w["review"] or not w["menu"] or w["menu"] == "?":
            continue
        for a in actual_items:
            if a.get("needs_review") and (a.get("person") in (w["person"], "?", None)):
                options = (candidates_of(a) if candidates_of else a.get("candidates")) or []
                if w["menu"] in [c.get("menu_id") for c in options]:
                    by_candidate += 1
                    break

    # 과생성도 오답이다 — 정답보다 많이 만든 잔을 "정답을 다 맞혔다"로 세면 틀림이 숨는다
    resolved = min(exact + by_candidate, want_cups)
    over_cups = max(0, got_cups - resolved)
    return {"expected_cups": want_cups, "actual_cups": got_cups, "matched_cups": exact,
            "by_candidate": by_candidate, "over_cups": over_cups,
            "resolved_cups": resolved,
            "ok": resolved == want_cups and over_cups == 0,
            "accuracy": None if not max(want_cups, got_cups)
            else round(resolved / max(want_cups, got_cups), 4),
            "expected_groups": {str(k): v for k, v in want_groups.items()},
            "actual_groups": {str(k): v for k, v in got_groups.items()}}


# --- 축 3 적절 확인 재현율 · 축 4 과잉 확인율 --------------------------------
def review_counts(expected: str, actual_items: list[dict]) -> dict:
    """정답이 확인 필요(Y)인 항목 중 올린 비율(재현율)과, 정답이 확정(N)인데 올린 비율(과잉)."""
    want = parse_expected_items(expected)
    want_review = sum(1 for w in want if w["review"])
    want_fixed = sum(1 for w in want if not w["review"])
    got_review = sum(1 for a in actual_items if a.get("needs_review"))

    # 항목 단위 정답 짝짓기가 불가능한 칸(사람 ?)이 있으므로 건수로 센다 — Q-02 1절 측정 방법
    appropriate = min(want_review, got_review)
    over = max(0, got_review - want_review)
    return {"want_review": want_review, "want_fixed": want_fixed, "got_review": got_review,
            "appropriate": appropriate, "over": over,
            "recall": None if not want_review else appropriate / want_review,
            "over_rate": None if not want_fixed else over / want_fixed}


# --- 축 6 근거 표기 정확도 ---------------------------------------------------
def evidence_ok(expected_cell: str, evidence_seqs: list[int]) -> bool | None:
    """`expected_evidence`(선행 문맥 줄 번호 · 1부터)가 이벤트 근거에 들어 있는가. 빈 칸은 해당 없음."""
    want = [int(t) for t in (expected_cell or "").replace(",", " ").split() if t.strip().isdigit()]
    if not want:
        return None
    return set(want) <= set(evidence_seqs or [])


# --- 집계 -------------------------------------------------------------------
def aggregate(rows: list[dict]) -> dict:
    """문항별 채점을 축별 수치로. **분모가 0인 축은 None 으로 둔다** — 0% 로 적으면 미측정이 실패로 보인다."""
    def ratio(hit, total):
        return None if not total else round(hit / total, 4)

    route_total = sum(r["route"]["messages"] for r in rows)
    route_hits = sum(r["route"]["hits"] for r in rows)
    # 분모는 정답 잔 수와 실제 잔 수의 큰 쪽 — 과생성이 정확도를 깎는다
    items_total = sum(max(r["items"]["expected_cups"], r["items"]["actual_cups"]) for r in rows)
    items_hits = sum(r["items"]["resolved_cups"] for r in rows)
    want_review = sum(r["review"]["want_review"] for r in rows)
    appropriate = sum(r["review"]["appropriate"] for r in rows)
    want_fixed = sum(r["review"]["want_fixed"] for r in rows)
    over = sum(r["review"]["over"] for r in rows)
    ev = [r["evidence_ok"] for r in rows if r["evidence_ok"] is not None]

    return {
        "questions": len(rows),
        "axis1_route_accuracy": ratio(route_hits, route_total),
        "axis1_question_level": ratio(sum(1 for r in rows if r["route"]["ok"]), len(rows)),
        "axis2_items_accuracy": ratio(items_hits, items_total),
        "axis3_review_recall": ratio(appropriate, want_review),
        "axis4_over_review_rate": ratio(over, want_fixed),
        "axis6_evidence_accuracy": ratio(sum(1 for e in ev if e), len(ev)),
        "axis7_llm_calls": sum(r["cost"]["llm_calls"] for r in rows),
        "axis7_max_latency_sec": max([r["cost"]["latency_sec"] for r in rows] or [0]),
        "axis7_degraded_questions": sum(1 for r in rows if r["cost"]["degraded"]),
    }
