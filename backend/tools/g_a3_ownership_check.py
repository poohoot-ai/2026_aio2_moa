"""G-A3 소유권 차단 확인 — 남의 데이터를 직접 조회해 보고 빈손인지 기록한다 (D-10 3절).

    python tools/g_a3_ownership_check.py        # .env 의 DB 로 실행 · 만든 세션은 끝나면 지운다

판정 기준 (드라이런 함정 — D-10 3.1)
  "결과 행이 있는가"가 아니다 — 안전한 구현도 **본인 데이터는 정상 반환**한다.
  **"돌려받은 데이터의 실제 소유자가 피해자인가"** 다. 소유자는 툴 출력이 아니라 DB 에서 따로 확인한다.

측정 도구 자기 검증 (G-A0) — 양방향
  정상(자기 데이터)은 통과로 나와야 하고, **소유 조건을 뺀 취약 구현**에 같은 공격을 걸면 유출로 잡혀야 한다.
  취약 구현에서 유출이 안 잡히면 판정기가 아무것도 못 잡는 것이고, 그때의 "차단"은 무효다.
"""

import datetime
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, tools  # noqa: E402
from app.main import app  # noqa: E402
from app.services import brand_flow, sessions  # noqa: E402
from app.tools import order_tools  # noqa: E402

LOG = pathlib.Path(__file__).resolve().parent.parent / "eval" / "g_a3_ownership_check.json"


# --- 판정기 ---------------------------------------------------------------------
def owners(item_ids: list[str]) -> set[str]:
    """DB 에서 항목의 실제 소유 세션을 찾는다 — 툴이 돌려준 값을 믿지 않는다."""
    if not item_ids:
        return set()
    rows = db.fetch_all("SELECT session_id FROM app.order_item WHERE item_id = ANY(%s)",
                        (list(item_ids),))
    return {r["session_id"] for r in rows}


def leaked(result: dict, victim_session: str) -> bool:
    items = result.get("items") or ([result["item"]] if result.get("item") else [])
    return victim_session in owners([i["item_id"] for i in items])


def vulnerable_get_order_item(ctx, item_id: str) -> dict:
    """**대조용 취약 구현** — 소유 조건(session_id)을 뺐다. 판정기가 유출을 잡는지 보려고만 쓴다."""
    row = db.fetch_one(
        "SELECT i.item_id, i.person, i.menu_text AS menu, i.options, i.ambiguous_options, "
        "       i.custom_request, i.quantity, i.validation_status "
        "  FROM app.order_item i WHERE i.item_id = %s", (item_id,))
    return {"item": order_tools._project(row) if row else None}


def make_session(lines: list[str]) -> dict:
    raw = "\n".join(lines[:-1] + [f"g-a3-check {uuid.uuid4().hex}", lines[-1]])
    started = brand_flow.start(raw, source="paste", trace_id="tr_GA3CHECK")
    sessions.run_pipeline(started["session_id"], trace_id="tr_GA3CHECK")
    row = db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s",
                       (started["session_id"],))
    return {"row": row, "token": started["session_token"],
            "items": [i["item_id"] for i in sessions.items(started["session_id"])]}


def main() -> None:
    victim = make_session(["[피해자] 아이스 아메리카노 톨 한 잔", "[피해자] 라떼 그란데 하나", "스타벅스로 갈 예정이야"])
    attacker = make_session(["[공격자] 아이스티 한 잔", "동네카페에서 사올게"])
    v_id, a_id = victim["row"]["session_id"], attacker["row"]["session_id"]
    a_ctx = tools.context_from_session(attacker["row"])
    v_item, a_item = victim["items"][0], attacker["items"][0]
    cases = []

    def record(kind, name, verdict, expect, detail):
        cases.append({"kind": kind, "case": name, "verdict": verdict, "expect": expect,
                      "pass": verdict == expect, "detail": detail})

    try:
        # ---- 툴 층 (G-A3 ② — 툴 코드 안의 소유권 검증) ----
        mine = tools.call(a_ctx, "get_order_item", {"item_id": a_item})
        record("정상", "자기 항목 조회 — get_order_item(자기 item_id)",
               "통과" if mine["item"] and owners([mine["item"]["item_id"]]) == {a_id} else "차단",
               "통과", {"item_id": a_item, "반환": bool(mine["item"])})

        theft = tools.call(a_ctx, "get_order_item", {"item_id": v_item})
        record("공격", "남의 항목 ID 조회 — get_order_item(피해자 item_id)",
               "유출" if leaked(theft, v_id) else "차단", "차단",
               {"item_id": v_item, "반환": theft})

        listing = tools.call(a_ctx, "list_order_items", {})
        record("공격", "목록에 남의 항목이 섞이는가 — list_order_items()",
               "유출" if leaked(listing, v_id) else "차단", "차단",
               {"반환 건수": len(listing["items"]), "소유 세션": sorted(owners([i["item_id"] for i in listing["items"]]))})

        cross = tools.call(a_ctx, "get_menu", {"menu_id": "sb_004"})   # 공격자는 일반 카페 기준
        record("공격", "다른 매장 기준 메뉴 조회 — get_menu(sb_004, 세션은 generic)",
               "유출" if cross["menu"] else "차단", "차단", {"반환": cross})

        for smuggle in ({"query": "아메리카노", "brand_id": "starbucks"},
                        {"item_id": v_item, "session_id": v_id}):
            name = "search_menu" if "query" in smuggle else "get_order_item"
            try:
                tools.call(a_ctx, name, smuggle)
                record("공격", f"식별자를 인자로 끼워 넣기 — {name}({sorted(smuggle)})", "실행됨", "거부", {})
            except tools.ToolRejected as exc:
                record("공격", f"식별자를 인자로 끼워 넣기 — {name}({sorted(smuggle)})", "거부", "거부",
                       {"사유": str(exc)})

        menus = tools.call(a_ctx, "search_menu", {"query": "아메리카노"})["menus"]
        record("공격", "검색 결과에 다른 매장 기준 메뉴가 섞이는가 — search_menu(아메리카노)",
               "유출" if any(m["menu_id"].startswith("sb_") for m in menus) else "차단", "차단",
               {"반환": [m["menu_id"] for m in menus]})

        # ---- G-A0 — 판정기가 유출을 실제로 잡는가 (취약 구현 대조) ----
        weak = vulnerable_get_order_item(a_ctx, v_item)
        record("판정기 검증", "소유 조건 없는 취약 구현에 같은 공격",
               "유출" if leaked(weak, v_id) else "차단", "유출",
               {"설명": "여기서 유출이 잡혀야 위의 '차단'이 의미를 가진다"})

        # ---- API 층 (D-02 1.1 — 관리 링크 토큰) ----
        client = TestClient(app)
        a_auth = {"X-Session-Token": attacker["token"]}
        res = client.get(f"/api/sessions/{v_id}", headers=a_auth)
        record("공격", "내 토큰으로 남의 세션 조회 — GET /api/sessions/{피해자}",
               "차단" if res.status_code == 404 else f"유출({res.status_code})", "차단",
               {"status": res.status_code})
        tampered = attacker["token"][:-2] + ("aa" if not attacker["token"].endswith("aa") else "bb")
        res = client.get(f"/api/sessions/{a_id}", headers={"X-Session-Token": tampered})
        record("공격", "변조한 관리 링크 토큰 — GET /api/sessions/{내 세션}",
               "차단" if res.status_code == 404 else f"유출({res.status_code})", "차단",
               {"status": res.status_code})
        res = client.get(f"/api/sessions/{a_id}", headers=a_auth)
        record("정상", "내 토큰으로 내 세션 조회 — GET /api/sessions/{내 세션}",
               "통과" if res.status_code == 200 else f"차단({res.status_code})", "통과",
               {"status": res.status_code})
    finally:
        for sid in (v_id, a_id):
            db.execute("DELETE FROM app.order_session WHERE session_id = %s", (sid,))

    ok = all(c["pass"] for c in cases)
    report = {"checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
              "victim_session": v_id, "attacker_session": a_id, "result": "통과" if ok else "미통과",
              "cases": cases}
    LOG.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"G-A3 소유권 차단 확인 — {report['result']}  (피해자 {v_id} · 공격자 {a_id} · 세션은 삭제함)")
    for c in cases:
        print(f"  {'OK' if c['pass'] else 'NG'}  [{c['kind']}] {c['case']} → {c['verdict']} (기대 {c['expect']})")
    print(f"로그: {LOG.relative_to(LOG.parent.parent)}")


if __name__ == "__main__":
    main()
