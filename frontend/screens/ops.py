# SCR-006. O. 운영 대시보드 — URL `/ops`
#
# 출처: 정본 시안 `Moa_운영대시보드.svg`(24 승인 필요 · 25-1 주입 직후 · 25-2 복구 중 · 25-3 복구 완료 ·
#       26 정상 운영) · 05.리포트/운영대시보드_고도화안.md v0.2 (블록 A ~ F)
#
#   A 상태 배너      지금 괜찮은가 · 내가 할 일이 있는가 (승인 카드는 배너 안에서 펼친다)
#   B 핵심 수치 4칸  목표를 지키고 있는가 — 숫자 옆에 목표와 판정 글자
#   C 지금 요청 경로  게이트웨이 · 해석기 · 메뉴 저장소 신호등
#   D 장애 시험 결과  F-1 ~ F-6 × 회차 격자 (30초마다 다시 받는다)
#   E 최근 사건      행을 누르면 타임라인 막대 + 진단 · 전략 · 시도 · 검증
#   F 주문 처리 기록(접힘) · 장애 주입 도구줄(실습)
#
# 자료를 화면 자료로 바꾸는 일은 `moa/ops_view.py`(순수 함수)가 한다. 이 파일은 조회 · 버튼 · 펼침만 맡는다.
#
# 인증 — 화면 맨 앞에서 인증 플래그를 확인하고, 없으면 O-0 구성만 그리고 멈춘다.
# **사건 · 처리 기록 조회 API를 호출하지 않는다**(P-06 "운영 대시보드 인증 유지 · 차단").
import time

import streamlit as st

import api_client
from moa import api, components as c, copy, dialogs, icons, layout, mock, ops_view as ov, state

st.session_state["_page"] = "ops"
layout.page_setup()

# --- 미인증 직접 접근 — 대화상자 대신 같은 구성의 단독 화면 ------------------
if not st.session_state.get(state.OPS_AUTHED):
    layout.header(brand=None, ops_login=True)
    with st.container(key="moa_ops_login"):
        submitted, _, password = dialogs.ops_login_form("ops_login_standalone", with_cancel=False)
    if submitted:
        dialogs.try_ops_login(password)
        st.rerun()
    st.stop()   # 여기서 멈춘다 — 사건 조회 API 호출 0건

_ORDER_TONE = {"주문 확정": "green", "공유 완료": "green"}

TRIALS_EVERY_SEC = 30          # D 는 30초마다 — F-6(DB 장애) 시험 중 대시보드가 DB 를 두드리지 않게
_TRIALS_CACHE = "_ops_trials_cache"
_APPR_OPEN = "_ops_appr_open"
_ORD_LIST_OPEN = "_ops_orders_open"
_REC_OPEN = "_ops_record_open"        # T2-R 시험 기록 — 주문 처리 기록과 같은 접힘
_FAULT_ASK = "_fault_ask"
# 미리보기 전용 — [F-1] 을 누른 시각. 지난 초로 25-1 → 25-2 → 25-3 을 흉내 낸다
_PREVIEW_INJECT_AT = "_ops_preview_inject_at"
_PREVIEW_DETECT_SEC = 8
_PREVIEW_RECOVER_SEC = 41


# --- 미리보기 대역 (MOA_UI_PREVIEW=1) ------------------------------------------
def _preview_scene() -> dict:
    """정본 시안 상태 하나 — `_preview_case` 세션 값으로 고른다(화면 테스트가 쓴다)."""
    label = st.session_state.get("_preview_case") or "24 승인 필요"
    name = mock.OPS_PREVIEW_CASES.get(label, "approval")
    started = st.session_state.get(_PREVIEW_INJECT_AT)
    if started is None:
        return mock.ops_scene(name)
    sec = int(time.monotonic() - started)
    name = ("injected" if sec < _PREVIEW_DETECT_SEC
            else "recovering" if sec < _PREVIEW_RECOVER_SEC else "recovered")
    return mock.ops_scene(name, injected_sec=sec)


def _fill(data: dict, sample: dict, keys: tuple, label: str) -> dict:
    """서버 응답에 아직 없는 칸(G-1 · G-3 · G-4 등)을 미리보기에서만 견본으로 채운다.

    채웠다면 하단 개발 표시에 이름을 남긴다 — 화면이 동작하는 척하지 않게.
    미리보기가 아니면 채우지 않고 화면이 `미측정` · `상태 미수집` 으로 그린다.
    """
    if not mock.PREVIEW_ENABLED:
        return data
    missing = [k for k in keys if k not in data and k in sample]
    if not missing:
        return data
    api.preview_used().add(label)
    return {**data, **{k: sample[k] for k in missing}}


def _trials(ops_token, scene) -> dict | None:
    """D 장애 시험 결과 — 30초 묶음. 서버가 없으면 미리보기 견본, 그것도 없으면 None(빈 안내)."""
    cache = st.session_state.get(_TRIALS_CACHE)
    scene_key = scene["name"] if scene else None
    if cache and time.monotonic() - cache["at"] < TRIALS_EVERY_SEC and cache.get("scene") == scene_key:
        return cache["data"]
    try:
        data = api.get(lambda: api_client.ops_trials(ops_token=ops_token),
                       fallback=scene["trials"] if scene else None, label="ops_trials")
    except api_client.ApiError:
        data = None     # 시험 결과가 없어도 나머지 블록은 그린다
    st.session_state[_TRIALS_CACHE] = {"at": time.monotonic(), "data": data, "scene": scene_key}
    return data


def _drop_trials_cache() -> None:
    st.session_state.pop(_TRIALS_CACHE, None)


# --- A 상태 배너 ---------------------------------------------------------------
def _banner(b: dict, ops_token) -> None:
    approvals = b["approvals"]
    with st.container(key=f"moa_banner_{b['tone']}"):
        with st.container(key="moa_banrow", horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="center"):
            st.html(ov.banner_head_html(b))
            if approvals:
                opened = st.session_state.get(_APPR_OPEN, True)
                label = copy.OA_APPROVAL_TOGGLE.format(n=len(approvals))
                with st.container(key="card_appr_toggle", width="content"):
                    st.html(f'<div class="moa-ban-toggle{" moa-ban-toggle-open" if opened else ""}">'
                            f'{c.esc(label)}{icons.icon("chevron-down", 14)}</div>')
                    if st.button(label, key="hit_appr_toggle"):
                        st.session_state[_APPR_OPEN] = not opened
                        st.rerun()
            else:
                st.html(f'<div class="moa-ban-none">{c.esc(copy.OA_NO_APPROVAL)}</div>')

        if approvals and st.session_state.get(_APPR_OPEN, True):
            for approval in approvals:
                iid = approval["incident_id"]
                with st.container(key=f"moa_appr_{iid}", horizontal=True,
                                  horizontal_alignment="distribute", vertical_alignment="center"):
                    st.html(ov.approval_html(approval))
                    with st.container(key=f"appr_btns_{iid}", horizontal=True, width="content"):
                        if st.button(copy.O4_REJECT, key=f"rej_{iid}"):
                            _decide(iid, "reject", ops_token)
                        if st.button(copy.O4_APPROVE, key=f"apr_{iid}", type="primary"):
                            _decide(iid, "approve", ops_token)
        if b.get("alert"):
            st.html(ov.alert_html(b["alert"]))


def _decide(incident_id: str, decision: str, ops_token) -> None:
    """API-044 — 첫 결정만 반영된다. 이미 결정된 사건은 409다."""
    data = api.act(lambda: api_client.ops_decide(incident_id, decision, ops_token=ops_token),
                   fallback={"user_message": f'{incident_id} {"승인함" if decision == "approve" else "거부함"}'},
                   label="ops_decision")
    if mock.PREVIEW_ENABLED and not data.get("_error"):
        st.session_state["_preview_case"] = "26 정상 운영"
    _drop_trials_cache()
    st.toast(data.get("user_message", ""))
    st.rerun()


# --- E 최근 사건 -----------------------------------------------------------------
# 목록은 10줄까지 보이고 나머지는 상자 안에서 스크롤한다.
# 사건이 쌓일수록 아래 블록이 화면 밖으로 밀려나 "구역 3 이 어디 갔나"가 되기 때문이다.
_LIST_ROWS = 10


def _ms_text(value) -> str:
    """ms 는 초로 바꿔 읽는다 — 화면 전체가 초를 쓴다(헤더 "시간은 ms를 초로 환산")."""
    if value is None:
        return "—"
    return f"{float(value) / 1000:.1f}초"


def _scroll_box(key: str, count: int, row_px: int):
    """줄 수가 상한을 넘을 때만 높이를 묶는다 — 몇 줄뿐인데 빈 스크롤 상자를 만들지 않는다."""
    if count <= _LIST_ROWS:
        return st.container(key=key)
    return st.container(key=key, height=_LIST_ROWS * row_px)


def _incidents(incidents: dict, ops_token, scene) -> None:
    with st.container(key="moa_inc_card"):
        st.html(f'<div class="moa-ops-title"><u class="moa-bbadge">E</u>'
                f'<b>{c.esc(copy.OE_TITLE)}</b>'
                f'<span>{c.esc(copy.OE_HINT)}</span></div>')
        rows = ov.incident_rows(incidents)
        if not rows:
            st.html(f'<div class="moa-ops-empty">{c.esc(copy.OE_EMPTY)}</div>')
        opened = st.session_state.get(state.OPS_INCIDENT_OPEN)
        with _scroll_box("moa_inc_scroll", len(rows), 56):
            for row in rows:
                iid = row["id"]
                with st.container(key=f"card_inc_{iid}"):
                    st.html(ov.incident_row_html(row, opened == iid))
                    if st.button(f"{iid} 자세히 보기", key=f"hit_inc_{iid}"):
                        st.session_state[state.OPS_INCIDENT_OPEN] = None if opened == iid else iid
                        st.rerun()
                if opened == iid:
                    sample = (scene or {}).get("details", {}).get(iid) or mock.OPS_INCIDENT_DETAIL.get(iid)
                    detail = api.get(lambda: api_client.ops_incident(iid, ops_token=ops_token),
                                     fallback=sample, label="ops_incident")
                    st.html(ov.incident_detail_html(detail) if detail else
                            f'<div class="moa-ops-empty">{c.esc(copy.OE_DETAIL_PENDING)}</div>')


# --- F 주문 처리 기록 (접힘) ------------------------------------------------------
def _order_detail(order_id: str, detail: dict) -> str:
    """케이스 23 — 시각 · 누구 · 전 → 후 · 방법. 전은 회색, 후는 진하게."""
    head = "".join(f'<span>{c.esc(col)}</span>' for col in copy.O6_DETAIL_COLUMNS)
    rows = []
    for ch in detail.get("changes", []):
        before, sep, after = str(ch.get("before_after", "")).partition(" → ")
        change = (f'<span class="moa-det-before">{c.esc(before)}</span> → '
                  f'<span class="moa-det-after">{c.esc(after)}</span>' if sep else c.esc(before))
        rows.append(f'<div class="moa-tr moa-det-grid"><span class="moa-td moa-num">{c.esc(ch.get("at_text"))}</span>'
                    f'<span class="moa-det-who">{c.esc(ch.get("who"))}</span>'
                    f'<span class="moa-td">{change}</span>'
                    f'<span><span class="moa-method">{c.esc(ch.get("method_text"))}</span></span></div>')
    shares = detail.get("share_history") or []
    share_html = ""
    if shares:
        # API-048 share_history — 언제 어떻게 공유(= 완료)했는가
        share_html = (f'<div class="moa-step-note" style="padding:8px 0">{c.esc(copy.O6_SHARE_LABEL)} · '
                      + " · ".join(f'{c.esc(sh.get("at_text"))} {c.esc(sh.get("method_text"))}' for sh in shares)
                      + "</div>")
    # 관리 링크 · 토큰 · 원문 대화 전문은 보여주지 않는다 (NFR-003)
    return (f'<div class="moa-detail"><div class="moa-detail-title">{c.esc(order_id)} 상세</div>'
            f'<div class="moa-tr moa-th moa-det-grid">{head}</div>{"".join(rows)}{share_html}</div>')


def _orders_toggle(orders: dict) -> None:
    opened = st.session_state.get(_ORD_LIST_OPEN, False)
    count = len(orders.get("orders") or [])
    with st.container(key="moa_ord_card"):
        with st.container(key="card_ord_toggle"):
            st.html(f'<div class="moa-ord-toggle"><u class="moa-bbadge">F</u>'
                    f'<b>{c.esc(copy.O6_TITLE)}</b>'
                    f'<span>{c.esc(copy.OF_ORDERS_COUNT.format(n=count))}</span>'
                    f'<i class="moa-chev{" moa-chev-up" if opened else ""}">{icons.icon("chevron-down", 16)}</i></div>')
            if st.button(f"{copy.O6_TITLE} 펼치기", key="hit_ord_toggle"):
                st.session_state[_ORD_LIST_OPEN] = not opened
                st.rerun()


def _orders_body(orders: dict, ops_token) -> None:
    """펼치면 기존 O-6 표 그대로 — 조회 전용이라 주 버튼이 없다."""
    with st.container(key="moa_ord_body"):
        with st.container(key="moa_ord_head", horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="center"):
            st.html(f'<div class="moa-ops-title"><b>{c.esc(copy.O6_TITLE)}</b></div>')
            query = st.text_input(copy.O6_SEARCH_HINT, placeholder=copy.O6_SEARCH_HINT,
                                  key="ops_order_q", label_visibility="collapsed", width=300,
                                  icon=":material/search:")
        head = "".join(f"<span>{c.esc(col)}</span>" for col in copy.O6_COLUMNS) + "<span></span>"
        st.html(f'<div class="moa-tr moa-th moa-ord-grid">{head}</div>')
        # 찾기는 받은 목록을 글자로 거르는 것까지다 — 조회 조건을 화면이 만들지 않는다
        order_rows = [o for o in orders.get("orders") or []
                      if not query or query in " ".join(str(v) for v in o.values())]
        opened_order = st.session_state.get(state.OPS_ORDER_OPEN)
        box = _scroll_box("moa_ord_scroll", len(order_rows), 46)
        for o in order_rows:
            oid = o["order_id"]
            state_text = o.get("state_text", "")
            with box, st.container(key=f"card_ord_{oid}"):
                st.html(f'<div class="moa-tr moa-ord-grid"><span class="moa-td-id">{c.esc(oid)}</span>'
                        f'<span class="moa-td moa-td-plain">{c.esc(o.get("brand_label"))}</span>'
                        f'<span>{c.pill(state_text, _ORDER_TONE.get(state_text, "gray"))}</span>'
                        f'<span class="moa-td moa-td-plain moa-num">{c.esc(o.get("confirmed_at_text"))}</span>'
                        f'<span class="moa-td moa-td-plain">{c.esc(o.get("change_count_text"))}</span>'
                        f'<span class="moa-td moa-td-plain">{c.esc(o.get("recovery_count_text"))}</span>'
                        f'<span class="moa-chev">{icons.icon("chevron-down", 16)}</span></div>')
                if st.button(f"{oid} 자세히 보기", key=f"hit_ord_{oid}"):
                    st.session_state[state.OPS_ORDER_OPEN] = None if opened_order == oid else oid
                    st.rerun()
            if opened_order == oid:
                detail = api.get(lambda: api_client.ops_order(oid, ops_token=ops_token),
                                 fallback=mock.OPS_ORDER_DETAIL.get(oid, {"changes": []}),
                                 label="ops_order")
                with box:
                    st.html(_order_detail(oid, detail))


# --- T1 도구 측정 — 정본 시안 `Moa_운영대시보드.svg` T1 카드 ---------------------
# 타일 6개(2행 x 3열) + 상태 줄 하나. 상태 줄은 세 모습을 돌아가며 쓴다:
#   안내   고른 유형이 무엇을 하는지
#   확인   [주입] [그만두기]
#   유지 중 경과 초 + [해제]
_FAULT_PICK = "_fault_pick"


def _fault_chips(where: str, chips) -> str:
    inner = "".join(f'<span><i>{c.esc(k)}</i>{c.esc(v)}</span>' for k, v in chips)
    return f'<div class="moa-zwhere"><em>{c.esc(where)}</em>{inner}</div>'


def _fault_tiles(active: str | None, locked: bool) -> None:
    """장애 유형 타일. 지금 주입 중인 유형은 검게 채운다(시안 25-1)."""
    for row in (copy.O5_FAULT_CODES[:3], copy.O5_FAULT_CODES[3:]):
        with st.container(key=f"moa_ftiles_{row[0]}", horizontal=True, gap="small"):
            for code in row:
                label = f"{code}  \n{copy.FAULT_NAMES[code]}"
                key = f"fault_{code}_on" if code == active else f"fault_{code}"
                if st.button(label, key=key, disabled=locked):
                    # 누르면 고른 유형을 상태 줄에 띄우고 확인을 받는다
                    st.session_state[_FAULT_PICK] = code
                    st.session_state[_FAULT_ASK] = code
                    st.rerun()


def _fault_strip(active: dict | None, ops_token, locked_target: str | None) -> None:
    """상태 줄 — **확인을 가장 먼저 본다.**

    주입 중 상태를 먼저 보면 [해제]를 눌러도 확인 줄이 뜨지 않아 **영영 해제되지 않는다**
    (2026-11-07 실서버에서 실제로 그랬다). 사람이 방금 누른 것이 지금 상태보다 앞선다.
    """
    asking = st.session_state.get(_FAULT_ASK)
    code = (active or {}).get("code")
    if asking:                    # 확인 — 실행 전 한 번 묻는다
        clearing = asking == "clear"
        text = copy.OT1_CLEAR_ASK if clearing else copy.OT1_HELP.get(asking, "")
        badge = copy.O5_FAULT_CLEAR if clearing else asking
        with st.container(key="moa_fstrip_ask", horizontal=True, vertical_alignment="center"):
            st.html(f'<div class="moa-fstrip moa-fstrip-ask"><i>{c.esc(badge)}</i>'
                    f'<span>{c.esc(text)}</span></div>')
            ok = copy.O5_FAULT_CLEAR if clearing else copy.OT1_CONFIRM
            if st.button(ok, key="fault_yes", type="primary"):
                _fault_run(asking, clearing, ops_token)
            if st.button(copy.OT1_CANCEL, key="fault_no"):
                st.session_state[_FAULT_ASK] = None
                st.rerun()
        return
    if locked_target and not code:
        st.html(f'<div class="moa-fstrip moa-fstrip-lock">'
                f'{c.esc(copy.OT1_LOCKED.format(target=locked_target))}</div>')
        return
    if code:                      # 주입 중 — 경과 초와 [해제]
        sec = int(active.get("elapsed_sec") or 0)
        head = copy.OT1_HOLDING_HEAD.format(code=code, name=copy.FAULT_NAMES.get(code, code),
                                            target=copy.OT1_TARGET.get(code, ""))
        with st.container(key="moa_fstrip_live", horizontal=True, vertical_alignment="center"):
            st.html(f'<div class="moa-flive">'
                    f'<div class="moa-fstrip moa-fstrip-live"><b>{c.esc(head)}</b>'
                    f'<span>{c.esc(copy.OT1_HOLDING)}</span></div>'
                    f'<div class="moa-fsec">{sec}<i>초</i></div></div>')
            if st.button(copy.O5_FAULT_CLEAR, key="fault_clear"):
                st.session_state[_FAULT_ASK] = "clear"
                st.rerun()
        return
    pick = st.session_state.get(_FAULT_PICK) or copy.O5_FAULT_CODES[0]
    st.html(f'<div class="moa-fstrip"><i>{c.esc(pick)}</i>'
            f'<span>{c.esc(copy.OT1_HELP.get(pick, ""))}</span></div>')


def _fault_run(asking: str, clearing: bool, ops_token) -> None:
    label = copy.O5_FAULT_CLEAR if clearing else asking
    data = api.act(
        lambda: api_client.ops_fault(None if clearing else asking,
                                     "clear" if clearing else "inject", ops_token=ops_token),
        fallback={"user_message": f"{label} 처리했어요"}, label="ops_fault")
    if mock.PREVIEW_ENABLED and not data.get("_error"):
        if clearing:
            st.session_state.pop(_PREVIEW_INJECT_AT, None)
            st.session_state["_preview_case"] = "26 정상 운영"
        elif asking == "F-1":
            st.session_state[_PREVIEW_INJECT_AT] = time.monotonic()
    st.session_state[_FAULT_ASK] = None
    _drop_trials_cache()
    st.toast(data.get("user_message", ""))
    st.rerun()


# --- 5초 주기 부분 갱신 (P-06 Streamlit 구현 대응 "운영 대시보드 자동 갱신") ---
# 조회 · 렌더를 한 조각(fragment)에 담아 5초마다 이 부분만 다시 그린다. D 만 30초 묶음이다.
@st.fragment(run_every=5)
def dashboard() -> None:
    ops_token = st.session_state.get(state.OPS_TOKEN)
    scene = _preview_scene() if mock.PREVIEW_ENABLED else None
    sample = scene or mock.ops_scene("approval")

    try:
        status = api.get(lambda: api_client.ops_status(ops_token=ops_token),
                         fallback=sample["status"], label="ops_status")
        incidents = api.get(lambda: api_client.ops_incidents(ops_token=ops_token),
                            fallback=sample["incidents"], label="ops_incidents")
        metrics = api.get(lambda: api_client.ops_metrics(ops_token=ops_token),
                          fallback=sample["metrics"], label="ops_metrics")
        orders = api.get(lambda: api_client.ops_orders(ops_token=ops_token),
                         fallback=mock.OPS_ORDERS, label="ops_orders")
    except api_client.ApiError as err:
        # 5초마다 다시 묻는 조각이다 — 서버가 돌아오면 다음 갱신에서 저절로 채워진다
        layout.fetch_failed(err, ops_mode=True)

    # 서버에 아직 없는 칸 — 고도화안 9절 G-1(복제본) · 무결성 · 주입 진행 · 추세 경보
    status = _fill(status, sample["status"], ("replicas", "active_injection", "alerts", "probe_need"),
                   "ops_status+")
    detail = _fill(metrics.get("detail") or {}, sample["metrics"]["detail"],
                   ("integrity_issue_count", "mttr_max_sec"), "ops_metrics+")
    metrics = {**metrics, "detail": detail}
    st.session_state["_ops_status"] = status
    trial_sum = ov.trial_summary(_trials(ops_token, scene))

    # --- 헤더 — 갱신 표시 · 실습 환경 배지 · 오른쪽 끝 [나가기] ------------------
    layout.header(brand=None, ops_mode=True)

    with st.container(key="moa_ops"):
        _banner(ov.banner(status, incidents), ops_token)                  # A
        st.html(ov.kpis_html(ov.kpis(metrics, trial_sum)))                 # B
        with st.container(key="moa_ops_mid", horizontal=True):
            with st.container(key="moa_route_card"):                       # C
                st.html(ov.route_html(ov.route(status)))
            with st.container(key="moa_trial_card"):                       # D
                st.html(ov.trials_html(trial_sum))
        _incidents(incidents, ops_token, scene)                            # E
        with st.container(key="moa_ops_foot", horizontal=True, vertical_alignment="center"):
            _orders_toggle(orders)                                         # F · 구역 2
        if st.session_state.get(_ORD_LIST_OPEN, False):
            _orders_body(orders, ops_token)

    layout._preview_notice()


# =============================================================================
# 장애 시험 워크플로 (origin/dev #123 feature/auto-healing 에서 이식)
# 운영 대시보드 하단에 두 번째 5초 fragment 로 붙는다 — 위 A~F 섹션과 독립이다.
# 상태는 wf_* 세션 키만 쓰므로 A~F 의 키와 겹치지 않는다.
# =============================================================================

_FAULT_GUIDE = {
    "F-1": ("해석 API 오류", "해석 API가 서버 오류(5xx)를 반환하는 상황입니다.",
            "오류를 감지하고 반대쪽 해석기로 최대 1회 전환한 뒤 결과를 확인합니다."),
    "F-2": ("해석 API 지연", "해석 응답이 5초를 넘는 상황입니다.",
            "지연을 감지해 반대쪽으로 전환합니다. 전체 처리 상한은 15초입니다."),
    "F-3": ("앱 backend 중단", "A 또는 B 앱 backend가 응답하지 않는 상황입니다.",
            "Gateway에서 제외 → backend 재시작 → 상태와 기능 확인 순서로 진행합니다."),
    "F-4": ("반복 장애와 회로 차단", "해석기 오류가 반복되어 회로 차단기가 열리는 상황입니다.",
            "3회 실패 → 60초 차단 → 단일 probe를 확인합니다. probe 실패는 사람 확인으로 넘깁니다."),
    "F-5": ("잘못된 에이전트 출력", "결과가 정해진 출력 형식이나 계약을 지키지 않는 상황입니다.",
            "최대 2회 다시 생성하고, 계속 실패하면 NEEDS_REVIEW로 넘깁니다."),
    "F-6": ("메뉴 조회 실패와 캐시 폴백", "메뉴 저장소 조회가 실패하는 상황입니다.",
            "같은 브랜드·버전·checksum의 캐시로 처리되는지 확인합니다. 안전한 캐시가 없으면 실패 처리합니다."),
}
_TARGET_LABELS = {
    "order_interpreter_primary": "주문 해석기 기본(primary)",
    "order_interpreter_backup": "주문 해석기 대체(backup)",
    "moa-a-backend-1": "앱 A backend",
    "moa-b-backend-1": "앱 B backend",
    "menu_store": "메뉴 저장소 조회",
}
# `quick` 은 화면 선택지에서 내렸다. 지난 기록 · 결과보고서가 읽히도록 라벨만 남긴다 (v0.3 7절)
_MODE_LABELS = {"quick": "직접 설정 · 선택 장애 1회",
                "custom": "직접 설정 · 선택 장애 1~10회",
                "full": "전체 복구 측정 · 25회 (F-4 제외)"}
_STATUS_LABELS = {
    "queued": "대기 중", "preflight": "환경 확인 중", "running": "실행 중",
    "stop_requested": "중지 처리 중", "cleaning": "장애 정리 중",
    "completed": "실행 종료", "stopped": "중지됨", "failed": "실행 실패",
    "needs_operator_action": "운영자 확인 필요", "invalid": "환경 확인 실패",
    "PASS": "예상대로 통과", "FAIL": "예상과 다름", "NEEDS_REVIEW": "사람 확인 필요",
    "AWAITING_APPROVAL": "승인 대기", "INVALID": "실행 전 조건 미충족",
}
_PHASE_LABELS = {
    "queued": "대기", "preflight": "환경 확인", "fault_injection": "장애 주입",
    "injecting": "장애 주입", "detecting": "장애 감지", "recovering": "자동 복구",
    "verifying": "복구 결과 확인", "cooldown_probe": "차단 후 단일 확인",
    "cleanup": "장애 정리", "stop_requested": "중지·정리",
}


def _report_escape(value):
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _workflow_report_markdown(run: dict) -> str:
    """저장된 실행 정보를 사람이 읽을 수 있는 Markdown 보고서로 만든다."""
    summary = run.get("summary") or {}
    mode = run.get("mode")
    title = "통합 장애 시험 결과보고서" if mode == "full" else "장애 시험 결과보고서"
    lines = [
        f"# {title}", "",
        f"- 실행 ID: {_report_escape(run.get('run_id'))}",
        f"- 시험 방식: {_report_escape(_MODE_LABELS.get(mode, mode))}",
        f"- 상태: {_report_escape(_STATUS_LABELS.get(run.get('status'), run.get('status')))}",
        f"- 시작: {_report_escape(run.get('started_at'))}",
        f"- 종료: {_report_escape(run.get('finished_at'))}",
        f"- 결과: 통과 {summary.get('pass', 0)} / 실패 {summary.get('fail', 0)} / "
        f"사람 확인 {summary.get('needs_review', 0) + summary.get('awaiting_approval', 0)} / "
        f"전체 {summary.get('total', run.get('total_iterations', 0))}",
        f"- 평균 감지 시간: {summary.get('avg_mttd_ms') if summary.get('avg_mttd_ms') is not None else '—'} ms",
        f"- 평균 복구 시간: {summary.get('avg_mttr_ms') if summary.get('avg_mttr_ms') is not None else '—'} ms",
        "", "> ‘실행 종료’는 장애 시험 절차가 끝났다는 뜻입니다. 복구 성공은 회차별 판정으로 확인합니다.",
        "", "## 회차별 결과", "",
        "| 회차 | 장애 유형 | 대상 | 판정 | 감지(ms) | 복구(ms) | 시도 횟수 | 결과 설명 |",
        "|---:|---|---|---|---:|---:|---:|---|",
    ]
    for item in run.get("iterations") or []:
        detail = item.get("result") or {}
        if not isinstance(detail, dict):
            detail = {}
        fault = item.get("fault_type", detail.get("fault_type", "—"))
        target = item.get("target", detail.get("target", "—"))
        status = item.get("status", detail.get("status", "—"))
        label = _FAULT_GUIDE.get(fault, (fault, "", ""))[0]
        lines.append(
            f"| {item.get('iteration_no', '—')} | {_report_escape(fault)} {_report_escape(label)} "
            f"| {_report_escape(_TARGET_LABELS.get(target, target))} "
            f"| {_report_escape(_STATUS_LABELS.get(status, status))} "
            f"| {item.get('mttd_ms') if item.get('mttd_ms') is not None else '—'} "
            f"| {item.get('mttr_ms') if item.get('mttr_ms') is not None else '—'} "
            f"| {len(detail.get('attempts') or [])} | {_report_escape(detail.get('detail'))} |"
        )
        for step in detail.get("steps") or []:
            lines.extend([
                "", f"### {item.get('iteration_no', '—')}회차 진행 단계", "",
                "| 단계 | 예상 흐름 | 실제 결과 | 걸린 시간(ms) | 판정 |",
                "|---|---|---|---:|---|",
            ])
            break
        for step in detail.get("steps") or []:
            lines.append(
                f"| {_report_escape(step.get('name'))} | {_report_escape(step.get('expected'))} "
                f"| {_report_escape(step.get('actual'))} | {step.get('duration_ms', '—')} "
                f"| {_report_escape(_STATUS_LABELS.get(step.get('status'), step.get('status')))} |"
            )
        attempts = detail.get("attempts") or []
        if attempts:
            lines.extend([
                "", f"### {item.get('iteration_no', '—')}회차 복구 시도", "",
                "| 시도 | 대상/액션 | 결과 | 확인 반복 | 걸린 시간(ms) |",
                "|---:|---|---|---:|---:|",
            ])
            for index, attempt in enumerate(attempts, start=1):
                lines.append(
                    f"| {attempt.get('attempt_no', index)} "
                    f"| {_report_escape(attempt.get('service') or attempt.get('action') or attempt.get('stage'))} "
                    f"| {_report_escape(attempt.get('outcome') or attempt.get('result'))} "
                    f"| {attempt.get('poll_count', attempt.get('request_attempt_no', '—'))} "
                    f"| {attempt.get('duration_ms', '—')} |"
                )
    lines.extend(["", "## 시간순 진행 기록", "", "| 시각 | 진행 내용 |", "|---|---|"])
    for event in run.get("events") or []:
        lines.append(
            f"| {_report_escape(event.get('created_at'))} "
            f"| {_report_escape(event.get('summary'))} · {_report_escape(event.get('details'))} |"
        )
    return "\n".join(lines) + "\n"


# --- 알림 한 지점 — 시안의 상태 색을 쓴다 ----------------------------------------
# Streamlit 기본 st.error · st.success · st.info 는 파랑 · 빨강 상자를 그려 화면의
# 나머지와 따로 논다. 정본 시안에는 그런 상자가 없다 — 상태는 **글자 색과 옅은 바탕**으로 말한다.
def _notice(text: str, tone: str = "muted") -> None:
    """tone: muted(안내) · green(통과) · red(실패) · amber(주의)."""
    if not text:
        return
    st.html(f'<div class="moa-note moa-note-{tone}">{c.esc(text)}</div>')


def _subnote(text: str) -> None:
    """한 단 작은 보조 설명 — 기존 st.caption 자리."""
    if text:
        st.html(f'<div class="moa-subnote">{c.esc(text)}</div>')


def _render_preflight(check: dict) -> None:
    checks = check.get("checks") or []
    failed = [item for item in checks if not item.get("ready")]
    if not failed and checks:
        _notice("시험에 필요한 환경을 모두 확인했어요 · 장애는 아직 주입하지 않았어요", "green")
    elif failed:
        _notice(f"시작 전에 확인할 항목이 {len(failed)}개 있어요 · 장애는 주입하지 않았어요", "red")
    else:
        _notice("점검 결과를 받지 못했어요 · 서버 연결 상태를 확인해 주세요", "amber")
    labels = {
        "lab 환경": ("실습 환경", "장애 시험은 lab 환경에서만 실행할 수 있습니다."),
        "DB 연결": ("데이터베이스 연결", "DB 서비스와 연결 설정을 확인하세요."),
        "workflow 스키마": ("시험 기록 테이블", "workflow 스키마가 적용됐는지 확인하세요."),
        "healing worker": ("자동 복구 작업기", "healing 서비스와 최근 heartbeat를 확인하세요."),
        "Docker proxy·allowlist": ("안전한 컨테이너 제어 연결", "socket-proxy 연결과 허용목록을 확인하세요."),
        "backend allowlist": ("A/B backend 허용목록", "두 backend가 복구 작업기 허용목록에 있는지 확인하세요."),
        "Gateway": ("트래픽 Gateway", "Gateway 서비스 상태를 확인하세요."),
        "interpreter primary": ("기본 해석 서비스", "기본 해석 서비스의 /health 상태를 확인하세요."),
        "interpreter backup": ("대체 해석 서비스", "대체 해석 서비스의 /health 상태를 확인하세요."),
        "A backend": ("앱 A backend", "앱 A의 /ready 상태를 확인하세요."),
        "B backend": ("앱 B backend", "앱 B의 /ready 상태를 확인하세요."),
        "A frontend": ("앱 A 화면 서비스", "앱 A frontend 상태를 확인하세요."),
        "B frontend": ("앱 B 화면 서비스", "앱 B frontend 상태를 확인하세요."),
        "F-3 allowlisted target": ("F-3 대상 권한", "선택한 backend가 허용목록에 있는지 확인하세요."),
    }
    rows = []
    for item in checks:
        title, guidance = labels.get(item.get("name"),
                                     (item.get("name", "점검 항목"), "세부 상태를 확인하세요."))
        ready = bool(item.get("ready"))
        # 이모지를 쓰지 않는다 — 판정은 B 수치 카드와 같은 알약으로 말한다
        mark = ("pass", "준비됨") if ready else ("fail", "확인 필요")
        detail = item.get("detail") if ready else guidance
        extra = (f'<u>{c.esc(item["detail"])}</u>'
                 if not ready and item.get("detail") else "")
        rows.append(f'<div class="moa-pfrow">'
                    f'<span class="moa-verdict moa-verdict-{mark[0]}">{c.esc(mark[1])}</span>'
                    f'<b>{c.esc(title)}</b><span>{c.esc(detail or "")}</span>{extra}</div>')
    if rows:
        st.html(f'<div class="moa-pflist">{"".join(rows)}</div>')


# --- 구역 3 · T2 복구 시험 — 정본 시안 T2 카드 ---------------------------------
# 대시보드(구역 1 · 2)를 다 그린 **뒤**에 부른다. 전에는 이 블록이 모듈 맨 위에서
# 먼저 그려져 화면 최상단을 실습 도구가 차지했다.
_T2_TARGETS = {
    "F-1": ["order_interpreter_primary", "order_interpreter_backup"],
    "F-2": ["order_interpreter_primary", "order_interpreter_backup"],
    "F-3": ["moa-a-backend-1", "moa-b-backend-1"],
    "F-4": ["order_interpreter_primary", "order_interpreter_backup"],
    "F-5": ["order_interpreter_primary", "order_interpreter_backup"],
    "F-6": ["menu_store"],
}
_T2_SHORT = {"order_interpreter_primary": "해석기 1차", "order_interpreter_backup": "대체",
             "moa-a-backend-1": "앱 A", "moa-b-backend-1": "앱 B", "menu_store": "메뉴 저장소"}


def _block_head(badge, title, ask="", note="", aside=""):
    """블록 배지 + 제목 + 보조 질문 칩 + 부제 (시안 2026-11-07 판 공통 머리).

    `aside` 는 제목과 같은 줄에 붙는 회색 한 줄이다(T2-R 의 "예전 기록은 ..." 처럼).
    `note` 는 머리 아래 줄이다. 둘을 섞지 않는다 — 시안이 자리를 달리 쓴다.
    """
    chip = f'<em>{c.esc(ask)}</em>' if ask else ""
    side = f'<u>{c.esc(aside)}</u>' if aside else ""
    sub = f'<div class="moa-bnote">{c.esc(note)}</div>' if note else ""
    return f'<div class="moa-bhead"><i>{c.esc(badge)}</i><b>{c.esc(title)}</b>{chip}{side}</div>{sub}'


def _t2_pick(keyname, chosen, title, note):
    """범위 타일 — 선택 상태는 버튼 key 끝의 `_on` 으로 CSS 가 칠한다."""
    key = f"{keyname}_on" if chosen else keyname
    return st.button(f"{title}  \n{note}", key=key)


def _t2_target(fault_type):
    """대상 세그먼트. 고르는 축은 여기에만 있다 — T1 은 고정이다(고도화안 v0.3 6절)."""
    options = _T2_TARGETS[fault_type]
    chosen = st.session_state.get(f"wf_target_{fault_type}") or options[0]
    if len(options) == 1:
        st.html(f'<div class="moa-t2one">{c.esc(_TARGET_LABELS.get(options[0], options[0]))}</div>')
        return options[0]
    with st.container(key="moa_t2_seg", horizontal=True, gap=None):
        for value in options:
            key = f"t2seg_{fault_type}_{value}"
            if st.button(_T2_SHORT.get(value, value), key=f"{key}_on" if value == chosen else key,
                         help=_TARGET_LABELS.get(value, value)):
                st.session_state[f"wf_target_{fault_type}"] = value
                st.rerun()
    return chosen


def trial_panel():
    """측정하는 쪽. 회차 · 대상 · 유지 시간을 정하고 워크플로가 정리까지 맡는다."""
    ops_token = st.session_state.get(state.OPS_TOKEN)
    st.html(_block_head(copy.OT2_BADGE, copy.OT2_TITLE, copy.OT2_ASK, copy.OT2_NOTE))

    mode = st.session_state.get("wf_mode") or "custom"
    with st.container(key="moa_t2_modes", horizontal=True, gap="small"):
        for value, (title, note) in copy.OT2_MODES.items():
            if _t2_pick(f"wf_mode_{value}", value == mode, title, note):
                st.session_state["wf_mode"] = value
                st.rerun()

    repetitions = 1
    if mode == "full":
        fault_type, target = None, None
        st.html(f'<div class="moa-t2fixed">{c.esc(copy.OT2_FULL_FIXED)}</div>')
    else:
        with st.container(key="moa_t2_pickrow", horizontal=True):
            with st.container(key="moa_t2_fault"):
                st.html(f'<div class="moa-flabel">{c.esc(copy.OT2_L_FAULT)}</div>')
                fault_type = st.selectbox(
                    copy.OT2_L_FAULT, list(_FAULT_GUIDE), key="wf_fault", label_visibility="collapsed",
                    format_func=lambda code: f"{code}  {copy.FAULT_NAMES.get(code, code)}")
            with st.container(key="moa_t2_target"):
                st.html(f'<div class="moa-flabel">{c.esc(copy.OT2_L_TARGET)}</div>')
                target = _t2_target(fault_type)
        st.html(f'<div class="moa-t2guide"><i>{c.esc(fault_type)}</i>'
                f'<span>{c.esc(_FAULT_GUIDE[fault_type][2])}</span></div>')

    with st.container(key="moa_t2_numrow", horizontal=True):
        with st.container(key="moa_t2_rep"):
            st.html(f'<div class="moa-flabel">{c.esc(copy.OT2_L_REPEAT)}</div>')
            if mode == "custom":
                repetitions = st.number_input(copy.OT2_L_REPEAT, min_value=1, max_value=10, value=1,
                                              step=1, key="wf_repetitions",
                                              label_visibility="collapsed")
            else:
                st.html('<div class="moa-t2fixnum">5유형 x 5회</div>')
        with st.container(key="moa_t2_sec"):
            st.html(f'<div class="moa-flabel">{c.esc(copy.OT2_L_SECONDS)}</div>')
            injection_seconds = st.slider(copy.OT2_L_SECONDS, min_value=1, max_value=120, value=1,
                                          step=1, key="wf_seconds", label_visibility="collapsed",
                                          format="%d초")

    selection = (mode, fault_type, target, int(repetitions), int(injection_seconds))

    # 알림은 버튼 줄 **밖**에 그린다 — 줄 안에 넣으면 두 버튼 사이에 끼어 버린다
    failed_text = None
    with st.container(key="moa_t2_btns", horizontal=True, vertical_alignment="center"):
        if st.button(copy.OT2_PREFLIGHT, key="wf_preflight"):
            try:
                check = api_client.ops_workflow_preflight(mode=mode, fault_type=fault_type or "F-1",
                                                          target=target, ops_token=ops_token)
                st.session_state["wf_preflight_result"] = check
                st.session_state["wf_preflight_selection"] = selection
            except api_client.ApiUnreachable:
                failed_text = copy.OT2_PF_OFFLINE
            except api_client.ApiError as error:
                failed_text = error.user_message or copy.OT2_PF_FAILED
        start = st.button(copy.OT2_START, type="primary", key="wf_start")
    _notice(failed_text, "red")

    prior_check = st.session_state.get("wf_preflight_result")
    if prior_check and not start:
        if st.session_state.get("wf_preflight_selection") == selection:
            with st.expander("최근 점검 결과", expanded=False):
                _render_preflight(prior_check)
        else:
            _subnote("선택을 바꿨어요 · 이전 점검 결과는 시작 조건으로 쓰지 않아요")

    if start:
        _t2_start(mode, fault_type, target, repetitions, injection_seconds, selection, ops_token)

    st.html(_fault_chips(copy.OT2_WHERE, copy.OT2_CHIPS))


def _t2_start(mode, fault_type, target, repetitions, injection_seconds, selection, ops_token):
    """환경을 다시 확인하고 시작한다 — 점검과 시작 사이에 환경이 바뀔 수 있다."""
    try:
        check = api_client.ops_workflow_preflight(mode=mode, fault_type=fault_type or "F-1",
                                                  target=target, ops_token=ops_token)
        st.session_state["wf_preflight_result"] = check
        st.session_state["wf_preflight_selection"] = selection
        _render_preflight(check)
        if check.get("ready"):
            import uuid
            run = api_client.ops_workflow_create(
                mode=mode, fault_type=fault_type or "F-1", target=target,
                repetitions=int(repetitions), injection_seconds=int(injection_seconds),
                idempotency_key=str(uuid.uuid4()), ops_token=ops_token)
            if run and run.get("run_id"):
                st.session_state["wf_active_run"] = run["run_id"]
                st.session_state["wf_selected_run"] = run["run_id"]
                # 선택 상자는 **자기 상태**를 지키므로 index 를 바꿔도 따라오지 않는다.
                # 방금 시작한 실행을 보여 달라는 표시를 남겨 둔다(아래 workflow_results 가 쓴다)
                st.session_state["wf_focus_run"] = run["run_id"]
                st.session_state[_REC_OPEN] = True      # 방금 시작한 실행을 바로 볼 수 있게
                _notice(f"시험 기록 {run['run_id']}을 저장하고 실행 대기열에 넣었어요", "green")
                st.rerun()
            elif run and run.get("preflight"):
                st.session_state["wf_preflight_result"] = run["preflight"]
                _notice("시작 직전에 환경 상태가 바뀌었어요 · 장애는 주입하지 않았어요", "red")
                _render_preflight(run["preflight"])
            else:
                _notice("시험이 시작되지 않았고 기록도 만들어지지 않았어요", "red")
    except api_client.ApiError as error:
        _notice(error.user_message or "시험을 시작하지 못했어요", "red")
        if error.detail:
            with st.expander("기술 세부 정보"):
                st.code(str(error.detail))


@st.fragment(run_every=5)
def workflow_results():
    ops_token = st.session_state.get(state.OPS_TOKEN)
    try:
        recent_response = api_client.ops_workflows(ops_token=ops_token)
    except api_client.ApiUnreachable:
        # 연결 실패 — 빨간 경고가 아니라 지금 상태를 말한다. 기록이 사라진 것이 아니다
        st.html(f'<div class="moa-ops-empty">{c.esc(copy.OT2R_OFFLINE)}</div>')
        return
    except api_client.ApiError as error:
        # 서버가 답은 했다 — 서버가 준 문장을 그대로 쓴다 (D-02 2.2)
        st.html(f'<div class="moa-ops-empty">'
                f'{c.esc(error.user_message or copy.OT2R_OFFLINE)}</div>')
        return
    recent = recent_response.get("runs", []) if isinstance(recent_response, dict) else []
    if not recent:
        st.html(f'<div class="moa-ops-empty">{c.esc(copy.OT2R_EMPTY)}</div>')
        return

    run_by_id = {item["run_id"]: item for item in recent if item.get("run_id")}
    options = list(run_by_id)
    # 방금 시작한 실행이 있으면 선택 상자의 상태를 그쪽으로 옮긴다.
    # 목록에 아직 없으면(기록 조회가 한 박자 늦으면) 표시를 남겨 두고 다음 갱신에서 다시 본다
    focus = st.session_state.get("wf_focus_run")
    if focus:
        if focus in options:
            st.session_state["wf_record_select"] = focus
            st.session_state.pop("wf_focus_run", None)
    wanted = st.session_state.get("wf_selected_run") or st.session_state.get("wf_active_run")
    default_id = wanted if wanted in options else options[0]
    selected_id = st.selectbox(
        "열어볼 실행 기록", options, index=options.index(default_id),
        format_func=lambda rid: (
            f"{rid} · {_STATUS_LABELS.get(run_by_id[rid].get('status'), run_by_id[rid].get('status'))} · "
            f"{run_by_id[rid].get('created_at', '')}"
        ), key="wf_record_select")
    st.session_state["wf_selected_run"] = selected_id
    try:
        run = api_client.ops_workflow(selected_id, ops_token=ops_token)
    except api_client.ApiError as error:
        _notice("선택한 실행의 상세 결과를 불러오지 못했어요", "red")
        _subnote(error.user_message or "")
        return

    status, phase = run.get("status"), run.get("phase")
    current, total = int(run.get("current_iteration") or 0), int(run.get("total_iterations") or 0)
    # 글씨 크기는 T1 · T2 카드와 같은 층이다 — 라벨 11.5 / 값 12.5 (st.markdown 굵은 글씨는 16px 라 떴다)
    st.html(f'<div class="moa-recline">'
            f'<span>상태</span><b>{c.esc(_STATUS_LABELS.get(status, status))}</b>'
            f'<span>현재 단계</span><b>{c.esc(_PHASE_LABELS.get(phase, phase))}</b>'
            f'<span>회차</span><b>{current}/{total}</b></div>')
    if total:
        st.progress(min(1.0, current / total))
    if status in ("queued", "preflight", "running", "stop_requested", "cleaning"):
        events = run.get("events") or []
        _notice(f"현재 진행: {events[-1].get('summary', '시험 준비 중') if events else '시험 준비 중'}")
        if st.button("안전하게 중지", key=f"wf_stop_{selected_id}"):
            try:
                api_client.ops_workflow_stop(selected_id, ops_token=ops_token)
                _notice("중지 요청을 받았어요 · 진행 중인 장애를 정리한 뒤 멈춰요", "green")
            except api_client.ApiError as error:
                _notice(error.user_message or "중지 요청을 전달하지 못했어요", "red")

    summary = run.get("summary") or {}
    if summary:
        values = [
            ("예상대로 통과", summary.get("pass", 0)),
            ("예상과 다름", summary.get("fail", 0)),
            ("사람 확인", summary.get("needs_review", 0) + summary.get("awaiting_approval", 0)),
            ("전체 회차", summary.get("total", total)),
        ]
        cells = "".join(f'<div><span>{c.esc(label)}</span><b>{value}</b></div>'
                        for label, value in values)
        st.html(f'<div class="moa-recstats">{cells}</div>')
        total_done = summary.get("completed_iterations", 0)
        passed = summary.get("pass", 0)
        rate = (f"{passed}/{total_done} 완료 회차 ({passed / total_done:.0%})" if total_done
                else "아직 판정이 난 회차가 없어요")
        mttd, mttr = summary.get("avg_mttd_ms"), summary.get("avg_mttr_ms")
        st.html(f'<div class="moa-recfoot">'
                f'<span>예상 흐름 일치율</span><b>{c.esc(rate)}</b>'
                f'<span>평균 감지</span><b>{_ms_text(mttd)}</b>'
                f'<span>평균 복구</span><b>{_ms_text(mttr)}</b></div>')

    iterations = run.get("iterations") or []
    if iterations:
        st.html('<div class="moa-reclabel">회차별 진행 결과</div>')
        for item in iterations:
            fault = item.get("fault_type", "—")
            label = _FAULT_GUIDE.get(fault, (fault, "", ""))[0]
            state_label = _STATUS_LABELS.get(item.get("status"), item.get("status", "대기 중"))
            detail = item.get("result") or {}
            if not isinstance(detail, dict):
                detail = {}
            with st.expander(
                f"{item.get('iteration_no', '?')}회차 · {fault} {label} · {state_label}",
                expanded=item.get("status") in ("FAIL", "NEEDS_REVIEW", "AWAITING_APPROVAL"),
            ):
                st.markdown(
                    f"**대상:** {_TARGET_LABELS.get(item.get('target'), item.get('target', '—'))}  \n"
                    f"**결과:** {detail.get('detail', '아직 결과가 기록되지 않았습니다.')}"
                )
                steps = detail.get("steps") or []
                if steps:
                    st.markdown("**예상 흐름과 실제 진행**")
                    st.dataframe(
                        [{"단계": step.get("name"), "예상": step.get("expected"),
                          "실제": step.get("actual"), "걸린 시간": f"{step.get('duration_ms', '—')} ms",
                          "판정": _STATUS_LABELS.get(step.get("status"), step.get("status"))}
                         for step in steps],
                        use_container_width=True, hide_index=True,
                    )
                attempts = detail.get("attempts") or []
                st.markdown(
                    f"**기록된 요청·복구 시도:** {len(attempts)}회 · "
                    f"감지 {item.get('mttd_ms') if item.get('mttd_ms') is not None else '—'} ms · "
                    f"복구 {item.get('mttr_ms') if item.get('mttr_ms') is not None else '—'} ms"
                )
                if attempts:
                    st.dataframe(
                        [{"시도": attempt.get("attempt_no", index + 1),
                          "대상/액션": attempt.get("service") or attempt.get("action") or attempt.get("stage"),
                          "결과": attempt.get("outcome") or attempt.get("result"),
                          "확인 반복": attempt.get("poll_count", attempt.get("request_attempt_no", "—")),
                          "걸린 시간": f"{attempt.get('duration_ms', '—')} ms"}
                         for index, attempt in enumerate(attempts)],
                        use_container_width=True, hide_index=True,
                    )
                events = [event for event in run.get("events") or []
                          if event.get("iteration_no") == item.get("iteration_no")]
                if events:
                    st.markdown("**이 회차의 시간순 진행 기록**")
                    st.dataframe(
                        [{"시각": event.get("created_at"), "진행": event.get("summary"),
                          "세부 기록": str(event.get("details") or "")} for event in events],
                        use_container_width=True, hide_index=True,
                    )

    if run.get("events"):
        with st.expander("전체 시험 진행 기록"):
            st.dataframe(
                [{"시각": event.get("created_at"), "회차": event.get("iteration_no"),
                  "진행": event.get("summary"), "세부 기록": str(event.get("details") or "")}
                 for event in run["events"]],
                use_container_width=True, hide_index=True,
            )

    title = "통합 장애 시험 결과보고서" if run.get("mode") == "full" else "장애 시험 결과보고서"
    file_name = "통합_장애_시험_결과보고서.md" if run.get("mode") == "full" else f"장애_시험_{selected_id}.md"
    st.download_button(title + " 다운로드", _workflow_report_markdown(run),
                       file_name=file_name, mime="text/markdown", key=f"wf_report_{selected_id}")
    try:
        json_data = api_client.ops_workflow_export(selected_id, "json", ops_token=ops_token)
        csv_data = api_client.ops_workflow_export(selected_id, "csv", ops_token=ops_token)
        c1, c2 = st.columns(2)
        c1.download_button("원본 JSON", json_data, file_name=f"{selected_id}.json",
                           mime="application/json", key=f"wf_json_{selected_id}")
        c2.download_button("회차 CSV", csv_data, file_name=f"{selected_id}.csv",
                           mime="text/csv", key=f"wf_csv_{selected_id}")
    except api_client.ApiError as error:
        _subnote(f"원본 내보내기를 하지 못했어요 · {error.user_message or ''}".strip(" ·"))


# --- 구역 3 · T1 도구 측정 — 정본 시안 T1 카드 ---------------------------------
def fault_tools() -> None:
    """일으켜 보는 쪽. 1건 · 대상 고정 · 상시 자가 복구가 처리한다 (고도화안 v0.3 6절).

    상태는 대시보드 조각이 세션에 올려 둔 값을 읽는다 — 같은 조회를 두 번 하지 않는다.
    """
    status = st.session_state.get("_ops_status") or {}
    active = status.get("active_injection") or None
    # 시험이 쥐고 있는 대상은 도구줄이 건드리지 않는다(시안 25-3). 서버가 알려줄 때만 잠근다
    locked = status.get("trial_holding") or None
    st.html(_block_head(copy.OT1_BADGE, copy.OT1_TITLE, copy.OT1_ASK, copy.OT1_NOTE))
    _fault_tiles((active or {}).get("code"), bool(locked))
    _fault_strip(active, st.session_state.get(state.OPS_TOKEN), locked)
    st.html(_fault_chips(copy.OT1_WHERE, copy.OT1_CHIPS))


def zone_three() -> None:
    """구역 3 — 장애를 일으키는 영역. 회색 패널 하나로 묶어 구역 1 · 2 와 가른다."""
    with st.container(key="moa_zone3"):
        st.html(f'<div class="moa-zhead"><i>{c.esc(copy.OZ_BADGE)}</i>'
                f'<b>{c.esc(copy.OZ_TITLE)}</b><span>{c.esc(copy.OZ_SUB)}</span>'
                f'<em>{c.esc(copy.OZ_LAB)}</em></div>')
        with st.container(key="moa_zone3_row", horizontal=True):
            with st.container(key="moa_t1_card"):
                fault_tools()
            with st.container(key="moa_t2_card"):
                trial_panel()
        _record_card()


def _record_card() -> None:
    """T2-R 시험 기록 — 기본 접힘. 주문 처리 기록(F)과 같은 구조다.

    기록은 **끝난 시험을 다시 열어 보는 자리**다. 늘 펼쳐 두면 구역 3 이 두 배로 길어지고,
    정작 위에서 시험을 시작하려는 사람이 매번 지나쳐야 한다.
    """
    opened = st.session_state.get(_REC_OPEN, False)
    with st.container(key="moa_t2r_card"):
        with st.container(key="card_rec_toggle"):
            st.html(f'<div class="moa-ord-toggle">'
                    f'<u class="moa-bbadge">{c.esc(copy.OT2R_BADGE)}</u>'
                    f'<b>{c.esc(copy.OT2R_TITLE)}</b>'
                    f'<span>{c.esc(copy.OT2R_NOTE)}</span>'
                    f'<i class="moa-chev{" moa-chev-up" if opened else ""}">'
                    f'{icons.icon("chevron-down", 16)}</i></div>')
            if st.button(f"{copy.OT2R_TITLE} 펼치기", key="hit_rec_toggle"):
                st.session_state[_REC_OPEN] = not opened
                st.rerun()
    if opened:
        with st.container(key="moa_t2r_body"):
            workflow_results()


dashboard()                                   # 구역 1 운영 상황 · 구역 2 업무 기록
zone_three()                                  # 구역 3 장애 시험 (T1 · T2 · T2-R)
