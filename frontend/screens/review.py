# SCR-002. B. 주문 정리 (채팅 + 주문서 패널) — URL `/s?s={관리 토큰}`
#
# 출처: P-06 SCR-002 · 정본 시안 `Moa_전체화면.svg` 케이스 08 ~ 17
#   08 정리 중 · 09 복구 중 · 10 처리 지연 · 11 확인 질문 1/2 ·
#   12 펼침 전체 · 13 답한 뒤 질문 2/2 · 14 채팅으로 고치기 ·
#   15 메뉴 검색 모달 · 16 채팅으로 매장 기준 바꾸기 · 17 확인 0건
#
# 구조 — 왼쪽 주문서 패널(400px 고정) + 오른쪽 채팅 한 열. 두 열 모두 화면 높이에 맞추고
# 각자 안에서 스크롤한다. 정리 결과를 채팅 말풍선이 아니라 고정 패널에 두는 이유: 채팅이
# 길어져도 결과가 위로 밀려 올라가지 않게 하기 위해서다(P-06 SCR-002 머리말).
# 정리 중 · 복구 중 · 지연에도 패널 자리는 두고 빈 칸만 보인다(시안 케이스 08 ~ 10).
#
# 이 화면이 하지 않는 것
#   - 채팅 입력을 해석하지 않는다. 문장을 그대로 보내고 받은 "전 → 후" 안내만 그린다
#   - 금액을 계산하지 않는다. 서버가 준 정수를 포맷만 한다
#   - 확인 질문을 두 개 이상 한꺼번에 묻지 않는다 (U-3)
import uuid

import streamlit as st

import api_client
from moa import api, components as c, copy, dialogs, icons, layout, mock, nav, state

st.session_state["_page"] = "review"
layout.page_setup()
state.keep_token_in_url()   # P-06 주소 `?s={관리 토큰}` 유지

PREVIEW_CASES = {
    "08 정리 중": "running",
    "09 복구 중": "recovering",
    "10 처리 지연": "failed",
    "11 확인 필요": "needs_review",
    "17 확인 0건": "awaiting_confirmation",
}

if not mock.PREVIEW_ENABLED and (not state.token() or not state.session_id()):
    # 관리 토큰이 없으면 사유를 구분해 설명하지 않는다 (P-06 상태별 화면 "링크 무효").
    # 토큰은 있는데 세션 식별자가 없으면 **새로고침 · 새 탭**이다 — 그때는 사정을 밝힌다.
    # 여기서 걸러 내지 않으면 `/api/sessions/None/...` 을 불러 서버 404 문구("이 링크는 더 이상
    # 쓸 수 없어요")가 뜨고, 담당자가 멀쩡한 링크를 의심한다(2026-11-04 전체 흐름 실측)
    layout.header(brand=None)
    lost = bool(state.token()) and not state.session_id()
    layout.status_line({"tone": "danger",
                        "text": copy.S2_SESSION_LOST if lost else copy.S2_LINK_INVALID})
    if st.button(copy.S2_LINK_INVALID_HOME, type="primary", key="link_invalid_home"):
        state.reset_order()
        nav.go("start")
    layout.footnote()
    st.stop()


# --- 진행 상태 (API-014) -----------------------------------------------------
def _preview_case() -> str:
    """미리보기(MOA_UI_PREVIEW=1) 대역의 케이스 — `_preview_case` 세션 값으로만 고른다(화면 테스트가 쓴다).

    화면 맨 아래의 "화면 미리보기 — 상태 고르기" 칸은 지웠다(2026-11-04 사용자 요청) — 담당자 화면에 개발용 조작을 두지 않는다.
    """
    label = st.session_state.get("_preview_case") or "11 확인 필요"
    return PREVIEW_CASES.get(label, "needs_review")


case = _preview_case() if mock.PREVIEW_ENABLED else "needs_review"
if case == "needs_review" and st.session_state.get("_answered", 0) >= len(mock.QUESTIONS):
    # 미리보기에서 질문을 모두 답하면 케이스 17로 넘어간다 — 서버에서는 API-014가 바꿔 준다
    case = "awaiting_confirmation"


def _preview_progress(case_key: str) -> dict:
    """미리보기 대역 — 답한 만큼 남은 건수와 상태 줄 문구가 줄어든다."""
    base = mock.PROGRESS[case_key]
    if case_key != "needs_review":
        return base
    left = max(len(mock.QUESTIONS) - st.session_state.get("_answered", 0), 0)
    return {**base, "review_count": left,
            "status_line": {**base["status_line"], "text": f"{left}건만 답해 주시면 돼요"}}


try:
    progress = api.get(
        lambda: api_client.get_progress(state.session_id(), session_token=state.token()),
        fallback=_preview_progress(case), label="progress")
except api_client.ApiError as err:
    layout.fetch_failed(err, brand=st.session_state.get(state.BRAND))
status = progress.get("status")
review_count = progress.get("review_count")
busy = status in ("running", "recovering")
has_result = not (busy or status == "failed")
st.session_state["_remaining"] = review_count   # 확인 질문 큐가 오면 그 값으로 바뀐다

session = {}
if has_result:
    # 미리보기 대역에서도 답한 행은 확인 필요가 풀리고 사이즈 · 금액이 채워져야
    # 왼쪽 패널과 채팅이 어긋나지 않는다. 서버에서는 API-015가 이미 반영된 결과를 준다.
    #
    # **미리보기가 꺼져 있으면 계산하지 않는다.** 서버가 붙은 경로에서 쓰이지도 않는 대역을
    # 계산하다 `mock.QUESTIONS` 길이를 넘는 `_answered` 로 터져 화면 전체가 멈췄다
    # (서버 질문 3건 · 대역 질문 2건 → 세 번째 답에서 IndexError, 2026-11-03 확인)
    # `fallback=` 은 **서버가 응답해도 평가된다**(파이썬 인자 계산) — 그래서 대역을
    # 미리 만들어 두고, 미리보기가 꺼져 있으면 `None` 을 넘긴다(api.get 이 그러면 대역을
    # 쓰지 않고 오류를 올린다)
    session_fallback = None
    if mock.PREVIEW_ENABLED:
        preview_rows = mock.rows_after(st.session_state.get("_answered", 0))
        if st.session_state.get("_preview_chat_edit"):
            preview_rows = [{**r, **mock.CHAT_EDIT_EFFECT, "changed": True} if r["item_id"] == "it_03"
                            else {**r, "changed": False} for r in preview_rows]
        session_fallback = {
            **mock.SESSION, "status": status, "rows": preview_rows,
            # 되묻기 · 매장 바꾸기로 고른 기준을 덮어쓰지 않는다 (미리보기 대역)
            "brand": st.session_state.get(state.BRAND) or mock.BRAND,
            "summary": {**mock.session_summary(preview_rows),
                        "review_count": review_count or 0}}
    try:
        session = api.get(
            lambda: api_client.get_session(state.session_id(), session_token=state.token()),
            fallback=session_fallback, label="session")
    except api_client.ApiError as err:
        layout.fetch_failed(err, brand=st.session_state.get(state.BRAND))
    if session.get("version") is not None:
        st.session_state[state.VERSION] = session["version"]
    if session.get("brand"):
        st.session_state[state.BRAND] = session["brand"]
    if not st.session_state[state.CHAT]:
        # 채팅 말풍선 문구는 서버가 만든다
        for item in session.get("chat", []):
            state.add_chat(item.get("role", "assistant"), item.get("text", ""), kind=item.get("kind"))
        # 주문서에서 답장을 반영하고 돌아온 경우 — 무엇이 일어났는지 알린다(1차 보고 11번 · 3차 보고 6번).
        # 요약 한 줄 + **바뀐 주문마다 한 줄**. 문구는 서버가 만든 것을 그대로 쓴다
        # 그 앞에 붙여넣은 답장을 내 말로 둔다 — 무엇을 넣어 이 결과가 나왔는지 보이게(4차 보고 2번)
        if st.session_state.get(state.REPLY_TEXT):
            state.add_chat("user", st.session_state[state.REPLY_TEXT])
            st.session_state[state.REPLY_TEXT] = ""
        if st.session_state.get(state.NOTICE):
            state.add_chat("assistant", st.session_state[state.NOTICE])
            st.session_state[state.NOTICE] = None
        for applied in st.session_state.get(state.REPLY_APPLIED) or []:
            state.add_chat("assistant", applied.get("text", ""))
        st.session_state[state.REPLY_APPLIED] = []

brand = st.session_state.get(state.BRAND)

# --- 1 헤더 · 2 상태 줄 · 바꾸는 법 한 줄 --------------------------------------
layout.header(brand=brand)
layout.status_line(progress.get("status_line"))
layout.change_hint(brand)


# =============================================================================
# 동작 — 행 편집 · 되돌리기 · 질문 답 · 채팅 보내기
# =============================================================================
def _open_row(item_id):
    st.session_state[state.EDIT_ROW] = item_id
    st.rerun()


def _save_row(row, changes):
    """API-023 — 행 편집. 바로 반영하고 되돌리기를 남긴다(U-5)."""
    data = api.act(
        lambda: api_client.patch_item(
            state.session_id(), row["item_id"], changes,
            session_token=state.token(), version=st.session_state.get(state.VERSION)),
        fallback={"user_message": f'{row["person"]} 님 주문을 고쳤어요.', "undo_token": "preview-undo"},
        label="edit_row")
    state.add_chat("assistant", data.get("user_message", ""), undo_token=data.get("undo_token"),
                   undo_step=data.get("undo_token"))
    st.session_state[state.EDIT_ROW] = None
    st.rerun()


def _undo(token_value):
    """API-024 — 되돌리기(직전 1회)."""
    data = api.act(lambda: api_client.undo(state.session_id(), token_value,
                                           session_token=state.token()),
                   fallback={"user_message": "되돌렸어요"}, label="undo")
    if data.get("_error"):
        state.add_chat("assistant", data["user_message"])   # 되돌리기 버튼은 남긴다
        st.rerun()
    # 결과 안내만 지우면 답한 질문 말풍선이 남아, 서버가 그 질문을 다시 물어도 화면은
    # 처리된 상태로 보인다 — 한 단계를 함께 지운다 (state.split_undone_step 주석)
    kept, dropped = state.split_undone_step(st.session_state[state.CHAT], token_value)
    # 되돌리기는 직전 1회다 — 되돌린 뒤에는 유효한 토큰이 없다(서버가 이미 지웠다).
    # 남은 말풍선의 낡은 토큰을 지우지 않으면 눌러도 422 가 나는 [되돌리기]가 다시 보인다
    st.session_state[state.CHAT] = [{**m, "undo_token": None} if m.get("undo_token") else m
                                    for m in kept]
    if any(m.get("kind") == "answered_question" for m in dropped):
        # 답을 되돌렸으면 질문 큐도 한 칸 되돌아간다 (서버에서는 API-024가 질문을 다시 연다)
        st.session_state["_answered"] = max(st.session_state.get("_answered", 0) - 1, 0)
    st.session_state["_preview_chat_edit"] = False
    state.add_chat("assistant", data.get("user_message") or "되돌렸어요")
    st.rerun()


def _record_answer(question, said, chosen, data):
    """답한 질문을 대화 흐름에 남긴다 — 질문 → 내 말 → 결과 순서다(버튼 답 · 채팅 답 공통).

    `said` 는 내 말풍선 글자(누른 버튼 라벨 또는 친 문장), `chosen` 은 답한 질문에서 진하게 보일 버튼 라벨이다.
    세 말풍선은 **한 단계**다 — 되돌리기가 셋을 함께 걷어 가야 그 질문을 다시 묻는 상태로 돌아간다 (U-5)
    """
    step = data.get("undo_token")
    state.add_chat("assistant", question.get("text", ""), kind="answered_question",
                   order_text="{}/{}".format(question.get("index"), question.get("total")),
                   options=[o["label"] for o in question.get("options", [])]
                   + ([copy.S2_QUICK_OTHER_MENU] if question.get("allow_other_menu") else []),
                   answer=chosen, undo_step=step)
    state.add_chat("user", said, undo_step=step)
    state.add_chat("assistant", data.get("text") or data.get("user_message", ""),
                   undo_token=step, undo_step=step)
    st.session_state["_answered"] = st.session_state.get("_answered", 0) + 1


def _send_pending_chat() -> bool:
    """적어 둔 채팅 문장을 **제출한 실행이 아닌 다음 실행에서** 보낸다 (2026-11-05 4차 보고 1번).

    보냈으면 True — 호출자가 **다시 실행한다.** 이 실행은 진행 상태 · 세션을 이미 읽은 뒤이므로
    남은 건수 · 상태 줄 · 주문서 패널이 모두 **보내기 전 값**이다. 다시 읽지 않으면 채팅으로 답해
    확인이 끝났는데도 하단 바가 "1건만 답해 주시면 돼요"로 막혀 다음 단계로 못 넘어간다
    (2026-11-06 5차 보고 1번).

    왜 여기서 보내는가 — `st.chat_input` 은 제출 뒤 값을 비우는 재실행을 한 번 더 보내고,
    그 재실행이 `POST /messages` 에 매달려 있던 실행을 **취소한다.** 그러면 서버는 반영했는데
    결과 말풍선과 [되돌리기]가 영영 오지 않는다 — 담당자 눈에는 "채팅으로 고치기가 안 된다"로 보인다
    (브라우저 실측: 서버 200 · 주문서 패널은 바뀌고 채팅만 비어 있었다).

    이 실행이 또 취소되면 적어 둔 문장이 그대로 남아 다음 실행이 다시 보낸다. 두 번 반영되지
    않는 것은 **같은 Idempotency-Key** 가 보장한다(D-02 1.2) — 서버가 보관한 응답을 돌려준다.
    """
    pending = st.session_state.get(state.PENDING_CHAT)
    if not pending:
        return False
    sent, key = pending["text"], pending["key"]
    question = pending.get("question")

    # 질문이 떠 있었으면 먼저 그 질문의 답으로 보낸다 (2026-11-04 사용자 보고 3 · 4).
    # 답으로 읽히면 버튼 답과 같은 순서(질문 → 내 말 → 결과)로 남긴다. 못 읽으면(422) 채팅 수정으로 넘긴다
    if question:
        answered = api.act(
            lambda: api_client.answer_question_text(
                state.session_id(), question["question_id"], sent,
                session_token=state.token(), version=st.session_state.get(state.VERSION),
                idempotency_key=f"{key}-answer"),
            label="answer_text")
        if not answered.get("_error"):
            st.session_state[state.PENDING_CHAT] = None
            if answered.get("version") is not None:
                st.session_state[state.VERSION] = answered["version"]
            _record_answer(question, sent, answered.get("answer_label") or sent, answered)
            return True

    state.add_chat("user", sent)
    data = api.act(
        lambda: api_client.post_message(state.session_id(), sent, session_token=state.token(),
                                        version=st.session_state.get(state.VERSION),
                                        idempotency_key=key),
        fallback=mock.interpret(sent), label="message")
    # 보낸 것은 지운다 — 응답을 받았으므로 다시 보내지 않는다
    st.session_state[state.PENDING_CHAT] = None
    if data.get("version") is not None:
        st.session_state[state.VERSION] = data["version"]
    is_brand_ask = data.get("kind") == "brand_change_ask"
    if data.get("kind") == "applied" and "preview" in str(data.get("undo_token")):
        st.session_state["_preview_chat_edit"] = True     # 미리보기 대역에서만 쓰는 표시
    state.add_chat("assistant", data.get("text") or data.get("user_message", ""),
                   undo_token=data.get("undo_token"),
                   undo_step=data.get("undo_token"),
                   brand_change=data.get("brand_id") if is_brand_ask else None,
                   guide_text=data.get("guide_text") if is_brand_ask else None)
    return True


def _row_editor(row):
    def _menu():
        dialogs.menu_dialog(lambda menu: _save_row(row, {"menu_id": menu["menu_id"]}),
                            key=f"rowmenu_{row['item_id']}")

    picked, custom = c.option_editor(row.get("option_choices"), key=row["item_id"],
                                     custom_value=row.get("custom_request"))
    c.editor_actions(key=row["item_id"], remove_label=copy.S2_PANEL_EDIT_REMOVE,
                     on_menu=_menu, on_remove=lambda: _save_row(row, {"removed": True}),
                     on_save=lambda: _save_row(row, {"options": picked,
                                                     "custom_request": custom or None}))


if has_result and _send_pending_chat():
    # 보냈으면 다시 실행한다 — 이 실행이 읽어 둔 진행 상태 · 세션은 **보내기 전 값**이다.
    # 그대로 그리면 확인이 끝났는데도 하단 바가 막혀 있고 패널도 안 바뀐 것으로 보인다(5차 보고 1번)
    st.rerun()

with st.container(key="moa_work", horizontal=True):
    # =========================================================================
    # 왼쪽 — 5 주문서 패널 (400px · 화면 높이 · 행 목록만 스크롤)
    # =========================================================================
    with st.container(key="moa_panel"):
        if has_result:
            with st.container(key="moa_panel_head"):
                c.order_panel_head(session.get("summary", {}))
            # 확인 필요 행은 패널에 두지 않는다 — 원문 문장이 메뉴 자리에 보이고, 같은 것을 채팅이 또 묻는다
            # (2026-11-05 사용자 보고 9번). 확인은 채팅 질문 하나로만 하고, 패널은 정리된 주문만 보여준다
            settled = c.by_person([r for r in session.get("rows", []) if not r.get("needs_review")])
            asking = len(session.get("rows", [])) - len(settled)
            with st.container(key="moa_panel_rows"):
                for row in settled:
                    opened = st.session_state.get(state.EDIT_ROW) == row["item_id"]
                    c.order_row(row, on_open=_open_row, opened=opened,
                                editor=lambda row=row: _row_editor(row))
                if asking:
                    st.html('<div class="moa-panel-asking">'
                            f'{c.esc(copy.S2_PANEL_ASKING.format(n=asking))}</div>')
                if not settled and not asking:
                    st.html(f'<div class="moa-panel-asking">{c.esc(copy.S2_PANEL_EMPTY)}</div>')
                elif not settled:
                    st.html(f'<div class="moa-panel-asking">{c.esc(copy.S2_PANEL_NONE_YET)}</div>')
            # 하단 — 넣은 대화 전체. 패널에서 뺀 문장도 여기서 그대로 볼 수 있다(보고 9번)
            panel_raw = st.session_state[state.PANEL_RAW_OPEN]
            with st.container(key="moa_panel_foot"):
                st.html(f'<div class="moa-panel-foot-text">{c.esc(session.get("ai_notice"))}</div>')
                if st.button(copy.S2_PANEL_RAW_CLOSE if panel_raw else copy.S2_PANEL_RAW_OPEN,
                             key="link_panel_raw",
                             icon=":material/expand_less:" if panel_raw else ":material/expand_more:",
                             icon_position="right"):
                    st.session_state[state.PANEL_RAW_OPEN] = not panel_raw
                    st.rerun()
                if panel_raw:
                    whole = api.get(
                        lambda: api_client.get_evidence(state.session_id(), session_token=state.token()),
                        fallback=mock.EVIDENCE, label="evidence")
                    st.html('<div class="moa-raw-list moa-panel-raw">' + "".join(
                        f'<div class="moa-raw-row"><span class="moa-raw-name">{c.esc(m.get("sender"))}</span>'
                        f'<span class="moa-raw-text">{c.esc(m.get("text"))}</span></div>'
                        for m in whole.get("messages", [])) + "</div>")
        else:
            with st.container(key="moa_panel_skel"):
                c.panel_skeleton()

    # =========================================================================
    # 오른쪽 — 채팅 열 (대화 기록은 안에서 스크롤 · 입력창은 아래 고정)
    # =========================================================================
    with st.container(key="moa_chatcol"):
        with st.container(key="moa_chatlog", autoscroll=True):
            # --- 3 내가 넣은 대화 — 오른쪽 연초록 묶음. [원본 보기]로 펼친다 ---------
            label = (session.get("input_summary") or {}).get("label_text")
            if not label:
                counted = len(((st.session_state.get(state.PREVIEW) or {}).get("messages")) or [])
                label = f"{copy.S2_INPUT_LABEL} {counted}개" if counted else copy.S2_INPUT_LABEL
            raw_open = st.session_state[state.RAW_OPEN]
            pill_key = "moa_rawpill_open" if raw_open else "moa_rawpill"
            with st.container(key=pill_key):
                with st.container(key=f"{pill_key}_head", horizontal=True, vertical_alignment="center"):
                    st.html(f'<span class="moa-raw-label">{icons.icon("file", 16)}{c.esc(label)}</span>')
                    if st.button(copy.S2_INPUT_VIEW_RAW,
                                 key="link_raw_close" if raw_open else "link_raw_open"):
                        st.session_state[state.RAW_OPEN] = not raw_open
                        st.rerun()
                    if st.button(copy.S2_INPUT_REENTER, key="link_reenter"):
                        # 넣은 대화를 유지한 채 SCR-001로 돌아간다 (U-9)
                        nav.go("start")
                if raw_open:
                    evidence = api.get(
                        lambda: api_client.get_evidence(state.session_id(), session_token=state.token()),
                        fallback=mock.EVIDENCE, label="evidence")
                    rows_html = "".join(
                        f'<div class="moa-raw-row"><span class="moa-raw-name">{c.esc(m.get("sender"))}</span>'
                        f'<span class="moa-raw-text">{c.esc(m.get("text"))}</span>'
                        f'{c.pill((m.get("event") or {}).get("label", ""), mock.EVENT_TONE.get((m.get("event") or {}).get("type"), "gray"))}</div>'
                        for m in evidence.get("messages", []))
                    st.html(f'<div class="moa-raw-list">{rows_html}</div>')

            # --- 4 진행 말풍선 — 4단계. 끝나면 한 줄로 접는다 ------------------------
            collapsed = progress.get("collapsed_text")
            with c.bot("progress"):
                if collapsed and not st.session_state[state.PROGRESS_OPEN]:
                    with st.container(key="bubblerow_progress", horizontal=True,
                                      vertical_alignment="center"):
                        st.html(c.progress_line(collapsed, progress.get("steps")))
                        if st.button(copy.S2_PROGRESS_EXPAND, key="link_progress_expand",
                                     icon=":material/expand_more:", icon_position="right"):
                            st.session_state[state.PROGRESS_OPEN] = True
                            st.rerun()
                else:
                    st.html(c.steps_html(progress.get("steps")))
                    if collapsed and st.button(copy.S2_PROGRESS_COLLAPSE, key="link_progress_collapse"):
                        st.session_state[state.PROGRESS_OPEN] = False
                        st.rerun()

            # --- 채팅 말풍선 ----------------------------------------------------------
            # 정리 중 · 처리 지연에는 결과 말풍선을 두지 않는다 — 아직 결과가 없다.
            # 진행 말풍선만 남는다(정본 시안 케이스 08 ~ 10).
            shown_chat = st.session_state[state.CHAT] if has_result else []
            # 되돌리기는 **직전 1회**다 — 가장 마지막 안내에만 버튼을 둔다(시안 케이스 14).
            undoable = next((i for i in range(len(shown_chat) - 1, -1, -1)
                             if shown_chat[i].get("undo_token")), None)

            for index, message in enumerate(shown_chat):
                if message["role"] != "assistant":
                    c.me(message["text"])
                    continue
                kind = message.get("kind")
                with c.bot(f"m{index}"):
                    if kind == "answered_question":
                        # 답한 질문은 대화 흐름에 남긴다 — 순번 배지는 회색, 고른 답은 진한 테두리
                        st.html(f'<div class="moa-q">{c.pill(message["order_text"], "gray")}'
                                f'<span>{c.esc(message["text"])}</span></div>')
                        with st.container(key=f"answers_done_{index}", horizontal=True):
                            for n, option_label in enumerate(message.get("options", [])):
                                if option_label == message.get("answer"):
                                    with st.container(key=f"chosen_{index}_{n}", width="content"):
                                        st.button(option_label, key=f"done_{index}_{n}", disabled=True)
                                else:
                                    st.button(option_label, key=f"done_{index}_{n}", disabled=True)
                    elif kind == "summary":
                        # "5명 · 5잔으로 정리했어요 …" — 인원 · 잔 수만 굵게 (시안 케이스 11)
                        text = c.esc(message["text"])
                        head, sep, tail = text.partition("으로 ")
                        st.html(f'<div class="moa-bubble-text">'
                                f'{"<b>" + head + "</b>" + sep + tail if sep else text}</div>')
                    elif message.get("undo_token") and index == undoable:
                        with st.container(key=f"bubblerow_{index}", horizontal=True,
                                          vertical_alignment="center"):
                            st.html(f'<div class="moa-bubble-text">{c.esc(message["text"])}</div>')
                            if st.button(copy.S2_UNDO, key=f"link_undo_{index}", icon=":material/undo:"):
                                _undo(message["undo_token"])
                    elif message.get("brand_change") is not None or kind == "brand_change_ask":
                        # "일반 카페 메뉴 기준으로 새로 정리할까요?" — 매장 이름만 굵게 (시안 케이스 16)
                        name, sep, rest = c.esc(message["text"]).partition(" 메뉴 기준")
                        st.html(f'<div class="moa-bubble-text">'
                                f'{"<b>" + name + "</b>" + sep + rest if sep else name}</div>')
                        if message.get("guide_text"):
                            st.html(f'<div class="moa-dlg-foot-text">{c.esc(message["guide_text"])}</div>')
                        if message.get("brand_change"):
                            # 매장 기준 바꾸기 — 묻는 것은 이것 하나뿐이다 (U-5)
                            with st.container(key=f"brandask_{index}", horizontal=True):
                                if st.button(copy.S2_BRAND_CHANGE_YES, key=f"brand_yes_{index}",
                                             type="primary"):
                                    data = api.act(
                                        lambda: api_client.change_brand(
                                            state.session_id(), message["brand_change"],
                                            session_token=state.token(),
                                            version=st.session_state.get(state.VERSION)),
                                        fallback={"user_message": "새로 정리하고 있어요"}, label="brand_change")
                                    if not data.get("_error"):
                                        st.session_state[state.CHAT] = []
                                    state.add_chat("assistant", data.get("user_message", ""))
                                    st.rerun()
                                if st.button(copy.S2_BRAND_CHANGE_NO, key=f"brand_no_{index}"):
                                    st.session_state[state.CHAT][index] = {**message, "brand_change": ""}
                                    st.rerun()
                    else:
                        st.html(f'<div class="moa-bubble-text">{c.esc(message["text"])}</div>')

            # --- 6 확인 질문 (한 번에 하나) · 7 빠른 답 버튼 -------------------------
            # 지금 떠 있는 질문 — 채팅 입력을 이 질문의 답으로 먼저 읽는다(아래 8 채팅 입력창)
            st.session_state["_open_question"] = None
            if status == "needs_review":
                answered = st.session_state.get("_answered", 0)
                preview_question = mock.QUESTIONS[answered] if answered < len(mock.QUESTIONS) else None
                questions = api.get(
                    lambda: api_client.get_questions(state.session_id(), session_token=state.token()),
                    # `review_count`는 위에서 이미 답한 만큼 줄어든 값이다 — 여기서 또 빼지 않는다
                    fallback={"question": preview_question, "remaining": review_count or 0},
                    label="questions")
                st.session_state["_remaining"] = questions.get("remaining", review_count)
                question = questions.get("question")
                if question:
                    qid = question["question_id"]
                    st.session_state["_open_question"] = question

                    def _retry_on_stale(call, **kwargs):
                        """409 STALE_VERSION 이면 세션을 다시 읽어 **한 번만** 다시 보낸다 (D-02 2.5).

                        예전에는 409 를 토스트로만 알리고 끝나서, 담당자 눈에는 "버튼을 눌러도 아무 일도
                        일어나지 않는" 것으로 보였다(2026-11-05 3차 보고 5번 — 사이즈 버튼 3개 다 반영 안 됨).
                        """
                        data = api.act(lambda: call(version=st.session_state.get(state.VERSION)), **kwargs)
                        if data.get("code") != "STALE_VERSION":
                            return data
                        fresh = api.act(lambda: api_client.get_session(state.session_id(),
                                                                       session_token=state.token()),
                                        label="session_refresh")
                        if fresh.get("version") is not None:
                            st.session_state[state.VERSION] = fresh["version"]
                        return api.act(lambda: call(version=st.session_state.get(state.VERSION)), **kwargs)

                    def _answer(value, label=None, question=question):
                        """API-021 — 빠른 답. **누른 버튼의 라벨**이 내 말풍선으로 남는다.

                        `value` 는 서버로 보내는 내부 값(`grande`)이고 `label` 은 화면 글자
                        (`그란데`)다. 둘을 섞으면 내 말풍선에 `grande` 가 그대로 찍히고,
                        답한 질문에서 고른 답을 진하게 표시하는 대조도 어긋난다
                        (옵션 목록은 라벨로 들고 있다 — 2026-11-03 확인).
                        """
                        shown = label or value
                        data = _retry_on_stale(
                            lambda version: api_client.answer_question(
                                state.session_id(), question["question_id"], value,
                                session_token=state.token(), version=version),
                            fallback=mock.answer_result(question, value), label="answer")
                        if data.get("version") is not None:
                            st.session_state[state.VERSION] = data["version"]
                        if data.get("_error"):
                            st.toast(data["user_message"])  # 질문을 답한 것으로 남기지 않는다
                            st.rerun()
                        # 답한 질문은 사라지지 않고 흐름에 남는다(정본 시안 케이스 13 · 14 · 17)
                        _record_answer(question, shown, shown, data)
                        st.rerun()

                    with c.bot(f"q_{qid}"):
                        order = "{}/{}".format(question.get("index"), question.get("total"))
                        st.html(f'<div class="moa-q">{c.pill(order, "solid")}'
                                f'<span>{c.esc(question.get("text"))}</span></div>')
                        with st.container(key=f"answers_{qid}", horizontal=True):
                            # 후보 버튼은 같은 크기 · 기본 선택 없음 · 추천 표시 없음
                            for option in question.get("options", []):
                                if st.button(option["label"], key=f"ans_{qid}_{option['value']}"):
                                    _answer(option["value"], option["label"])
                            if question.get("allow_other_menu"):
                                if st.button(copy.S2_QUICK_OTHER_MENU, key=f"other_{qid}"):
                                    dialogs.menu_dialog(lambda menu: _answer(menu["name"]), key=f"q_{qid}")
                            if st.button(copy.S2_QUICK_VIEW_EVIDENCE, key=f"link_evidence_{qid}",
                                         icon=":material/chat_bubble:"):
                                # 근거 메시지(참고한 대화 포함)를 **이 말풍선 안에** 펼친다(케이스 12)
                                found = api.get(
                                    lambda: api_client.get_evidence(
                                        state.session_id(), question_id=qid, session_token=state.token()),
                                    fallback=mock.evidence_for(question), label="evidence")
                                st.session_state[f"_ev_{qid}"] = found.get("messages", [])
                                st.rerun()
                        shown = st.session_state.get(f"_ev_{qid}")
                        if shown:
                            st.html('<div class="moa-raw-list moa-evidence-list">' + "".join(
                                f'<div class="moa-raw-row"><span class="moa-raw-name">{c.esc(m.get("sender"))}</span>'
                                f'<span class="moa-raw-text">{c.esc(m.get("text"))}</span>'
                                f'{c.pill((m.get("event") or {}).get("label", ""), mock.EVENT_TONE.get((m.get("event") or {}).get("type"), "gray"))}</div>'
                                for m in shown) + "</div>")

            # --- 17 확인 0건 --------------------------------------------------------
            if status == "awaiting_confirmation":
                summary = session.get("summary", {})
                c.bot_text("all_done", f'{c.esc(copy.S2_ALL_DONE)}<div class="moa-step-note moa-num">'
                           f'{c.esc(summary.get("people", 0))}명 · {c.esc(summary.get("cups", 0))}잔 · '
                           f'{c.esc(c.amount_text(summary.get("estimated_total"), unit=True))}</div>')

        # --- 8 채팅 입력창 --------------------------------------------------------
        sent = st.chat_input(copy.S2_CHAT_PLACEHOLDER_BUSY if not has_result else copy.S2_CHAT_PLACEHOLDER,
                             disabled=not has_result, key="chat_in")
        if sent:
            # **여기서 서버를 부르지 않는다.** 보낼 문장과 Idempotency-Key 만 적어 두고 바로 재실행한다 —
            # 보내는 일은 `_send_pending_chat` 이 다음 실행에서 한다. 이유는 그 함수의 설명문
            # (chat_input 의 두 번째 재실행이 이 실행을 취소한다 · 4차 보고 1번)
            st.session_state[state.PENDING_CHAT] = {
                "text": sent, "key": f"chat-{uuid.uuid4().hex}",
                # 질문이 떠 있었으면 그 질문의 답으로 먼저 읽는다 (2026-11-04 사용자 보고 3 · 4)
                "question": st.session_state.get("_open_question")}
            st.rerun()

# --- 9 하단 바 — [‹ 대화 넣기로] + [주문서 보기] -----------------------------
# 남은 건수는 **질문 큐와 확인 필요 건수 중 큰 값**이다 (2026-11-05 사용자 보고 10번).
# 질문 큐만 보면, 물을 질문이 없는데 확인 필요 항목이 남은 상태에서 주문서로 넘어가고
# 거기서 처음 "확인이 필요한 주문이 1건 있어요"를 만난다 — 주문서에서는 그것을 풀 수단이 없다.
# 서버도 같은 구멍을 질문 보충(questions.reconcile)으로 막는다 — 둘은 같은 방향의 이중 방어다
remaining = max(int(st.session_state.get("_remaining") or 0), int(review_count or 0))
# 반영할지 정하지 않은 답장 제안도 막는다 — 주문서에서야 만나면 거기서는 풀 수단이 없다(3차 보고 7번)
pending_suggestions = int(progress.get("pending_suggestions") or 0)
blocked = None
if not has_result:
    blocked = copy.S2_BUSY_HINT
elif remaining:
    blocked = copy.S2_REMAINING_HINT.format(n=remaining)
elif pending_suggestions:
    blocked = copy.S2_PENDING_SUGGESTION_HINT.format(n=pending_suggestions)

back, go_order = layout.bottom_bar(
    back_label=copy.S2_BACK, back_key="s2_back",
    primary_label=copy.S2_PRIMARY, primary_key="s2_primary",
    primary_enabled=blocked is None, blocked_reason=blocked)
if back:
    nav.go("start")
if go_order:
    nav.go("order")

layout.footnote()

# 정리 중이면 진행 상태를 다시 묻는다 — D-02 1절 "분석 진행 1초 폴링", 주기는 서버가 준다(API-002).
# 미리보기 대역은 상태를 바꾸지 않으므로 폴링하지 않는다.
if busy and "progress" not in api.preview_used():
    import time

    time.sleep(api.meta().get("limits", {}).get("poll_interval_ms", 1000) / 1000)
    st.rerun()
