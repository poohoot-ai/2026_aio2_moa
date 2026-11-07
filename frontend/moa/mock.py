# 모의 응답 — 서버가 아직 그 구간을 구현하지 않았을 때만 쓴다
#
# 왜 두는가: 이 프로젝트의 화면은 증분 1 ~ 3에 걸쳐 완성되지만(P-06 "동작 증분"),
# 사용자 결정에 따라 **SVG 23케이스 전체를 먼저 그린다**. 증분 3 API(확인 질문 ·
# 채팅 수정 · 주문서 · 공유 · 운영 대시보드)가 서버에 없는 동안 화면을 확인할
# 수단이 필요하다.
#
# 규칙
#   1. **응답 모양을 D-02와 같게** 만든다. 화면 코드는 모의인지 실제인지 모른다
#   2. **화면에 보일 문구는 전부 이 안에 완성형으로** 둔다 — 화면이 금액을 계산하거나
#      공유 가능 조건을 판정하지 않는다는 원칙(P-06 프론트엔드 원칙)을 모의에서도 깨지 않는다
#   3. 값은 전부 **정본 시안 `Moa_전체화면.svg`의 예시값**이다.
#      와이어프레임의 금액 · 메뉴명 · 사건 수치는 예시값이다(P-06 프론트엔드 원칙 마지막 줄)
#   4. 서버가 그 엔드포인트에 응답하기 시작하면 **이 파일은 더 이상 타지 않는다**.
#      D-16 구현점검서에서 잔존 호출 0건을 대조한다
import os

# 미리보기 모드 — 서버가 아직 없는 구간을 모의로 채운다. **기본은 끈다**(2026-11-04).
#   켜 두면 서버 호출이 실패할 때(새로고침으로 세션을 잃음 · 401 등) 오류 대신 예시 주문서 · 예시 질문을 그려,
#   담당자가 가짜 데이터를 보고 답하고 공유했다("이미지는 아직 만들지 못했어요"도 예시 응답의 값이었다).
#   서버 없이 화면을 볼 때만 MOA_UI_PREVIEW=1 로 켠다. 화면 테스트는 frontend/conftest.py 가 켠다
PREVIEW_ENABLED = os.getenv("MOA_UI_PREVIEW", "0") == "1"

META = {
    "menu_as_of": "2026-10-29",
    "price_notice": "메뉴와 가격은 2026.10.29 기준이며 실제와 다를 수 있어요",
    "brands": [
        {"id": "starbucks", "label": "스타벅스 메뉴 기준"},
        {"id": "generic", "label": "일반 카페 메뉴 기준"},
    ],
    "limits": {"max_messages": 500, "max_bytes": 1048576, "poll_interval_ms": 1000},
    "ops_poll_interval_ms": 5000,
}

# --- SCR-001 케이스 02 — 예시 대화 (SVG 미리보기 묶음 8개 · 5명) --------------
_SAMPLE_MESSAGES = [
    ("김민수", "아이스 아메리카노 톨 2잔, 하나는 샷 추가요."),
    ("이지연", "바닐라라떼 큰 거 얼음 적게요."),
    ("박서연", "아아 톨 한 잔이요. 빨대는 빼 주시고 개인 텀블러에 담아 주세요, "
               "얼음은 컵 반만 채워 주시면 좋겠어요!"),
    ("최유나", "여기 인기 있는 메뉴 뭐야?"),
    ("정하준", "민트 초코가 유명하대"),
    ("최유나", "그럼 난 그걸로 하나~"),
    ("김민수", "아까 샷 추가한 한 잔 취소할게요."),
    ("정하준", "나도 민수랑 같은 걸로!"),
]
# 대화 끝 매장 요청 한 줄 — 서버(meta.SAMPLE_STORE_REQUEST)와 같은 값. 메시지가 아니다
_SAMPLE_STORE_REQUEST = "스타벅스로 갈 예정이야"

SAMPLE_CONVERSATION = {
    "title": "예시 대화",
    "is_sample": True,
    "raw_text": "\n".join([f"[{s}] {t}" for s, t in _SAMPLE_MESSAGES] + [_SAMPLE_STORE_REQUEST]),
    "store_request": _SAMPLE_STORE_REQUEST,
    "messages": [
        {"message_id": f"m{i}", "order": i, "sender": s, "text": t}
        for i, (s, t) in enumerate(_SAMPLE_MESSAGES, start=1)
    ],
    "summary_text": "5명 · 메시지 8개를 읽었어요",
}


def preview_from(raw_text: str) -> dict:
    """API-011 모의 — `[이름] 메시지` 형식만 본다. LLM 호출 0건.

    서버가 할 일을 흉내 내는 것이며, 분석 · 금액 · 판정은 하지 않는다.
    """
    messages, unreadable = [], []
    for line_no, line in enumerate((raw_text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            sender, _, text = line[1:].partition("]")
            messages.append({
                "message_id": f"m{len(messages) + 1}",
                "order": len(messages) + 1,
                "sender": sender.strip(),
                "text": text.strip(),
            })
        else:
            unreadable.append({
                "line_no": line_no,
                "text": line,
                "notice_text": f"{line_no}번째 줄은 읽지 못했어요",
            })
    people = len({m["sender"] for m in messages})
    return {
        "messages": messages,
        "summary_text": f"{people}명 · 메시지 {len(messages)}개를 읽었어요",
        "unreadable_lines": unreadable,
        "can_proceed": bool(messages),
    }


# --- SCR-001 케이스 06 · 07 — 매장 되묻기 ------------------------------------
BRAND_QUESTION = {
    "question_text": "스타벅스와 다른 매장이 함께 나와요. 어디 기준으로 정리할까요?",
    "subtitle_text": "대화에 스타벅스와 다른 매장이 함께 나와요",
    # 대화에서 찾은 매장과 그 근거 문장 (시안 케이스 06 · 07)
    "found_stores": [
        {"name": "스타벅스", "evidence_text": '"스타벅스로 갈 예정이야"'},
        {"name": "메가커피", "evidence_text": '이지연 "스벅 말고 메가커피 가자"'},
    ],
    "guide_text": "하나만 골라 주세요. 고르면 바로 정리를 시작해요",
    "options": [
        {"value": "starbucks", "label": "스타벅스", "description": "스타벅스 메뉴판과 가격으로 정리해요"},
        {"value": "generic", "label": "일반 카페",
         "description": "흔한 카페 메뉴로 정리하고 예상 금액으로 보여줘요"},
        {"value": "other", "label": "기타", "needs_store_name": True,
         "description": "매장 이름을 직접 적어 주세요",
         "hint_text": "메뉴가 아직 없는 매장이면 일반 카페 메뉴로 정리하고 예상 금액으로 보여줘요"},
    ],
    "cancel_text": "대화로 돌아가기",
}

BRAND = {
    "id": "starbucks",
    "label": "스타벅스 메뉴 기준",
    "source": "request",
    "evidence_text": "'스타벅스로 갈 예정이야'에서 찾았어요",
    "change_hint_text": "매장 기준을 바꾸려면 채팅으로 '일반 카페로 해줘'라고 보내 주세요",
}

SESSION_CREATED = {
    "session_id": "ORD-0012",
    "session_token": "preview-token",
    "snapshot_id": "snap_1",
    "status": "running",
    "version": 1,
    "brand": BRAND,
    "menu_version": "mv_20261029",
    "progress_url": "/api/sessions/ORD-0012/progress",
}


# --- SCR-002 케이스 08 ~ 10 — 진행 상태 --------------------------------------
def _steps(states, notes=None):
    labels = [
        ("read_chat", "대화 읽기", "intake"),
        ("match_menu", "메뉴판과 맞춰 보기", "menu"),
        ("check_changes", "바뀐 주문 · 빠진 정보 확인", "validation"),
        ("done", "정리 완료", None),
    ]
    notes = notes or {}
    return [
        {"key": k, "label": label, "state": states[i], "agent": agent, "note_text": notes.get(k)}
        for i, (k, label, agent) in enumerate(labels)
    ]


# 정본 시안 케이스 08 · 12의 단계 옆 메모
_STEP_NOTES = {"read_chat": "5명 · 메시지 7개", "match_menu": "스타벅스 메뉴 기준"}

PROGRESS = {
    # 케이스 08 정리 중
    "running": {
        "session_id": "ORD-0012",
        "status": "running",
        "status_line": {"tone": "info", "text": "대화를 정리하고 있어요. 금방 끝나요"},
        "steps": _steps(["done", "running", "pending", "pending"], _STEP_NOTES),
        "collapsed_text": None,
        "review_count": None,
        "trace_id": "tr_01JC8X7K2M",
    },
    # 케이스 09 복구 중 — 끝난 단계는 done을 유지한다
    "recovering": {
        "session_id": "ORD-0012",
        "status": "recovering",
        "status_line": {"tone": "warning",
                        "text": "잠깐 멈췄다가 다시 하고 있어요. 입력한 내용은 그대로 있어요"},
        "steps": [{**st, "warn_text": "잠깐 멈췄다가 다시 하고 있어요"} if st["key"] == "match_menu" else st
                  for st in _steps(["done", "running", "pending", "pending"], _STEP_NOTES)],
        "collapsed_text": None,
        "review_count": None,
        "trace_id": "tr_01JC8X7K2M",
    },
    # 케이스 10 처리 지연 — HTTP는 200이고 부분 결과를 주지 않는다
    "failed": {
        "session_id": "ORD-0012",
        "status": "failed",
        "status_line": {"tone": "danger",
                        "text": "잠시 멈춰 있어요. 입력한 내용은 그대로 있어요. "
                                "조금 뒤 이 링크로 다시 와 주세요"},
        "steps": _steps(["done", "failed", "pending", "pending"], _STEP_NOTES),
        "collapsed_text": None,
        "review_count": None,
        "trace_id": "tr_01JC8X7K2M",
    },
    # 케이스 11 확인 필요
    "needs_review": {
        "session_id": "ORD-0012",
        "status": "needs_review",
        "status_line": {"tone": "info", "text": "2건만 답해 주시면 돼요"},
        "steps": _steps(["done", "done", "done", "done"], _STEP_NOTES),
        "collapsed_text": "정리했어요",
        "review_count": 2,
        "trace_id": "tr_01JC8X7K2M",
    },
    # 케이스 17 확인 0건
    "awaiting_confirmation": {
        "session_id": "ORD-0012",
        "status": "awaiting_confirmation",
        "status_line": {"tone": "info", "text": "확인이 필요한 주문이 없어요. 주문서를 만들 수 있어요"},
        "steps": _steps(["done", "done", "done", "done"], _STEP_NOTES),
        "collapsed_text": "정리했어요",
        "review_count": 0,
        "trace_id": "tr_01JC8X7K2M",
    },
}

# --- SCR-002 케이스 11 · 12 — 분석 결과 (주문서 패널) ------------------------
SESSION = {
    "session_id": "ORD-0012",
    "status": "needs_review",
    "version": 7,
    "brand": BRAND,
    "summary": {"people": 5, "cups": 5, "review_count": 2,
                "estimated_total": 25800,
                "amount_notice": "예상 25,800원 · 참고용 금액이에요"},
    "rows": [
        {"item_id": "it_01", "person": "김민수", "menu_text": "아이스 아메리카노",
         "options": [{"label": "사이즈", "value": "톨"}], "custom_request": None,
         "estimated_price": 4500, "needs_review": False, "changed": False,
         "option_choices": [{"label": "사이즈", "values": ["톨", "그란데", "벤티"], "current": "톨"},
                            {"label": "얼음", "values": ["적게", "보통", "많이"], "current": "보통"},
                            {"label": "샷", "values": ["기본", "샷 추가"], "current": "기본"}]},
        {"item_id": "it_02", "person": "이지연", "menu_text": "아이스 바닐라라떼",
         "options": [{"label": "사이즈", "value": '"큰 거"', "ambiguous": True},
                     {"label": "옵션", "value": "얼음 적게"}],
         "custom_request": None, "estimated_price": None, "needs_review": True,
         "review_reason_text": "사이즈를 확인해 주세요", "changed": False,
         "option_choices": [{"label": "사이즈", "values": ["톨", "그란데", "벤티"], "current": None},
                            {"label": "얼음", "values": ["적게", "보통", "많이"], "current": "적게"}]},
        {"item_id": "it_03", "person": "박서연", "menu_text": "아이스 아메리카노",
         "options": [{"label": "사이즈", "value": "톨"}],
         "custom_request": "빨대는 빼 주시고 개인 텀블러에 담아 주세요, 얼음은 컵 반만 채워 주시면 좋겠어요",
         "estimated_price": 4500, "needs_review": False, "changed": False,
         "option_choices": [{"label": "사이즈", "values": ["톨", "그란데", "벤티"], "current": "톨"},
                            {"label": "얼음", "values": ["적게", "보통", "많이"], "current": "보통"},
                            {"label": "샷", "values": ["기본", "샷 추가"], "current": "기본"}]},
        {"item_id": "it_04", "person": "최유나", "menu_text": '"그걸로 하나"',
         "options": [{"label": "사이즈", "value": "그란데"}], "custom_request": None,
         "estimated_price": None, "needs_review": True,
         "review_reason_text": "메뉴를 확인해 주세요", "changed": False,
         "option_choices": [{"label": "사이즈", "values": ["톨", "그란데", "벤티"], "current": "그란데"}]},
        {"item_id": "it_05", "person": "정하준", "menu_text": "아이스 아메리카노",
         "options": [{"label": "사이즈", "value": "톨"}], "custom_request": None,
         "estimated_price": 4500, "needs_review": False, "changed": False,
         "option_choices": [{"label": "사이즈", "values": ["톨", "그란데", "벤티"], "current": "톨"}]},
    ],
    "input_summary": {"message_count": 8, "label_text": "넣은 대화 8개"},
    "ai_notice": "AI가 정리한 결과예요 · 행을 누르면 고칠 수 있어요",
    # 정본 시안 케이스 11 ~ 17의 채팅에는 **요약 한 줄만** 있다.
    # 관리 링크 안내 말풍선은 두지 않는다(2026-11-03 — projection.chat_intro 주석).
    "chat": [
        {"role": "assistant", "kind": "summary",
         "text": "5명 · 5잔으로 정리했어요. 왼쪽 주문서에서 확인하고 고칠 수 있어요"},
    ],
}

# --- SCR-002 케이스 11 · 13 — 확인 질문 (한 번에 하나) -----------------------
QUESTIONS = [
    {"question_id": "q1", "index": 1, "total": 2,
     "text": '이지연 님 바닐라라떼 "큰 거"는 어떤 사이즈인가요?',
     "options": [{"value": "그란데", "label": "그란데"}, {"value": "벤티", "label": "벤티"}],
     "item_id": "it_02",
     "applied_text_template": "이지연 님 바닐라라떼를 {answer}로 했어요."},
    {"question_id": "q2", "index": 2, "total": 2,
     "text": '최유나 님 "그걸로 하나"는 정하준 님이 말한 "민트 초코"로 이해했어요. 메뉴를 골라 주세요',
     "options": [{"value": "민트 초콜릿 칩 블렌디드", "label": "민트 초콜릿 칩 블렌디드"}],
     "allow_other_menu": True, "item_id": "it_04",
     "applied_text_template": "최유나 님 메뉴를 {answer}로 했어요."},
]

# --- SCR-002 케이스 12 — 넣은 대화 원본 보기 (이벤트 라벨 포함) --------------
EVIDENCE = {
    "messages": [
        {"message_id": "m1", "sender": "김민수", "text": "아이스 아메리카노 톨 2잔, 하나는 샷 추가요.",
         "event": {"type": "add", "label": "추가"}},
        {"message_id": "m2", "sender": "이지연", "text": "바닐라라떼 큰 거 얼음 적게요.",
         "event": {"type": "add", "label": "추가"}},
        {"message_id": "m3", "sender": "박서연",
         "text": "아아 톨 한 잔이요. 빨대는 빼 주시고 개인 텀블러에 담아 주세요, 얼음은 컵 반만 채워 주시면 좋겠어요!",
         "event": {"type": "add", "label": "추가"}},
        {"message_id": "m4", "sender": "최유나", "text": "여기 인기 있는 메뉴 뭐야?",
         "event": {"type": "ignore", "label": "주문 아님"}},
        {"message_id": "m5", "sender": "정하준", "text": "민트 초코가 유명하대",
         "event": {"type": "ignore", "label": "주문 아님 · 참고"}},
        {"message_id": "m6", "sender": "최유나", "text": "그럼 난 그걸로 하나~",
         "event": {"type": "add", "label": "추가"}},
        {"message_id": "m7", "sender": "김민수", "text": "아까 샷 추가한 한 잔 취소할게요.",
         "event": {"type": "cancel", "label": "취소"}},
        {"message_id": "m8", "sender": "정하준", "text": "나도 민수랑 같은 걸로!",
         "event": {"type": "add", "label": "추가"}},
    ],
}

# 원본 보기 · 대화 보기의 이벤트 배지 색 — 추가 초록 · 취소 빨강 · 주문 아님 회색 (시안 케이스 12)
EVENT_TONE = {"add": "green", "update": "green", "confirm": "green", "cancel": "red", "ignore": "gray"}


# --- SCR-002 케이스 14 — 채팅으로 고치기 결과 (서버 대역) ----------------------
CHAT_EDIT_EFFECT = {"menu_text": "아이스 카페 라떼", "estimated_price": 5000}


def session_summary(rows: list) -> dict:
    """패널 머리 — 정본 시안의 금액(25,800 · 채팅으로 라떼로 바꾸면 26,300)을 그대로 쓴다."""
    edited = any(r.get("menu_text") == CHAT_EDIT_EFFECT["menu_text"] for r in rows)
    total = 26300 if edited else 25800
    return {**SESSION["summary"], "estimated_total": total,
            "amount_notice": f"예상 {total:,}원 · 참고용 금액이에요"}


# --- SCR-002 케이스 15 — 메뉴 찾기 모달 --------------------------------------
MENUS = [
    {"menu_id": "sb_001", "name": "민트 초콜릿 칩 블렌디드", "spec_text": "톨", "estimated_price": 5700},
    {"menu_id": "sb_002", "name": "자바 칩 프라푸치노", "spec_text": "톨", "estimated_price": 5800},
    {"menu_id": "sb_003", "name": "아이스 카페 라떼", "spec_text": "톨", "estimated_price": 5000},
    {"menu_id": "sb_004", "name": "아이스 아메리카노", "spec_text": "톨", "estimated_price": 4500},
    {"menu_id": "sb_005", "name": "유자 민트 티", "spec_text": "톨", "estimated_price": 5400},
    {"menu_id": "sb_006", "name": "아이스 바닐라라떼", "spec_text": "톨", "estimated_price": 5100},
]
MENU_SCOPE_NOTICE = "스타벅스 메뉴 기준 메뉴만 보여줘요"

# --- SCR-005 케이스 18 ~ 21 — 주문서 (묶음 목록) ----------------------------
# 정본 시안은 두 벌을 쓴다.
#   base : 케이스 19 ~ 21 — 5명 · 5잔 · 예상 25,800 (SCR-002 결과와 이어지는 이야기)
#   many : 케이스 18   — 10명 · 10잔 · 예상 48,300 (같은 메뉴 7명 → `+2명 더 보기`)
# **공유 카드는 같은 벌에서 만든다** — 주문서는 10잔인데 카드는 5잔이면 데이터가 어긋난다.
_SIZE = ["톨", "그란데", "벤티"]
_ICE = ["적게", "보통", "많이"]
_SHOT = ["기본", "샷 추가"]


def _choices(size: str, ice: str) -> list:
    return [{"label": "사이즈", "values": _SIZE, "current": size},
            {"label": "얼음", "values": _ICE, "current": ice},
            {"label": "샷", "values": _SHOT, "current": "기본"}]


_AMERICANO_PEOPLE = ["김민수", "정하준", "한소희", "오태민", "윤가람", "서지후", "임나래"]
_AMERICANO_ITEMS = ["it_01", "it_05", "it_06", "it_07", "it_08", "it_09", "it_10"]


def _groups(case: str) -> list:
    count = 7 if case == "many" else 2
    return [
        {"group_id": "g1", "menu_text": "아이스 아메리카노",
         "options": [{"label": "사이즈", "value": "톨"}], "custom_request": None,
         "option_choices": _choices("톨", "보통"),
         "quantity": count, "estimated_amount": 4500 * count,
         "people": _AMERICANO_PEOPLE[:count], "item_ids": _AMERICANO_ITEMS[:count]},
        {"group_id": "g2", "menu_text": "아이스 바닐라라떼",
         "options": [{"label": "사이즈", "value": "벤티"}, {"label": "옵션", "value": "얼음 적게"}],
         "custom_request": None, "option_choices": _choices("벤티", "적게"),
         "quantity": 1, "estimated_amount": 6100, "people": ["이지연"], "item_ids": ["it_02"]},
        {"group_id": "g3", "menu_text": "아이스 아메리카노",
         "options": [{"label": "사이즈", "value": "톨"}],
         "custom_request": "빨대는 빼 주시고 개인 텀블러에 담아 주세요, 얼음은 컵 반만 채워 주시면 좋겠어요",
         "option_choices": _choices("톨", "보통"),
         "quantity": 1, "estimated_amount": 4500, "people": ["박서연"], "item_ids": ["it_03"]},
        {"group_id": "g4", "menu_text": "민트 초콜릿 칩 블렌디드",
         "options": [{"label": "사이즈", "value": "그란데"}], "custom_request": None,
         "option_choices": _choices("그란데", "보통"),
         "quantity": 1, "estimated_amount": 6200, "people": ["최유나"], "item_ids": ["it_04"]},
    ]


SHARED_STATUS_LINE = {"tone": "success", "text": "주문서를 공유했어요! 매장에 보여 주며 주문해 주세요"}


def order_sheet(*, shared: bool = False, case: str = "base") -> dict:
    """API-030 모의 — 합계 · 인원 · 잔 수를 묶음에서 세어 맞춘다."""
    groups = _groups(case)
    cups = sum(g["quantity"] for g in groups)
    people = sum(len(g["people"]) for g in groups)
    total = sum(g["estimated_amount"] for g in groups)
    return {
        "session_id": "ORD-0012",
        "status": "completed" if shared else "awaiting_confirmation",
        "version": 9,
        "brand": BRAND,
        "summary": {"people": people, "cups": cups},
        "groups": groups,
        "totals": {"cups": cups, "estimated_total": total,
                   "notice": "참고용 금액이에요. 실제 결제 금액과 다를 수 있어요",
                   "integrity_ok": True},
        "status_line": SHARED_STATUS_LINE if shared else
            {"tone": "info", "text": "단톡방에 공유해 확인받고 매장에는 이 주문서를 보여 주세요"},
        "can_share": True,
        "blocked_reason_text": None,
        "ai_notice": "AI가 정리한 결과예요 · 이름을 누르면 고칠 수 있어요",
    }


# --- SCR-005 케이스 20 — 답장 제안 · 공유 불가 -------------------------------
SUGGESTIONS = {
    "found_text": "바뀐 주문 2건을 찾았어요",
    "ai_notice": "AI가 찾은 내용이에요",
    "suggestions": [
        {"suggestion_id": "sg1", "tone": "warning", "person": "이지연",
         "quote": "얼음 많이로 바꿔요", "change_text": "얼음 적게 → 많이",
         "change_label": "얼음", "before_text": "적게", "after_text": "많이",
         "actionable": True, "item_id": "it_02"},
        {"suggestion_id": "sg2", "tone": "question", "person": "최유나",
         "quote": "그거 말고 다른 거", "change_text": "어떤 메뉴인지 확인해 주세요",
         "actionable": False, "question_id": "q2"},
    ],
    "blocked_reason_text": "반영할지 정하지 않은 제안이 2건 있어요",
}

# --- SCR-005 케이스 21 — 공유 카드 (서버가 만든 내용) ------------------------
# **주문서와 같은 벌에서 만든다** — 내용은 서버가 만들고 화면은 그리기만 한다.
def share_text(case: str = "base") -> dict:
    """API-033 모의 — 텍스트와 카드 미리보기를 같은 자료에서 만든다."""
    sheet = order_sheet(case=case)
    groups, totals = sheet["groups"], sheet["totals"]

    def title(group: dict) -> str:
        spec = " ".join(o["value"] for o in group.get("options", []))
        return f'{group["menu_text"]} {spec}'.strip()

    head = f'커피 주문 · {BRAND["label"].replace(" 메뉴 기준", "")}'
    sub = (f'10/29 14:05 기준 · {sheet["summary"]["people"]}명 · '
           f'{sheet["summary"]["cups"]}잔')
    lines = [{"menu_text": title(g), "quantity": g["quantity"], "people": g["people"],
              "custom_request": g.get("custom_request")} for g in groups]
    total_text = f'합계 {totals["cups"]}잔 · 예상 {totals["estimated_total"]:,}원'

    # 복사 텍스트 — 서버(share.render_text)와 같은 번호 목록 형식(2026-11-06)
    count = f'{sheet["summary"]["people"]}명 · {sheet["summary"]["cups"]}잔'
    body = [f'[{BRAND["label"].replace(" 메뉴 기준", "")} 주문 정리]', "",
            f"주문 현황: {count}", f'예상 결제 금액: {totals["estimated_total"]:,}원']
    for index, g in enumerate(groups, start=1):
        size = next((o["value"] for o in g.get("options", []) if o.get("label") == "사이즈"), None)
        extras = [o["value"] for o in g.get("options", []) if o.get("label") != "사이즈"]
        body += ["", f'{index}. {g["menu_text"]}{f" ({size})" if size else ""} × {g["quantity"]}잔',
                 "주문자: " + ", ".join(g["people"])]
        body += [f"-{d}" for d in extras + ([g["custom_request"]] if g.get("custom_request") else [])]
    body += ["", f'총 {count} | 예상 {totals["estimated_total"]:,}원', "",
             "※ 금액은 참고용이며 실제 결제 금액과 다를 수 있습니다."]
    content = chr(10).join(body)
    return {
        "format": "text",
        "content": content,
        "card": {"head": head, "sub": sub, "lines": lines, "total_text": total_text,
                 "notice": totals["notice"], "foot": "Moa · 비공식 시연 서비스"},
        "image_filename": "주문_스타벅스_20261029_1405.png",
        "image_available": False,
        "image_unavailable_text": "이미지는 아직 만들지 못했어요. 텍스트 복사로 공유해 주세요",
    }


# --- SCR-006 운영 대시보드 — 정본 시안 `Moa_운영대시보드.svg` 5개 상태 ----------------
# 24 승인 필요 · 25-1 F-1 주입 직후 · 25-2 F-1 복구 중 · 25-3 F-1 복구 완료(대체 해석기) · 26 정상 운영
# 화면 테스트와 미리보기는 `_preview_case` 세션 값(아래 OPS_PREVIEW_CASES 의 라벨)으로 상태를 고른다.
# 아래 칸 중 서버에 아직 없는 것(G-1 replicas · G-3/G-4 trials · integrity_issue_count · active_injection ·
# alerts · ago_text · elapsed_sec)은 화면이 기대하는 모양을 보여 주는 계약 견본이기도 하다.
OPS_PREVIEW_CASES = {
    "24 승인 필요": "approval",
    "25-1 주입 직후": "injected",
    "25-2 복구 중": "recovering",
    "25-3 복구 완료": "recovered",
    "26 정상 운영": "normal",
}

_REPLICAS_UP = [{"key": "app_a", "name": "앱 A", "state": "up", "active": True},
                # SC-12 — 복제본 전환은 감지 · 전환 각 15초 이내
                {"key": "app_b", "name": "앱 B", "state": "up", "active": True, "switch_sec": 11}]


def _svc(key, name, state, *, active=False, text="", probe=None, streak=0):
    return {"key": key, "name": name, "state": state, "state_text": text, "active": active,
            "probe_in_sec": probe, "healthy_streak": streak}


_PRIMARY_UP = _svc("order_interpreter_primary", "주문 해석 1차", "up", active=True, text="정상")
_BACKUP_STANDBY = _svc("order_interpreter_backup", "주문 해석 대체", "up", text="정상")
_BACKUP_ACTIVE = _svc("order_interpreter_backup", "주문 해석 대체", "up", active=True, text="정상")
_STORE_UP = _svc("menu_store", "메뉴 저장소", "up", active=True, text="정상")


def _status(services, *, active, injection=None, alerts=None):
    return {"updated_at_text": "14:02:16", "environment_label": "실습 환경", "replicas": _REPLICAS_UP,
            "services": services, "active_interpreter": active, "cache_fallback_active": False,
            "probe_need": 3, "active_injection": injection, "alerts": alerts or []}


def _inc(iid, fault, code, state, text, ago, *, elapsed=None, approved=False, detected="14:00:02"):
    return {"incident_id": iid, "fault": fault, "fault_code": code, "state": state, "state_text": text,
            "ago_text": ago, "elapsed_sec": elapsed, "approved": approved,
            "detected_at_text": detected, "resolved_at_text": "-"}


_INC_5 = _inc("INC-0005", "F-6", "DB_UNAVAILABLE", "failed", "실패 · 수동 점검", "34분 전", elapsed=64)
_INC_6 = _inc("INC-0006", "F-3", "WORKER_DOWN", "recovered", "복구 성공", "12분 전", elapsed=27)
# UNKNOWN 은 F 코드가 아니라 원인 미확인 에스컬레이션이다(PRD SC-6) — 코드 자리에 그대로 쓴다
_INC_7_WAIT = _inc("INC-0007", "UNKNOWN", "UNKNOWN", "awaiting_approval", "승인 대기", "2분 전")
_INC_7_DONE = _inc("INC-0007", "UNKNOWN", "UNKNOWN", "recovered", "승인 후 복구", "9분 전", elapsed=148,
                   approved=True)


def _cells(*values):
    """41 → 사람 없이 복구 41초 · 그 밖은 결과 이름(approved · failed · invalid · running · pending)."""
    return [{"result": "auto", "sec": v} if isinstance(v, int) else {"result": v} for v in values]


def _trials(f1_last):
    return {
        "total_runs": 25,
        "rows": [
            {"fault": "F-1", "cells": _cells(41, 38, 52, "approved", f1_last)},
            {"fault": "F-2", "cells": _cells(24, 29, 22, 31, 27)},
            {"fault": "F-3", "cells": _cells(66, "approved", 81, "failed", 74)},
            {"fault": "F-5", "cells": _cells(35, 44, "invalid", 72, 39)},
            {"fault": "F-6", "cells": _cells(48, 57, "failed", 52, "pending")},
        ],
        # F-4 는 무인 복구율 분모 밖이다(PRD 9.1) — 안전 전환 여부만
        "f4": {"fault": "F-4", "cells": [{"result": "switched"}, {"result": "switched"},
                                         {"result": "pending"}, {"result": "pending"}, {"result": "pending"}]},
        # 어느 실행의 숫자인지 — D 제목줄 오른쪽에 그대로 나온다
        "latest_run": {"run_id": "run_2ec6c62a", "ok": 23, "valid": 23,
                       "avg_mttd_ms": 5843, "avg_mttr_ms": 14705},
    }


_METRICS = {
    "metrics": [{"label": "MTTD", "value_text": "8s"}, {"label": "MTTR", "value_text": "41s"},
                {"label": "성공률", "value_text": "82%"}, {"label": "에스컬레이션", "value_text": "2"}],
    "detail": {"mttd_sec": 8.2, "mttr_sec": 41.3, "mttr_max_sec": 81, "integrity_issue_count": 0,
               "escalation_count": 2, "incident_count": 22},
}

_APPROVAL_7 = {
    "incident_id": "INC-0007", "fault": "UNKNOWN", "summary": "원인 미확인 장애", "waiting_sec": 134,
    "waiting_text": "2분째 대기",
    "description_text": "해석기 1차 · 대체 모두 응답이 없어 자동 복구를 멈췄어요. 지금은 규칙 해석으로 주문을 받고 있어요",
    "recommendation_text": "추천: 점검 후 1차 해석기 재개 · 근거 3건 (응답 없음 ×3) · 회로 시험까지 42초",
}


def _detail(iid, fault, code, *, det, start, end=None, failed=None, wait=None, summary):
    secs = {"detected": det, "recovery_started": start}
    if end is not None:
        secs["resolved"] = end
    if failed is not None:
        secs["failed"] = failed
    return {"incident_id": iid, "fault": fault, "fault_code": code, "timeline_sec": secs,
            "approval": {"waiting_sec": wait} if wait else None, "summary": summary}


_DETAILS = {
    "INC-0005": _detail("INC-0005", "F-6", "DB_UNAVAILABLE", det=6, start=7, failed=64, summary={
        "diagnosis": "DB 연결 3회 연속 실패", "strategy": "재연결 후 캐시 조회",
        "attempts": "재연결 3회 · 모두 실패", "verification": "캐시 조회 실패 · 수동 점검"}),
    "INC-0006": _detail("INC-0006", "F-2", "WORKER_DOWN", det=8, start=9, end=27, summary={
        "diagnosis": "워커 응답 없음 2회 연속", "strategy": "워커 재시작",
        "attempts": "재시도 1 · 2초 → 2회째 성공", "verification": "헬스체크 3/3 · 유실 0"}),
    "INC-0007": _detail("INC-0007", "F-3", "UNKNOWN", det=9, start=10, end=148, wait=134, summary={
        "diagnosis": "해석기 1차 · 대체 모두 응답 없음", "strategy": "운영자 승인 후 1차 해석기 재개",
        "attempts": "재시도 3 → 승인 후 성공", "verification": "헬스체크 3/3 · 유실 0"}),
    "INC-0008": _detail("INC-0008", "F-1", "DEP_HTTP_5XX", det=8, start=9, end=41, summary={
        "diagnosis": "1차 해석기 5xx 응답 3회 연속", "strategy": "대체 해석기로 전환",
        "attempts": "재시도 2 · 1초 → 대체 전환 성공", "verification": "헬스체크 3/3 · 유실 0"}),
}


def ops_scene(name: str = "approval", *, injected_sec: int | None = None) -> dict:
    """정본 시안 상태 하나의 응답 묶음 — status · incidents(+approvals) · metrics · trials · details.

    `injected_sec` 는 미리보기에서 [F-1] 을 누른 뒤 지난 초다. 주어지면 배너 · 경과 시간에 그 값을 쓴다.
    """
    primary_blocked = _svc("order_interpreter_primary", "주문 해석 1차", "blocked",
                           text="차단 · 42초 후 시험", probe=42)
    details = dict(_DETAILS)
    if name == "approval":
        backup_dead = _svc("order_interpreter_backup", "주문 해석 대체", "blocked", text="차단 · 응답 없음")
        status = _status([primary_blocked, backup_dead, _STORE_UP], active=None,
                         alerts=[{"text": "최근 5분 주문 해석 실패율 12% (기준 10%)"}])
        incidents = [_INC_7_WAIT, _INC_6, _INC_5]
        approvals = [_APPROVAL_7]
        details["INC-0007"] = {**_DETAILS["INC-0007"], "timeline_sec": {"detected": 9, "recovery_started": 10}}
        trials = _trials("pending")
    elif name == "injected":
        sec = 3 if injected_sec is None else injected_sec
        status = _status([_PRIMARY_UP, _BACKUP_STANDBY, _STORE_UP], active="order_interpreter_primary",
                         injection={"code": "F-1", "run_no": 5, "elapsed_sec": sec})
        incidents, approvals, trials = [_INC_7_DONE, _INC_6, _INC_5], [], _trials("running")
    elif name == "recovering":
        sec = 22 if injected_sec is None else injected_sec     # 주입 0초 기준 — 타임라인 · MTTR 과 같은 기준
        probe = max(1, 42 - sec)
        status = _status([_svc("order_interpreter_primary", "주문 해석 1차", "blocked",
                               text=f"차단 · {probe}초 후 시험", probe=probe),
                          _BACKUP_ACTIVE, _STORE_UP], active="order_interpreter_backup",
                         injection={"code": "F-1", "run_no": 5, "elapsed_sec": sec})
        inc8 = _inc("INC-0008", "F-1", "DEP_HTTP_5XX", "recovering", "복구 중", "방금", elapsed=sec)
        incidents, approvals, trials = [inc8, _INC_7_DONE, _INC_6, _INC_5], [], _trials("running")
        details["INC-0008"] = {**_DETAILS["INC-0008"], "timeline_sec": {"detected": 8, "recovery_started": 9}}
    elif name == "recovered":
        status = _status([_svc("order_interpreter_primary", "주문 해석 1차", "probing", text="복귀 확인", streak=2),
                          _BACKUP_ACTIVE, _STORE_UP], active="order_interpreter_backup")
        inc8 = _inc("INC-0008", "F-1", "DEP_HTTP_5XX", "recovered", "복구 성공", "방금", elapsed=41)
        incidents, approvals, trials = [inc8, _INC_7_DONE, _INC_6, _INC_5], [], _trials(44)
    else:  # normal
        status = _status([_PRIMARY_UP, _BACKUP_STANDBY, _STORE_UP], active="order_interpreter_primary")
        incidents, approvals, trials = [_INC_7_DONE, _INC_6, _INC_5], [], _trials("pending")
    return {"name": name, "status": status, "incidents": {"incidents": incidents, "approvals": approvals},
            "metrics": _METRICS, "trials": trials, "details": details}


# 예전 이름 — 다른 코드 · 테스트가 쓰는 기본 상태(24 승인 필요)
_DEFAULT_SCENE = ops_scene("approval")
OPS_STATUS = _DEFAULT_SCENE["status"]
OPS_INCIDENTS = {"incidents": _DEFAULT_SCENE["incidents"]["incidents"]}
OPS_APPROVALS = {"approvals": _DEFAULT_SCENE["incidents"]["approvals"]}
OPS_METRICS = _DEFAULT_SCENE["metrics"]
OPS_TRIALS = _DEFAULT_SCENE["trials"]

OPS_ORDERS = {
    "orders": [
        {"order_id": "ORD-0012", "brand_label": "스타벅스", "state_text": "주문 확정",
         "confirmed_at_text": "14:20", "change_count_text": "4건", "recovery_count_text": "1건"},
        {"order_id": "ORD-0011", "brand_label": "일반 카페", "state_text": "확인 받는 중",
         "confirmed_at_text": "-", "change_count_text": "1건", "recovery_count_text": "0건"},
        {"order_id": "ORD-0010", "brand_label": "스타벅스", "state_text": "주문 확정",
         "confirmed_at_text": "12:40", "change_count_text": "2건", "recovery_count_text": "0건"},
    ],
}

# --- SCR-006 케이스 23 — O-6 상세 펼침 --------------------------------------
OPS_ORDER_DETAIL = {
    "ORD-0012": {
        "order_id": "ORD-0012",
        # 정본 시안 케이스 23 — 시각순 5건
        "changes": [
            {"at_text": "14:03", "who": "시스템", "before_after": "주문 해석 1차 → 대체 경로",
             "method_text": "자동 복구", "incident_id": "INC-0007"},
            {"at_text": "14:06", "who": "김민수", "before_after": "아메리카노 2잔 → 1잔",
             "method_text": "대화에서 취소", "incident_id": None},
            {"at_text": "14:08", "who": "이지연", "before_after": '사이즈 "큰 거" → 벤티',
             "method_text": "직접 선택", "incident_id": None},
            {"at_text": "14:09", "who": "박서연", "before_after": "아이스 아메리카노 → 아이스 카페 라떼",
             "method_text": "채팅으로 수정", "incident_id": None},
            {"at_text": "14:15", "who": "이지연", "before_after": "얼음 적게 → 많이",
             "method_text": "답장 반영", "incident_id": None},
        ],
    },
    "ORD-0011": {
        "order_id": "ORD-0011",
        "changes": [
            {"at_text": "13:58", "who": "한소희", "before_after": "아이스 라떼 → 아이스 아메리카노",
             "method_text": "채팅으로 수정", "incident_id": None},
        ],
    },
}


# --- 파일 판정 흉내 (서버 대역) ----------------------------------------------
# 실제로는 **서버가 내용으로 판정한다**(D-02 API-011 · P-06 Streamlit 구현 대응).
# 미리보기 모드에서 서버가 없을 때만 그 자리를 메운다. 화면 코드는 이 판정을 하지 않는다.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif")

FILE_ERRORS = {
    "IMAGE_NOT_SUPPORTED": "사진은 읽지 못해요. 대화를 복사해 붙여넣거나 .txt 파일로 올려 주세요",
    "FILE_NOT_SUPPORTED": "이 파일은 읽을 수 없어요. 대화를 복사해 붙여넣어 주세요",
}


def judge_file(filename: str, data: bytes):
    """돌려주는 값: `(raw_text, None)` 또는 `(None, 에러코드)`."""
    lowered = (filename or "").lower()
    if lowered.endswith(_IMAGE_SUFFIXES) or data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0"):
        return None, "IMAGE_NOT_SUPPORTED"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "FILE_NOT_SUPPORTED"
    if "\x00" in text:
        return None, "FILE_NOT_SUPPORTED"
    return text, None


# --- 채팅 입력 해석 흉내 (서버 대역) ----------------------------------------
# 실제 해석은 서버(API-022)가 한다. 화면은 입력 문장을 그대로 보내고 받은 안내만 그린다
# (P-06 프론트엔드 원칙 "채팅 입력 해석은 백엔드가 한다").
# 아래는 미리보기 모드에서 케이스 13 · 14 · 16을 눈으로 확인하기 위한 대역이며,
# 서버가 붙으면 타지 않는다.
_BRAND_WORDS = {"일반 카페": "generic", "일반카페": "generic", "스타벅스": "starbucks", "스벅": "starbucks"}


def interpret(text: str) -> dict:
    """케이스 14(바로 반영) · 16(매장 기준 바꾸기) · 무관한 말 3분기."""
    for word, brand_id in _BRAND_WORDS.items():
        if word in text and ("로 해" in text or "기준" in text or "로 바꿔" in text):
            label = "일반 카페 메뉴 기준" if brand_id == "generic" else "스타벅스 메뉴 기준"
            # 결과를 새로 만들므로 이때만 한 번 묻는다 (U-5)
            return {"kind": "brand_change_ask", "brand_id": brand_id,
                    "text": f"{label}으로 새로 정리할까요?",
                    "guide_text": "지금 정리한 결과와 답한 내용은 새로 정리돼요. 넣은 대화는 그대로예요",
                    "options": [{"value": "apply", "label": "새로 정리"},
                                {"value": "cancel", "label": "아니요"}]}
    if any(word in text for word in ("바꿔", "빼", "취소", "추가")):
        return {"kind": "applied",
                "text": "박서연 님 주문을 아이스 아메리카노 (톨)에서 아이스 카페 라떼 (톨)로 바꿨어요.",
                "undo_token": "preview-undo", "changed_item_ids": ["it_03"]}
    # 주문과 무관한 말 — 주문은 그대로 둔다
    return {"kind": "no_change", "text": "주문에서 바꿀 것을 찾지 못했어요", "undo_token": None}


def answer_result(question: dict, answer: str) -> dict:
    """확인 질문에 답한 결과 — "전 → 후" 안내와 되돌리기 토큰 (케이스 13)."""
    template = question.get("applied_text_template", "{answer}로 했어요.")
    return {
        "text": template.format(answer=answer),
        "undo_token": "preview-undo",
        "changed_item_ids": [question.get("item_id")] if question.get("item_id") else [],
    }


# --- 분석 요청 흉내 (서버 대역) ----------------------------------------------
# 매장 기준 판별은 **서버가 규칙으로** 한다(API-012 · F-024 · LLM 호출 0건).
# 아래는 미리보기에서 케이스 06 · 07(되묻기)을 눈으로 확인하기 위한 대역이다.
_STORE_WORDS = {"스타벅스": "starbucks", "스벅": "starbucks",
                "메가커피": None, "이디야": None, "투썸": None, "컴포즈": None}


def create_session(raw_text: str) -> dict:
    """같은 단계에 스타벅스와 다른 매장 이름이 함께 나오면 되묻는다.

    되묻기는 오류가 아니라 **물을 것이 생긴 정상 결과**다(D-02 2.3).
    """
    found = {word for word in _STORE_WORDS if word in (raw_text or "")}
    has_starbucks = any(_STORE_WORDS[w] == "starbucks" for w in found)
    has_other = any(_STORE_WORDS[w] is None for w in found)
    if has_starbucks and has_other:
        return {"session_id": "ORD-0012", "session_token": "preview-token",
                "status": "awaiting_brand_answer", "brand_question": BRAND_QUESTION}
    if has_other and not has_starbucks:
        # 미지원 매장만 나오면 묻지 않고 일반 카페 기준으로 정리한다
        name = next(w for w in found if _STORE_WORDS[w] is None)
        return {**SESSION_CREATED, "brand": {
            **BRAND, "id": "generic", "label": "일반 카페 메뉴 기준", "source": "unsupported",
            "evidence_text": f"{name} 메뉴는 아직 없어 일반 카페 메뉴로 정리했어요",
            "change_hint_text": "다른 기준이면 채팅으로 '스타벅스로 해줘'라고 보내 주세요"}}
    if not has_starbucks:
        return {**SESSION_CREATED, "brand": {
            **BRAND, "id": "generic", "label": "일반 카페 메뉴 기준", "source": "default",
            "evidence_text": "매장 이름이 없어 일반 카페 메뉴로 정리했어요",
            "change_hint_text": "다른 기준이면 채팅으로 '스타벅스로 해줘'라고 보내 주세요"}}
    return SESSION_CREATED


def evidence_for(question: dict) -> dict:
    """`대화 보기` — 그 질문의 근거가 된 메시지만 추린다 (정본 시안 케이스 12).

    실제로는 서버(API-025)가 참고한 대화까지 묶어 준다.
    """
    wanted = {"q1": ["m2"], "q2": ["m5", "m6"]}.get(question.get("question_id"), [])
    return {"messages": [m for m in EVIDENCE["messages"] if m["message_id"] in wanted]}


# --- 답한 뒤의 결과 흉내 (서버 대역) ----------------------------------------
# 실제로는 API-015가 이미 반영된 결과를 준다. 미리보기에서 케이스 13 · 17을
# 왼쪽 패널까지 맞춰 보여주기 위한 대역이다.
ANSWER_EFFECTS = {
    "it_02": {"options": [{"label": "사이즈", "value": "벤티"},
                          {"label": "옵션", "value": "얼음 적게"}],
              "estimated_price": 6100},
    "it_04": {"menu_text": "민트 초콜릿 칩 블렌디드",
              "options": [{"label": "사이즈", "value": "그란데"}],
              "estimated_price": 6200},
}


def rows_after(answered: int) -> list:
    """앞에서부터 `answered`개 질문에 답했을 때의 주문서 행.

    `answered` 가 대역 질문 수를 넘어도 터지지 않는다 — 서버 질문 수가 대역보다 많을 수 있다.
    """
    answered = max(0, min(int(answered or 0), len(QUESTIONS)))
    done = {q["item_id"] for q in QUESTIONS[:answered]}
    # 노란 강조는 **방금 바뀐 행 하나**다 (시안 케이스 13 · 17)
    last = QUESTIONS[answered - 1]["item_id"] if answered else None
    rows = []
    for row in SESSION["rows"]:
        if row["item_id"] in done:
            rows.append({**row, **ANSWER_EFFECTS.get(row["item_id"], {}),
                         "needs_review": False, "review_reason_text": None,
                         "changed": row["item_id"] == last})
        else:
            rows.append(row)
    return rows


# --- O-2 사건 상세 (API-043) — 상태와 무관하게 같은 사건은 같은 상세다
OPS_INCIDENT_DETAIL = _DETAILS
