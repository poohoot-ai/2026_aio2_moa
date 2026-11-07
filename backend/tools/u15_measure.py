"""PRD U-15 판정 — 별칭 + 규칙만으로 메뉴를 얼마나 맞히는가 (D-08 착수 판정).

    python tools/u15_measure.py            # eval/u15_menu_match.csv 전체
    python tools/u15_measure.py --self     # 판정기 자기 검증(G-A0)만

실제 분석 경로와 같은 함수를 탄다: 메시지 원문 → intake.extract_events(옵션 · 수량 · 온도 단서)
→ sessions.match_item(엄격 매칭 + 온도 판 선택). 메뉴 DB 는 .env 의 DATABASE_URL 로 읽는다.

판정 4종
  correct_auto   자동 확정 · 정답
  asked_hit      확신이 없어 물었다 · 후보에 정답 있음   (안전 — 담당자가 한 번 누른다)
  asked_miss     물었지만 후보에 정답 없음               (불편 — 메뉴 찾기로 간다. 임베딩이 줄일 수 있는 것)
  wrong_auto     **틀린 메뉴로 자동 확정**               (위험 — SC-8 · 0건이어야 한다)
미등록(expected 비움)은 자동 확정이 없으면 correct_none, 있으면 wrong_auto 다.
"""

import csv
import pathlib
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

CSV = pathlib.Path(__file__).resolve().parent.parent / "eval" / "u15_menu_match.csv"
MENU_VERSION = "mv_20261029"


def judge(expected: str, matched: dict) -> str:
    """판정 함수. 입력은 menu.match 결과 모양 그대로다."""
    got = matched.get("menu_id")
    candidates = [c["menu_id"] for c in matched.get("candidates") or []]
    if not expected:
        return "correct_none" if not got else "wrong_auto"
    if got:
        return "correct_auto" if got == expected else "wrong_auto"
    return "asked_hit" if expected in candidates else "asked_miss"


def self_check() -> list[str]:
    """G-A0 — 판정기가 정상은 정상으로, 실패는 실패로 가르는가. 양방향을 다 본다."""
    cases = [
        ("sb_004", {"menu_id": "sb_004"}, "correct_auto"),
        ("sb_004", {"menu_id": "sb_011"}, "wrong_auto"),          # 오답 확정을 잡아야 한다
        ("sb_004", {"menu_id": None, "candidates": [{"menu_id": "sb_004"}]}, "asked_hit"),
        ("sb_004", {"menu_id": None, "candidates": [{"menu_id": "sb_011"}]}, "asked_miss"),
        ("", {"menu_id": None, "candidates": []}, "correct_none"),
        ("", {"menu_id": "sb_003"}, "wrong_auto"),                # 미등록을 확정하면 잡아야 한다
    ]
    return [f"{exp or '(미등록)'} {m} -> {judge(exp, m)} (기대 {want})"
            for exp, m, want in cases if judge(exp, m) != want]


def main() -> None:
    bad = self_check()
    print("G-A0 판정기 자기 검증:", "통과 (6/6 — 정상 · 오답 · 미등록 양방향)" if not bad else bad)
    if bad or "--self" in sys.argv:
        return

    import logging

    logging.disable(logging.CRITICAL)
    from app.services import intake, sessions

    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    by_group: dict[str, Counter] = defaultdict(Counter)
    total = Counter()
    problems = []
    for row in rows:
        # 분석 경로와 같은 함수 — 메시지 1건을 해석하고(intake) 그 항목을 매칭한다(sessions.match_item)
        event = intake.extract_events([{"message_id": "m1", "seq": 1, "sender": "측정",
                                        "text": row["text"]}],
                                      brand_id=row["brand"], menu_version=MENU_VERSION)[0]
        item = (event.get("items") or [{"match_text": intake._menu_text(row["text"]),
                                        "menu_text": row["text"]}])[0]
        key = item.get("match_text") or item["menu_text"]
        matched = sessions.match_item(item, brand_id=row["brand"], menu_version=MENU_VERSION)
        verdict = judge(row["expected"], matched)
        by_group[row["group"]][verdict] += 1
        total[verdict] += 1
        if verdict not in ("correct_auto", "correct_none"):
            got = matched.get("menu_id") or [c["menu_id"] for c in matched.get("candidates") or []]
            problems.append((verdict, row["brand"], row["text"], key, row["expected"] or "(미등록)", got))

    order = ["correct_auto", "asked_hit", "asked_miss", "wrong_auto", "correct_none"]
    print(f"\n문항 {len(rows)}개 (메뉴 DB {MENU_VERSION})")
    print(f"{'묶음':8} " + " ".join(f"{o:>12}" for o in order))
    for group, counter in by_group.items():
        print(f"{group:8} " + " ".join(f"{counter[o]:>12}" for o in order))
    print(f"{'합계':8} " + " ".join(f"{total[o]:>12}" for o in order))

    registered = sum(total[o] for o in ("correct_auto", "asked_hit", "asked_miss")) + \
        sum(1 for p in problems if p[0] == "wrong_auto" and p[4] != "(미등록)")
    print(f"\n등록 메뉴 자동 정답률: {total['correct_auto']}/{registered}"
          f" · 질문 포함 정답 도달률: {total['correct_auto'] + total['asked_hit']}/{registered}"
          f" · 오답 자동 확정(SC-8): {total['wrong_auto']}건")
    print("\n== 정답이 아닌 문항")
    for verdict, brand, text, key, expected, got in problems:
        print(f"  {verdict:11} [{brand}] {text!r} → 매칭키 {key!r} · 정답 {expected} · 결과 {got}")


if __name__ == "__main__":
    main()
