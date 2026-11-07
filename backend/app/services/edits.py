"""담당자 수정 — 확인 질문 답변 · 행 편집 · 채팅 해석 · 되돌리기.

네 경로가 모두 같은 일을 한다: 항목을 고치고 · 재검증하고 · 금액을 다시 재고 ·
변경 이력을 남기고 · 세션 version 을 올린다. 경로마다 따로 구현하면
한 곳을 빠뜨렸을 때 **그 경로에서만 조용히 깨진다** — 그래서 `_commit` 하나로 모은다.
"""

import json
import re

from app import config, db, ids, logging_setup
from app.services import brand as brand_service
from app.services import changes, intake
from app.services import menu as menu_service
from app.services import questions, sessions, settlement, validation
from app.services.text_norm import normalize, parse_quantity


class Conflict(Exception):
    """If-Match 가 어긋났다 — 호출자가 409 를 돌려준다."""

    def __init__(self, current: int):
        super().__init__(current)
        self.current = current


class NotApplicable(Exception):
    """되돌릴 수 없다 · 고칠 수 없다 — 호출자가 422 를 돌려준다."""


def _guard(session: dict) -> None:
    """그래프 밖에서 항목을 바꾸는 곳(확인 질문 답 · 개별 바꾸기 · 되돌리기)도 **N-10 과 같은 함수**를 지난다.

    검사를 복사하지 않는다 — 같은 함수를 부른다(D-12 6.2). D-16 이 호출 지점 수를 센다.
    """
    from app.graph.nodes import guard_items

    guard_items(session)


def _revalidate(cur, session: dict, item_id: str) -> dict:
    """항목 1건을 다시 판정하고 금액을 다시 잰다. 수정 경로가 모두 이것을 부른다."""
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    cur.execute(
        "SELECT i.*, m.name AS menu_name FROM app.order_item i "
        "  LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
        " WHERE i.session_id = %s AND i.item_id = %s",
        (session["session_id"], item_id))
    row = cur.fetchone()
    if not row:
        raise NotApplicable(item_id)
    if row.get("removed"):
        # 빠진 항목은 판정 대상이 아니다 — 금액도 다시 재지 않는다("주문 아니에요"로 뺀 항목)
        return dict(row)

    verdict = validation.validate_item(
        {**row, "reference": False, "traits": [],
         "confirmed_by_person": True,       # 사람이 직접 고친 항목은 참조 사유가 해소된다
         "match_reason": None if row["menu_id"] else "unregistered_menu"},
        brand_id=brand_id, menu_version=menu_version)
    verdict = validation.apply_llm_review(verdict, row.get("llm_review_verdict"))
    unit, estimated = settlement.item_price(row, brand_id=brand_id, menu_version=menu_version)

    cur.execute(
        "UPDATE app.order_item SET validation_status = %s, rule_verdict = %s, "
        "       review_reason = %s, review_reason_text = %s, unit_price = %s, "
        "       estimated_price = %s, updated_at = NOW() "
        " WHERE session_id = %s AND item_id = %s",
        (verdict["validation_status"], verdict["rule_verdict"], verdict["review_reason"],
         verdict["review_reason_text"], unit, estimated, session["session_id"], item_id))

    if verdict["validation_status"] == "passed":
        # 확인 필요가 풀렸으면 그 항목의 열린 질문도 끝난 것이다 — **여기 한 곳에서** 닫는다.
        #
        # 경로마다 닫게 두면 한 경로를 빠뜨리고, 그 경로에서만 조용히 다시 묻는다.
        # 실제로 그랬다: 답장 반영(`suggestions.decide`)과 개별 바꾸기가 항목은 고치면서
        # 질문은 `open` 으로 남겨, 이미 벤티로 정해진 사이즈를 질문 큐가 다시 물었다
        # (2026-11-07 실측 · ORD-0038 q1). `reconcile` 이 질문을 만드는 조건도
        # `validation_status <> 'passed'` 이므로 닫는 조건을 같은 기준으로 맞춘다.
        #
        # 질문에 답해서 풀린 경우는 그 질문이 이미 `answered` 라 여기서 건드리지 않는다
        # (`void_for_item` 은 `status='open'` 만 본다). 되묻기 상한은 voided 까지 세므로
        # 닫아도 질문이 무한히 다시 생기지 않는다.
        questions.void_for_item(session["session_id"], item_id, cur=cur)
    return {**row, **verdict, "unit_price": unit, "estimated_price": estimated}


def _commit(cur, session: dict, *, expected_version: int | None) -> int:
    """세션 상태를 정리하고 version 을 올린다. 모든 수정 경로의 출구다."""
    session_id = session["session_id"]
    version = sessions.bump_version(cur, session_id, expected_version)
    if version is None:
        cur.execute("SELECT version FROM app.order_session WHERE session_id = %s",
                    (session_id,))
        raise Conflict(cur.fetchone()["version"])

    cur.execute(
        "SELECT COUNT(*) AS n FROM app.order_item "
        " WHERE session_id = %s AND removed = false AND validation_status <> 'passed'",
        (session_id,))
    pending = int(cur.fetchone()["n"])
    if session["status"] not in ("completed", "failed"):
        sessions.set_status(cur, session_id,
                            "needs_review" if pending else "awaiting_confirmation")

    cur.execute(
        "SELECT i.*, m.name AS menu_name FROM app.order_item i "
        "  LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
        " WHERE i.session_id = %s AND i.removed = false", (session_id,))
    rows = cur.fetchall()
    settlement.save_lines(session_id, version, settlement.compute_lines(rows), cur=cur)
    return version


# --- API-021 확인 질문 답변 ---------------------------------------------------
def answer_question(session: dict, question_id: str, answer: str, *,
                    expected_version: int | None, extra_options: dict | None = None) -> dict:
    session_id = session["session_id"]

    with db.transaction() as cur:
        cur.execute(
            "SELECT * FROM app.review_question "
            " WHERE session_id = %s AND question_id = %s AND status = 'open'",
            (session_id, question_id))
        question = cur.fetchone()
        if not question:
            raise NotApplicable(question_id)

        cur.execute("SELECT * FROM app.order_item WHERE session_id = %s AND item_id = %s",
                    (session_id, question["item_id"]))
        before = cur.fetchone()
        before_text = _row_text(before, session)

        _apply_answer(cur, session, question, answer, extra_options)
        cur.execute("UPDATE app.review_question "
                    "   SET status = 'answered', chosen_value = %s, answered_at = NOW() "
                    " WHERE question_id = %s", (answer, question_id))

        after = _revalidate(cur, session, question["item_id"])
        _close_suggestion(cur, session_id, question_id, answer)
        _follow_up(cur, session, after)
        token = changes.record(cur, session_id, method="question_answer",
                               item_id=question["item_id"], who=before["person"],
                               before_text=before_text, after_text=_row_text(after, session),
                               before_json=_row_json(before), after_json=_row_json(after))
        version = _commit(cur, session, expected_version=expected_version)

    _guard(session)
    stats = questions.counts(session_id)
    return {
        "text": _applied_text(question, answer, before["person"]),
        "undo_token": token,
        "changed_item_ids": [question["item_id"]],
        "version": version,
        "remaining": stats["remaining"],
    }


def _close_suggestion(cur, session_id: str, question_id: str, answer: str) -> None:
    """답장 제안에서 올라온 질문을 답하면 그 제안도 결정된 것이다 (2026-11-05 3차 보고 7번).

    닫지 않으면 `pending` 으로 남아 공유 게이트가 계속 막는다 — 담당자에게는 풀 방법이 보이지 않는다.
    """
    cur.execute("UPDATE app.suggestion SET status = %s, decided_at = NOW() "
                " WHERE session_id = %s AND question_id = %s AND status = 'pending'",
                ("ignored" if answer == questions.TARGET_KEEP else "applied",
                 session_id, question_id))


def answer_from_text(session: dict, question_id: str, text: str, *,
                     expected_version: int | None) -> dict:
    """API-021 (text) — 질문이 열려 있을 때 채팅으로 친 말을 **그 질문의 답**으로 읽는다 (2026-11-04 사용자 보고 3 · 4).

    예전에는 질문이 떠 있어도 채팅이 채팅 수정(API-022)으로만 가서, "아이스아메리카노"처럼 사람 이름이 없는 말은
    "어느 주문을 말하는지 찾지 못했어요"가 됐고 질문은 그대로 남았다. 답으로 읽지 못하면 NotApplicable 을 올린다 —
    화면이 그때 채팅 수정으로 넘긴다. 반영은 버튼 답과 **같은 함수**(answer_question)를 지난다.
    """
    question = db.fetch_one(
        "SELECT q.*, i.person, i.ambiguous_options FROM app.review_question q "
        "  JOIN app.order_item i ON i.session_id = q.session_id AND i.item_id = q.item_id "
        " WHERE q.session_id = %s AND q.question_id = %s AND q.status = 'open'",
        (session["session_id"], question_id))
    if not question:
        raise NotApplicable(question_id)
    value = _text_answer(session, question, text)
    key = questions.spec_key(question["ambiguous_options"]) if question["reason"] == "ambiguous_spec" else None
    # 메뉴를 고르는 답이면 함께 적은 옵션(사이즈 · 얼음 등)도 받는다. 사이즈 · 수량 질문은 답 자체가 그 값이다
    extra = intake._options(text) if key is None and question["reason"] not in ("ambiguous_person",) else None
    result = answer_question(session, question_id, value, expected_version=expected_version,
                             extra_options=extra)
    label = next((o.get("label") for o in question["options"] or [] if str(o.get("value")) == value), value)
    return {**result, "answer": value, "answer_label": label}


_TARGET_KEEP_WORDS = re.compile(r"아니|그대로|놔\s*두|괜찮|안\s*바꿔")
_TARGET_APPLY_WORDS = re.compile(r"^\s*(?:네|넵|넹|예|응|어|ㅇㅇ|맞아|맞아요|맞습니다|그거|바꿔|반영)")


def _text_answer(session: dict, question: dict, text: str) -> str:
    """채팅 문장 → 질문의 답 값(버튼 value 또는 메뉴 이름). 못 읽으면 NotApplicable."""
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    said = (text or "").strip()
    options = question["options"] or []
    reason = question["reason"]
    if not said:
        raise NotApplicable(text)

    # 다른 사람 이름이 들어 있으면 이 질문의 답이 아니다 — "박서연 벤티로"는 박서연 주문 수정이다
    people = {r["person"] for r in db.fetch_all(
        "SELECT DISTINCT person FROM app.order_item WHERE session_id = %s AND removed = false",
        (session["session_id"],))}
    if any(p and p != question["person"] and p in said for p in people):
        raise NotApplicable(text)

    if reason == "ambiguous_person":
        names = [str(o["value"]) for o in options]
        hit = [n for n in names[:-1] if n in said]
        if len(hit) == 1:
            return hit[0]
        if names and _TARGET_KEEP_WORDS.search(said):
            return names[-1]
        raise NotApplicable(text)

    key = questions.spec_key(question["ambiguous_options"]) if reason == "ambiguous_spec" else None
    values = {str(o["value"]) for o in options}
    if key == "size":
        size = intake._options(said).get("size")
        if size and size in values:
            return size
    elif key == "quantity":
        digits = re.fullmatch(r"\s*(\d+)\s*(?:잔|개|컵)?\s*(?:이요|요|으로|로)?[.!~]*\s*", said)
        quantity = int(digits.group(1)) if digits else parse_quantity(said)
        if quantity > 0 and (digits or re.search(r"잔|개|컵", said)):
            return str(quantity)
        raise NotApplicable(text)
    elif key == "temperature":
        hint = intake.temperature_hint(said)
        picks = [o for o in options if hint and o["label"].startswith("아이스") == (hint == "ice")]
        if len(picks) == 1:
            return str(picks[0]["value"])
    elif reason == "ambiguous_target":
        if _TARGET_KEEP_WORDS.search(said) and questions.TARGET_KEEP in values:
            return questions.TARGET_KEEP
        if _TARGET_APPLY_WORDS.search(said) and questions.TARGET_APPLY in values:
            return questions.TARGET_APPLY

    if questions.NO_ORDER in values and re.search(r"주문\s*아니|아니에요|아닙니다|빼\s*주|뺄게|취소", said):
        return questions.NO_ORDER

    # 버튼 라벨을 그대로 친 경우 — 긴 라벨부터("아이스 카페 라떼"가 "카페 라떼"보다 먼저)
    said_key = normalize(said)
    for option in sorted(options, key=lambda o: -len(normalize(o.get("label") or ""))):
        label_key = normalize(option.get("label") or "")
        if len(label_key) >= 2 and label_key in said_key:
            return str(option["value"])
    if key in ("size", "quantity"):
        raise NotApplicable(text)

    # 메뉴 이름 — 채팅 수정과 같은 매칭(엄격 아님 · 되돌리기가 있다). 온도 단서로 아이스 · 따뜻한 판을 고른다.
    # 이름을 그대로 친 경우가 가장 흔하므로 이름 일치를 먼저 본다(별칭 충돌 회피 · 보고 2번)
    said_menu = intake._menu_text(said) or said
    hit = menu_service.by_name(brand_id, menu_version, said_menu)         or menu_service.match(brand_id, menu_version, said_menu)
    hit = menu_service.with_temperature(brand_id, menu_version, hit, intake.temperature_hint(said))
    if hit.get("menu_id"):
        return hit["name"]
    raise NotApplicable(text)


def _apply_answer(cur, session, question, answer: str, extra_options: dict | None = None) -> None:
    """답이 사이즈인가 메뉴인가에 따라 다른 컬럼을 고친다."""
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    item_id = question["item_id"]

    if question["reason"] == "ambiguous_person":
        # 대리 주문 주문자 — 질문 버튼에 있던 이름만 받는다(후보 참여자 · "그대로 둘게요"의 원래 표현)
        allowed = {str(o.get("value")) for o in question.get("options") or []}
        if str(answer) not in allowed:
            raise NotApplicable(answer)
        cur.execute("UPDATE app.order_item SET person = %s, person_check = NULL, updated_at = NOW() "
                    " WHERE session_id = %s AND item_id = %s", (answer, session["session_id"], item_id))
        return

    if answer == questions.NO_ORDER:
        # [주문 아니에요, 뺄게요] — 메뉴를 알아듣지 못한 항목을 뺀다(보고 2번). 지우지 않고 removed 로 둔다
        cur.execute("UPDATE app.order_item SET removed = true, updated_at = NOW() "
                    " WHERE session_id = %s AND item_id = %s", (session["session_id"], item_id))
        questions.void_for_item(session["session_id"], item_id, cur=cur)
        return

    if question["reason"] == "ambiguous_target" and answer in (questions.TARGET_APPLY, questions.TARGET_KEEP):
        # [네, 바꿔 주세요] — 질문이 들고 있는 변경 문장을 이 항목에 반영한다(채팅 수정과 같은 함수).
        # [그대로 둘게요] — 고칠 것 없이 확인만 끝낸다(재검증이 대상 모호 사유를 지운다)
        change = next((o.get("change_text") for o in question.get("options") or []
                       if o.get("value") == questions.TARGET_APPLY), None)
        if answer == questions.TARGET_APPLY and change:
            cur.execute("SELECT * FROM app.order_item WHERE session_id = %s AND item_id = %s",
                        (session["session_id"], item_id))
            _apply_text_change(cur, session, cur.fetchone(), change)
        return

    cur.execute("SELECT options, ambiguous_options, custom_request FROM app.order_item "
                " WHERE session_id = %s AND item_id = %s", (session["session_id"], item_id))
    current = cur.fetchone()
    options = dict(current["options"] or {})
    ambiguous = dict(current["ambiguous_options"] or {})
    key = questions.spec_key(ambiguous) if question["reason"] == "ambiguous_spec" else None

    def save(**columns):
        # 답한 사양만 지운다 — 통째로 비우면 남은 모호 사양(수량 등)이 조용히 확정된다
        sets = ", ".join(f"{name} = %s" for name in columns)
        values = [json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
                  for v in columns.values()]
        cur.execute(f"UPDATE app.order_item SET {sets} WHERE session_id = %s AND item_id = %s",
                    (*values, session["session_id"], item_id))

    if key == "size":
        # 답은 사이즈 라벨("그란데")이다 → option_value 로 되돌린다
        value = _option_value(brand_id, menu_version, "size", answer)
        if value is None:
            raise NotApplicable(answer)
        ambiguous.pop("size")
        save(options={**options, "size": value}, ambiguous_options=ambiguous)
        return

    if key == "quantity":
        try:
            quantity = int(str(answer).replace("잔", "").strip())
        except ValueError:
            raise NotApplicable(answer)
        if quantity <= 0:
            raise NotApplicable(answer)
        ambiguous.pop("quantity")
        save(quantity=quantity, ambiguous_options=ambiguous)
        return

    # 메뉴를 고른 경우(온도 확인 포함) — **이름을 먼저** 본다(2026-11-05 2차 보고 2번).
    # 버튼 라벨은 메뉴 이름인데, 그 이름이 다른 메뉴의 별칭이기도 하면 match() 가 후보 2개로 보고 거부한다
    # ("카라멜 마키아또" → 카라멜 마키아또 · 아이스 카라멜 마키아또). 메뉴 이름이 온도를 품으므로 온도도 정해진다
    matched = menu_service.by_name(brand_id, menu_version, answer)         or menu_service.match(brand_id, menu_version, answer)
    if not matched.get("menu_id"):
        raise NotApplicable(answer)
    ambiguous.pop("temperature", None)
    # 채팅으로 답하며 함께 적은 옵션("아이스 아메리카노 그란데로") — 고른 메뉴에 있는 값만 받는다
    for group, value in (extra_options or {}).items():
        if menu_service.is_valid_option(brand_id, menu_version, group, value, menu_id=matched["menu_id"]):
            options[group] = value
            ambiguous.pop(group, None)
    # 고른 메뉴에 그 종류가 없는 옵션은 기타 요청으로 옮긴다 — 분석 경로(sessions._spec_checks)와 같은 규칙
    options, moved = menu_service.split_unoffered(brand_id, menu_version, options, matched["menu_id"])
    if moved:
        save(custom_request=intake.move_to_custom(current.get("custom_request"), moved))
    options = fit_size(brand_id, menu_version, options, matched["menu_id"])
    # 메뉴가 정해지면 기본 사이즈를 채운다 — 분석 경로(graph.nodes.match_one)와 같은 규칙이다.
    # 채우지 않으면 사이즈 칩 없이 금액만 나와, 매장에 보여 줄 주문서가 불완전해진다
    # (최유나 · 정하준 행에 사이즈가 비어 있었다 — 2026-11-03 확인)
    if "size" not in options and not ambiguous.get("size"):
        default_size = menu_service.default_option(brand_id, menu_version, "size",
                                                   menu_id=matched["menu_id"])
        if default_size:
            options = {**options, "size": default_size}
    save(menu_id=matched["menu_id"], menu_text=matched["name"], options=options,
         ambiguous_options=ambiguous)


def _follow_up(cur, session: dict, row: dict) -> None:
    """답한 뒤에도 모호한 사양이 남으면 다음 질문을 하나 더 만든다.

    질문은 항목당 하나씩 만들어 두므로, 사양 두 개(사이즈 + 수량)가 모호한 항목은
    첫 답 뒤 **물을 질문이 없어 확인 필요로 영원히 남는다** — 공유가 막힌다.
    """
    # 사양이 남았거나 주문자 확인이 남았으면 묻는다 — 주문자 확인은 사유 우선순위 맨 뒤라 앞 답 뒤에야 차례가 온다
    if row.get("removed"):
        return
    if row.get("validation_status") != "needs_review" or not (
            questions.spec_key(row.get("ambiguous_options")) or row.get("review_reason") == "ambiguous_person"):
        return
    # 되묻기 상한 (D-12 7절 · CLAUDE.md 협상 불가) — 항목당 질문 MAX_REASK 개. 넘으면 더 묻지 않고
    # 확인 필요로 둔 채 개별 바꾸기로 넘긴다. 없으면 답이 새 모호함을 낳을 때 질문이 끝없이 이어진다
    cur.execute("SELECT COUNT(*) AS n FROM app.review_question "
                " WHERE session_id = %s AND item_id = %s", (session["session_id"], row["item_id"]))
    if cur.fetchone()["n"] >= config.MAX_REASK:
        logging_setup.log("route_decision", session_id=session["session_id"], source="answer",
                          item_id=row["item_id"], reask_limit=config.MAX_REASK)
        return
    cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM app.review_question "
                " WHERE session_id = %s", (session["session_id"],))
    seq = cur.fetchone()["seq"]
    built = questions.build({**row, "candidates": []}, session_id=session["session_id"],
                            brand_id=session["brand_id"], menu_version=session["menu_version"],
                            seq=seq)
    questions.insert(session["session_id"], [built], cur=cur)


def fit_size(brand_id: str, menu_version: str, options: dict, menu_id: str) -> dict:
    """메뉴를 바꾼 뒤 지금 사이즈가 새 메뉴에 없으면 새 메뉴의 기본 사이즈로 맞춘다 (2026-11-04 전체 흐름 확인).

    아이스 아메리카노(톨)를 망고 바나나 블렌디드(그란데만)로 바꾸면 "톨"이 남아 옵션 오류("옵션을 다시 골라 주세요")가
    됐다. 사이즈 종류가 아예 없는 메뉴(에스프레소)는 사이즈를 뺀다. 사이즈를 정하지 않은 행은 건드리지 않는다.
    """
    size = (options or {}).get("size")
    if not size or menu_service.is_valid_option(brand_id, menu_version, "size", size, menu_id=menu_id):
        return options
    out = {k: v for k, v in options.items() if k != "size"}
    default = menu_service.default_option(brand_id, menu_version, "size", menu_id=menu_id)
    return {**out, "size": default} if default else out


def _option_value(brand_id, menu_version, group, label) -> str | None:
    catalog = menu_service.catalog(brand_id, menu_version)
    for opt in catalog["options"]:
        if opt["option_group"] == group and (opt["value_label"] == label
                                             or opt["option_value"] == label):
            return opt["option_value"]
    return None


def _applied_text(question: dict, answer: str, person: str) -> str:
    """"전 → 후" 안내 — 서버가 완성형으로 준다.

    **버튼의 표시 이름(label)을 쓴다.** 값(value)을 쓰면 "tall로 했어요"처럼 내부 값이 담당자 화면에
    뜬다(F-013 — D-05 확인 2026-11-03). 질문 행에 버튼 목록이 저장돼 있으므로 거기서 찾는다.
    """
    if question.get("reason") == "ambiguous_person":
        return f"{answer} 님 주문으로 정했어요."
    if answer == questions.TARGET_APPLY:
        return f"{person} 님 주문에 반영했어요."
    if answer == questions.TARGET_KEEP:
        return f"{person} 님 주문을 그대로 뒀어요."
    if answer == questions.NO_ORDER:
        return f"{person} 님 주문을 뺐어요."
    label = next((option.get("label") for option in (question.get("options") or [])
                  if str(option.get("value")) == str(answer)), None) or answer
    return f"{person} 님 주문을 {label}{_ro(label)} 했어요."


def _ro(word: str) -> str:
    """조사 '로/으로' — 받침이 없거나 ㄹ 받침이면 '로'(톨로 · 그란데로), 그 밖은 '으로'(3잔으로)."""
    last = (word or "").strip().rstrip(")]」'\" ")[-1:]      # "아이스 카페 라떼 (톨)" 은 톨을 본다
    if not last or not ("가" <= last <= "힣"):
        return "로"
    jong = (ord(last) - 0xAC00) % 28
    return "로" if jong in (0, 8) else "으로"


# --- API-023 행 편집 ----------------------------------------------------------
ALLOWED_CHANGES = {"options", "custom_request", "menu_id", "menu_name", "quantity", "removed"}


def patch_item(session: dict, item_id: str, payload: dict, *,
               expected_version: int | None) -> dict:
    """사양 · 옵션 · 기타 요청 · 메뉴 바꾸기 · 빼기.

    옵션은 **화이트리스트에 있는 키만** 받는다. 없는 키는 거른다 (API-023).
    """
    session_id = session["session_id"]
    brand_id, menu_version = session["brand_id"], session["menu_version"]

    unknown = set(payload) - ALLOWED_CHANGES
    if unknown:
        raise NotApplicable(f"unknown fields: {sorted(unknown)}")

    options = payload.get("options")
    if options:
        # 바꿀 메뉴가 있으면 그 메뉴, 없으면 지금 메뉴 기준으로 본다 — 메뉴 이름으로 바꾸는 경우는
        # 반영 뒤 재검증(_revalidate → validate_item)이 같은 규칙으로 다시 거른다
        menu_id = payload.get("menu_id") or (db.fetch_one(
            "SELECT menu_id FROM app.order_item WHERE session_id = %s AND item_id = %s",
            (session["session_id"], item_id)) or {}).get("menu_id")
        bad = [f"{g}={v}" for g, v in options.items()
               if not menu_service.is_valid_option(brand_id, menu_version, g, v, menu_id=menu_id)]
        if bad:
            raise NotApplicable(f"unknown options: {bad}")

    with db.transaction() as cur:
        cur.execute(
            "SELECT i.*, m.name AS menu_name FROM app.order_item i "
            "  LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
            " WHERE i.session_id = %s AND i.item_id = %s", (session_id, item_id))
        before = cur.fetchone()
        if not before:
            raise NotApplicable(item_id)
        before_text = _row_text(before, session)

        sets, params = [], []
        # 고친 사양의 모호 표시만 지운다 (사이즈를 골랐다고 수량 확인까지 사라지면 안 된다)
        ambiguous = dict(before["ambiguous_options"] or {})
        resolved = set(options or {}) | ({"quantity"} if "quantity" in payload else set())             | ({"temperature"} if payload.get("menu_name") or payload.get("menu_id") else set())
        new_menu = None
        if payload.get("menu_name"):
            new_menu = menu_service.match(brand_id, menu_version, payload["menu_name"])
            if not new_menu.get("menu_id"):
                raise NotApplicable(payload["menu_name"])
        elif payload.get("menu_id"):
            new_menu = {"menu_id": payload["menu_id"]}

        merged = {**(before["options"] or {}), **options} if options is not None else None
        custom_changed = "custom_request" in payload
        custom = payload.get("custom_request") if custom_changed else before.get("custom_request")
        if new_menu:
            # 바꾼 메뉴에 그 종류가 없는 옵션은 기타 요청으로 옮긴다 — 분석 경로와 같은 규칙(sessions._spec_checks)
            kept, moved = menu_service.split_unoffered(
                brand_id, menu_version, merged if merged is not None else (before["options"] or {}),
                new_menu["menu_id"])
            if moved:
                merged, custom, custom_changed = kept, intake.move_to_custom(custom, moved), True
            fitted = fit_size(brand_id, menu_version, kept, new_menu["menu_id"])
            if fitted != kept:
                merged = fitted

        if merged is not None:
            sets.append("options = %s")
            params.append(json.dumps(merged, ensure_ascii=False))
        if resolved & set(ambiguous):
            sets.append("ambiguous_options = %s")
            params.append(json.dumps({k: v for k, v in ambiguous.items() if k not in resolved},
                                     ensure_ascii=False))
        if custom_changed:
            sets.append("custom_request = %s")
            params.append(custom or None)
        if "quantity" in payload:
            sets.append("quantity = %s")
            params.append(max(1, int(payload["quantity"])))
        if payload.get("menu_name"):
            sets += ["menu_id = %s", "menu_text = %s"]
            params += [new_menu["menu_id"], new_menu["name"]]
        elif payload.get("menu_id"):
            sets += ["menu_id = %s"]
            params += [payload["menu_id"]]
        if payload.get("removed"):
            sets.append("removed = true")

        if sets:
            cur.execute(
                f"UPDATE app.order_item SET {', '.join(sets)}, updated_at = NOW() "
                f" WHERE session_id = %s AND item_id = %s", (*params, session_id, item_id))

        if payload.get("removed"):
            questions.void_for_item(session_id, item_id, cur=cur)
            after = {**before, "removed": True}
            after_text = "빼기"
        else:
            after = _revalidate(cur, session, item_id)
            after_text = _row_text(after, session)

        token = changes.record(cur, session_id, method="question_answer", item_id=item_id,
                               who=before["person"], before_text=before_text,
                               after_text=after_text, before_json=_row_json(before),
                               after_json=_row_json(after))
        version = _commit(cur, session, expected_version=expected_version)

    _guard(session)
    return {"user_message": f'{before["person"]} 님 주문을 고쳤어요.', "undo_token": token,
            "changed_item_ids": [item_id], "version": version}


# --- API-022 채팅 입력 해석 ---------------------------------------------------
def interpret_message(session: dict, text: str, *, expected_version: int | None) -> dict:
    """해석은 서버가 한다 — 화면은 받은 안내만 그린다 (P-06).

    3분기: 매장 기준 바꾸기 되묻기 · 바로 반영 · 바꿀 것 없음.
    매장 기준 바꾸기는 그래프 앞의 세션 설정이다(P-03 4절 — 경로가 아니다).
    """

    # 1 매장 기준 바꾸기 — 결과를 새로 만들므로 이때만 한 번 묻는다 (U-5)
    target_brand = brand_service.detect_change_request(text)
    if target_brand and target_brand != session["brand_id"]:
        label = brand_service.BRAND_LABEL[target_brand]
        return {"kind": "brand_change_ask", "brand_id": target_brand,
                "text": f"{label}으로 새로 정리할까요?",
                "options": [{"value": "apply", "label": "새로 정리"},
                            {"value": "cancel", "label": "아니요"}]}

    # 2 · 3 — 해석 · 대상 연결 · 반영은 그래프가 한다(D-12 source=chat). 출력 가드도 거기서 지난다
    from app import graph

    return graph.run("chat", session, text=text, expected_version=expected_version)["result"]


def apply_chat_event(session: dict, text: str, event_type: str, target: dict | None, *,
                     expected_version: int | None) -> dict:
    """D-12 N-09 (source=chat) — 담당자 입력은 되묻지 않고 바로 반영한다(되돌리기 제공)."""
    session_id = session["session_id"]
    if event_type == "ignore":
        return {"kind": "no_change", "text": "주문에서 바꿀 것을 찾지 못했어요",
                "undo_token": None}
    if event_type == "add":
        return add_from_chat(session, text, expected_version=expected_version)
    if target is None:
        # 사람은 하나로 정해졌고 **어느 잔인지만** 모르는 경우를 구분해 말한다 (4차 보고 1번).
        # 예전에는 둘 다 "어느 주문을 말하는지 찾지 못했어요"였다 — 이름을 똑바로 썼는데도 같은 답이
        # 돌아와, 담당자에게는 채팅으로 고치는 길이 막힌 것으로 보였다
        cups = _cups_of_named_person(session_id, text)
        if cups:
            # 문구는 **그 자체로 쓸 수 있게** 잔을 적어 넣는다 — 화면은 받은 말풍선만 그린다(P-06).
            # 목록을 options 로만 주면 담당자는 "2건이에요"만 보고 무엇이 둘인지 모른다
            spec = [_row_text(row, session) for row in cups]
            listed = " · ".join(f"{n}) {label}" for n, label in enumerate(spec, start=1))
            return {"kind": "ask_which", "undo_token": None,
                    "text": f'{cups[0]["person"]} 님 주문이 {len(cups)}건이에요 — {listed}. '
                            f'어느 것인지 알려 주세요. 주문서 화면에서 한 잔씩 고칠 수도 있어요',
                    "options": [{"value": row["item_id"], "label": label}
                                for row, label in zip(cups, spec)]}
        return {"kind": "no_change", "text": "어느 주문을 말하는지 찾지 못했어요",
                "undo_token": None}

    with db.transaction() as cur:
        cur.execute(
            "SELECT i.*, m.name AS menu_name FROM app.order_item i "
            "  LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
            " WHERE i.session_id = %s AND i.item_id = %s", (session_id, target["item_id"]))
        before = cur.fetchone()
        before_text = _row_text(before, session)

        if event_type == "cancel":
            cur.execute("UPDATE app.order_item SET removed = true, updated_at = NOW() "
                        " WHERE session_id = %s AND item_id = %s",
                        (session_id, target["item_id"]))
            questions.void_for_item(session_id, target["item_id"], cur=cur)
            after_text = "취소"
            message = f'{before["person"]} 님 주문을 취소했어요.'
            after = {**before, "removed": True}
        else:
            changed = _apply_text_change(cur, session, before, text)
            if not changed:
                raise NotApplicable(text)
            after = _revalidate(cur, session, target["item_id"])
            after_text = _row_text(after, session)
            message = (f'{before["person"]} 님 주문을 {before_text}에서 '
                       f'{after_text}{_ro(after_text)} 바꿨어요.')

        token = changes.record(cur, session_id, method="chat_edit", item_id=target["item_id"],
                               who=before["person"], before_text=before_text,
                               after_text=after_text, before_json=_row_json(before),
                               after_json=_row_json(after))
        version = _commit(cur, session, expected_version=expected_version)

    return {"kind": "applied", "text": message, "undo_token": token,
            "changed_item_ids": [target["item_id"]], "version": version}


# --- 채팅으로 주문 추가 (API-022 · 4차 보고 1번) --------------------------------
# 예전에는 채팅으로 주문을 **만드는 경로가 없었다**(N-06 `_chat_route` 주석). 담당자가
# "박서연 아이스 아메리카노 한 잔 추가해줘"를 치면 어느 행도 그 사람의 것이 아니므로
# "어느 주문을 말하는지 찾지 못했어요"로 끝났다 — 단톡방에서 늦게 온 주문을 넣을 길이 없었다.
#
# 이름은 **규칙으로만** 읽는다(LLM 호출 0건). 읽지 못하면 임의로 정하지 않고 무엇이 더
# 필요한지 말한다 — 이름을 잘못 읽으면 엉뚱한 사람 주문이 조용히 생긴다.
_NAME_PARTICLE = re.compile(r"(?:님께서|님에게|님한테|님은|님이|님도|님|씨는|씨가|씨도|씨|"
                            r"에게|한테|께서|께|이는|이가|이도|은|는|이|가|도)$")
_HANGUL_NAME = re.compile(r"^[가-힣]{2,5}$")
# "… 추가해줘" 의 지시 꼬리. 메뉴 매칭에 들어가면 **메뉴 이름이 아니게 되어 미등록으로 떨어진다**
# ("아이스 아메리카노 톨 한 잔 추가해줘" → 매칭 실패 실측 2026-11-05). 수량 · 사양은 남긴다
_ADD_TAIL = re.compile(r"\s*(?:좀\s*)?(?:더\s*)?(?:추가|넣어|시켜|주문)\s*(?:해|하)?\s*"
                       r"(?:줘요|줘|주세요|주시고|주라|줄래|할게|해|요)?\s*[.!?~]*\s*$")
# "넣어 달라"고 적었는가 — 채팅 추가의 유일한 근거다. 여기 하나에 둔다(N-06 `_chat_route` 도 쓴다):
# `intake.classify` 는 **분류되지 않는 말을 add 로** 돌려주므로(잡담 포함) 이 울타리가 없으면
# 잡담이 주문 추가 시도가 되고, 담당자는 "메뉴 이름을 적어 주세요"라는 엉뚱한 답을 받는다
ADD_INSTRUCTION = re.compile(r"추가|넣어|시켜|더\s*(?:한\s*잔|하나|주문)")


def _known_people(session_id: str) -> list[str]:
    return [r["person"] for r in db.fetch_all(
        "SELECT DISTINCT person FROM app.order_item "
        " WHERE session_id = %s AND removed = false AND person IS NOT NULL", (session_id,))]


def _leading_name(session_id: str, text: str) -> str | None:
    """문장 맨 앞 낱말을 이름으로 읽는다 — 메뉴 · 사양으로 읽히는 말이면 이름이 아니다.

    거르는 것 셋. 하나라도 빠지면 **엉뚱한 사람 주문이 조용히 생긴다.**
      ① 메뉴 이름 · 별칭      "라떼 추가해줘" 의 "라떼"
      ② 사양 · 온도 낱말      "아이스 아메리카노 …" 의 "아이스", "톨 …" 의 "톨"
      ③ 뒤에 메뉴가 없는 말   이름이라고 볼 근거가 없다
    """
    head = (text or "").strip().split()
    if not head:
        return None
    word = _NAME_PARTICLE.sub("", head[0].strip()) or head[0].strip()
    if not _HANGUL_NAME.match(word):
        return None
    brand_id, menu_version = _brand_of(session_id)
    if not brand_id:
        return None
    if menu_service.match(brand_id, menu_version, word).get("menu_id"):
        return None                                                      # ①
    if intake._options(word) or intake.temperature_hint(word):
        return None                                                      # ②
    if not menu_service.mentions(brand_id, menu_version, " ".join(head[1:])):
        return None                                                      # ③
    return word


def _brand_of(session_id: str) -> tuple[str | None, str | None]:
    row = db.fetch_one("SELECT brand_id, menu_version FROM app.order_session WHERE session_id = %s",
                       (session_id,))
    return (row["brand_id"], row["menu_version"]) if row else (None, None)


def _named_stranger(session_id: str, text: str) -> str | None:
    """세션에 없는 사람 이름이 문장 맨 앞에 적혀 있는가 — 그러면 수정이 아니라 추가다."""
    if any(p and p in text for p in _known_people(session_id)):
        return None
    return _leading_name(session_id, text)


def chat_person(session: dict, text: str) -> str | None:
    """이 문장이 누구 주문인가 — 세션에 있는 이름이 먼저, 없으면 맨 앞 낱말."""
    session_id = session["session_id"]
    named = [p for p in _known_people(session_id) if p and p in text]
    if len(named) == 1:
        return named[0]
    if named:
        return None                      # 두 사람 이름이 함께 있으면 누구 것인지 모른다
    return _leading_name(session_id, text)


def _without_person(text: str, person: str) -> str:
    """이름(과 붙은 조사)을 뺀 나머지 — 메뉴 매칭이 이름을 메뉴 글자로 읽지 않게."""
    words = []
    dropped = False
    for word in (text or "").split():
        if not dropped and _NAME_PARTICLE.sub("", word) == person:
            dropped = True
            continue
        words.append(word)
    return " ".join(words) if dropped else (text or "")


def add_from_chat(session: dict, text: str, *, expected_version: int | None) -> dict:
    """채팅 한 줄로 주문을 새로 만든다. 판정 · 금액 · 질문은 분석 경로와 **같은 함수**를 지난다."""
    from app.graph import nodes

    session_id = session["session_id"]
    # 메뉴를 먼저 본다 — 메뉴가 아예 없으면 이름을 못 읽은 것이 아니라 메뉴를 안 적은 것이다.
    # 순서를 뒤집으면 "박서연 추가해줘"에 "누구 주문인지 알려 주세요"가 돌아가 담당자가 이름을 의심한다
    if not menu_service.mentions(session["brand_id"], session["menu_version"], text):
        if not ADD_INSTRUCTION.search(text or ""):
            # 넣어 달라고 적지도 않았고 메뉴도 없다 — 주문에 관한 말이 아니다.
            # `intake.classify` 가 잡담도 add 로 돌려주므로 여기까지 온다("오늘 날씨 좋네요" 실측)
            return {"kind": "no_change", "undo_token": None,
                    "text": "주문에서 바꿀 것을 찾지 못했어요"}
        return {"kind": "no_change", "undo_token": None,
                "text": "메뉴를 찾지 못했어요. 메뉴 이름을 적어 주세요"}

    person = chat_person(session, text)
    if not person:
        return {"kind": "no_change", "undo_token": None,
                "text": "누구 주문인지 알려 주세요. 예: '박서연 아이스 아메리카노 한 잔 추가'"}

    # 이름과 지시 꼬리를 떼고 **주문 문장만** 남긴다 — 분석 경로가 받는 모양과 같아진다
    rest = _ADD_TAIL.sub("", _without_person(text, person)).strip()
    rest = re.sub(r"\s*더\s*$", "", rest).strip() or _without_person(text, person)
    event = {"message_id": "chat", "_raw_text": rest, "person": person}
    drafts = []
    for item in intake.build_items(rest, person):
        row = nodes.match_one(item, event, [], 0, session)
        if row.get("menu_id") or row.get("candidates"):
            drafts.append(row)
    if not drafts:
        return {"kind": "no_change", "undo_token": None,
                "text": "메뉴를 찾지 못했어요. 메뉴 이름을 적어 주세요"}

    with db.transaction() as cur:
        made, texts, last = [], [], {}
        for row in drafts:
            item_id = _next_item_id(cur, session_id)
            cur.execute(
                "INSERT INTO app.order_item "
                "  (item_id, session_id, person, menu_id, menu_text, options, ambiguous_options, "
                "   custom_request, quantity, validation_status, rule_verdict, review_reason) "
                # 넣는 순간의 값은 자리만 채운다 — 바로 뒤 `_revalidate` 가 같은 규칙으로 다시 적는다
                # (rule_verdict 는 passed · failed · undecided 3분기다 — D-07 4.5 CHECK)
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'needs_review', 'undecided', %s)",
                (item_id, session_id, person, row.get("menu_id"),
                 row.get("menu_name") or row.get("match_text") or rest,
                 json.dumps(row.get("options") or {}, ensure_ascii=False),
                 json.dumps(row.get("ambiguous_options") or {}, ensure_ascii=False),
                 row.get("custom_request"), row.get("quantity") or 1,
                 None if row.get("menu_id") else "unregistered_menu"))
            # 판정 · 금액은 수정 경로와 같은 함수가 한다 — 규칙을 복사하지 않는다
            last = _revalidate(cur, session, item_id)
            made.append(item_id)
            texts.append(_row_text(last, session))

        token = changes.record(cur, session_id, method="chat_add", item_id=made[0], who=person,
                               before_text="없음", after_text=" · ".join(texts),
                               # 되돌리기는 **빼기**다 — 추가 전 상태가 "그 주문이 없음"이다
                               before_json={**_row_json(last), "removed": True},
                               after_json=_row_json(last))
        version = _commit(cur, session, expected_version=expected_version)

    added = " · ".join(texts)
    return {"kind": "applied", "undo_token": token, "changed_item_ids": made, "version": version,
            "text": f'{person} 님 주문으로 {added}{_ro(added)} 넣었어요.'}


def _next_item_id(cur, session_id: str) -> str:
    """기존 번호와 겹치지 않는 다음 항목 번호 — 항목을 지우지 않으므로 최대 번호 + 1 이다."""
    cur.execute("SELECT item_id FROM app.order_item WHERE session_id = %s", (session_id,))
    used = {r["item_id"] for r in cur.fetchall()}
    seq = len(used) + 1
    while ids.item_id(session_id, seq) in used:
        seq += 1
    return ids.item_id(session_id, seq)


def _live_rows(session_id: str) -> list[dict]:
    return db.fetch_all(
        "SELECT i.item_id, i.person, i.menu_text, i.options, i.quantity, m.name AS menu_name "
        "  FROM app.order_item i LEFT JOIN app.menu m ON m.menu_id = i.menu_id "
        " WHERE i.session_id = %s AND i.removed = false ORDER BY i.item_id",
        (session_id,))


def _find_target(session_id: str, text: str) -> dict | None:
    """문장에서 사람 이름 · 메뉴 이름 · 옵션으로 대상을 찾는다.

    못 찾으면 **임의로 고르지 않는다** — 담당자에게 되묻거나 "찾지 못했어요"를 돌려준다.
    아무 행이나 고치면 담당자가 모르는 채로 틀린 주문을 공유한다.
    """
    rows = _live_rows(session_id)
    by_person = [r for r in rows if r["person"] and r["person"] in text]
    if len(by_person) == 1:
        return by_person[0]
    if by_person:
        # 사람은 하나인데 잔이 여럿이다 — 문장에 적힌 메뉴 · 옵션으로 좁힌다.
        # 좁혀서 **정확히 하나**가 남을 때만 쓴다(N-06 `_narrow_by_options` 와 같은 규칙)
        narrowed = _narrow(by_person, text)
        return narrowed[0] if len(narrowed) == 1 else None
    if _named_stranger(session_id, text):
        # 세션에 없는 사람 이름이 적혀 있다 — 그 사람의 주문은 없으므로 **남의 행을 고르지 않는다**.
        # 예전에는 메뉴 이름이 겹치는 남의 행이나 "행이 하나뿐"이라는 이유로 아무 행을 골라,
        # "박서연 … 추가해줘"가 김민수 주문을 건드리고 422 로 끝났다 (4차 보고 1번)
        return None
    narrowed = _narrow(rows, text)
    if len(narrowed) == 1:
        return narrowed[0]
    if len(rows) == 1:
        return rows[0]
    return None


def _narrow(rows: list[dict], text: str) -> list[dict]:
    """메뉴 이름으로, 그래도 여럿이면 문장에 적힌 옵션으로 좁힌다."""
    by_menu = [r for r in rows
               if (r["menu_name"] and r["menu_name"] in text)
               or (r["menu_text"] and r["menu_text"] in text)]
    pool = by_menu if len(by_menu) == 1 else rows
    said = intake._options(text)
    if len(pool) < 2 or not said:
        return pool
    hit = [r for r in pool
           if all((r["options"] or {}).get(group) == value for group, value in said.items())]
    return hit if len(hit) == 1 else pool


def _cups_of_named_person(session_id: str, text: str) -> list[dict]:
    """문장이 가리킨 사람의 잔이 둘 이상일 때 그 목록. 아니면 빈 목록."""
    rows = [r for r in _live_rows(session_id) if r["person"] and r["person"] in text]
    return rows if len(rows) > 1 else []


def _apply_text_change(cur, session, before: dict, text: str) -> bool:
    """문장에서 읽은 옵션 · 메뉴를 반영한다. 반영한 것이 없으면 False."""
    brand_id, menu_version = session["brand_id"], session["menu_version"]
    options = intake._options(text)
    applied = False

    if options:
        merged = {**(before["options"] or {}), **options}
        ambiguous = {k: v for k, v in (before["ambiguous_options"] or {}).items()
                     if k not in options}
        cur.execute("UPDATE app.order_item SET options = %s, ambiguous_options = %s, "
                    "       updated_at = NOW() WHERE item_id = %s",
                    (json.dumps(merged, ensure_ascii=False),
                     json.dumps(ambiguous, ensure_ascii=False), before["item_id"]))
        applied = True

    # "아이스 라떼로 바꿔줘" — 바꿀 메뉴 이름이 문장에 있는가
    stripped = intake._menu_text(text)
    if stripped:
        matched = menu_service.match(brand_id, menu_version, stripped)
        if matched.get("menu_id") and matched["menu_id"] != before["menu_id"]:
            cur.execute("UPDATE app.order_item SET menu_id = %s, menu_text = %s, "
                        "       updated_at = NOW() WHERE item_id = %s",
                        (matched["menu_id"], matched["name"], before["item_id"]))
            applied = True
    return applied


# --- API-024 되돌리기 ---------------------------------------------------------
def undo(session: dict, undo_token: str) -> dict:
    session_id = session["session_id"]
    row = changes.find_undoable(session_id, undo_token)
    if not row:
        raise NotApplicable("UNDO_UNAVAILABLE")

    with db.transaction() as cur:
        before = row["before_json"] or {}
        if row["item_id"] and before:
            cur.execute(
                "UPDATE app.order_item "
                "   SET menu_id = %s, menu_text = %s, options = %s, "
                "       ambiguous_options = %s, custom_request = %s, quantity = %s, "
                "       removed = %s, person = COALESCE(%s, person), "
                "       person_check = CASE WHEN %s THEN %s::jsonb ELSE person_check END, updated_at = NOW() "
                " WHERE session_id = %s AND item_id = %s",
                (before.get("menu_id"), before.get("menu_text"),
                 json.dumps(before.get("options") or {}, ensure_ascii=False),
                 json.dumps(before.get("ambiguous_options") or {}, ensure_ascii=False),
                 before.get("custom_request"), before.get("quantity") or 1,
                 bool(before.get("removed")), before.get("person"),
                 # 예전 기록(person_check 키 없음)은 그 칸을 건드리지 않는다
                 "person_check" in before,
                 json.dumps(before["person_check"], ensure_ascii=False) if before.get("person_check") else None,
                 session_id, row["item_id"]))
            _revalidate(cur, session, row["item_id"])
        if row["method"] == "question_answer" and row["item_id"]:
            # 답을 되돌리면 그 질문도 다시 묻는 상태로 돌아간다 (U-5) — 항목만 되돌리면
            # 확인 필요로 남은 항목을 물을 질문이 없어 공유까지 막힌다
            questions.reopen_last_answered(cur, session_id, row["item_id"])
        changes.mark_undone(cur, row["change_id"])
        version = _commit(cur, session, expected_version=None)

    _guard(session)
    return {"user_message": "되돌렸어요", "changed_item_ids": [row["item_id"]],
            "version": version}


# --- 공통 표시 문구 ------------------------------------------------------------
def _row_text(row: dict, session: dict | None = None) -> str:
    """O-6 · 안내 문구에 쓰는 짧은 조각. 원문 전문을 넣지 않는다 (D-07 4.7).

    옵션은 화면 라벨로 쓴다(venti → 벤티) — 내부 값이 담당자 화면에 나가지 않게(P-06).
    """
    if not row:
        return "-"
    name = row.get("menu_name") or row.get("menu_text") or "주문"
    options = row.get("options") or {}
    if session:
        spec = " ".join(menu_service.option_label(session["brand_id"], session["menu_version"], g, v)
                        for g, v in options.items())
    else:
        spec = " ".join(str(v) for v in options.values())
    quantity = int(row.get("quantity") or 1)
    tail = f" {quantity}잔" if quantity > 1 else ""
    return f"{name} {spec}".strip() + tail


def _row_json(row: dict) -> dict:
    """되돌리기가 복원하는 실제 값."""
    if not row:
        return {}
    return {k: row.get(k) for k in ("menu_id", "menu_text", "options", "ambiguous_options",
                                    "custom_request", "quantity", "removed", "person", "person_check")}
