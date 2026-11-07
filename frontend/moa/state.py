# st.session_state 키와 화면 이동 — 키 이름을 화면 코드에 흩지 않는다
#
# 지키는 것
#   - 세션 관리 토큰은 쿼리 파라미터(`?s=`)로 받고 헤더로 보낸다 (P-06 Streamlit 구현 대응)
#   - **비밀번호는 session_state에 남기지 않는다** (P-06 O-0 검증 위치 · NFR-003)
#   - 입력한 것은 사라지지 않는다 (U-9) — 화면 이동 · 되묻기 · O-0 취소 후에도 유지한다
import streamlit as st

# --- 키 이름 -------------------------------------------------------------------
RAW_TEXT = "raw_text"                 # 입력창에 넣은 대화 원문
PREVIEW = "preview"                   # API-011 미리보기 결과
PREVIEW_IS_SAMPLE = "preview_is_sample"
SAMPLE_RAW = "sample_raw"            # 예시 대화 원문 — 입력창은 비워 두고 미리보기만 채운다
SESSION_ID = "session_id"
SESSION_TOKEN = "session_token"       # 관리 토큰 — 로그에 남기지 않는다
VERSION = "version"                   # 낙관적 잠금 (D-02 2.5)
BRAND = "brand"                       # 매장 기준 판별 결과 (라벨 · 근거 · 바꾸는 법)
BRAND_QUESTION = "brand_question"     # 되묻기 질문 (있으면 모달을 띄운다)
BRAND_OTHER_OPEN = "brand_other_open"
CHAT = "chat"                         # 채팅 말풍선 목록
PENDING_CHAT = "pending_chat"         # 보낼 채팅 문장 + Idempotency-Key (4차 보고 1번)
                                      # 제출한 실행에서 보내지 않는다 — chat_input 이 값을 비우는
                                      # 재실행을 한 번 더 보내 그 실행을 취소하고, 결과 말풍선을 잃는다
PROGRESS_OPEN = "progress_open"       # 진행 말풍선 펼침 여부 (끝나면 접힌 상태가 기본)
RAW_OPEN = "raw_open"                 # 넣은 대화 원본 보기 펼침 여부
PANEL_RAW_OPEN = "panel_raw_open"     # 주문서 패널 하단 "전체 대화" 펼침 여부 (2026-11-05 보고 9번)
REPLY_APPLIED = "reply_applied"       # 답장으로 반영된 변경 문장 목록 (3차 보고 6번) — 주문 정리 채팅에 남긴다
EDIT_ROW = "edit_row"                 # 행 편집 중인 item_id
UNDO_TOKEN = "undo_token"
NOTICE = "notice"                     # 서버가 준 user_message (화면에 그대로)
META = "meta"                         # API-002 기준 정보
SHARED = "shared"                     # 공유 완료 여부 (상태 줄 초록 전환)
SUGGESTIONS = "suggestions"           # 답장 분석 제안
REPLY_TEXT = "reply_text"
OPS_TOKEN = "ops_token"               # 운영 대시보드 세션 토큰
OPS_AUTHED = "ops_authed"
OPS_ERROR = "ops_error"
OPS_ORDER_OPEN = "ops_order_open"     # O-6 펼친 주문
OPS_INCIDENT_OPEN = "ops_incident_open"  # O-1에서 펼친 사건 (O-2)
OPS_RETURN = "ops_return"             # O-0을 띄운 화면 (취소하면 그대로 돌아간다)
SERVICE_AUTHED = "service_authed"     # 서비스 진입 비밀번호 통과 여부 (app.py 진입 게이트)
SERVICE_ERROR = "service_error"

_DEFAULTS = {
    RAW_TEXT: "",
    PREVIEW: None,
    PREVIEW_IS_SAMPLE: False,
    SAMPLE_RAW: "",
    SESSION_ID: None,
    SESSION_TOKEN: None,
    VERSION: None,
    BRAND: None,
    BRAND_QUESTION: None,
    BRAND_OTHER_OPEN: False,
    CHAT: [],
    PENDING_CHAT: None,
    PROGRESS_OPEN: False,
    RAW_OPEN: False,
    PANEL_RAW_OPEN: False,
    REPLY_APPLIED: [],
    EDIT_ROW: None,
    UNDO_TOKEN: None,
    NOTICE: None,
    META: None,
    SHARED: False,
    SUGGESTIONS: [],
    REPLY_TEXT: "",
    OPS_TOKEN: None,
    OPS_AUTHED: False,
    OPS_ERROR: None,
    OPS_ORDER_OPEN: None,
    OPS_INCIDENT_OPEN: None,
    OPS_RETURN: None,
    SERVICE_AUTHED: False,
    SERVICE_ERROR: None,
}


def init() -> None:
    """모든 페이지 맨 앞에서 부른다. 이미 있는 값은 건드리지 않는다(U-9)."""
    for key, value in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = list(value) if isinstance(value, list) else value
    _adopt_token_from_url()


def _adopt_token_from_url() -> None:
    """`?s=` 로 들어온 관리 토큰을 받아 둔다. API 경로에는 넣지 않는다(D-02 1.1)."""
    token = st.query_params.get("s")
    if token and token != st.session_state.get(SESSION_TOKEN):
        st.session_state[SESSION_TOKEN] = token


def keep_token_in_url() -> None:
    """화면 주소를 P-06 대로 `/s?s={관리 토큰}` · `/order?s=…` 로 유지한다.

    `st.switch_page` 는 이동할 때 쿼리 파라미터를 지운다 — 그대로 두면 주소창이 `/s` 만 남아
    담당자가 주소를 복사해 돌아올 수 없다(D-05 확인 2026-11-03). 토큰이 필요한 화면에서만 부른다.
    """
    token = st.session_state.get(SESSION_TOKEN)
    if token and st.query_params.get("s") != token:
        st.query_params["s"] = token


def token() -> str | None:
    return st.session_state.get(SESSION_TOKEN)


def session_id() -> str | None:
    return st.session_state.get(SESSION_ID)


def add_chat(role: str, text: str, **extra) -> None:
    """채팅 말풍선을 더한다. 문구는 서버가 만든 것을 그대로 넣는다."""
    st.session_state[CHAT].append({"role": role, "text": text, **extra})


def split_undone_step(chat: list[dict], undo_token: str) -> tuple[list[dict], list[dict]]:
    """되돌리기 — 그 변경이 만든 말풍선을 **한 단계 전체** 떼어 낸다 (U-5).

    돌려주는 것은 (남길 말풍선, 떼어 낸 말풍선)이다.

    되돌린 결과 안내 하나만 지우면 질문 말풍선과 내가 고른 답이 그대로 남는다. 서버가
    그 질문을 다시 물어도 화면에는 "이미 답한 질문"으로 보여서, 되돌리기가 이전 단계로
    돌아가지 않고 **처리된 상태로 지나가 버린다**. 그래서 한 단계를 `undo_step` 으로
    묶어 두고 함께 지운다.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    for message in chat:
        target = undo_token in (message.get("undo_token"), message.get("undo_step"))
        (dropped if target else kept).append(message)
    return kept, dropped


def remember_session(data: dict) -> None:
    """분석 요청 · 결과 응답에서 세션 식별자 · 버전 · 매장 기준을 받아 둔다."""
    for key, field in ((SESSION_ID, "session_id"), (SESSION_TOKEN, "session_token"),
                       (VERSION, "version"), (BRAND, "brand")):
        if data.get(field) is not None:
            st.session_state[key] = data[field]
    if st.session_state[SESSION_TOKEN]:
        st.query_params["s"] = st.session_state[SESSION_TOKEN]


def clear_ops() -> None:
    """[나가기] — 인증 플래그와 토큰을 지운다."""
    st.session_state[OPS_TOKEN] = None
    st.session_state[OPS_AUTHED] = False
    st.session_state[OPS_ERROR] = None
    st.session_state[OPS_ORDER_OPEN] = None
    st.session_state[OPS_INCIDENT_OPEN] = None


def reset_order() -> None:
    """처음부터 다시 정리 — 넣은 대화만 남기고 분석 결과를 비운다."""
    for key in (SESSION_ID, SESSION_TOKEN, VERSION, BRAND, BRAND_QUESTION, UNDO_TOKEN, NOTICE):
        st.session_state[key] = None
    st.session_state[CHAT] = []
    st.session_state[SHARED] = False
    st.session_state[SUGGESTIONS] = []
    if "s" in st.query_params:
        del st.query_params["s"]
