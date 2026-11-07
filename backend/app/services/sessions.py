"""세션 수명 주기 — 생성 · 분석 파이프라인 · 조회 · 수정.

소유권 차단 (G-A3 · D-07 2.2)
  모든 조회에 `session_id` 조건이 들어간다. 토큰은 **해시로** 조회하고,
  경로의 session_id 와 불일치면 404 다 — 존재 여부를 알려주지 않는다.
  brand_id · menu_version 은 **LLM 인자로 받지 않고 세션에서 주입한다.**

낙관적 잠금 (D-07 D-3)
  세션 단위 `version` 하나다. 쓰기마다 +1 하고, If-Match 가 어긋나면 409 다.
"""

import json

from app import config, db, ids, logging_setup
from app.services import chat_parse, intake
from app.services import menu as menu_service
from app.services import questions

CHECKPOINTS = ("created", "chat_read", "menu_matched", "validated", "settled")

# 되묻기 대기 세션을 담는 상태. D-02 API-012 가 쓰는 이름이고, 스키마 CHECK 에 맞춰 두었다
AWAITING_BRAND = "awaiting_brand_answer"


# --- 조회 · 소유권 ------------------------------------------------------------
def load(session_id: str, token: str | None) -> dict | None:
    """세션 1건. 토큰이 맞지 않으면 None — 호출자가 404 를 돌려준다."""
    if not session_id or not token:
        return None
    return db.fetch_one(
        "SELECT * FROM app.order_session WHERE session_id = %s AND token_hash = %s",
        (session_id, ids.token_hash(token)),
    )


def items(session_id: str, *, include_removed: bool = False) -> list[dict]:
    """항목 목록. **모든 조회에 session_id 조건이 들어간다** (2.2 2층)."""
    sql = (
        "SELECT i.*, m.name AS menu_name, m.base_price "
        "  FROM app.order_item i LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
        " WHERE i.session_id = %s "
        + ("" if include_removed else "AND i.removed = false ")
        + " ORDER BY i.item_id"
    )
    return db.fetch_all(sql, (session_id,))


def review_count(session_id: str) -> int:
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM app.order_item "
        " WHERE session_id = %s AND removed = false AND validation_status <> 'passed'",
        (session_id,))
    return int(row["n"])


def bump_version(cur, session_id: str, expected: int | None) -> int:
    """쓰기마다 +1. expected 가 주어지고 어긋나면 None 을 돌려준다(409 신호)."""
    if expected is None:
        cur.execute(
            "UPDATE app.order_session SET version = version + 1, updated_at = NOW() "
            " WHERE session_id = %s RETURNING version", (session_id,))
    else:
        cur.execute(
            "UPDATE app.order_session SET version = version + 1, updated_at = NOW() "
            " WHERE session_id = %s AND version = %s RETURNING version",
            (session_id, expected))
    row = cur.fetchone()
    return row["version"] if row else None


def set_checkpoint(cur, session_id: str, checkpoint: str) -> None:
    """체크포인트 저장 실패는 **치명**이다 — 삼키지 않는다 (FR-013 · D-07 4.13).

    이력(request_history · llm_history)과 다르게 취급하는 지점이다.
    """
    if checkpoint not in CHECKPOINTS:
        raise ValueError(f"checkpoint 는 5종에서만 쓴다: {checkpoint}")
    cur.execute(
        "UPDATE app.order_session SET checkpoint = %s, checkpoint_at = NOW() "
        " WHERE session_id = %s", (checkpoint, session_id))
    if cur.rowcount != 1:
        logging_setup.log("checkpoint_save_failed", level="error",
                          session_id=session_id, checkpoint=checkpoint)
        raise RuntimeError(f"체크포인트 저장 실패: {session_id} {checkpoint}")
    logging_setup.log("checkpoint_saved", session_id=session_id, checkpoint=checkpoint)


def set_status(cur, session_id: str, status: str) -> None:
    cur.execute("UPDATE app.order_session SET status = %s, updated_at = NOW() "
                " WHERE session_id = %s", (status, session_id))


# --- 중복 재입력 판정 ---------------------------------------------------------
def find_by_content(content_hash: str, token_hash: str) -> dict | None:
    """같은 담당자가 같은 내용을 다시 넣으면 기존 결과를 돌려준다 (FR-011).

    **그 관리 토큰의 세션만** 찾는다 — 남이 같은 원문을 넣은 세션은 돌려주지 않는다(brand_flow.start).
    검증용 세션(is_verification)은 제외한다 — 검증이 운영 결과를 덮으면 안 된다.
    """
    return db.fetch_one(
        "SELECT s.* FROM app.snapshot sn JOIN app.order_session s ON s.session_id = sn.session_id "
        " WHERE sn.content_hash = %s AND s.token_hash = %s AND s.is_verification = false "
        " ORDER BY sn.created_at DESC LIMIT 1",
        (content_hash, token_hash),
    )


# --- 생성 --------------------------------------------------------------------
def create(raw_text: str, *, source: str, brand: dict, trace_id: str,
           is_verification: bool = False) -> dict:
    """세션 · 스냅샷 · 메시지를 한 트랜잭션으로 넣는다.

    매장 기준은 호출자가 brand 서비스로 먼저 판별해 넘긴다 — 되묻기 여부를
    여기서 다시 따지지 않는다(판별은 한 곳에서만).
    """
    parsed = chat_parse.parse(raw_text)
    token = ids.new_token()

    with db.transaction() as cur:
        cur.execute("SELECT 1")                       # 연결 확인 (statement_timeout 적용 상태)
        session_id = _next_session_id(cur)
        cur.execute(
            "INSERT INTO app.order_session "
            "  (session_id, token_hash, brand_id, brand_source, brand_evidence_text, "
            "   brand_store_name, menu_version, status, trace_id, is_verification) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s, %s)",
            (session_id, ids.token_hash(token), brand["brand_id"], brand["source"],
             brand.get("evidence_text"), brand.get("store_name"), config.MENU_VERSION,
             trace_id, is_verification),
        )
        snapshot_id = ids.snapshot_id(session_id, 1)
        cur.execute(
            "INSERT INTO app.snapshot "
            "  (snapshot_id, session_id, snapshot_no, raw_text, content_hash, source, "
            "   message_count, person_count, unreadable_lines) "
            "VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s)",
            (snapshot_id, session_id, raw_text, ids.content_hash(raw_text), source,
             parsed["message_count"], parsed["person_count"],
             json.dumps(parsed["unreadable_lines"], ensure_ascii=False)),
        )
        # 파서가 준 m1 · m2 는 세션 안에서만 유일하다 — 저장 시 세션 범위로 바꾼다
        for message in parsed["messages"]:
            message["message_id"] = ids.message_id(session_id, message["seq"])
        cur.executemany(
            "INSERT INTO app.message (message_id, snapshot_id, seq, sender, text) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(m["message_id"], snapshot_id, m["seq"], m["sender"], m["text"])
             for m in parsed["messages"]],
        )
        set_checkpoint(cur, session_id, "created")

    logging_setup.log("session_created", session_id=session_id, trace_id=trace_id,
                      brand_id=brand["brand_id"], messages=parsed["message_count"])
    logging_setup.log("brand_decision", session_id=session_id, trace_id=trace_id,
                      brand_id=brand["brand_id"], brand_source=brand["source"], llm_calls=0)
    return {"session_id": session_id, "session_token": token, "snapshot_id": snapshot_id,
            "parsed": parsed}


def _next_session_id(cur) -> str:
    cur.execute(
        "SELECT COALESCE(MAX(NULLIF(regexp_replace(session_id, '^ORD-', ''), '')::int), 0) AS n "
        "  FROM app.order_session WHERE session_id ~ '^ORD-[0-9]+$'")
    return f"ORD-{cur.fetchone()['n'] + 1:04d}"


# --- 분석 파이프라인 ----------------------------------------------------------
def run_pipeline(session_id: str, *, trace_id: str) -> dict:
    """대화 읽기 → 메뉴 맞추기 → 검증 → 정산 — 업무 층 그래프(D-12 · D-15)를 source=snapshot 으로 돌린다.

    단계 경계 · 체크포인트 5값 · route_decision 로그는 그래프 노드(app/graph/nodes.py)가 그대로 남긴다.
    """
    from app import graph

    session = db.fetch_one("SELECT * FROM app.order_session WHERE session_id = %s",
                           (session_id,))
    if not session:
        raise LookupError(session_id)
    return graph.run("snapshot", session, trace_id=trace_id)["result"]


def _named_person(text: str, events) -> str | None:
    """참조 문장이 가리키는 사람. 이름 전체(`김민수`) · 성을 뗀 이름(`민수`) 둘 다 본다.

    여러 사람이 걸리면 **가장 길게 걸린 이름**을 고른다 — 짧은 조각이 우연히 겹칠 수 있다.
    """
    best = None
    for person in {e.get("person") for e in events if e.get("person")}:
        for token in (person, person[1:] if len(person) >= 3 else None):
            if token and token in text and (best is None or len(token) > best[0]):
                best = (len(token), person)
    return best[1] if best else None


def _referenced_menus(events, upto_index, *, brand_id, menu_version) -> list[dict]:
    """참조 주문("그걸로" · "나도 같은 걸로")이 가리키는 메뉴 후보.

    앞선 메시지를 거꾸로 훑어 메뉴로 읽히는 것을 찾는다. **`ignore` 로 분류된 잡담도 본다** —
    "민트 초코가 유명하대"처럼 주문이 아닌 말에서 메뉴를 가져오는 것이 참조 주문이다.
    후보가 없으면 빈 목록이고, 그때는 미등록으로 간다.
    """
    from app.services import intake as intake_service

    earlier_events = events[:upto_index]
    # "민수랑 같은 걸로" — 가리키는 **사람 이름이 있으면 그 사람의 메시지에서만** 찾는다.
    # 이름을 무시하고 가장 가까운 메뉴를 집으면 엉뚱한 사람의 주문이 붙는다
    # (정하준 "나도 민수랑 같은 걸로" → 바로 앞 잡담의 "민트 초코" — 2026-11-03 확인)
    said = (events[upto_index].get("_raw_text") or "") if upto_index < len(events) else ""
    named = _named_person(said, earlier_events)
    if named:
        earlier_events = [e for e in earlier_events if e.get("person") == named]

    found: list[dict] = []
    for earlier in reversed(earlier_events):
        text = earlier.get("_raw_text") or ""
        hint = intake_service._menu_text(text)
        if not hint:
            continue
        matched = menu_service.match(brand_id, menu_version, hint)
        if matched.get("menu_id"):
            found.append({"menu_id": matched["menu_id"], "name": matched["name"],
                          "base_price": matched["base_price"],
                          "trait_tags": matched.get("trait_tags", []),
                          "source_message_id": earlier["message_id"]})
        else:
            found.extend(matched.get("candidates") or [])
        if found:
            break
        # 더 거슬러 올라가기 전에 — 이 메시지가 **지금 못 내는 메뉴**를 가리키고 있나?
        #
        # 가리킨 것을 찾았는데 낼 수 없는 경우다. 여기서 멈춰야 한다. 그냥 지나치면
        # 더 오래된 남의 주문을 집어 **확인 필요도 없이 조용히 확정된다** — 2026-11-07 실측:
        # 최유나 "그럼 난 그걸로"(= 정하준이 말한 민트 초코)가 세 메시지 앞 박서연의
        # 아이스 아메리카노로 정해졌다. 담당자는 안 시킨 커피를 받는다.
        # 빈 목록으로 돌아가면 미등록으로 가고, 미등록은 담당자에게 묻는다(도메인 용어 "확인 필요").
        if menu_service.unavailable_mentions(brand_id, menu_version, text):
            return []
    return found[:5]


def match_item(item: dict, *, brand_id: str, menu_version: str) -> dict:
    """분석 경로의 메뉴 매칭 — 한 곳에서만 한다. 측정 도구(tools/u15_measure.py)도 이 함수를 부른다.

      1 엄격 매칭 — 포함 규칙에 설명되지 않는 글자가 남으면 확정하지 않는다(미등록 메뉴 차단 · SC-8)
      2 온도 단서 — 얼음 옵션 · 뒤에 붙은 "아이스" 로 같은 메뉴의 아이스 판을 고른다(PRD 8.1 #1)
      3 온도로 후보 좁히기 — 후보가 아이스 판 · 따뜻한 판 둘뿐이면 단서로 하나를 고른다(보고 6번)
    """
    matched = menu_service.match(brand_id, menu_version, item.get("match_text") or item["menu_text"],
                                 strict=True)
    # 3 후보가 아이스 판 · 따뜻한 판으로만 갈렸으면 온도 단서로 하나를 고른다 (2026-11-05 보고 6번)
    matched = menu_service.narrow_by_temperature(matched, item.get("temperature_hint"))
    return menu_service.with_temperature(brand_id, menu_version, matched,
                                         item.get("temperature_hint"))


# 사이즈 원문 표기 — 다른 매장 용어를 확인 질문에 그대로 보여 준다
SIZE_WORD = {"short": "숏", "tall": "톨", "grande": "그란데", "venti": "벤티",
             "regular": "레귤러", "large": "라지"}


def _spec_checks(item: dict, *, brand_id: str, menu_version: str) -> dict:
    """해석 뒤 · 검증 전에 매장 기준을 알아야 보이는 사양 문제를 확인 질문으로 돌린다.

    1 다른 매장의 사이즈 용어 — 스타벅스 세션의 "라지", 일반 카페 세션의 "톨".
      옵션 오류(failed)로 두면 "옵션을 다시 골라 주세요"만 뜨고 고를 후보가 없다.
      사이즈 확인 질문으로 바꾸면 그 매장의 사이즈 버튼이 뜬다
    2 온도 충돌 — "따뜻한 아이스 …". 아이스 · 따뜻한 두 메뉴를 후보로 붙인다
    0 (먼저) 메뉴에 그 종류가 없는 옵션 — 스타벅스 세션의 "두유" · "시럽 추가", 에스프레소의 "톨".
      옵션 오류로 두지 않고 기타 요청으로 옮긴다(2026-11-04 · 스타벅스는 5종만 메뉴판 옵션)
    """
    options, moved = menu_service.split_unoffered(brand_id, menu_version, item.get("options") or {},
                                                  item.get("menu_id"))
    ambiguous = dict(item.get("ambiguous_options") or {})
    if moved:
        item = {**item, "custom_request": intake.move_to_custom(
            item.get("custom_request"), moved, item.get("option_words"))}

    size = options.get("size")
    if size and not menu_service.is_valid_option(brand_id, menu_version, "size", size,
                                                 menu_id=item.get("menu_id")):
        options.pop("size")
        ambiguous.setdefault("size", f'"{SIZE_WORD.get(size, size)}"')

    out = {**item, "options": options, "ambiguous_options": ambiguous}
    if ambiguous.get("temperature"):
        out["temperature_candidates"] = menu_service.temperature_variants(
            brand_id, menu_version, item.get("temperature_base") or item.get("match_text") or "")
    return out


def _insert_events(cur, session_id, snapshot_id, events, trace_id):
    """Intake 결과 저장 — `payload` 는 재개용 저장본이다(이벤트 전체 · PRD 5.2 Handoff · D-12 10.4)."""
    for index, event in enumerate(events, start=1):
        event["event_id"] = ids.event_id(session_id, index)
        cur.execute(
            "INSERT INTO app.order_event "
            "  (event_id, session_id, snapshot_id, message_id, evidence_message_ids, "
            "   event_type, is_reference, agent, trace_id, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (event["event_id"], session_id, snapshot_id, event["message_id"],
             event["evidence_message_ids"], event["event_type"], event["is_reference"],
             event.get("agent", "intake"), trace_id,
             json.dumps(event, ensure_ascii=False,
                        default=lambda v: sorted(v) if isinstance(v, (set, frozenset)) else str(v))),
        )
    # 항목이 가리킬 수 있도록 매핑을 돌려준다
    return {e["message_id"]: e["event_id"] for e in events}


def _insert_items(cur, session_id, rows):
    for row in rows:
        cur.execute(
            "SELECT event_id FROM app.order_event "
            " WHERE session_id = %s AND message_id = %s LIMIT 1",
            (session_id, row["_message_id"]))
        found = cur.fetchone()
        cur.execute(
            "INSERT INTO app.order_item "
            "  (item_id, session_id, source_event_id, person, menu_id, menu_text, options, "
            "   ambiguous_options, custom_request, quantity, unit_price, estimated_price, "
            "   validation_status, rule_verdict, llm_review_verdict, review_reason, "
            "   review_reason_text, person_check) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (row["item_id"], session_id, found["event_id"] if found else None, row["person"],
             row["menu_id"], row["menu_text"],
             json.dumps(row.get("options") or {}, ensure_ascii=False),
             json.dumps(row.get("ambiguous_options") or {}, ensure_ascii=False),
             row.get("custom_request"), row.get("quantity") or 1, row.get("unit_price"),
             row.get("estimated_price"), row["validation_status"], row["rule_verdict"],
             row.get("llm_review_verdict"), row.get("review_reason"),
             row.get("review_reason_text"),
             json.dumps(row["person_check"], ensure_ascii=False) if row.get("person_check") else None),
        )


def _build_questions(session_id, rows, *, brand_id, menu_version):
    """확인 필요 항목 → 질문. 후보 · 특성 태그 · 참조 근거를 그대로 들고 간다."""
    live = db.fetch_all(
        "SELECT item_id, validation_status FROM app.order_item "
        " WHERE session_id = %s AND removed = false", (session_id,))
    needs = {r["item_id"] for r in live if r["validation_status"] == "needs_review"}

    source_texts = _message_texts(session_id)
    built = []
    for row in rows:
        if row["item_id"] not in needs:
            continue
        built.append(questions.build(
            row, session_id=session_id, brand_id=brand_id, menu_version=menu_version,
            seq=len(built) + 1,
            source_text=source_texts.get(row.get("_reference_source")),
            change_text=source_texts.get(row.get("_unclear_event"))))
    questions.insert(session_id, built)
    return built


def _message_texts(session_id: str) -> dict[str, str]:
    rows = db.fetch_all(
        "SELECT m.message_id, m.text FROM app.message m "
        "  JOIN app.snapshot s ON s.snapshot_id = m.snapshot_id WHERE s.session_id = %s",
        (session_id,))
    return {r["message_id"]: r["text"] for r in rows}
