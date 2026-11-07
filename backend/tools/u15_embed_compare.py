"""U-15 대조 — 같은 54문항을 임베딩 최근접 검색으로 매칭하면 어떻게 되는가.

    python tools/u15_embed_compare.py

별칭 + 규칙(u15_measure.py)이 틀린 문항이 **임베딩으로 고쳐지는 종류인지**를 재는 일회성 측정이다.
메뉴 이름 + 별칭을 문서로 임베딩하고, 매칭키를 질의로 임베딩해 매장 범위 안에서 코사인 최근접을 고른다.
  - 1위 점수가 문턱 이상이면 자동 확정, 아니면 상위 5개를 후보로 질문 (임계값은 아래 THRESHOLDS 로 훑는다)
판정은 u15_measure.judge 를 그대로 쓴다 — 측정 도구를 하나로 둔다.
"""

import csv
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tools.u15_measure import CSV, MENU_VERSION, judge  # noqa: E402

MODEL = "text-embedding-3-small"
THRESHOLDS = (0.0, 0.5, 0.6, 0.7, 0.8)


def embed(texts: list[str]) -> list[list[float]]:
    from app import config

    res = httpx.post(f"{config.OPENAI_BASE_URL}/embeddings",
                     headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                     json={"model": MODEL, "input": texts}, timeout=60)
    res.raise_for_status()
    return [d["embedding"] for d in sorted(res.json()["data"], key=lambda d: d["index"])]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> None:
    import logging

    logging.disable(logging.CRITICAL)
    from app.services import intake, menu

    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    docs = []                                    # (brand, menu_id, 문서 문자열)
    for brand in ("starbucks", "generic"):
        cat = menu.catalog(brand, MENU_VERSION)
        aliases: dict[str, list[str]] = {}
        for key, ids in cat["alias_index"].items():
            for mid in ids:
                aliases.setdefault(mid, []).append(key)
        for m in cat["menus"]:
            docs.append((brand, m["menu_id"], f'{m["name"]} ({", ".join(sorted(set(aliases.get(m["menu_id"], []))))})'))
    keys = [intake._menu_text(r["text"]) for r in rows]
    vectors = embed([d[2] for d in docs] + keys)
    doc_vecs, key_vecs = vectors[:len(docs)], vectors[len(docs):]
    print(f"임베딩 {MODEL} · 차원 {len(doc_vecs[0])} · 문서 {len(docs)}개 · 문항 {len(rows)}개")

    ranked = []
    for row, key, kv in zip(rows, keys, key_vecs):
        scores = sorted(((cosine(kv, dv), d[1]) for d, dv in zip(docs, doc_vecs) if d[0] == row["brand"]),
                        reverse=True)
        ranked.append((row, key, scores))

    order = ["correct_auto", "asked_hit", "asked_miss", "wrong_auto", "correct_none"]
    print(f"\n{'문턱':>5} " + " ".join(f"{o:>12}" for o in order))
    for th in THRESHOLDS:
        counter = dict.fromkeys(order, 0)
        for row, key, scores in ranked:
            top_score, top_id = scores[0]
            matched = ({"menu_id": top_id} if top_score >= th
                       else {"menu_id": None, "candidates": [{"menu_id": mid} for _, mid in scores[:5]]})
            counter[judge(row["expected"], matched)] += 1
        print(f"{th:>5} " + " ".join(f"{counter[o]:>12}" for o in order))

    print("\n== 규칙이 틀린 문항의 임베딩 1위 (점수)")
    focus = {"바닐라라떼 큰 거 얼음 적게요", "차이 라떼", "아이스 카페 라테", "바닐라 라테 아이스", "아메리카노 아이스로",
             "카라멜마끼아또", "라떼 얼음 많이", "헤이즐넛 라떼", "아이스 연유라떼", "토피넛 라떼", "딸기 라떼"}
    names = {d[1]: d[2].split(" (")[0] for d in docs}
    for row, key, scores in ranked:
        if row["text"] in focus:
            top_score, top_id = scores[0]
            ok = "정답" if top_id == row["expected"] else ("미등록인데 확정" if not row["expected"] else "오답")
            print(f"  [{row['brand']}] {row['text']!r} → {names[top_id]} ({top_score:.2f}) · {ok}"
                  f" · 정답 {names.get(row['expected'], '(미등록)')}")


if __name__ == "__main__":
    main()
