# SCR-005. E. 주문서 (주문서 · 정산 통합 · 공유하면 완료) — URL `/order?s={관리 토큰}`
#
# 출처: P-06 SCR-005 · 정본 시안 `Moa_전체화면.svg` 케이스 18 ~ 21
#   18 공유 전(같은 메뉴 여러 명 포함) · 19 이름 눌러 바꾸기 · 21 공유 메뉴
#
# 배치 — 요약 카드 2칸 + 주문서 묶음 목록 + 합계 한 열.
#   "단톡방 답장 반영하기"(케이스 20)는 하단 바의 [공유하기] 왼쪽 보조 버튼이다(2026-11-05 사용자 요청)
#
# **확정 단계를 두지 않는다**(v3.0). 텍스트 복사 · 이미지 저장을 하면
# 그 시점에 주문을 완료 처리한다 — 확정 대화상자 · 상태 배지 · [주문 확정] 버튼은 없다.
#
# 이 화면이 하지 않는 것
#   - 공유 내용(텍스트 · 이미지)을 조립하지 않는다. 서버가 만든 것을 그대로 보여준다
#   - 공유 가능 조건을 판정하지 않는다. `can_share` · `blocked_reason_text`를 그대로 쓴다
#   - 개인별 금액을 표시하지 않는다 (PRD v1.5 FR-010 · D-02 2.4)
#   - 완료 처리를 직접 하지 않는다. 복사 · 저장 사실을 알리는 호출만 한다
import streamlit as st

import api_client
from moa import api, components as c, copy, dialogs, icons, layout, mock, nav, state

st.session_state["_page"] = "order"
layout.page_setup()
state.keep_token_in_url()   # P-06 주소 `?s={관리 토큰}` 유지
dialogs.copy_to_clipboard()  # [텍스트 복사]를 누른 바로 다음 실행에서만 동작한다

PREVIEW_CASES = {
    "18 같은 메뉴 여러 명 (10명)": "many",
    "19 ~ 21 기본 (5명)": "base",
}


def _preview_case() -> str:
    """미리보기(MOA_UI_PREVIEW=1) 대역의 예시 주문서 — `_order_case` 세션 값으로만 고른다(화면 테스트가 쓴다).

    화면 맨 아래의 "화면 미리보기 — 주문서 고르기" 칸은 지웠다(2026-11-04 사용자 요청).
    """
    if not mock.PREVIEW_ENABLED:
        return "base"
    return PREVIEW_CASES.get(st.session_state.get("_order_case") or "19 ~ 21 기본 (5명)", "base")


if not mock.PREVIEW_ENABLED and (not state.token() or not state.session_id()):
    # 토큰은 있는데 세션 식별자가 없으면 새로고침 · 새 탭이다 (SCR-002와 같은 판정)
    layout.header(brand=None)
    lost = bool(state.token()) and not state.session_id()
    layout.status_line({"tone": "danger",
                        "text": copy.S2_SESSION_LOST if lost else copy.S2_LINK_INVALID})
    if st.button(copy.S2_LINK_INVALID_HOME, type="primary", key="link_invalid_home"):
        state.reset_order()
        nav.go("start")
    layout.footnote()
    st.stop()

# --- 산출물 조회 (API-030) ---------------------------------------------------
shared = st.session_state.get(state.SHARED)
_sheet_fallback = {**mock.order_sheet(shared=shared, case=_preview_case()),
                   "brand": st.session_state.get(state.BRAND) or mock.BRAND}
try:
    sheet = api.get(
        lambda: api_client.get_order_sheet(state.session_id(), session_token=state.token()),
        fallback=_sheet_fallback, label="order_sheet")
except api_client.ApiError as err:
    layout.fetch_failed(err, brand=st.session_state.get(state.BRAND))
if sheet.get("version") is not None:
    st.session_state[state.VERSION] = sheet["version"]
if sheet.get("brand"):
    st.session_state[state.BRAND] = sheet["brand"]

# 공유 가능 여부 · 막는 이유는 서버 판정 그대로다 — 답장 제안 영역을 지워 화면이 들고 있는 대기 제안도 없다
can_share = bool(sheet.get("can_share"))
blocked_reason = sheet.get("blocked_reason_text")

# --- 1 헤더 · 2 상태 줄 — 공유하면 초록으로 바뀐다 ----------------------------
# 바뀐 문구는 **서버가 준다**(공유 완료 기록 후 산출물 조회가 초록 줄을 돌려준다).
layout.header(brand=st.session_state.get(state.BRAND))
layout.status_line(sheet.get("status_line"))


def _apply_person_edit(group, index, changes):
    """API-023 — 이름 칩을 눌러 그 자리에서 고친다. 바로 반영 + 되돌리기(U-5).

    `index` 는 칩 순번이다. 이름으로 찾으면(people.index) 같은 사람이 두 잔일 때 늘 첫 잔만 고쳐진다
    """
    item_ids = group.get("item_ids") or []
    people = group.get("people") or []
    if len(item_ids) != len(people) or not 0 <= index < len(people):
        return
    item_id, person = item_ids[index], people[index]
    data = api.act(
        lambda: api_client.patch_item(state.session_id(), item_id, changes,
                                      session_token=state.token(),
                                      version=st.session_state.get(state.VERSION)),
        fallback={"user_message": f"{person} 님 주문을 고쳤어요."}, label="edit_person")
    st.session_state["_edit_person"] = None
    st.toast(data.get("user_message") or "반영했어요")
    st.rerun()


def _pick_person(group, index):
    """편집 중인 칩 — (묶음, 순번). 이름이 아니라 순번이다(components.names_row)."""
    st.session_state["_edit_person"] = (group.get("group_id"), index) if index is not None else None
    st.rerun()


def _person_editor(group, index):
    """케이스 19 — 묶음 안에서 그 사람 한 잔만 고친다. 옵션 값은 서버가 준 것만 펼친다."""
    gid = group.get("group_id")
    person = (group.get("people") or [])[index]
    key = f"{gid}_{index}"
    title = (f'<div class="moa-edit-head">{c.esc(group.get("menu_text"))}'
             f'<span>× 1 · {c.esc(person)}</span></div>')
    picked, custom = c.option_editor(group.get("option_choices"), key=key, title_html=title,
                                     custom_value=group.get("custom_request"))

    def _menu():
        dialogs.menu_dialog(lambda menu: _apply_person_edit(group, index, {"menu_id": menu["menu_id"]}),
                            key=f"gpick_{key}")

    c.editor_actions(key=key, remove_label=copy.S5_EDIT_REMOVE_PERSON, on_menu=_menu,
                     on_remove=lambda: _apply_person_edit(group, index, {"removed": True}),
                     on_save=lambda: _apply_person_edit(group, index,
                                                        {"options": picked, "custom_request": custom or None}))


summary = sheet.get("summary", {})
with st.container(key="moa_order", horizontal=True):
    with st.container(key="moa_order_main"):
        # --- 3 요약 — 큰 숫자 2칸 (금액 · 시각 없음) ------------------------------
        st.html(
            f'<div class="moa-sum-grid">'
            f'<div class="moa-sum moa-sum-dark"><div class="moa-sum-label">{icons.icon("users", 15)}'
            f'{c.esc(copy.S5_SUMMARY_PEOPLE)}</div><div class="moa-sum-num moa-num">'
            f'{c.esc(summary.get("people", 0))}<small>명</small></div></div>'
            f'<div class="moa-sum moa-sum-light"><div class="moa-sum-label">{icons.icon("coffee", 15)}'
            f'{c.esc(copy.S5_SUMMARY_CUPS)}</div><div class="moa-sum-num moa-num">'
            f'{c.esc(summary.get("cups", 0))}<small>잔</small></div></div></div>')

        # --- 4 주문서 — 묶음 목록 · 이름 칩 ---------------------------------------
        with st.container(key="moa_sheet"):
            hint = "" if shared else f'<span>{c.esc(copy.S5_ORDER_HINT)}</span>'
            st.html(f'<div class="moa-sheet-title"><b>{c.esc(copy.S5_ORDER_TITLE)}</b>{hint}</div>')
            editing = st.session_state.get("_edit_person")
            for group in sheet.get("groups", []):
                gid = group.get("group_id")
                editing_index = editing[1] if editing and editing[0] == gid else None
                # 순번이 지금 묶음 밖이면(다시 그려 인원이 줄었다 · 예전 이름 기록) 편집을 닫는다
                if not (isinstance(editing_index, int) and 0 <= editing_index < len(group.get("people") or [])):
                    editing_index = None
                with st.container(key=f"group_{gid}"):
                    st.html(c.group_html(group))
                    c.names_row(group, editable=not shared, editing_index=editing_index,
                                on_pick=_pick_person)
                    if editing_index is not None:
                        with st.container(key=f"moa_editor_group_{gid}"):
                            _person_editor(group, editing_index)
            # --- 5 합계 -------------------------------------------------------------
            c.order_totals(sheet.get("totals", {}))


# --- 7 공유하기 = 완료 --------------------------------------------------------
def _mark_shared(method: str) -> None:
    """API-034 — 복사 · 저장 사실을 알린다. 완료 처리는 서버가 기록한다."""
    data = api.act(lambda: api_client.complete_share(state.session_id(), method,
                                                     session_token=state.token()),
                   fallback={"user_message": "복사했어요" if method == "text" else "저장했어요"},
                   label="share_complete")
    if data.get("_error"):
        st.session_state.pop("_clipboard", None)
        st.toast(data["user_message"])                  # 완료로 표시하지 않는다
        return
    st.session_state[state.SHARED] = True
    st.toast(data.get("user_message", ""))


# --- 6 단톡방 답장 반영하기 (케이스 20 · 2026-11-05 보고 11번) ----------------
# 공유한 뒤 단톡방에 온 "나 라떼로 바꿔줘"를 받는 자리다. 붙여넣으면 서버가 분명한 변경은 반영하고
# 불분명한 것은 확인 질문으로 올린다 — 그 결과를 보려면 주문 정리 화면으로 돌아가야 한다.
def _after_reply(data: dict) -> None:
    found = data.get("found_text") or ""
    st.session_state[state.NOTICE] = copy.S2_REPLY_APPLIED.format(found=found)
    # 바뀐 주문마다 한 줄 — 주문 정리 화면의 채팅에 그대로 남는다(3차 보고 6번)
    st.session_state[state.REPLY_APPLIED] = data.get("applied") or []
    st.session_state[state.CHAT] = []          # 주문 정리 화면이 서버 문구로 다시 채운다
    st.session_state["_answered"] = 0
    nav.go("review")


def _share_button() -> None:
    # [단톡방 답장 반영하기]는 공유하기 바로 왼쪽의 보조 버튼이다(2026-11-05 사용자 요청).
    # 공유 → 단톡방 답장 → 반영이 한 흐름이라 같은 자리에 둔다. 주 버튼은 여전히 [공유하기] 하나다(U-1)
    # 공유한 **뒤**에도 둔다. 이 서비스의 변경 요청 받기는 "한 번 공유하고 바꿀 것만 답장으로 받는다"라
    # 답장은 공유 다음에 온다 — `not shared` 로 가려 두면 정작 쓸 때 버튼이 없다(2026-11-07 사용자 보고)
    with st.container(key="moa_reply_open", width="content"):
        if st.button(copy.S5_REPLY_OPEN_BUTTON, key="reply_open", icon=":material/forum:"):
            dialogs.reply_dialog(_after_reply)
    # 화면의 주 버튼은 이 하나뿐이다 (U-1). 누르면 공유 대화상자가 열린다(케이스 21)
    if st.button(copy.S5_SHARE, key="share_open", type="primary", disabled=not can_share,
                 icon=":material/ios_share:"):
        share = api.get(
            lambda: api_client.get_share(state.session_id(), "text", session_token=state.token()),
            fallback=mock.share_text(_preview_case()), label="share")
        dialogs.share_dialog(share, _mark_shared)


# --- 8 하단 바 — [‹ 주문 정리로] + (막는 이유) + [공유하기] ------------------
back, _ = layout.bottom_bar(back_label=copy.S5_BACK, back_key="s5_back", primary_label=copy.S5_SHARE,
                            primary_key="share_open", primary_enabled=can_share,
                            blocked_reason=blocked_reason, primary_slot=_share_button,
                            # 막는 이유(확인 필요 · 정하지 않은 답장)는 주문 정리 화면에서 푼다(3차 보고 7번)
                            blocked_action=(lambda: nav.go("review")) if blocked_reason else None)
if back:
    nav.go("review")

layout.footnote()
