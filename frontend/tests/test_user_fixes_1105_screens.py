# 2026-11-05 사용자 보고 9 · 10 · 11번 — 화면 (Streamlit AppTest · 서버 없이 미리보기 대역으로 돈다)
#
# 9번  주문 정리 왼쪽 패널에는 확정된 주문만 · 확인 필요는 채팅이 묻는다 · 하단에서 전체 대화를 펼친다
# 10번 확인이 남아 있으면 [주문서 보기]가 막힌다
# 11번 주문서에 [단톡방 답장 반영하기]가 다시 있다
import os
import pathlib

os.environ.setdefault("MOA_UI_PREVIEW", "1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from moa import copy, mock, state  # noqa: E402

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
REVIEW = str(FRONTEND / "screens" / "review.py")
ORDER = str(FRONTEND / "screens" / "order.py")
CASE_REVIEW = "11 확인 필요"
CASE_NO_REVIEW = "17 확인 0건"


def app(script, case=None, **session):
    at = AppTest.from_file(script, default_timeout=60)
    at.session_state[state.SESSION_ID] = "ORD-0012"
    at.session_state[state.SESSION_TOKEN] = "tok_1105"
    if case:
        at.session_state["_preview_case"] = case
    for key, value in session.items():
        at.session_state[key] = value
    return at.run()


def html_of(at):
    return "\n".join([e.body for e in at.markdown] + [getattr(e, "body", "") for e in at.get("html")])


def keys_of(at):
    return {b.key for b in at.button}


# --- 9번 ---------------------------------------------------------------------------------
def test_09_panel_shows_only_settled_rows():
    """확인 필요 행(원문 문장)은 패널에 그리지 않는다 — 메뉴 자리에 문장이 들어가 보이는 것을 막는다."""
    at = app(REVIEW, CASE_REVIEW)
    html = html_of(at)
    settled = [r for r in mock.rows_after(0) if not r["needs_review"]]
    # 패널 행은 메뉴 이름 한 줄로 그려진다 — 확정된 행 수만큼만 있어야 한다.
    # 클래스 이름은 스타일 블록에도 나오므로 **여는 태그**를 센다
    drawn = html.count('class="moa-row-menu"')
    assert drawn == len(settled), f"그려진 행 {drawn}개 · 확정된 행 {len(settled)}개"
    # 확인 필요 행의 원문 문장은 패널에 없다 (질문 말풍선에는 있다)
    assert '"그걸로 하나"' not in html.split("moa-panel-asking")[0]


def test_09_panel_says_how_many_are_being_asked():
    at = app(REVIEW, CASE_REVIEW)
    asking = sum(1 for r in mock.rows_after(0) if r["needs_review"])
    assert copy.S2_PANEL_ASKING.format(n=asking) in html_of(at)


def test_09_panel_has_a_whole_conversation_toggle():
    at = app(REVIEW, CASE_REVIEW)
    assert "link_panel_raw" in keys_of(at)
    assert copy.S2_PANEL_RAW_OPEN in [b.label for b in at.button]


def test_09_whole_conversation_opens_in_the_panel():
    at = app(REVIEW, CASE_REVIEW)
    at.button(key="link_panel_raw").click().run()
    html = html_of(at)
    assert "moa-panel-raw" in html
    # 넣은 대화의 보낸 사람 · 문장이 그대로 보인다
    first = mock.EVIDENCE["messages"][0]
    assert first["sender"] in html and first["text"][:10] in html
    assert copy.S2_PANEL_RAW_CLOSE in [b.label for b in at.button]


def test_09_settled_rows_are_still_editable():
    """패널에서 뺀 것은 확인 필요 행뿐이다 — 확정된 행의 행 편집은 그대로 쓴다."""
    at = app(REVIEW, CASE_REVIEW)
    settled = [r for r in mock.rows_after(0) if not r["needs_review"]]
    assert f"card_row_{settled[0]['item_id']}" in keys_of(at) or \
        any(settled[0]["item_id"] in (k or "") for k in keys_of(at))


# --- 10번 --------------------------------------------------------------------------------
def test_10_order_button_is_blocked_while_anything_needs_review():
    at = app(REVIEW, CASE_REVIEW)
    primary = at.button(key="s2_primary")
    assert primary.disabled is True
    assert copy.S2_REMAINING_HINT.format(n=mock.PROGRESS["needs_review"]["review_count"]) in html_of(at)


def test_10_order_button_opens_once_nothing_needs_review():
    at = app(REVIEW, CASE_NO_REVIEW)
    assert at.button(key="s2_primary").disabled is False


# --- 11번 --------------------------------------------------------------------------------
def test_11_order_sheet_has_the_reply_button_again():
    at = app(ORDER)
    assert "reply_open" in keys_of(at)
    assert copy.S5_REPLY_OPEN_BUTTON in [b.label for b in at.button]


def test_11_reply_dialog_asks_for_the_pasted_chat():
    at = app(ORDER)
    at.button(key="reply_open").click().run()
    assert "reply_raw" in {t.key for t in at.text_area}
    # 입력과 버튼은 폼으로 묶여 있다 — 붙여넣고 한 번만 눌러도 제출된다(3차 보고 1번)
    labels = [b.label for b in at.button] + [b.label for b in at.get("form_submit_button")]
    assert copy.S5_REPLY_ANALYZE in labels


def test_11_reply_button_stays_after_sharing():
    """공유 **뒤에도** 둔다 (2026-11-07 사용자 보고 — 종전 판정을 뒤집었다).

    종전에는 "공유했으면 더 못 바꾼다"로 보고 버튼을 지웠다. 그런데 이 서비스의 변경 요청 받기는
    *담당자가 전체 주문을 단톡방에 한 번 공유하고 바꿀 것만 답장으로 받는* 흐름이다 —
    답장은 공유 **다음**에 온다. 공유 뒤에 지우면 정작 쓸 때 들어갈 문이 없다.
    """
    at = app(ORDER, **{state.SHARED: True})
    assert "reply_open" in keys_of(at)
    assert copy.S5_REPLY_OPEN_BUTTON in [b.label for b in at.button]
