# 대화상자 4종 — O-0 비밀번호 · 매장 되묻기 · 메뉴 찾기 · 주문서 공유하기
#
# 출처: P-06 SCR-006 O-0 · SCR-001 매장 되묻기(케이스 06 · 07) · SCR-002 메뉴 검색(케이스 15) ·
#       SCR-005 공유 메뉴(케이스 21) · 정본 시안 `Moa_전체화면.svg`
# 그 밖의 확인 대화상자는 두지 않는다 — 선택 · 수정은 바로 반영 + 되돌리기다(U-5).
import html
import json

import streamlit as st

import api_client
from moa import api, copy, icons, mock, nav, state


def _esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def _head(icon_name: str, title: str, sub: str, *, green: bool = False) -> None:
    cls = "moa-dlg-icon moa-dlg-icon-green" if green else "moa-dlg-icon"
    st.html(f'<div class="moa-dlg-head"><span class="{cls}">{icons.icon(icon_name, 18)}</span>'
            f'<div><div class="moa-dlg-title">{_esc(title)}</div>'
            f'<div class="moa-dlg-sub">{_esc(sub)}</div></div></div>')


# --- O-0. 비밀번호 입력 -------------------------------------------------------
def open_ops_login() -> None:
    """담당자 화면 헤더 [운영 대시보드] → O-0을 띄운다.

    들어갈 때마다 묻는다 — 서비스 진입 비밀번호를 통과했어도, 전에 운영 대시보드에 들어갔어도
    다시 받는다(2026-11-05 사용자 요청). 운영 화면 밖에서는 `app.py`가 운영 인증을 버린다.
    """
    st.session_state[state.OPS_ERROR] = None
    ops_login_dialog()


def ops_login_form(form_key: str, *, with_cancel: bool) -> tuple[bool, bool, str]:
    """비밀번호 칸 1개 · 아이디 칸 없음 · Enter로도 제출(P-06 O-0). 단독 화면도 같은 모양이다."""
    return password_form(form_key, title=copy.O0_DIALOG_TITLE, prompt=copy.O0_PROMPT,
                         error_key=state.OPS_ERROR, with_cancel=with_cancel)


def _lock_head(title: str, prompt: str) -> None:
    """비밀번호 폼 머리 — 왼쪽 초록 그라데이션 자물쇠 배지 · 오른쪽 제목 + 안내(2026-11-05 사용자 요청).

    배지 뒤 원 두 개는 로고의 작은 원(#BFD8C9 · #7FB197)을 따왔다. 장식이라 스크린리더에서 숨긴다.
    """
    st.html('<div class="moa-lock-head">'
            '<div class="moa-lock-badge" aria-hidden="true">'
            '<span class="moa-lock-dot moa-lock-dot-a"></span><span class="moa-lock-dot moa-lock-dot-b"></span>'
            f'<span class="moa-lock-core">{icons.icon("lock-keyhole", 22, width=2)}</span></div>'
            f'<div><div class="moa-lock-title">{_esc(title)}</div>'
            f'<div class="moa-lock-sub">{_esc(prompt)}</div></div></div>')


def password_form(form_key: str, *, title: str, prompt: str, error_key: str,
                  with_cancel: bool) -> tuple[bool, bool, str]:
    """O-0 구성의 비밀번호 폼 — 운영 대시보드 · 서비스 진입 게이트가 함께 쓴다.

    **입력값을 session_state에 남기지 않는다** — `clear_on_submit`으로 제출 즉시 칸을 비우고,
    읽은 값은 지역 변수로만 쓴다(NFR-003 · P-06 O-0 "틀리면 칸을 비운다").
    """
    _lock_head(title, prompt)
    error = st.session_state.get(error_key)
    with st.container(key="dlg_ops_err" if error else "dlg_ops_ok"):
        with st.form(form_key, border=False, clear_on_submit=True):
            st.html(f'<div class="moa-field-label">{_esc(copy.O0_LABEL)}</div>')
            password = st.text_input(copy.O0_LABEL, type="password",
                                     placeholder=copy.O0_PLACEHOLDER, label_visibility="collapsed")
            # 오류는 **칸 바로 아래, 버튼 위**다 (정본 시안 케이스 05).
            # 틀린 사유를 구분하지 않고 한 줄만 보여준다(NFR-003)
            if error:
                st.html(f'<div class="moa-form-error">{_esc(error)}</div>')
            cancelled = False
            if with_cancel:
                cancel_col, submit_col = st.columns([5, 7])
                with cancel_col:
                    cancelled = st.form_submit_button(copy.O0_CANCEL, use_container_width=True)
                with submit_col:
                    submitted = st.form_submit_button(copy.O0_SUBMIT, type="primary",
                                                      use_container_width=True)
            else:
                submitted = st.form_submit_button(copy.O0_SUBMIT, type="primary",
                                                  use_container_width=True)
    return submitted, cancelled, password


@st.dialog(copy.O0_DIALOG_TITLE, dismissible=False)
def ops_login_dialog() -> None:
    """**틀렸을 때 앱 전체를 다시 실행하지 않는다** — 대화상자가 닫혀 오류 문구를 볼 수 없다.

    오른쪽 위 X는 두지 않는다(2026-11-05 사용자 요청) — 닫는 길은 [취소] 하나다.
    `dismissible=False`라 ESC · 바깥 클릭으로도 닫히지 않는다.
    """
    with st.container(key="dlg_ops"):
        submitted, cancelled, password = ops_login_form("ops_login_form", with_cancel=True)
    if cancelled:
        # 취소하면 원래 담당자 화면으로 — 넣은 대화 · 정리 결과를 그대로 둔다(U-9)
        st.session_state[state.OPS_ERROR] = None
        st.rerun()
    if submitted and try_ops_login(password):
        # `@st.dialog`은 조각(fragment)이라 **조각만** 다시 그리면 대화상자가 닫히지 않는다.
        st.rerun(scope="fragment")


def try_ops_login(password: str):
    """성공하면 운영 대시보드로 넘어가고, 실패하면 **보여줄 문구를 돌려준다**."""
    if not password:
        st.session_state[state.OPS_ERROR] = copy.O0_ERROR_FALLBACK
        return copy.O0_ERROR_FALLBACK
    try:
        data = api_client.ops_login(password)
    except api_client.ApiError as err:
        if mock.PREVIEW_ENABLED and isinstance(err, api_client.ApiUnreachable):
            api.preview_used().add("ops_login")
            data = {"ops_token": "preview-ops-token"}
        else:
            # 서버가 준 `user_message`를 그대로 보여준다(D-02 `OPS_AUTH_FAILED`)
            message = err.user_message or copy.O0_ERROR_FALLBACK
            st.session_state[state.OPS_ERROR] = message
            return message
    st.session_state[state.OPS_TOKEN] = data.get("ops_token")
    st.session_state[state.OPS_AUTHED] = True
    st.session_state[state.OPS_ERROR] = None
    nav.go("ops")
    return None


def try_service_login(password: str):
    """서비스 진입 게이트 — 운영 대시보드와 **같은 비밀번호**를 서버가 비교한다(API `/api/ops/login`).

    비밀번호 비교는 서버에만 둔다(화면에 비밀번호 값을 두지 않는다 · P-06 O-0).
    검증용으로 받은 운영 토큰은 바로 돌려준다 — 서비스에 들어왔다고 운영 대시보드가 열리지 않는다.
    """
    if not password:
        st.session_state[state.SERVICE_ERROR] = copy.O0_ERROR_FALLBACK
        return copy.O0_ERROR_FALLBACK
    try:
        data = api_client.ops_login(password)
    except api_client.ApiError as err:
        if mock.PREVIEW_ENABLED and isinstance(err, api_client.ApiUnreachable):
            api.preview_used().add("service_login")
            data = {}
        else:
            message = err.user_message or copy.O0_ERROR_FALLBACK
            st.session_state[state.SERVICE_ERROR] = message
            return message
    if data.get("ops_token"):
        try:
            api_client.ops_logout(ops_token=data["ops_token"])
        except api_client.ApiError:
            pass  # 반납하지 못해도 토큰은 1시간 뒤 서버에서 만료된다
    st.session_state[state.SERVICE_AUTHED] = True
    st.session_state[state.SERVICE_ERROR] = None
    return None


def drop_ops_auth() -> None:
    """운영 인증 해제 — 서버 토큰을 반납하고 화면의 인증 플래그를 지운다."""
    try:
        api_client.ops_logout(ops_token=st.session_state.get(state.OPS_TOKEN))
    except api_client.ApiError:
        pass  # 서버에 닿지 못해도 화면의 인증 플래그는 반드시 지운다
    state.clear_ops()


def logout_ops() -> None:
    """[나가기] — 인증을 해제하고 SCR-001로 돌아간다."""
    drop_ops_auth()
    nav.go("start")


# --- 매장 되묻기 (SCR-001 케이스 06 · 07) ------------------------------------
_OPTION_ICON = {
    # 선택지 그림 — 같은 크기의 정사각 아이콘. 추천 · 기본 선택을 뜻하지 않는다
    "starbucks": ('style="background:var(--moa-green-deep)"', 'style="background:#EEDFC2"'),
    "generic": ('style="background:#F1E8D8"', 'style="background:#CDB394"'),
}


@st.dialog(copy.S1_BRAND_DIALOG_TITLE)
def brand_dialog() -> None:
    """같은 단계에 스타벅스와 다른 매장 이름이 함께 나올 때만 정리 전에 한 번 띄운다.

    선택지는 **같은 크기 · 같은 테두리 · 기본 선택 없음 · 추천 표시 없음**이다.
    브랜드 선택 화면처럼 보이지 않게 한다(PRD 2.4.1 (4)).
    질문 · 안내 · 선택지 · 찾은 매장 근거는 서버가 준다(API-012 `brand_question`).
    """
    question = st.session_state.get(state.BRAND_QUESTION) or {}
    with st.container(key="dlg_brand"):
        _head("store", copy.S1_BRAND_DIALOG_TITLE,
              question.get("subtitle_text") or copy.S1_BRAND_SUBTITLE_FALLBACK, green=True)

        found = question.get("found_stores") or []
        if found:
            chips = "".join(f'<span class="moa-found-chip"><b>{_esc(f.get("name"))}</b>'
                            f'· {_esc(f.get("evidence_text"))}</span>' for f in found)
            st.html(f'<div class="moa-found"><div class="moa-found-label">'
                    f'{_esc(copy.S1_BRAND_FOUND_LABEL)}</div>{chips}</div>')

        options = question.get("options", [])
        other_open = st.session_state.get(state.BRAND_OTHER_OPEN)
        for option in options:
            value = option.get("value")
            desc = option.get("description") or copy.S1_BRAND_OPTION_DESC.get(value, "")
            if option.get("needs_store_name"):
                if other_open:
                    _other_store(option)
                    continue
                icon_html = (f'<span class="moa-opt-icon" style="background:var(--moa-chip);'
                             f'color:var(--moa-ink-4)">{icons.icon("plus", 16)}</span>')
            else:
                box, dot = _OPTION_ICON.get(value, _OPTION_ICON["generic"])
                icon_html = f'<span class="moa-opt-icon" {box}><span {dot}></span></span>'
            with st.container(key=f"card_brand_{value}"):
                st.html(f'<div class="moa-opt">{icon_html}<div><div class="moa-opt-title">'
                        f'{_esc(option.get("label"))}</div><div class="moa-opt-desc">{_esc(desc)}</div></div>'
                        f'<span class="moa-opt-chev">{icons.icon("chevron-right", 16)}</span></div>')
                if st.button(option.get("label", ""), key=f"hit_brand_{value}"):
                    if option.get("needs_store_name"):
                        st.session_state[state.BRAND_OTHER_OPEN] = True
                        st.rerun(scope="fragment")
                    else:
                        _answer_brand(value)

        with st.container(key="dlg_brand_foot", horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="center"):
            st.html(f'<span class="moa-dlg-foot-text">{_esc(question.get("guide_text", ""))}</span>')
            if question.get("cancel_text") and st.button(question["cancel_text"], key="brand_cancel"):
                # 모달을 닫고 입력 화면으로 — 넣은 대화는 유지한다(U-9)
                st.session_state[state.BRAND_QUESTION] = None
                st.session_state[state.BRAND_OTHER_OPEN] = False
                st.rerun()


def _other_store(option: dict) -> None:
    """기타 매장 — 이름을 적어야 [이 매장으로 정리]가 켜진다 (케이스 07)."""
    with st.container(key="dlg_brand_other"):
        st.html(f'<div class="moa-opt-other-title"><span class="moa-opt-icon" style="background:'
                f'var(--moa-surface);color:var(--moa-ink-4)">{icons.icon("plus", 16)}</span>'
                f'{_esc(copy.S1_BRAND_OTHER_TITLE)}</div>')
        with st.container(key="dlg_brand_other_row", horizontal=True, vertical_alignment="center"):
            store_name = st.text_input(copy.S1_BRAND_OTHER_PLACEHOLDER,
                                       placeholder=copy.S1_BRAND_OTHER_PLACEHOLDER,
                                       label_visibility="collapsed", key="brand_other_name")
            go = st.button(copy.S1_BRAND_OTHER_SUBMIT, key="brand_other_submit", type="primary",
                           disabled=not store_name.strip())
        if option.get("hint_text"):
            st.html(f'<div class="moa-opt-hint">{_esc(option["hint_text"])}</div>')
    if go:
        _answer_brand("other", store_name.strip())


def _answer_brand(answer: str, store_name: str | None = None) -> None:
    """API-013 — 되묻기 답을 보내고 바로 분석을 시작한다."""
    data = api.get(
        lambda: api_client.submit_brand_answer(
            state.session_id(), answer, store_name, session_token=state.token()),
        fallback={**mock.SESSION_CREATED, "brand": _preview_brand(answer, store_name)},
        label="brand_answer")
    state.remember_session(data)
    st.session_state[state.BRAND_QUESTION] = None
    st.session_state[state.BRAND_OTHER_OPEN] = False
    nav.go("review")


def _preview_brand(answer: str, store_name: str | None) -> dict:
    """미리보기용 매장 기준 — 서버가 만드는 근거 문장을 흉내 낸 예시값이다."""
    if answer == "starbucks":
        return mock.BRAND
    hint = "매장 기준을 바꾸려면 채팅으로 '스타벅스로 해줘'라고 보내 주세요"
    if answer == "generic":
        return {**mock.BRAND, "id": "generic", "label": "일반 카페 메뉴 기준", "source": "asked",
                "evidence_text": "'일반 카페'로 답해 주셨어요", "change_hint_text": hint}
    return {**mock.BRAND, "id": "generic", "label": "일반 카페 메뉴 기준", "source": "asked",
            "evidence_text": f"'{store_name}'로 답해 주셨어요. 메뉴가 아직 없어 일반 카페 메뉴로 정리했어요",
            "change_hint_text": hint}


# --- 메뉴 찾기 (SCR-002 케이스 15) -------------------------------------------
@st.dialog(copy.S2_MENU_DIALOG_TITLE)
def menu_dialog(on_pick, *, key: str = "menu") -> None:
    """화면 가운데 모달 · 목록은 모달 안에서만 스크롤.

    매장 기준은 세션에서 주입되므로 화면이 `brand_id`를 넘기지 않는다(G-A3 (1)).
    """
    with st.container(key="dlg_menu"):
        query = st.text_input(copy.S2_MENU_SEARCH_PLACEHOLDER,
                              placeholder=copy.S2_MENU_SEARCH_PLACEHOLDER,
                              label_visibility="collapsed", key=f"{key}_q")
        data = api.get(lambda: api_client.search_menus(query, session_token=state.token()),
                       fallback={"menus": [m for m in mock.MENUS if not query or query in m["name"]],
                                 "scope_notice": mock.MENU_SCOPE_NOTICE},
                       label="menu_search")
        # 어떤 매장 기준의 메뉴를 보고 있는지 한 줄로 밝힌다 (정본 시안 케이스 15)
        scope = data.get("scope_notice") or ""
        head, sep, tail = scope.partition(" 메뉴만")
        scope_html = (f"<b>{_esc(head)}</b>{_esc(sep.strip())}{_esc(tail)}" if sep else _esc(scope))
        st.html(f'<div class="moa-scope">{scope_html}</div>')
        menus = data.get("menus", [])
        with st.container(key="dlg_menu_list", height=360, border=False):
            if not menus:
                st.html('<div class="moa-scope" style="padding:16px 12px">찾는 메뉴가 없어요</div>')
            for menu in menus:
                price = menu.get("estimated_price")
                # 메뉴판 가격이라 "예상"을 붙이지 않는다 — 주문 행 금액과 자리가 다르다
                amount = f"{price:,}원" if price is not None else ""
                with st.container(key=f"card_menu_{key}_{menu['menu_id']}"):
                    st.html(f'<div class="moa-menu-row">{icons.swatch(menu["name"], 30)}'
                            f'<span class="moa-menu-name">{_esc(menu["name"])}</span>'
                            f'<span class="moa-menu-spec">{_esc(menu.get("spec_text", ""))}</span>'
                            f'<span class="moa-menu-price moa-num">{_esc(amount)}</span></div>')
                    if st.button(menu["name"], key=f"hit_menu_{key}_{menu['menu_id']}"):
                        on_pick(menu)
                        st.rerun()


# --- 단톡방 답장 반영하기 (SCR-005 케이스 20 · 2026-11-05 보고 11번) ------------
@st.dialog(copy.S5_REPLY_TITLE)
def reply_dialog(on_analyzed) -> None:
    """답장 대화를 붙여넣는 모달. [바뀐 주문 찾기]를 누르면 서버가 분석 · 반영까지 한다.

    이 화면은 답장을 해석하지 않는다 — 문장을 그대로 보내고, 결과 문구도 서버가 준 것을 쓴다.
    반영이 끝나면 호출자가 주문 정리 화면으로 보낸다(그 화면이 바뀐 주문과 남은 확인을 보여준다).
    """
    with st.container(key="dlg_reply"):
        st.html(f'<div class="moa-dlg-sub">{_esc(copy.S5_REPLY_SUB)}</div>')
        # 입력과 버튼을 **폼으로 묶는다** — 붙여넣고 바로 눌러도 값과 클릭이 같은 재실행에 함께 온다.
        # 폼이 아니면 첫 클릭은 입력창 blur 로만 쓰여, 담당자는 두 번 눌러야 했다(2026-11-05 3차 보고 1번)
        with st.form("reply_form", border=False, clear_on_submit=False):
            text = st.text_area(copy.S5_REPLY_PLACEHOLDER, placeholder=copy.S5_REPLY_PLACEHOLDER,
                                label_visibility="collapsed", height=180, key="reply_raw")
            submitted = st.form_submit_button(copy.S5_REPLY_ANALYZE, type="primary")
        if submitted:
            if not (text or "").strip():
                st.toast(copy.S5_REPLY_EMPTY)
                return
            # 붙여넣은 답장은 주문 정리 채팅에 **내 말**로 남긴다(2026-11-05 4차 보고 2번)
            st.session_state[state.REPLY_TEXT] = text.strip()
            data = api.act(
                lambda: api_client.analyze_replies(state.session_id(), text, apply=True,
                                                   session_token=state.token()),
                fallback={"found_text": "바뀐 주문을 찾지 못했어요", "applied_count": 0,
                          "asked_count": 0}, label="reply_analyze")
            on_analyzed(data)
            st.rerun()


# --- 주문서 공유하기 (SCR-005 케이스 21) --------------------------------------
@st.dialog(copy.S5_SHARE_TITLE)
def share_dialog(share: dict, on_shared) -> None:
    """공유 카드 미리보기 + [텍스트 복사] [이미지 저장]. 복사 · 저장하면 그 시점에 완료된다.

    공유 내용은 서버가 만든다. 이미지 생성이 실패해도 텍스트 복사는 항상 남긴다.
    """
    from moa import components as c

    with st.container(key="dlg_share"):
        # 이름 노출 선택 — 기본은 지금처럼 이름을 보낸다. 끄면 서버에 이름 없는 공유 내용을 다시 받는다(2026-11-04 · #8).
        # 미리보기 · 복사 텍스트 · 이미지가 **같은 선택**으로 만들어져야 한다 — 하나만 바꾸면 보낸 것과 본 것이 달라진다
        show_names = st.toggle(copy.S5_SHARE_NAMES, value=True, key="share_names")
        if not show_names:
            share = api.get(
                lambda: api_client.get_share(state.session_id(), "text", names=False,
                                             session_token=state.token()),
                fallback=_without_names(share), label="share")
            st.html(f'<div class="moa-dlg-caption">{_esc(copy.S5_SHARE_NAMES_OFF)}</div>')
        if share.get("card"):
            st.html(c.share_card_html(share["card"]))
        with st.container(key="dlg_share_btns", horizontal=True):
            copy_clicked = st.button(copy.S5_SHARE_COPY, key="share_copy", type="primary",
                                     icon=":material/content_copy:")
            image = None
            if share.get("image_available"):
                try:
                    image = api.get(
                        lambda: api_client.get_share(state.session_id(), "image", names=show_names,
                                                     session_token=state.token()),
                        fallback=None, label="share_image")
                except api_client.ApiError:
                    # 받아 오지 못해도 **대화상자를 깨뜨리지 않는다** — [텍스트 복사]는 늘 남는다.
                    # 여기서 예외가 올라가면 공유 창 자체가 사라져 복사도 못 한다
                    image = None
            if image:
                if st.download_button(copy.S5_SHARE_IMAGE, data=image,
                                      file_name=share.get("image_filename", "order.png"),
                                      mime="image/png", key="share_image",
                                      icon=":material/download:"):
                    on_shared("image")
                    st.rerun()
            elif st.button(copy.S5_SHARE_IMAGE, key="share_image_na", icon=":material/download:"):
                # 이미지를 받지 못했으면 이유만 알린다 — 완료로 기록하지 않는다
                st.toast(share.get("image_unavailable_text", ""))
        st.html(f'<div class="moa-dlg-caption">{_esc(copy.S5_SHARE_CAPTION)}</div>')

    if copy_clicked:
        # 클립보드에 넣는 일은 다음 실행의 화면이 한다(`copy_to_clipboard`) — 누른 직후라 브라우저가 허락한다
        st.session_state["_clipboard"] = share.get("content", "")
        on_shared("text")
        st.rerun()


def _without_names(share: dict) -> dict:
    """미리보기(서버 없음)에서만 쓰는 대체값 — 서버와 같은 규칙으로 이름 줄만 뺀다."""
    card = share.get("card") or {}
    lines = [{**line, "people": []} for line in card.get("lines") or []]
    # 주문자 줄은 서버가 "이름 없음 2명"처럼 묶어 쓰므로 이름 목록과 맞춰 보지 않고 머리말로 가른다
    content = "\n".join(row for row in (share.get("content") or "").split("\n") if not row.startswith("주문자: "))
    return {**share, "card": {**card, "lines": lines}, "content": content}


# 복사가 둘 다 실패했을 때만 펼치는 자리 — 담당자가 직접 끌어 복사한다.
# Streamlit 이 `st.container(key=...)` 에 붙이는 클래스 이름이다(기본은 CSS 로 숨겨 둔다)
COPY_FAILED_FLAG = "st-key-moa_copy_failed"


def copy_script(text: str) -> str:
    """클립보드에 넣는 스크립트 — **비보안 컨텍스트(평문 http)에서도 되는 경로를 함께 둔다.**

    왜 두 경로인가 (브라우저 실측 2026-11-05)
      `navigator.clipboard` 는 보안 컨텍스트(https · localhost)에만 있다. 배포한 화면은
      게이트웨이 8501 을 평문 http 로 열기 때문에 `isSecureContext === false` 이고 그 API 가
      **아예 없다.** 예전 코드는 `navigator.clipboard && …` 로 짧게 끊고 끝나서 아무 일도
      일어나지 않았는데, 화면은 "복사했어요" 토스트를 띄우고 공유 완료로 기록했다 — 담당자는
      단톡방에 붙여넣기를 하고 나서야 빈 것을 알았다(2026-11-05 4차 보고 2번).

      같은 환경에서 `document.execCommand('copy')` 는 **된다**(실측으로 붙여넣기까지 확인).
      오래된 방식이지만 평문 http 에서 쓸 수 있는 유일한 경로다.

    실패를 삼키지 않는다 — 둘 다 못 하면 본문에 표시를 남겨 직접 복사할 자리를 연다.
    """
    return (
        "<script>(function(){var t=" + json.dumps(text) + ";"
        "function legacy(){try{"
        "var a=document.createElement('textarea');a.value=t;a.setAttribute('readonly','');"
        "a.style.position='fixed';a.style.left='-9999px';document.body.appendChild(a);"
        "a.select();a.setSelectionRange(0,t.length);"
        "var ok=document.execCommand('copy');document.body.removeChild(a);return ok;"
        "}catch(e){return false;}}"
        "function failed(){var f=document.querySelector('." + COPY_FAILED_FLAG + "');"
        "if(f){f.style.display='block';}}"
        "if(navigator.clipboard&&navigator.clipboard.writeText){"
        "navigator.clipboard.writeText(t).catch(function(){if(!legacy()){failed();}});"
        "}else if(!legacy()){failed();}"
        "})();</script>")


def copy_to_clipboard() -> None:
    """[텍스트 복사]를 누른 바로 다음 실행에서 한 번만 클립보드에 넣는다.

    복사한 내용을 **접어서 함께 둔다** — 브라우저가 둘 다 막으면(사파리 구버전 등) 담당자가
    여기서 직접 끌어 복사한다. 복사가 됐으면 이 자리는 보이지 않는다.
    """
    text = st.session_state.pop("_clipboard", None)
    if text is None:
        return
    st.html(copy_script(text), unsafe_allow_javascript=True)
    with st.container(key="moa_copy_failed"):     # 기본은 CSS 로 숨어 있다 (tokens.py)
        st.html(f'<div class="moa-copy-failed-text">{_esc(copy.S5_COPY_FAILED)}</div>')
        st.code(text, language=None, wrap_lines=True)
