"""금액 계산과 정합성 — 금액은 INTEGER 원 단위, 미정은 NULL 이다 (D-07 D-11).

0 과 NULL 을 구분하는 것이 이 모듈의 핵심이다. 확인 필요 항목을 0원으로 세면
합계가 우연히 맞아떨어져 `integrity_ok` 가 통과로 보인다.
"""

from app import db
from app.services import menu as menu_service


def item_price(item: dict, *, brand_id: str, menu_version: str) -> tuple[int | None, int | None]:
    """(unit_price, estimated_price). 메뉴가 안 정해졌으면 둘 다 None.

    기타 요청(custom_request)은 금액에 반영하지 않는다 (P-06).
    """
    if not item.get("menu_id"):
        return None, None
    # 사양이 모호하면 금액을 내지 않는다. 사이즈에 따라 추가금이 갈리므로
    # 기준가를 그대로 보여주면 담당자가 **확정된 금액으로 오해한다**(정본 시안도 null 이다)
    if item.get("ambiguous_options"):
        return None, None
    catalog = menu_service.catalog(brand_id, menu_version)
    base = catalog["by_id"].get(item["menu_id"], {}).get("base_price")
    if base is None:
        return None, None
    extra = menu_service.option_extra_price(brand_id, menu_version, item.get("options") or {},
                                            menu_id=item["menu_id"])
    unit = base + extra
    return unit, unit * int(item.get("quantity") or 1)


def compute_lines(rows: list[dict]) -> list[dict]:
    """개인별 금액 — 저장하되 화면 · 공유 카드로 내보내지 않는다 (D-07 D-5).

    SC-5(개인 합계의 합 = 전체 금액)를 재려면 계산과 저장이 있어야 한다.
    """
    by_person: dict[str, dict] = {}
    for row in rows:
        if row.get("removed"):
            continue
        person = row["person"]
        bucket = by_person.setdefault(person, {"person": person, "cups": 0, "amount": 0,
                                               "has_undecided": False})
        bucket["cups"] += int(row.get("quantity") or 1)
        if row.get("estimated_price") is None:
            bucket["has_undecided"] = True
        else:
            bucket["amount"] += int(row["estimated_price"])
    return [by_person[p] for p in sorted(by_person)]


def save_lines(session_id: str, version: int, lines: list[dict], *, cur=None) -> None:
    """settlement_line 에 그 시점 계산을 고정한다 — 버전이 PK 에 들어간다."""
    if not lines:
        return
    sql = (
        "INSERT INTO app.settlement_line (session_id, session_version, person, cups, amount) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (session_id, session_version, person) DO UPDATE "
        "   SET cups = EXCLUDED.cups, amount = EXCLUDED.amount, computed_at = NOW()"
    )
    params = [(session_id, version, line["person"], line["cups"], line["amount"])
              for line in lines]
    if cur is not None:
        cur.executemany(sql, params)
        return
    with db.cursor() as own:
        own.executemany(sql, params)


def integrity(session_id: str) -> dict:
    """v_settlement_check 를 읽어 판정한다 (API-030 totals.integrity_ok).

    `undecided_count` 를 함께 보는 이유 — 금액이 NULL 인 항목이 있으면 SUM 이 그 행을
    건너뛰어 두 합계가 우연히 같아진다. 하나라도 미정이면 통과로 보지 않는다.
    """
    row = db.fetch_one(
        "SELECT person_total, item_total, undecided_count "
        "  FROM app.v_settlement_check WHERE session_id = %s AND version = "
        "       (SELECT version FROM app.order_session WHERE session_id = %s)",
        (session_id, session_id),
    )
    if not row:
        return {"integrity_ok": False, "reason": "세션을 찾지 못했어요",
                "person_total": None, "item_total": None, "undecided_count": None}

    undecided = int(row["undecided_count"] or 0)
    person_total = row["person_total"]
    item_total = row["item_total"]
    ok = undecided == 0 and person_total is not None and person_total == item_total
    return {
        "integrity_ok": ok,
        "person_total": person_total,
        "item_total": item_total,
        "undecided_count": undecided,
        "reason": None if ok else ("확인이 필요한 주문이 있어요" if undecided
                                   else "금액 합계가 맞지 않아요"),
    }


def check(rows: list[dict]) -> dict:
    """로그용 정산 대조 — `v_settlement_check` + `integrity()` 와 **같은 규칙**을 메모리에서 계산한다.

    내부 검증 자료(SC-5)를 로그 한 줄로 남기기 위한 것이다. 판정 기준은 `integrity()` 와 같다 —
    미정(NULL) 항목이 하나라도 있으면 두 합계가 우연히 같아도 통과로 보지 않는다.
    """
    live = [r for r in rows if not r.get("removed")]
    lines = compute_lines(live)
    priced = [int(r["estimated_price"]) for r in live if r.get("estimated_price") is not None]
    undecided = len(live) - len(priced)
    person_total = sum(line["amount"] for line in lines) if lines else None
    item_total = sum(priced) if priced else None      # SQL SUM 처럼 대상이 없으면 NULL
    ok = undecided == 0 and person_total is not None and person_total == item_total
    return {
        "integrity_ok": ok,
        "person_total": person_total,
        "item_total": item_total,
        "undecided_count": undecided,
        "person_amounts": {line["person"]: line["amount"] for line in lines},
    }


def count_people(names) -> int:
    """인원 수 — 이름이 있으면 같은 이름은 1명, `이름 없음`은 **항목마다 1명**으로 센다.

    이름이 없으면 같은 사람인지 알 수 없다. 중복 없이 세면 이름 없는 참여자 여럿이 1명으로 줄어
    "C · 이름 없음 · 이름 없음 · D"가 3명으로 나갔다(2026-11-06). 적게 세는 것보다 따로 세는 쪽을 택한다.
    """
    from app.services.chat_parse import UNNAMED

    names = list(names)
    unnamed = sum(1 for name in names if not name or name == UNNAMED)
    return len({name for name in names if name and name != UNNAMED}) + unnamed


def totals(rows: list[dict]) -> dict:
    """합계 — 미정이 하나라도 있으면 estimated_total 을 None 으로 둔다."""
    live = [r for r in rows if not r.get("removed")]
    cups = sum(int(r.get("quantity") or 1) for r in live)
    undecided = [r for r in live if r.get("estimated_price") is None]
    total = None if undecided else sum(int(r["estimated_price"]) for r in live)
    return {
        "cups": cups,
        "people": count_people(r.get("person") for r in live),
        "estimated_total": total,
        "undecided_count": len(undecided),
    }


def amount_notice(total: int | None) -> str:
    """화면 문구는 서버가 완성형으로 준다 (D-02 2.4)."""
    if total is None:
        return "확인이 필요한 주문이 있어 금액은 아직 알 수 없어요"
    return f"예상 {total:,}원 · 참고용 금액이에요"
