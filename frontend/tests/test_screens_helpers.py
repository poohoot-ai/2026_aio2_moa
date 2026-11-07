# 화면 보조 함수 테스트 — Streamlit 런타임이 필요 없는 순수 함수만 다룬다
#
# 금액 포맷(components.amount_text)과 서버 대역(mock)이 대상이다.
# 화면 렌더 자체는 Q-01 테스트시나리오 · Q-03 하네스의 몫이다.
from moa import components as c, mock, state


# --- 금액 표기 (P-06 U-4 · 정본 시안 실측) -----------------------------------
def test_row_amount_has_no_unit():
    """행 · 묶음 · 합계는 `예상 4,500`이다 — 시안에 "원"이 없다."""
    assert c.amount_text(4500) == "예상 4,500"


def test_panel_amount_has_unit():
    """주문서 패널 머리와 채팅 요약은 `예상 25,800원`이다."""
    assert c.amount_text(25800, unit=True) == "예상 25,800원"


def test_amount_none_is_undecided_not_zero():
    """`None`은 0원이 아니라 미정이다 (D-02 API-015) — 0원으로 그리면 정산 검증이 깨진다."""
    assert c.amount_text(None) == ""
    assert c.amount_text(0) == "예상 0"


# --- 미리보기 파싱 (API-011 대역 · LLM 호출 0건) -----------------------------
def test_preview_parses_name_bracket_format():
    result = mock.preview_from("[김민수] 아아 톨 2잔\n[이지연] 바닐라라떼 큰 거")
    assert [m["sender"] for m in result["messages"]] == ["김민수", "이지연"]
    assert result["summary_text"] == "2명 · 메시지 2개를 읽었어요"
    assert result["unreadable_lines"] == []
    assert result["can_proceed"] is True


def test_preview_keeps_going_with_unreadable_lines():
    """읽지 못한 줄이 있어도 나머지로 계속 진행할 수 있다 (P-06 SCR-001)."""
    result = mock.preview_from("[김민수] 아아 톨\n아까 말한 거 그대로요")
    assert len(result["messages"]) == 1
    assert result["unreadable_lines"][0]["notice_text"] == "2번째 줄은 읽지 못했어요"
    assert result["can_proceed"] is True


def test_preview_empty_cannot_proceed():
    assert mock.preview_from("")["can_proceed"] is False


def test_sample_conversation_matches_design_example():
    """예시 대화는 정본 시안 케이스 02와 같다 — 5명 · 메시지 8개."""
    assert mock.SAMPLE_CONVERSATION["summary_text"] == "5명 · 메시지 8개를 읽었어요"
    assert len(mock.SAMPLE_CONVERSATION["messages"]) == 8
    assert mock.SAMPLE_CONVERSATION["is_sample"] is True


# --- 파일 판정 대역 (실제로는 서버가 내용으로 판정한다) ----------------------
def test_image_is_refused_with_its_own_notice():
    _, code = mock.judge_file("photo.png", b"\x89PNG\r\n")
    assert code == "IMAGE_NOT_SUPPORTED"
    assert "사진은 읽지 못해요" in mock.FILE_ERRORS[code]


def test_binary_file_is_refused():
    _, code = mock.judge_file("chat.bin", b"\xff\xfe\x00\x01")
    assert code == "FILE_NOT_SUPPORTED"


def test_txt_file_is_read_as_text():
    text, code = mock.judge_file("chat.txt", "[김민수] 아아 톨".encode("utf-8"))
    assert code is None and text.startswith("[김민수]")


# --- 채팅 입력 해석 대역 (케이스 14 · 16 · 무관한 말) -----------------------
def test_brand_change_asks_once():
    """묻는 것은 매장 기준 바꾸기 하나뿐이다 (U-5)."""
    result = mock.interpret("일반 카페로 해줘")
    assert result["kind"] == "brand_change_ask"
    assert result["brand_id"] == "generic"
    assert [o["label"] for o in result["options"]] == ["새로 정리", "아니요"]


def test_order_change_applies_without_asking():
    result = mock.interpret("서연이는 아이스 라떼로 바꿔줘")
    assert result["kind"] == "applied"
    assert result["undo_token"]          # 바로 반영 + 되돌리기


def test_unrelated_message_changes_nothing():
    result = mock.interpret("고마워")
    assert result["kind"] == "no_change"
    assert result["undo_token"] is None


def test_answer_result_shows_before_after():
    result = mock.answer_result(mock.QUESTIONS[0], "벤티")
    assert result["text"] == "이지연 님 바닐라라떼를 벤티로 했어요."
    assert result["changed_item_ids"] == ["it_02"]


# --- 개인별 금액을 내보내지 않는다 (PRD v1.5 FR-010 · D-02 2.4) -------------
def test_order_sheet_has_no_per_person_amount():
    for group in mock.order_sheet()["groups"]:
        assert "per_person" not in group
        assert "per_person_amount" not in group
    assert mock.order_sheet()["totals"]["integrity_ok"] is True


# --- 담당자 화면에 내부 용어가 없다 (U-7) -----------------------------------
INTERNAL_TERMS = ["NEEDS_REVIEW", "brand_id", "Snapshot", "snapshot", "intake", "menu_agent",
                  "DEP_TIMEOUT", "WORKER_DOWN", "session_token"]


def test_manager_facing_texts_have_no_internal_terms():
    """담당자가 읽는 문구에 내부 용어가 섞이지 않는지 본다.

    장애 코드(`DEP_TIMEOUT` 등)는 **운영 대시보드 응답에만** 있어야 한다(D-02 3.1).
    """
    texts = []
    texts.append(mock.SESSION["ai_notice"])
    texts += [row.get("review_reason_text") or "" for row in mock.SESSION["rows"]]
    texts += [chat["text"] for chat in mock.SESSION["chat"]]
    texts += [p["status_line"]["text"] for p in mock.PROGRESS.values()]
    texts += [step["label"] for step in mock.PROGRESS["running"]["steps"]]
    texts.append(mock.order_sheet()["status_line"]["text"])
    texts.append(mock.share_text()["content"])
    joined = "\n".join(texts)
    assert not [term for term in INTERNAL_TERMS if term in joined]


def test_fault_codes_live_only_in_ops_responses():
    codes = [i["fault_code"] for i in mock.OPS_INCIDENTS["incidents"]]
    assert "DB_UNAVAILABLE" in codes    # 운영자는 본다
    assert "DB_UNAVAILABLE" not in mock.share_text()["content"]


# --- 정본 시안 대조 — 눈으로 보지 않아도 어긋나면 걸리게 ---------------------
def test_menu_list_matches_design_sheet():
    """메뉴 찾기 목록은 정본 시안 케이스 15와 같은 메뉴 · 가격이다."""
    names = [m["name"] for m in mock.MENUS]
    assert names[:4] == ["민트 초콜릿 칩 블렌디드", "자바 칩 프라푸치노",
                         "아이스 카페 라떼", "아이스 아메리카노"]
    assert {m["name"]: m["estimated_price"] for m in mock.MENUS}["아이스 아메리카노"] == 4500
    assert mock.MENU_SCOPE_NOTICE == "스타벅스 메뉴 기준 메뉴만 보여줘요"


def test_row_edit_has_three_option_groups():
    """행 편집은 사이즈 · 얼음 · 샷 세 묶음이다 (케이스 12 · 19)."""
    row = next(r for r in mock.SESSION["rows"] if r["item_id"] == "it_03")
    assert [o["label"] for o in row["option_choices"]] == ["사이즈", "얼음", "샷"]
    for group in mock.order_sheet()["groups"]:
        assert [o["label"] for o in group["option_choices"]] == ["사이즈", "얼음", "샷"]


def test_brand_change_states_what_is_kept():
    """매장 기준을 바꿀 때 무엇이 새로 정리되고 무엇이 남는지 밝힌다 (케이스 16)."""
    result = mock.interpret("일반 카페로 해줘")
    assert result["guide_text"] == "지금 정리한 결과와 답한 내용은 새로 정리돼요. 넣은 대화는 그대로예요"


def test_chat_edit_notice_names_the_size():
    """전 → 후 안내에 사이즈까지 담는다 (케이스 14)."""
    assert mock.interpret("서연이는 라떼로 바꿔줘")["text"] == (
        "박서연 님 주문을 아이스 아메리카노 (톨)에서 아이스 카페 라떼 (톨)로 바꿨어요.")


def test_evidence_for_question_is_narrowed():
    """`대화 보기`는 그 질문의 근거만 보여준다 — 넣은 대화 전체가 아니다 (케이스 12)."""
    all_messages = len(mock.EVIDENCE["messages"])
    assert 0 < len(mock.evidence_for({"question_id": "q1"})["messages"]) < all_messages


def test_progress_steps_have_design_notes():
    """진행 단계 옆 메모는 시안 케이스 08 · 12의 값이다."""
    notes = {s["key"]: s["note_text"] for s in mock.PROGRESS["running"]["steps"]}
    assert notes["read_chat"] == "5명 · 메시지 7개"
    assert notes["match_menu"] == "스타벅스 메뉴 기준"


def test_collapsed_progress_summary_drops_last_step():
    """접힌 진행 말풍선 요약은 `대화 읽기 · 메뉴판과 맞춰 보기 · 바뀐 주문 확인`이다."""
    assert c.steps_summary(mock.PROGRESS["needs_review"]["steps"]) == (
        "대화 읽기 · 메뉴판과 맞춰 보기 · 바뀐 주문 확인")


# --- 주문서와 공유 카드는 같은 자료에서 나온다 ------------------------------
def test_share_card_totals_match_the_order_sheet():
    """주문서는 10잔인데 카드는 5잔이면 담당자가 무엇을 믿어야 할지 알 수 없다."""
    for case in ("base", "many"):
        sheet = mock.order_sheet(case=case)
        card = mock.share_text(case)["card"]
        cups = sheet["totals"]["cups"]
        assert card["sub"].endswith(f'{sheet["summary"]["people"]}명 · {cups}잔')
        assert card["total_text"] == f'합계 {cups}잔 · 예상 {sheet["totals"]["estimated_total"]:,}원'
        assert sum(line["quantity"] for line in card["lines"]) == cups


def test_order_sheet_matches_design_numbers():
    """케이스 19 ~ 21은 5명 · 5잔 · 25,800, 케이스 18은 10명 · 10잔 · 48,300이다."""
    base, many = mock.order_sheet(), mock.order_sheet(case="many")
    assert (base["summary"], base["totals"]["estimated_total"]) == ({"people": 5, "cups": 5}, 25800)
    assert (many["summary"], many["totals"]["estimated_total"]) == ({"people": 10, "cups": 10}, 48300)
    assert many["groups"][0]["quantity"] == 7      # `+2명 더 보기`가 나오는 묶음


def test_shared_sheet_turns_the_status_line_green():
    """공유하면 서버가 초록 줄을 돌려준다 — 화면이 문구를 지어내지 않는다."""
    assert mock.order_sheet(shared=True)["status_line"]["tone"] == "success"
    assert mock.order_sheet(shared=False)["status_line"]["tone"] == "info"


def test_review_chat_has_only_the_summary_line():
    """정본 시안 케이스 11 ~ 17의 채팅에는 요약 한 줄만 있다."""
    assert [m["kind"] for m in mock.SESSION["chat"]] == ["summary"]


def test_layout_keys_have_matching_css():
    """영역 컨테이너는 `key="moa_*"`로 CSS가 고른다 — 이름이 어긋나면 조용히 문서 흐름으로 돌아간다."""
    import pathlib, re
    from moa import tokens
    root = pathlib.Path(__file__).resolve().parents[1]
    used = set()
    for path in [*root.glob("moa/*.py"), *root.glob("screens/*.py")]:
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'key="(moa_[a-z0-9_]+)"', text))
    # 시안 구조의 뼈대 — 헤더 · 하단 바 · 주문서 패널 · 채팅 열 · 주문서 · 운영 카드
    # (주문서의 답장 반영 영역 moa_reply 는 2026-11-04 사용자 요청으로 지웠다)
    assert {"moa_header", "moa_bar", "moa_panel", "moa_chatcol", "moa_chatlog",
            "moa_sheet", "moa_inc_card", "moa_ord_card"} <= used
    # 접두어로 고르는 규칙도 있다(`[class*="st-key-moa_says"]`) — 정확히 같거나 접두어가 맞으면 된다
    styled = set(re.findall(r"st-key-([a-z0-9_]+)", tokens.COMPONENT_CSS))
    for key in used:
        assert any(key == s or key.startswith(s) for s in styled), key
    assert "position: fixed" in tokens.COMPONENT_CSS


def test_icons_survive_html_sanitizer():
    """`st.html`은 `<svg>`를 지운다 — 아이콘 · 로고는 svg 태그 없이 내보내야 화면에 남는다."""
    from moa import icons
    for html_ in (icons.icon("info"), icons.logo(), icons.cup_badge(), icons.swatch("아이스 아메리카노")):
        assert "<svg" not in html_


def test_menu_swatch_is_decoration_only():
    """메뉴 색 견본은 이름으로 색만 고른다. 확인 필요 항목은 회색이다."""
    from moa import icons
    assert icons.swatch_color("유자 민트 티") != icons.swatch_color("민트 초콜릿 칩 블렌디드")
    assert icons.swatch_color('"그걸로 하나"', unknown=True) == icons.SWATCH_UNKNOWN


# --- 되돌리기 — 한 단계를 함께 지운다 (U-5 · SCR-002) -------------------------
def _answered_step(token: str) -> list:
    """확인 질문에 답했을 때 쌓이는 말풍선 3개 — 질문 · 내가 고른 답 · 결과 안내."""
    return [
        {"role": "assistant", "text": "앞선 안내", "kind": "summary"},
        {"role": "assistant", "text": "어떤 사이즈인가요?", "kind": "answered_question",
         "answer": "톨", "undo_step": token},
        {"role": "user", "text": "톨", "undo_step": token},
        {"role": "assistant", "text": "톨로 했어요.", "undo_token": token, "undo_step": token},
    ]


def test_undo_drops_the_whole_step_not_just_the_result():
    """결과 안내만 지우면 답한 질문이 화면에 남아 되돌리기가 지나가 버린다."""
    kept, dropped = state.split_undone_step(_answered_step("u1"), "u1")
    assert [m["text"] for m in kept] == ["앞선 안내"]
    assert len(dropped) == 3
    assert not any(m.get("kind") == "answered_question" for m in kept)


def test_undo_keeps_other_steps():
    """되돌리기는 직전 1회다 — 다른 단계의 말풍선은 건드리지 않는다."""
    chat = _answered_step("u1") + [{"role": "assistant", "text": "그 뒤 안내",
                                    "undo_token": "u2", "undo_step": "u2"}]
    kept, dropped = state.split_undone_step(chat, "u2")
    assert [m["text"] for m in dropped] == ["그 뒤 안내"]
    assert len(kept) == 4


def test_undo_matches_token_only_bubble():
    """서버가 준 대화 기록에는 `undo_step` 이 없다 — 토큰만으로도 지워져야 한다."""
    chat = [{"role": "assistant", "text": "고쳤어요.", "undo_token": "u1"}]
    kept, dropped = state.split_undone_step(chat, "u1")
    assert kept == [] and len(dropped) == 1


def test_order_totals_amount_matches_the_other_places():
    """합계 금액도 `예상 24,900원`이다 — 자리마다 띄어쓰기가 달라지지 않게 한 함수로 만든다."""
    assert c.amount_text(24900, unit=True) == "예상 24,900원"


# --- 행 편집 저장 값 (API-023 · 2026-11-04) -------------------------------------------------------
def test_option_change_sends_group_key_and_internal_value():
    from moa import components
    group = {"label": "사이즈", "option_group": "size", "values": ["톨", "그란데"], "current": "톨",
             "choices": [{"value": "tall", "label": "톨"}, {"value": "grande", "label": "그란데"}]}
    assert components.option_change(group, "그란데") == {"size": "grande"}
    assert components.option_change(group, "톨") == {}            # 지금 값 그대로 — 보내지 않는다
    assert components.option_change(group, None) == {}            # 안 골랐다


def test_preview_share_without_names_drops_only_name_rows():
    from moa import dialogs

    share = {"content": "[스타벅스 주문 정리]\n\n1. 아아 × 2잔\n주문자: 김민수, 박서연\n-빨대 제외\n\n총 2명 · 2잔",
             "card": {"lines": [{"menu_text": "아아", "quantity": 2, "people": ["김민수", "박서연"],
                                 "custom_request": "빨대 빼기"}]}}
    out = dialogs._without_names(share)
    assert "김민수" not in out["content"] and "-빨대 제외" in out["content"] and "1. 아아 × 2잔" in out["content"]
    assert out["card"]["lines"][0]["people"] == []
