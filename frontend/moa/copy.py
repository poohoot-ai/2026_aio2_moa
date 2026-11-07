# 화면 문구 상수 — 출처는 P-06 화면설계서 v3.0과 정본 시안
# `Moa_전체화면.svg`(23케이스, 2026-11-03 판)다.
#
# 여기에 두는 것은 **화면 틀의 고정 문구**뿐이다.
# 서버가 만드는 문구(상태 줄 · 매장 기준 근거 · 반영 안내 · 공유 불가 이유 ·
# 가격 기준 고지)는 상수로 두지 않고 API 응답 필드를 그대로 그린다
# (P-06 프론트엔드 원칙 · D-02 2.2 `user_message`).
#
# 말투는 해요체 · 쉬운 말 한 줄이며(P-06 U-12 · NFR-014 (3)),
# 내부 용어(세션 · 토큰 · 주문 이벤트 · Snapshot · 검증 · NEEDS_REVIEW ·
# brand_id · 에이전트 이름)는 0건이다(U-7).

APP_NAME = "Moa"
LOGO_WORD = "moa"
# 헤더 단계 표시 — 누르는 탭이 아니라 지금 어느 단계인지 보여주는 표시다(P-06 U-2: 이동은 하단 바로만)
TABS = [("A", "주문 시작", "start"), ("B", "주문 정리", "review"), ("E", "주문서", "order")]

# --- 공통 (P-06 공통 레이아웃) ------------------------------------------------
OPS_ENTRY_BUTTON = "운영 대시보드"
# 가격 기준 고지는 서버가 완성 문구로 준다(API-002 `price_notice`).
# 서버에 닿지 못했을 때만 쓰는 대체 문구다.
PRICE_NOTICE_FALLBACK = "메뉴와 가격은 2026.10.29 기준이며 실제와 다를 수 있어요"

# --- SCR-001. A. 주문 시작 (케이스 01 ~ 07) -----------------------------------
S1_STATUS = "단톡방 대화를 붙여넣고 [정리하기]만 누르면 돼요"
S1_HERO_KICKER = "모임 커피 주문 정리"
S1_HERO_TITLE = ["흩어진 주문을", "한 장으로 모아요"]
S1_GREETING = "안녕하세요! 모임 커피 주문을 대신 정리해 주는 Moa예요"
S1_WHAT = (
    "단톡방 대화를 붙여넣고 **[정리하기]** 만 누르면 누가 뭘 시켰는지 사람별로 "
    "정리해 드려요. 매장은 따로 고르지 않아도 대화에서 찾아요."
)
S1_STEPS_TITLE = "이렇게 진행돼요"
S1_STEPS = [
    "대화 붙여넣기 또는 .txt 파일 올리기",
    "확인이 필요한 주문만 골라 주기",
    "주문서 · 정산을 텍스트나 이미지로 공유",
    "단톡방에 공유하고 바꿀 것만 답장 받기",
]
S1_FORMAT_TITLE = "대화는 이런 형식이면 돼요"
S1_FORMAT_EXAMPLES = [
    "[김민수] 아이스 아메리카노 톨 2잔이요",
    "[이지연] 바닐라라떼 큰 거 얼음 적게요",
]
S1_QUICK_SAMPLE = "예시로 채워보기"
S1_PREVIEW_TITLE_PASTE = "넣은 대화 미리보기"
S1_PREVIEW_TITLE_SAMPLE = "예시 대화"
S1_PREVIEW_BADGE_SAMPLE = "예시"
S1_PREVIEW_CLEAR = "지우기"
S1_PREVIEW_LOADING = "대화를 불러오고 있어요"
S1_UNREADABLE_LABEL = "읽지 못한 줄"
S1_INPUT_PLACEHOLDER = "단톡방 대화를 붙여넣거나 .txt 파일을 끌어다 놓아 주세요"
S1_FILE_LABEL = "대화 불러오기"
# Streamlit은 드래그 상태를 감지해 자리표시를 바꿀 수 없다 → 드롭 영역 안내로 대신한다
# (P-06 Streamlit 구현 대응 "입력창 위 드롭 자리표시 전환" 행)
S1_DROP_HINT = "여기에 .txt 대화 파일을 놓아도 돼요"
S1_SUBMIT = "정리하기"
# 누르는 동안 문구를 띄우지 않는다 — 돌아가는 표시만 남긴다(2026-11-03 요청)
S1_SUBMIT_DISABLED_REASON = "대화를 먼저 넣어 주세요"
S1_STORE_CAPTION = '매장 요청은 선택이에요 · 대화 끝에 "스타벅스 기준으로 정리해줘"처럼 적으면 돼요'
S1_SAMPLE_REPLACE_ASK = "지금 내용을 바꿀까요?"
# Ctrl/Cmd + Enter는 값 반영까지만 하고 버튼을 누르지 않는다(P-06 Streamlit 구현 대응)
S1_SUBMIT_HELP = "Enter는 줄바꿈이에요. 정리는 [정리하기]를 눌러 시작해 주세요"

# 매장 되묻기 모달 (케이스 06 · 07) — 질문 · 선택지 · 안내는 서버가 준다(API-012
# `brand_question`). 아래는 모달 제목처럼 틀에 속하는 문구다.
S1_BRAND_DIALOG_TITLE = "어디 기준으로 정리할까요?"
# 서버가 `subtitle_text`를 주지 않을 때만 쓰는 대체 문구
S1_BRAND_SUBTITLE_FALLBACK = "대화에 스타벅스와 다른 매장이 함께 나와요"
S1_BRAND_FOUND_LABEL = "대화에서 찾은 매장"
# 선택지 설명 — 선택지가 고정 3종이라 틀 문구로 둔다(서버가 `description`을 주면 그것이 먼저다)
S1_BRAND_OPTION_DESC = {
    "starbucks": "스타벅스 메뉴판과 가격으로 정리해요",
    "generic": "흔한 카페 메뉴로 정리하고 예상 금액으로 보여줘요",
    "other": "매장 이름을 직접 적어 주세요",
}
S1_BRAND_OTHER_TITLE = "기타 매장"
S1_BRAND_OTHER_PLACEHOLDER = "매장 이름"
S1_BRAND_OTHER_SUBMIT = "이 매장으로 정리"

# --- SCR-002. B. 주문 정리 (케이스 08 ~ 17) -----------------------------------
S2_INPUT_LABEL = "넣은 대화"
S2_INPUT_VIEW_RAW = "원본 보기"
S2_INPUT_REENTER = "다시 넣기"
S2_PROGRESS_EXPAND = "펼치기"
S2_PROGRESS_COLLAPSE = "접기"
S2_PANEL_TITLE = "주문서"
S2_PANEL_EMPTY = "정리가 끝나면 사람별 주문이 여기에 보여요"
# 2026-11-05 보고 9번 — 패널에는 확정된 주문만 둔다. 확인 필요는 채팅이 묻고, 전체 대화는 하단에서 펼친다
S2_PANEL_ASKING = "확인 중인 주문 {n}건은 오른쪽 채팅에서 묻고 있어요"
S2_PANEL_NONE_YET = "확정된 주문이 아직 없어요. 오른쪽 채팅의 질문에 답해 주세요"
S2_PANEL_RAW_OPEN = "전체 대화 보기"
S2_PANEL_RAW_CLOSE = "전체 대화 접기"
S2_REVIEW_BADGE = "확인 {n}"
S2_ROW_REVIEW = "확인 필요"
S2_BUSY_HINT = "정리가 끝나면 볼 수 있어요"
S2_REMAINING_HINT = "확인할 것이 {n}건 남았어요"
# 답장을 반영한 뒤 주문 정리 화면에 남기는 말풍선 (2026-11-05 보고 11번)
S2_REPLY_APPLIED = "단톡방 답장을 반영했어요. {found}"
S2_PENDING_SUGGESTION_HINT = "반영할지 정하지 않은 답장이 {n}건 있어요"
S2_PANEL_EDIT_SAVE = "저장"
S2_PANEL_EDIT_MENU = "메뉴 바꾸기"
S2_PANEL_EDIT_REMOVE = "빼기"
S2_PANEL_CUSTOM_LABEL = "기타 요청"
S2_CHAT_PLACEHOLDER = "답하거나 고칠 것을 적어 주세요 (예: 서연이 라떼로 바꿔줘)"
S2_CHAT_PLACEHOLDER_BUSY = "정리가 끝나면 답할 수 있어요"
S2_CHAT_SEND = "보내기"
S2_QUICK_VIEW_EVIDENCE = "대화 보기"
S2_QUICK_OTHER_MENU = "다른 메뉴"
S2_UNDO = "되돌리기"
S2_BACK = "대화 넣기로"
S2_PRIMARY = "주문서 보기"
S2_BRAND_CHANGE_YES = "새로 정리"
S2_BRAND_CHANGE_NO = "아니요"
# 정본 시안 케이스 15의 제목은 "메뉴를 찾아 주세요"다(P-06 본문 그림의 "메뉴 찾기"와 다르다 — 시안이 정본)
S2_MENU_DIALOG_TITLE = "메뉴를 찾아 주세요"
S2_MENU_SEARCH_PLACEHOLDER = "메뉴 이름으로 찾기"
S2_ALL_DONE = "확인할 것을 모두 끝냈어요. 이제 단톡방에 확인 받으러 가요"
S2_LINK_COPY = "링크 복사"
S2_MOBILE_PANEL_PRIMARY = "주문서 보기"
S2_LINK_INVALID_HOME = "처음으로"
S2_LINK_INVALID = "이 링크는 더 이상 쓸 수 없어요. 처음부터 다시 정리해 주세요"
# 새로고침 · 새 탭 — 주소의 관리 토큰은 남지만 정리 결과는 브라우저 세션에만 있었다.
# 토큰으로 세션을 찾는 경로가 아직 없어(D-02 미결) 불러올 수 없다. "링크가 죽었다"고
# 말하면 담당자가 링크를 의심하며 시간을 쓴다 — 무엇이 일어났고 무엇을 하면 되는지 적는다
S2_SESSION_LOST = "새로고침하면 정리하던 내용을 다시 불러올 수 없어요. 대화를 다시 넣어 주세요"

# --- SCR-005. E. 주문서 (케이스 18 ~ 21) --------------------------------------
S5_SUMMARY_PEOPLE = "함께한 사람"
S5_SUMMARY_CUPS = "모은 음료"
S5_ORDER_TITLE = "주문서"
S5_ORDER_HINT = "메뉴별로 묶었어요 · 이름을 누르면 고칠 수 있어요"
S5_TOTAL_LABEL = "합계"
S5_NAMES_MORE = "{n}명 더 보기"
S5_NAMES_FOLD = "접기"
S5_REPLY_TITLE = "단톡방 답장 반영하기"
S5_REPLY_SUB = "답장을 붙여넣으면 바뀐 주문을 찾아 줘요"
S5_REPLY_PLACEHOLDER = "[이름] 바꿀 내용 형식으로 붙여넣어 주세요"
S5_REPLY_ANALYZE = "바뀐 주문 찾기"
S5_REPLY_AI_NOTICE = "AI가 찾은 내용이에요"
S5_REPLY_APPLY = "반영"
S5_REPLY_IGNORE = "무시"
S5_REPLY_OPEN = "보기"
S5_REPLY_APPLY_ALL = "모두 반영"
S5_REPLY_OPEN_BUTTON = "단톡방 답장 반영하기"     # 2026-11-05 보고 11번 — 주문서에서 다시 연다
S5_REPLY_EMPTY = "답장 대화를 먼저 붙여넣어 주세요"
S5_EDIT_REMOVE_PERSON = "이 사람 빼기"
S5_SHARE = "공유하기"
S5_SHARE_TITLE = "주문서 공유하기"
S5_SHARE_CAPTION = "복사하거나 저장하면 주문이 완료돼요"
S5_SHARE_NAMES = "이름 같이 보내기"
S5_SHARE_NAMES_OFF = "이름 없이 메뉴와 수량만 보내요"
S5_SHARE_COPY = "텍스트 복사"
# 브라우저가 클립보드를 막았을 때만 보인다 (평문 http · 사파리 구버전 — 4차 보고 2번)
S5_COPY_FAILED = "브라우저가 복사를 막았어요. 아래 내용을 끌어서 복사해 주세요"
S5_SHARE_IMAGE = "이미지 저장"
S5_SHARE_IMAGE_LOADING = "이미지를 만들고 있어요"
S5_BACK = "주문 정리로"
S5_CARD_FOOT = "Moa · 비공식 시연 서비스"

# --- SCR-006. O. 운영 대시보드 (케이스 04 · 05 · 22 · 23) ---------------------
O0_DIALOG_TITLE = "운영 대시보드"
O0_PROMPT = "운영자 비밀번호를 입력해 주세요"
O0_PLACEHOLDER = "비밀번호"
O0_LABEL = "비밀번호"
O0_SUBMIT = "들어가기"
O0_CANCEL = "취소"
# 사유를 구분하지 않는다(P-06 O-0 · NFR-003 · D-02 `OPS_AUTH_FAILED`)
O0_ERROR_FALLBACK = "비밀번호가 맞지 않아요"
# 서비스 진입 비밀번호 — O-0과 같은 구성 · 같은 비밀번호(OPS_DASHBOARD_PASSWORD)를 쓴다 (2026-11-05 사용자 요청)
G0_TITLE = "Moa"
G0_PROMPT = "서비스 비밀번호를 입력해 주세요"
O6_LOGOUT = "나가기"
O_ENV_BADGE = "실습 환경"
O_REFRESH_NOTE = "5초마다 갱신 · 시험 결과 30초 · 시간은 ms를 초로 환산"
O4_TITLE = "승인 필요 ({n})"
O4_EMPTY = "승인 필요 없음"
O4_APPROVE = "승인"
O4_REJECT = "거부"
O3_TITLE = "지금 상태"
O1_TITLE = "사건"
O1_EMPTY = "발생한 사건이 없어요"
O1_COLUMNS = ["ID", "장애 코드", "상태", "탐지", "정상화"]
O5_TITLE = "지표"
O5_METRIC_LABELS = ["MTTD", "MTTR", "성공률", "에스컬레이션"]
O5_FAULT_TITLE = "장애 주입"
O5_FAULT_NOTE = "실습 전용"
O5_FAULT_CODES = ["F-1", "F-2", "F-3", "F-4", "F-5", "F-6"]
O5_FAULT_CLEAR = "해제"
O6_TITLE = "주문 처리 기록"
O6_SEARCH_HINT = "주문 찾기: 확정 시각 · 매장"
O6_COLUMNS = ["주문", "매장", "상태", "확정 시각", "변경", "복구"]
O6_DETAIL_COLUMNS = ["시각", "누구", "전 → 후", "방법"]
O6_SHARE_LABEL = "공유 기록"

# --- SCR-006 고도화 (정본 시안 `Moa_운영대시보드.svg` 24 · 25-1 ~ 25-3 · 26) --------
# 블록 A ~ F 는 05.리포트/운영대시보드_고도화안.md v0.2 의 구성이다.
# 장애 이름은 쉬운 말을 먼저, 코드는 뒤에 작게 쓴다(고도화안 6절 원칙 2).
# 장애 유형 이름 — PRD 5.6 장애 표 · backend workflows.FAULT_LABELS 와 같은 매핑이어야 한다.
# UNKNOWN · REPLICA_DOWN 은 F 코드가 아니다(원인 미확인 에스컬레이션 · SC-12 복제본). 코드 자리에 그대로 쓴다
FAULT_NAMES = {
    "F-1": "주문 해석 오류 응답",
    "F-2": "해석 응답 지연",
    "F-3": "처리 프로세스 중단",
    "F-4": "반복 장애",
    "F-5": "잘못된 결과 형식",
    "F-6": "DB 연결 끊김",
    "UNKNOWN": "원인 미확인 장애",
    "REPLICA_DOWN": "앱 복제본 중단",
}
# 평가 유형(PRD 5.6.1 FT-01 ~ FT-04)과 감지 메트릭 — D 격자 행 아래 작은 줄.
# 평가 질문 "네트워크 · DB · API · LLM 유형별 감지 메트릭이 있는가"(PRD 1.2 P3-2)에 화면으로 답한다
FAULT_METRIC = {
    "F-1": "FT-01 dep_5xx_ratio",
    "F-2": "FT-01 timeout_count",
    "F-3": "FT-02 agent_error_count",
    "F-4": "circuit_open_count",
    "F-5": "FT-04 schema_error_rate",
    "F-6": "FT-03 db_connection_error",
}
# 복구 전략(PRD 5.6 1차 · 2차 복구) — D 격자 복구 전략 열.
# 평가 질문 "재시작 · 재연결 · 대체 모델 · 캐시 폴백 복구가 있는가"에 한 화면으로 답한다
FAULT_STRATEGY = {
    "F-1": "대체 전환",
    "F-2": "재시도 → 전환",
    "F-3": "재시작 · 재개",
    "F-4": "회로 차단 · probe",
    "F-5": "재생성 → 대체모델",
    "F-6": "재연결 → 캐시",
}
# 서버 사건의 장애 코드 → 시험 유형(F-n). 서버가 `fault` 를 주면 그것을 먼저 쓴다
FAULT_CODE_TO_F = {
    "DEP_HTTP_5XX": "F-1",
    "DEP_TIMEOUT": "F-2",
    "WORKER_DOWN": "F-3",
    "AGENT_EXCEPTION": "F-3",
    "REPEATED_FAILURE": "F-4",
    "AGENT_OUTPUT_INVALID": "F-5",
    "DB_UNAVAILABLE": "F-6",
    "DB_CONN_LOST": "F-6",
    # F 코드가 아닌 둘 — 코드 자리에 그대로 보여 준다
    "UNKNOWN": "UNKNOWN",
    "REPLICA_DOWN": "REPLICA_DOWN",
}

# A 상태 배너 — 우선순위 표의 위에서부터 하나만 쓴다
OA_APPROVAL = "승인이 필요해요"
OA_APPROVAL_SUB = "{name}({code}) · {waited} 대기"
OA_APPROVAL_TOGGLE = "승인 필요 {n}건"
OA_FAILED = "자동 복구에 실패했어요"
OA_INJECTED = "주입 후 {sec}초"
OA_INJECTED_SUB = "탐지 대기 중 · {name}({code})"
OA_RECOVERING = "복구 중"
OA_RECOVERING_SUB = "{name}({code}) · {sec}초 경과"
OA_DEGRADED = "정상 처리 중"
OA_DEGRADED_BACKUP = "대체 해석기를 쓰고 있어요"
OA_DEGRADED_RULE = "규칙 해석으로 처리하고 있어요"
OA_DEGRADED_CACHE = "메뉴를 캐시로 조회하고 있어요"
OA_NORMAL = "정상 운영 중"
OA_NO_APPROVAL = "지금 승인할 일이 없어요"
OA_RUN_SUFFIX = "{n}회차"

# B 핵심 수치 4칸
OB_CARDS = {
    "mttd": ("탐지까지", "MTTD"),
    "mttr": ("복구까지", "MTTR"),
    "auto": ("사람 없이 복구", "무인 복구율"),
    "integrity": ("주문 중복 · 유실", "데이터 무결성"),
}
OB_TARGET = {
    "mttd": "목표 15초 이하",
    "mttr": "목표 60초 · F-3 90초",
    "auto": "목표 80% 이상",
    "integrity": "목표 0건",
}
OB_PASS = "통과"
OB_FAIL = "미달"
OB_UNMEASURED = "미측정"

# C 지금 요청 경로
OC_TITLE = "지금 요청 경로"
OC_GROUPS = {"gateway": "게이트웨이", "interpreter": "해석기", "store": "메뉴 저장소"}
OC_ACTIVE = "사용 중"
OC_STANDBY = "대기"
OC_UNKNOWN = "상태 미수집"
# SC-12 — 복제본 전환은 감지 · 전환 각 15초 이내
OC_SWITCH = "전환 {sec}초 · 목표 15초"
OC_BLOCKED = "차단 · {sec}초 후 시험"
OC_BLOCKED_DEAD = "차단 · 응답 없음"
OC_PROBING = "복귀 확인 {n}/{need}"
OC_CACHE = "캐시로 조회 중"
OC_NOTE_RULE = "규칙 해석으로 처리 중이에요 · 정확도가 낮을 수 있어요"
OC_NOTE_PROBING = "복귀하려면 1차 해석기가 {need}회 연속 정상이어야 해요"
OC_NOTE_CACHE = "메뉴 저장소가 끊겨 캐시로 조회하고 있어요 · 가격이 최신이 아닐 수 있어요"

# D 장애 시험 결과
OD_TITLE = "장애 시험 결과"
OD_HINT = "숫자 = 복구 초 · 굵게 = 목표 초과"
OD_RUNS = "{done}/{total}회 실시"
# 실행 식별 줄 — 어느 실행의 숫자인지 없으면 25회 측정을 증빙할 수 없다
# `{done}/{total}` 은 **격자에 채워진 칸 수**이지 그 실행의 회차가 아니다.
# 둘을 한 문장에 "6/25회"로 붙여 두면 "이 실행이 25회 중 6회를 했다"로 읽힌다(2026-11-07 실측).
OD_RUN_LINE = "최근 실행 {run_id} · 격자 {done}/{total}칸 · 예상 흐름 일치 {ok}/{valid}"
# 막 시작해 판정이 아직 없는 실행 — 0/0 을 적지 않는다
OD_RUN_LINE_SHORT = "최근 실행 {run_id} · 격자 {done}/{total}칸"
OD_STRATEGY = "복구 전략"
OD_SUCCESS = "성공"
OD_F4_NOTE = "별도 · 분모 밖"
OD_CELL_APPROVED = "승인"
OD_CELL_FAILED = "실패"
OD_CELL_INVALID = "무효"
OD_CELL_RUNNING = "진행 중"
OD_CELL_SWITCHED = "통과"
OD_CELL_NOT_SWITCHED = "안 됨"
OD_CELL_PENDING = "--"
OD_LEGEND = [("auto", "사람 없이 복구"), ("approved", "승인 · 수동"), ("failed", "실패"),
             ("invalid", "무효 · 분모 제외"), ("pending", "아직 안 함")]
OD_TOTAL = "합계 {ok}/{valid} · {pct}%"
OD_EMPTY = "아직 시험 결과를 받지 못했어요"

# E 최근 사건
OE_TITLE = "최근 사건"
OE_HINT = "행을 누르면 타임라인이 펼쳐져요"
OE_EMPTY = "기록된 장애가 없어요"
OE_WAITING = "대기 중"
OE_RUNNING = "{sec}초째"
OE_JUST_NOW = "방금"
OE_TL_LEGEND = [("pre", "탐지 전"), ("rec", "복구 중"), ("human", "사람 확인"), ("mark", "탐지 · 복구 시작 · 정상화")]
OE_TL_START = "주입 0초"
OE_CARDS = ["진단", "전략", "시도", "검증"]
OE_DETAIL_PENDING = "사건 상세는 서버가 응답한 뒤에 보여 드려요"
# 서버 전략 코드 → 쉬운 말
OE_STRATEGY = {
    "restart_resume": "워커 재시작",
    "retry_fallback": "재시도 후 대체 해석기",
    "switch_backup": "대체 해석기로 전환",
    "cache_fallback": "캐시로 조회",
    "reconnect_cache": "재연결 후 캐시 조회",
    "rule_fallback": "규칙 해석으로 전환",
    "replica_failover": "다른 복제본으로 전환",
}

# F 접힌 기록 · 장애 주입 도구줄
OF_ORDERS_COUNT = "오늘 {n}건"
OF_FAULT_TITLE = "장애 주입 (실습)"
# 시험 층과 역할이 갈리는 지점을 화면에 적는다 — 위에서 정한 대상 · 유지 시간은 여기 적용되지 않는다
OF_FAULT_NOTE = "1건 · 대상 고정 · 해제할 때까지 유지 (F-3은 즉시 1회)"
# =============================================================================
# 구역 3 장애 시험 — 정본 시안 `Moa_운영대시보드.svg` (T1 · T2 · T2-R, 2026-11-07 판)
# 블록 배지 · 제목 · 보조 질문 칩 · 부제 · 결과가 가는 곳 칩까지 시안 문구 그대로다.
# =============================================================================
OZ_BADGE = "3"
OZ_TITLE = "장애 시험"
OZ_SUB = "일으킨다"
OZ_LAB = "실습 환경에서만"

# T1 도구 측정
OT1_BADGE = "T1"
OT1_TITLE = "도구 측정"
OT1_ASK = "지금 진짜 복구되나"
OT1_NOTE = "1건 · 대상 고정 · 해제할 때까지 유지 (F-3은 즉시 1회) · 복구는 상시 감시가 해요"
OT1_WHERE = "결과가 가는 곳"
OT1_CHIPS = [("A", "상태 배너"), ("B", "핵심 수치"), ("E", "최근 사건")]
OT1_CONFIRM = "주입"
OT1_CANCEL = "그만두기"
OT1_HOLDING = "유지 중 · 상시 감시가 복구하는 중"
OT1_HOLDING_HEAD = "{code} {name} · {target}"
OT1_SEC = "{sec}초"
OT1_CLEAR_ASK = "주입한 장애를 모두 해제할까요?"
# 시험이 같은 대상을 쥐고 있는 동안 (시안 25-3)
OT1_LOCKED = "복구 시험이 {target}를 쓰고 있어요 · F-3은 시험이 끝나면 누를 수 있어요"

# T1 타일 설명 — 무엇이 일어나고 언제 끝나는지까지 한 줄에 쓴다
OT1_HELP = {
    "F-1": "해석기 1차가 서버 오류(5xx)를 반환해요 · 대체 해석기로 전환해요 · 해제할 때까지 유지",
    "F-2": "해석기 1차가 6초 지연해요 · 재시도한 뒤 대체로 전환해요 · 해제할 때까지 유지",
    "F-3": "앱 A backend를 실제로 중지합니다 · 상시 감시가 되살립니다 · 즉시 1회",
    "F-4": "해석기 오류를 반복해요 · 회로가 차단되고 probe로 복귀를 봐요 · 해제할 때까지 유지",
    "F-5": "해석기 1차가 형식이 깨진 결과를 돌려줘요 · 다시 생성해요 · 해제할 때까지 유지",
    "F-6": "메뉴 저장소 연결을 끊어요 · 다시 연결한 뒤 캐시로 조회해요 · 해제할 때까지 유지",
}
# 도구줄의 고정 대상 — 고르는 축은 T2 에만 둔다 (backend incidents.FAULT_TARGETS 와 같은 값)
OT1_TARGET = {
    "F-1": "해석기 1차", "F-2": "해석기 1차", "F-3": "앱 A backend",
    "F-4": "해석기 1차", "F-5": "해석기 1차", "F-6": "메뉴 저장소",
}

# T2 복구 시험
OT2_BADGE = "T2"
OT2_TITLE = "복구 시험"
OT2_ASK = "얼마나 잘 복구되나"
OT2_NOTE = "대상을 골라 반복해요 · 복구는 시험 워크플로가 하고 상시 감시는 비켜서요"
OT2_MODES = {
    "custom": ("직접 설정 · 1~10회", "유형 · 대상 · 횟수를 골라요 (기본 1회)"),
    "full": ("전체 복구 측정 · 25회", "무인 복구율 분모를 만들어요 · F-4 제외"),
}
OT2_L_FAULT = "유형"
OT2_L_TARGET = "대상"
OT2_L_REPEAT = "횟수 · 1~10회"
OT2_L_SECONDS = "유지 시간 · 1~120초"
OT2_REPEAT_UNIT = "{n}회"
OT2_SECONDS_UNIT = "{n}초"
OT2_PREFLIGHT = "환경 점검"
OT2_START = "시험 시작"
OT2_PF_OFFLINE = "환경 점검을 하지 못했어요 · 서버에 연결되지 않았어요"
OT2_PF_FAILED = "환경 점검 결과를 가져오지 못했어요"
OT2_WHERE = "결과가 가는 곳"
OT2_CHIPS = [("D", "장애 시험 결과 격자"), ("T2-R", "시험 기록")]
OT2_FULL_FIXED = "전체 복구 측정은 5유형을 고정 순서로 돌려요 · 유형과 대상을 고르지 않아요"

# T2-R 시험 기록
OT2R_BADGE = "T2-R"
OT2R_TITLE = "시험 기록"
OT2R_NOTE = "예전 '빠른 확인' 기록은 직접 설정으로 보여요"
# 빈 상태와 연결 실패를 **다른 문장으로** 말한다. 화면이 설명을 늘어놓지 않고 지금 상태만 말한다
OT2R_EMPTY = "아직 시험 기록이 없어요 · 위에서 시험을 한 번 끝내면 여기에 남아요"
OT2R_OFFLINE = "지금은 기록을 불러올 수 없어요 · 서버에 다시 연결되면 보여 드려요"

OF_FAULT_HELP = {
    "F-1": "해석기 1차가 5xx 를 반환합니다",
    "F-2": "해석기 1차가 6초 지연합니다",
    "F-3": "앱 A backend 를 실제로 중지합니다 · 상시 감시가 되살립니다",
    "F-4": "F-1 을 반복해 회로 차단을 봅니다",
    "F-5": "해석기 1차가 형식이 깨진 결과를 반환합니다",
    "F-6": "메뉴 저장소 조회를 막습니다",
}

# --- 통신 실패 안내 ------------------------------------------------------------
# 서버가 `user_message`를 주면 그것을 그대로 보여주고(D-02 2.2),
# 서버에 닿지 못해 응답 자체가 없을 때만 아래를 쓴다.
NETWORK_FAILED = "잠시 후 다시 눌러 주세요"
RETRY_FETCH = "다시 불러오기"
