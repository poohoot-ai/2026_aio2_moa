"""주문 이벤트 추출 — 메시지 1건 → 이벤트 1건 (P-03 3절 경로 5종).

★ 지금은 규칙 기반이다. LLM 교체 지점은 `extract_events` 하나다.
  D-04 에서 `llm.py` 의 `generate_structured` 를 붙이면 이 함수의 본문만 바뀌고
  호출자(그래프 · API)는 바뀌지 않는다. 계약(돌려주는 이벤트 모양)을 먼저 고정한다.

경로 5종 — 문자열을 바꾸지 않는다. State 필드 값 · 분류기 라벨 · DB CHECK ·
Q-02 expected_event 가 모두 같은 소문자 슬러그여야 한다.
"""

import re

from app import config
from app.services.text_norm import parse_quantity

EVENT_TYPES = ("add", "update", "cancel", "confirm", "ignore")

# --- 신호 어휘 ---------------------------------------------------------------
# "제외" · "지워" · "삭제" 는 담당자가 채팅으로 쓰는 말이다 — "박서연 과장님은 제외"(2026-11-05 2차 보고 7번).
# 메뉴 사양 요청의 "휘핑 제외"와 글자가 겹치지만, 그쪽은 order_text 가 기타 요청 구절로 먼저 떼어 낸다(_REQ_MOD)
_CANCEL = ("취소", "안 먹", "안먹", "빼 주", "빼주", "빼줘", "안 마", "없던", "뺄게", "뺄래",
           "제외", "지워", "삭제")
_UPDATE = ("바꿔", "바꿀", "변경", "말고", "대신", "으로 해", "로 해줘", "수정")
_CONFIRM = ("확정", "이대로", "맞아", "그대로 해", "주문 넣", "다 됐")
_REFERENCE = ("그걸로", "그거로", "같은 거", "같은걸", "같은 걸", "나도", "저도", "동일")
_QUESTION = ("?", "뭐야", "뭐예요", "어때", "있나요", "추천", "유명")
# 제안 · 의향 묻기 — 주문 문형(수량 · 주문 동사)이 있어도 주문이 아니다.
#   "다 같이 커피 한잔 어떠세요"가 주문으로 올라와 항목이 됐다(2026-11-05 사용자 보고 2번).
#   물음표 없이 쓰는 경우가 많아 _QUESTION 과 따로 둔다
_PROPOSAL = re.compile(
    r"어떠|어때|어떨|할까|갈까|볼까|괜찮|시킬까|마실까|먹을까|"
    # 권유 어미 — "커피 한잔 하실래요?" (2026-11-05 2차 보고 1번).
    # **낱말을 적어 둔다** — `[가-힣]실래요` 로 묶으면 "마실래요"(본인 주문)까지 제안으로 읽힌다
    r"하실래|하실까|가실래|가실까|드실래|드실까|하시겠|가시겠|드시겠|하시죠|가시죠|합시다|하자|가자|"
    # 확인하는 물음 — "스타벅스에서 주문하는 거죠?" · "맞죠?" · "어디로 가나요?"
    r"거죠|거지\?|맞죠|맞나요|맞지|인가요|인가\?|건가요|나요\?|난가요")
# 주문 문형 — 이것이 있으면 메뉴를 못 알아들었어도 주문으로 보고 담당자에게 묻는다(보고 2번).
#   "요"로 끝나는 것만으로는 주문이 아니다("어디로 가나요")
_ORDER_VERB = re.compile(r"주세요|주시|부탁|먹을래|마실래|먹을게|마실게|먹고\s*싶|마시고\s*싶|"
                         # "주문"만으로는 주문이 아니다 — "주문하는 거죠?"가 주문 문형으로 읽혔다
                         # (2026-11-05 2차 보고 1번). 주문하겠다는 어미가 붙었을 때만 센다
                         r"시킬게|시킬래|할게요|할래요|주문할게|주문할래|주문이요|주문해\s*주|골라|담아")
_ORDER_QTY = re.compile(r"-?\d+\s*(?:잔|개|컵)|[한두세네]\s*잔|하나|둘")
# 잔 수를 **세는** 말 — "일단 10잔으로 확인되네요" · "총 8잔이죠?" (2026-11-05 5차 보고).
# 수량이 있지만 주문이 아니라 담당자의 집계다. 주문 문형에서 뺀다
TALLY = re.compile(r"(?:총|모두|전부|일단|다\s*합쳐|합쳐서)\s*\d+\s*잔|"
                   r"\d+\s*잔\s*(?:으로|이)?\s*(?:확인|맞|정리|되네|이네|이에요|입니다|이죠|인가|인데)")

# 기타 요청 — 금액에 반영하지 않는다 (P-06)
#   "텀블러"는 뺐다 — 개인컵 여부(cup)가 메뉴판 옵션이 됐다(2026-11-04). 남겨 두면 "아아 텀블러로"
#   문장이 통째로 기타 요청이 되어 메뉴 표현에서 지워진다
_CUSTOM_HINT = ("빨대", "담아", "채워", "따로", "포장", "캐리어", "휴지", "컵홀더",
                "데워", "덜어", "나눠")

# 기타 요청 옵션 표현 — 메뉴판 옵션(size · ice · shot · bean · cup)이 아닌 사양 요청 (2026-11-04)
#   출처: 스타벅스_메뉴별_옵션정리.xlsx 의 시럽 · 우유 · 휘핑·드리즐 · 얼음·컵·기타 열.
#   _CUSTOM_HINT 는 **문장** 단위로 떼지만, 이것은 **구절** 단위로 뗀다 — "아이스 라떼 휘핑 많이"처럼
#   메뉴와 한 문장에 붙어 오기 때문이다. 떼지 않으면 엄격 매칭이 "메뉴판에 없는 말"로 보고 메뉴를 확정하지 않고,
#   요청 문구도 어디에도 남지 않았다. 떼어 낸 구절은 원문 그대로 기타 요청이 된다(금액 미반영).
#   "우유" · "카라멜" · "바닐라" 처럼 메뉴 이름에 들어가는 낱말은 혼자서는 잡지 않는다 — 요청 낱말과 붙어 있을 때만
_REQ_MOD = (r"(?:\s*(?:\d+|한|두|세|네)\s*(?:펌프|번))?"
            r"(?:\s*(?:은|는|을|를|도|만))?"
            r"(?:\s*(?:적게|조금|많이|듬뿍|가득|빼(?:고|서|주시고|\s*주세요|\s*줘|줘|주세요)?|없이|"
            r"추가(?:\s*해\s*(?:주세요|줘))?|올려(?:\s*(?:주세요|줘))?|넣어(?:\s*(?:주세요|주시고|줘))?|"
            r"제외(?:\s*(?:하고|해\s*(?:주세요|줘)))?|"
            r"변경|바꿔(?:\s*(?:주세요|줘))?|으로|로))?")
_REQUEST_RULES = [re.compile(p + _REQ_MOD) for p in (
    r"(?:(?:바닐라|헤이즐넛|카라멜|클래식|돌체|화이트\s*모카|모카)\s*)?시럽",           # 시럽 열
    r"(?:저지방|무지방|락토프리)(?:\s*우유)?",                                          # 우유 열
    r"우유\s*(?:양\s*)?(?=적게|조금|많이)",
    r"(?:일반\s*)?휘핑(?:\s*크림)?",                                                    # 휘핑·드리즐 열
    r"콜드\s*폼",
    r"(?:(?:카라멜|초콜릿|초코)\s*)?드리즐",
    r"리스트레또",                                                                      # 원두·샷 열
    r"돔\s*리드",                                                                       # 얼음·컵·기타 열
    r"(?:매장|일회용)\s*컵",
    r"(?:\d+|한|두|세|네)\s*펌프",
)]

# 모호한 사양 표현 — 따옴표째 유지한다 (P-06)
#   어느 매장 기준에도 없는 사이즈 표현은 기본 사이즈로 채우지 않고 묻는다.
#   "숏"은 일부 스타벅스 메뉴(따뜻한 티 · 초콜릿 등)에만 있다(2026-11-04 가격표). 메뉴마다 갈리므로
#   바로 정하지 않고 묻는다 — 확인 질문이 그 메뉴의 사이즈 버튼(숏 포함 여부)을 보여 준다
_AMBIGUOUS_SIZE = ("큰 거", "큰거", "작은 거", "작은거", "제일 큰", "제일 작은", "보통 거",
                   "중간 사이즈", "중간사이즈", "중간 거", "중간거", "중간으로",
                   "미디엄", "스몰", "숏")
# 한 글자 영문 사이즈(M · S · L)는 앞뒤가 영문이 아닐 때만 — "MD" 같은 낱말에 걸리지 않게
_AMBIGUOUS_SIZE_LETTER = re.compile(r"(?<![A-Za-z])[MSL](?![A-Za-z])")

# 온도 — 메뉴 이름이 온도를 품는다("아이스 카페 라떼" / "카페 라떼"). 별도 옵션이 아니다
#   차가운 · 시원한 · ice 는 "아이스"로 읽는다. 바꾸지 않으면 "차가운 라떼"가 별칭 "라떼"
#   (따뜻한 카페 라떼)에 걸려 **반대 온도로 확정된다**(실측 2026-11-03)
_COLD_WORDS = re.compile(r"차가운|차갑게|시원한|시원하게|(?<![A-Za-z])ice(?![A-Za-z])",
                         re.IGNORECASE)
_HOT_WORDS = re.compile(r"따뜻한|따뜻하게|뜨거운|뜨겁게|(?<![가-힣])핫(?![가-힣])|"
                        r"(?<![A-Za-z])hot(?![A-Za-z])", re.IGNORECASE)
_ICE_WORDS = re.compile(r"아이스|차가운|차갑게|시원한|시원하게|(?<![A-Za-z])ice(?![A-Za-z])",
                        re.IGNORECASE)

# 특성 표현 → trait_tags (FR-004)
#
# ★ 어휘를 좁게 유지한다. `티` · `차` 처럼 짧은 글자를 넣으면 "아이스티" · "자스민 그린 티"
#   같은 **정확한 메뉴 이름**에 걸려 특성 표현으로 오분류되고, 메뉴가 정해졌는데도
#   "메뉴를 골라 주세요" 질문이 뜬다(실측). 담당자가 취향을 말한 경우만 잡는다.
_TRAIT_WORDS = {
    "달달": "단맛", "달콤": "단맛", "단 거": "단맛", "단거": "단맛", "단걸": "단맛",
    "고소한": "고소한",
    "카페인 없": "카페인없음", "디카페인": "카페인없음", "논카페인": "카페인없음",
    "상큼": "과일", "과일 맛": "과일",
    "따뜻한 거": "따뜻한", "뜨거운 거": "따뜻한",
    "티 종류": "티", "차 종류": "티",
}

# 옵션 추출 — (정규식, option_group, option_value)
_OPTION_RULES = [
    (re.compile(r"벤티"), "size", "venti"),
    (re.compile(r"그란데"), "size", "grande"),
    (re.compile(r"톨"), "size", "tall"),
    (re.compile(r"라지|큰 사이즈"), "size", "large"),
    (re.compile(r"레귤러|보통 사이즈"), "size", "regular"),
    # "얼음 빼"는 얼음 없이다 — 예전에는 적게로 읽었다(2026-11-04 · 얼음 없이 값 추가)
    (re.compile(r"얼음\s*(?:은|을)?\s*(?:없이|빼)|얼음없이|노\s*아이스"), "ice", "none"),
    (re.compile(r"얼음\s*(?:은|을)?\s*(?:적게|조금)|얼음적게|얼음 조금"), "ice", "less"),
    (re.compile(r"얼음\s*(?:은|을)?\s*많(?:이|게)|얼음많이"), "ice", "more"),
    (re.compile(r"샷\s*추가|샷추가|샷\s*하나\s*더|투샷|샷 2"), "shot", "extra"),
    # 원두 — 1/2 디카페인을 디카페인보다 먼저 본다
    (re.compile(r"1/2\s*디카페?인|하프\s*디카페?인|반\s*디카페?인"), "bean", "half_decaf"),
    (re.compile(r"디카페인|디카페"), "bean", "decaf"),
    (re.compile(r"블론드"), "bean", "blonde"),
    (re.compile(r"개인\s*컵|텀블러"), "cup", "personal"),
    (re.compile(r"시럽\s*추가|시럽추가"), "syrup", "add"),
    (re.compile(r"오트\s*(?:우유|밀크)|오트밀크"), "milk", "oat"),
    (re.compile(r"두유|소이"), "milk", "soy"),
]

# 메뉴판에 그 종류가 없는 옵션을 기타 요청으로 옮길 때 쓰는 말 — 원문 조각이 없을 때(행 편집 · 답변 경로)
OPTION_WORDS = {
    ("size", "short"): "숏", ("size", "tall"): "톨", ("size", "grande"): "그란데", ("size", "venti"): "벤티",
    ("size", "regular"): "레귤러", ("size", "large"): "라지",
    ("ice", "none"): "얼음 없이", ("ice", "less"): "얼음 적게", ("ice", "more"): "얼음 많이",
    ("shot", "extra"): "샷 추가",
    ("bean", "decaf"): "디카페인", ("bean", "half_decaf"): "1/2 디카페인", ("bean", "blonde"): "블론드",
    ("cup", "personal"): "개인컵",
    ("syrup", "add"): "시럽 추가", ("milk", "oat"): "오트 우유", ("milk", "soy"): "두유",
}


def _has(text: str, words) -> bool:
    return any(word in text for word in words)


def _option_hits(text: str) -> dict:
    """{group: (value, 원문 조각)}. 먼저 걸린 쪽이 더 구체적인 규칙이다 (벤티 > 톨)."""
    found = {}
    for pattern, group, value in _OPTION_RULES:
        if group in found:
            continue
        hit = pattern.search(text)
        if hit:
            found[group] = (value, hit.group(0))
    return found


def _options(text: str) -> dict:
    return {group: value for group, (value, _) in _option_hits(text).items()}


def option_words(text: str) -> dict:
    """{group: 원문 조각} — 기타 요청으로 옮길 때 담당자가 쓴 말 그대로 남긴다("오트밀크로")."""
    return {group: word for group, (_, word) in _option_hits(text).items()}


def without_option_words(text: str | None) -> str | None:
    """LLM 이 기타 요청에 넣은 문장에서 **메뉴판 옵션 표현**을 뺀다 (2026-11-04 전체 흐름 확인).

    LLM 은 "샷 추가요" · "두유로 디카페인이요" · "개인컵이요"를 통째로 custom_request 에 넣는다. 그 말들은 규칙이 이미
    옵션(shot · bean · cup)으로 읽었으므로 그대로 두면 주문서에 "샷 추가" 칩과 "요청: 샷 추가"가 겹친다.
    매장에 그 옵션 종류가 없으면(스타벅스의 두유) 분석 경로가 원문 조각으로 기타 요청에 다시 넣는다(_spec_checks).
    남은 것이 조사 · 군말뿐이면 None.
    """
    if not text:
        return None
    rest = text
    for pattern, _, _ in _OPTION_RULES:
        rest = pattern.sub(" ", rest)
    # 옵션을 뗀 뒤 남는 수량 지칭("하나는 샷 추가" → "하나는")도 요청이 아니다
    rest = re.sub(r"(?:^|(?<=[\s,]))(?:하나는|하나|한\s*잔은?|두\s*잔은?|\d+\s*잔은?|둘\s*다|모두|전부)(?=[\s,]|$)",
                  " ", rest)
    rest = re.sub(r"\s*,\s*(?=,|$)|^\s*,\s*", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.~!?")
    core = re.sub(r"(?:으로|로|이요|요|은|는|을|를|도|만|해\s*주세요|주세요|해줘|줘|하고|랑|,|\s)+", "", rest)
    return rest if len(core) >= 2 else None


def llm_request(text: str, draft: dict) -> str | None:
    """LLM 초안에서 기타 요청을 모은다 — custom_request + **규칙이 옵션으로 못 읽은 option_texts**.

    LLM 은 "바닐라빈 파우더 조금"을 option_texts(메뉴판 옵션 칸)에, "뿌려 주세요"만 custom_request 에 넣기도 한다
    (같은 입력에서 실행마다 갈린다 · 2026-11-04 전체 흐름 확인). 규칙이 옵션으로 읽지 못한 option_texts 는 어디에도
    남지 않았다. 조각들이 원문에 모두 있으면 첫 조각부터 끝 조각까지 **원문 구간 그대로** 쓴다.
    """
    pieces = [t for t in (draft.get("option_texts") or []) if t and not _options(t) and request_phrases(t) != [t]]
    custom = without_option_words(draft.get("custom_request"))
    pieces = [p for p in pieces if p.strip()] + ([custom] if custom else [])
    if not pieces:
        return None
    spans = [(text.find(p), text.find(p) + len(p)) for p in pieces]
    if all(start >= 0 for start, _ in spans):
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        whole = text[start:end].strip(" ,.~!?")
        if len(whole) <= sum(len(p) for p in pieces) + 4 * len(pieces):     # 조각 사이에 다른 말이 끼지 않았다
            return whole
    return ", ".join(dict.fromkeys(pieces))


def move_to_custom(custom: str | None, moved: dict, words: dict | None = None) -> str | None:
    """메뉴판에 종류가 없는 옵션을 기타 요청 문장 뒤에 붙인다. 이미 같은 말이 있으면 다시 붙이지 않는다."""
    parts = [custom] if custom else []
    for group, value in moved.items():
        word = (words or {}).get(group) or OPTION_WORDS.get((group, value)) or f"{group} {value}"
        if not any(word in part for part in parts):
            parts.append(word)
    return ", ".join(parts) if parts else None


def _ambiguous(text: str) -> dict:
    for word in _AMBIGUOUS_SIZE:
        if word in text:
            return {"size": f'"{word}"'}
    letter = _AMBIGUOUS_SIZE_LETTER.search(text)
    if letter:
        return {"size": f'"{letter.group(0)}"'}
    return {}


def temperature_conflict(text: str) -> str | None:
    """"따뜻한 아이스 아메리카노" — 두 온도가 한 주문에 함께 있으면 그 원문 조각.

    둘 중 하나로 추정하지 않는다. 별칭 매칭은 더 긴 쪽(아이스 …)을 골라 **조용히 확정**한다.
    """
    hot, ice = _HOT_WORDS.search(text or ""), _ICE_WORDS.search(text or "")
    if not (hot and ice):
        return None
    start, end = min(hot.start(), ice.start()), max(hot.end(), ice.end())
    return text[start:end]


def temperature_hint(text: str, options: dict | None = None) -> str | None:
    """메시지의 온도 단서 — "ice" · "hot" · None. 두 온도가 함께면 None(온도 충돌 질문이 따로 묻는다).

    얼음 옵션("얼음 적게" · "얼음 많이")은 아이스를 뜻한다 — PRD 8.1 #1 이 "바닐라라떼 큰 거 얼음 적게요"를
    **아이스** 바닐라라떼로 기대한다. 이름 뒤에 붙은 "아이스"("아메리카노 아이스로")도 단서다.
    """
    ice = bool(_ICE_WORDS.search(text or "")) or "ice" in (options or {})
    hot = bool(_HOT_WORDS.search(text or ""))
    if ice and not hot:
        return "ice"
    if hot and not ice:
        return "hot"
    return None


def without_temperature(text: str) -> str:
    """온도 표현을 뺀 메뉴 표현 — 온도 확인 질문의 후보(아이스 / 따뜻한)를 찾는 기준."""
    stripped = _ICE_WORDS.sub(" ", _HOT_WORDS.sub(" ", text or ""))
    return re.sub(r"\s+", " ", stripped).strip()


def quantity_issue(quantity: int) -> bool:
    """0 · 음수 · 지나치게 큰 수량. 깎거나 1로 바꾸지 않고 담당자에게 묻는다."""
    return quantity <= 0 or quantity > config.MAX_ITEM_QUANTITY


def _traits(text: str) -> list[str]:
    return sorted({tag for word, tag in _TRAIT_WORDS.items() if word in text})


def _custom_request(text: str) -> str | None:
    """기타 요청 — 힌트 낱말이 든 문장 + 기타 요청 옵션 구절. 금액에 반영하지 않으므로 원문을 보존한다."""
    sentences = [s.strip() for s in re.split(r"[.!?\n]", text) if s.strip()]
    picked = [s for s in sentences if _has(s, _CUSTOM_HINT)]
    for phrase in request_phrases(text):
        if not any(phrase in part for part in picked):
            picked.append(phrase)
    return ", ".join(picked) if picked else None


def request_phrases(text: str) -> list[str]:
    """기타 요청 옵션 구절(휘핑 많이 · 헤이즐넛 시럽 넣어주세요 · 저지방 우유로)을 원문 순서대로. 겹치면 긴 쪽."""
    spans = []
    for pattern in _REQUEST_RULES:
        for hit in pattern.finditer(text or ""):
            spans.append((hit.start(), hit.end()))
    spans.sort(key=lambda sp: (sp[0], -(sp[1] - sp[0])))
    picked, end = [], -1
    for start, stop in spans:
        if start >= end:
            picked.append((start, stop))
            end = stop
    phrases = [text[a:b].strip() for a, b in picked if text[a:b].strip()]
    # 메뉴판 옵션 규칙이 통째로 읽는 구절("시럽 추가")은 옵션 몫이다 — 둘 다 넣으면 기타 요청에 같은 말이 겹친다.
    # 매장에 그 옵션 종류가 없으면 분석 경로가 옵션을 기타 요청으로 옮긴다(sessions._spec_checks)
    return [p for p in phrases if not any(rule.fullmatch(p) for rule, _, _ in _OPTION_RULES)]


def _without_requests(text: str) -> str:
    for phrase in request_phrases(text):
        text = text.replace(phrase, " ")
    return text


def _display_text(text: str) -> str:
    """화면에 보일 원문 조각 — 첫 문장만, 조사를 떼지 않는다."""
    head = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    first = re.split(r"[.!?]", head)[0].strip()
    return (first or head)[:60]


# 군말 — 맞장구 · 감탄 · 웃음 · 이모지. 주문 문장 앞뒤에 붙어 오면 엄격 매칭이 "메뉴판에 없는 말"로 보고
#   메뉴를 확정하지 않았다("오 좋아요 ㅎㅎ 저는 아이스 카페라떼 그란데요" → 미등록 · 2026-11-04 사용자 보고).
#   **낱말 단위로만** 뗀다 — 글자 단위로 지우면 "오트"의 "오" · "아몬드"의 "아"가 지워져,
#   메뉴판에 없는 수식어가 가려지고 미등록 메뉴가 조용히 다른 메뉴로 확정된다(SC-8)
_FILLER_WORDS = ("오", "오오", "아", "앗", "어", "음", "음음", "흠", "와", "우와", "헐", "넵", "넹", "네", "네네",
                 "예", "응", "웅", "좋아요", "좋아", "좋네요", "좋습니다", "좋죠", "콜", "오케이", "오키", "ok", "okay",
                 "그럼", "그러면", "그리고", "일단", "혹시", "저기", "맞다")
_FILLER = re.compile(r"(?<!\S)(?:" + "|".join(sorted(map(re.escape, _FILLER_WORDS), key=len, reverse=True))
                     + r")[,.!?~]*(?!\S)", re.IGNORECASE)
# 웃음 · 한글 낱자(ㅎㅎ · ㅋㅋ · ㅠㅠ) · 이모지는 메뉴 이름에 들어가지 않으므로 어디서든 지운다
_LAUGH = re.compile(r"[ㄱ-ㅎㅏ-ㅣ]+|\^\^|[☀-➿️\U0001F000-\U0001FFFF]+")


def _without_filler(text: str) -> str:
    # 뒤 공백을 지운다 — 남으면 아래 어미 규칙의 "요$"가 걸리지 않는다("라떼요 ㅎㅎ" → "라떼요")
    return re.sub(r"\s+", " ", _FILLER.sub(" ", _LAUGH.sub(" ", text or ""))).strip()


def _menu_text(text: str) -> str:
    """메뉴 표현 후보 — 수량 · 옵션 · 기타 요청 · 군말을 덜어낸 나머지."""
    # 기타 요청 구절을 옵션 규칙보다 먼저 뗀다 — "헤이즐넛 시럽 추가"에서 옵션 규칙이 "시럽 추가"만 떼면
    # "헤이즐넛"이 남아 메뉴판에 없는 수식어가 된다
    stripped = _COLD_WORDS.sub(" 아이스 ", _without_requests(text))     # 차가운 라떼 -> 아이스 라떼
    for pattern, _, _ in _OPTION_RULES:
        stripped = pattern.sub(" ", stripped)
    stripped = re.sub(r"-?\d+\s*(?:잔|개|컵)|한\s*잔|두\s*잔|세\s*잔|네\s*잔", " ", stripped)
    # 군말은 수량 뒤에 뗀다 — "네 잔"의 "네"를 맞장구로 읽지 않게
    stripped = _without_filler(stripped)
    # 조사 "이 · 가"는 빼지 않는다 — 메뉴 이름 끝 글자와 겹친다("차이 라떼" → "차 라떼" → 카페 라떼로
    # 확정됐다 · D-08 0.1). 주문 문장에서는 거의 쓰이지 않고, 남아도 포함 규칙(menu.match)이 받는다
    # 공손 어미("부탁드려요" · "해주세요")는 긴 것부터 — "부탁"만 떼면 "드려"가 남아 엄격 매칭이 확정하지 않는다
    # 주문 어미도 함께 뗀다 — "아이스 카페라떼 하나 먹을래요"가 미등록으로 떨어졌다(2026-11-05 보고 4번)
    stripped = re.sub(r"(?:부탁\s*드립니다|부탁\s*드릴게요|부탁\s*드려요|부탁\s*드려|부탁\s*합니다|"
                      r"부탁\s*할게요|부탁\s*해요|부탁\s*해|드릴게요|드려요|드립니다|"
                      r"해\s*주세요|주세요|주문할게요|주문할래요|주문이요|주문|이요|요$|부탁|"
                      r"먹을래요|먹을게요|먹겠어요|먹고\s*싶어요|마실래요|마실게요|마시고\s*싶어요|"
                      r"먹을래|먹을게|마실래|마실게|시킬게요|시킬래요|시킬게|시킬래|"
                      r"할게요|할래요|으로|로|은|는|을|를)\b",
                      " ", stripped)
    # 기타 요청 문장은 메뉴 표현에서 뺀다
    for sentence in re.split(r"[.!?\n]", stripped):
        if _has(sentence, _CUSTOM_HINT):
            stripped = stripped.replace(sentence, " ")
    stripped = re.sub(r"\s+", " ", stripped)
    # 조사 · 문장부호만 남은 꼬리를 떼어낸다. 남기면 "아이스 아메리카노 , 하나 요" 처럼
    # 정규화 키가 길어져 별칭 완전 일치를 놓친다
    stripped = re.sub(r"[\s,.!?~·]*(?:요|은|는|을|를|로|으로|도|만|하나|한)*$", "", stripped)
    return stripped.strip(" ,.~!?")


# --- 대리 주문 주문자 (2026-11-04 · P-03 INT-002 · 평가셋 EV-006) ------------------------------
# "서연이 것도 라떼" · "민수 씨 대신 아아" — 문장 맨 앞의 이름 + 호칭 · 조사 + 가리킴 말.
# 이름처럼 생긴 대명사 · 지시어는 이름이 아니다("그거 라떼" · "저도 아아" · "같은 거 하나")
_NOT_A_NAME = {"그", "이", "저", "요", "내", "제", "나", "너", "우리", "저희", "같은", "똑같은", "아까",
               "다른", "아무", "그것", "이것", "저것", "얘", "걔", "쟤", "다", "모두", "전부"}
_HONORIFIC = r"(?:\s*(?:씨|님|언니|누나|오빠|형|쌤|선배))?"
_PROXY = re.compile(r"^\s*([가-힣]{1,4}?)" + _HONORIFIC + r"(?:이네|이)?\s*(?:것도|거도|꺼도|것|거|꺼|대신)(?=\s|$)\s*")
_PERSON_SUFFIX = re.compile(r"(?:\s*(?:이네|이꺼|이거|이것|이|씨|님|언니|누나|오빠|형|쌤|선배))+$")


def proxy_phrase(text: str) -> tuple[str, str] | None:
    """대리 주문 앞머리 → (이름 표현, 떼어 낼 구절). 없으면 None. 이름이 참여자인지는 보지 않는다(link_person)."""
    hit = _PROXY.match(text or "")
    if not hit or hit.group(1) in _NOT_A_NAME:
        return None
    return hit.group(1), hit.group(0)


def person_label(said: str) -> str:
    """화면에 쓸 주문자 표현 — 호칭 · 조사를 뗀다("민수 씨" → "민수"). 질문이 '"민수 씨" 님'이 되지 않게."""
    return _PERSON_SUFFIX.sub("", (said or "").strip()).strip() or (said or "").strip()


def link_person(said: str, participants: list[str]) -> tuple[str, list[str]]:
    """주문자 표현("서연이" · "민수 씨")을 대화 참여자에 잇는 **후보**를 찾는다 — 확정하지 않는다.

    돌려주는 값 (판정, 후보 참여자):
      exact     표현이 참여자 이름 그대로 — 묻지 않는다
      linked    성을 뗀 이름 · 호칭 · 조사를 덜어 낸 뒤 참여자 1명과 맞음 — 담당자에게 맞는지 묻는다
      ambiguous 2명 이상과 맞음 — 고르지 않고 후보를 모두 보여 준다
      outsider  아무와도 안 맞음(대화에 없는 사람) — 그대로 둔다
    """
    text = (said or "").strip()
    if text in participants:
        return "exact", [text]
    core = _PERSON_SUFFIX.sub("", text).strip() or text
    hits = []
    for person in participants:
        given = person[1:] if len(person) == 3 else None        # 한국어 세 글자 이름: 성 1 + 이름 2
        if person not in hits and (core in (person, given) or (given and text.startswith(given))):
            hits.append(person)
    if len(hits) == 1:
        return "linked", hits
    return ("ambiguous", hits) if hits else ("outsider", [])


def order_text(text: str) -> str:
    """분류에 쓰는 문장 — **기타 요청 문장을 뺀 나머지**다.

    왜 빼는가: "빨대는 빼 주시고 …"의 `빼`가 취소 신호와 같은 글자다. 문장을 통째로
    보면 주문 추가가 취소로 분류되고, 그 사람의 주문이 조용히 사라진다.
    기타 요청은 **금액에도 판정에도 영향을 주지 않는** 문장이므로 분류에서 제외한다.
    """
    kept = [s.strip() for s in re.split(r"[.!?\n]", text or "")
            if s.strip() and not _has(s, _CUSTOM_HINT)]
    # 기타 요청 구절도 뺀다 — "휘핑 빼 주세요"의 `빼 주`가 취소 신호와 같은 글자다
    kept = [k for k in (re.sub(r"\s+", " ", _without_requests(k)).strip() for k in kept) if k]
    return " ".join(kept) if kept else (text or "")


def order_pattern(text: str) -> bool:
    """주문하겠다는 문형인가 — 수량 또는 주문 동사가 있고, 제안 · 의향 묻기가 아니다.

    메뉴를 못 알아들은 문장을 **버릴 것인가 물을 것인가**를 가른다 (consolidate · 사용자 보고 2번).
    """
    scope = order_text(text)
    if _PROPOSAL.search(scope) or TALLY.search(scope):
        return False
    return bool(_ORDER_VERB.search(scope) or _ORDER_QTY.search(scope))


def classify(text: str) -> str:
    """경로 5종 중 하나. 순서가 규칙이다 — 취소가 변경보다 먼저다."""
    scope = order_text(text)
    if _has(scope, _CANCEL):
        return "cancel"
    if _has(scope, _UPDATE):
        return "update"
    if _has(scope, _CONFIRM):
        return "confirm"
    # 질문 · 잡담이면서 주문 신호가 없으면 주문이 아니다
    if _has(scope, _QUESTION) and not _has(scope, _REFERENCE):
        return "ignore"
    # 제안 · 의향 묻기도 주문이 아니다 — "다 같이 커피 한잔 어떠세요"(보고 2번)
    if _PROPOSAL.search(scope) and not _has(scope, _REFERENCE):
        return "ignore"
    return "add"


def extract_events(messages: list[dict], *, brand_id: str, menu_version: str) -> list[dict]:
    """메시지 묶음 → 이벤트 묶음.

    ★ LLM 교체 지점. 지금은 규칙으로 판정하며, 규칙이 못 정한 것은 **임의로 확정하지 않고**
      needs_review 신호(`review_reason`)를 달아 올린다 — 그것이 "확인 필요"의 원천이다.
    """
    events: list[dict] = []
    for message in messages:
        text = message["text"]
        event_type = classify(text)
        event = {
            "message_id": message["message_id"],
            "seq": message.get("seq") or message.get("order"),
            "person": message["sender"],
            "event_type": event_type,
            "_raw_text": text,                 # 참조 주문 후보 탐색에 쓴다(저장하지 않는다)
            "evidence_message_ids": [message["message_id"]],
            "is_reference": False,
            "agent": "intake",
            "items": [],
        }

        if event_type == "ignore":
            # 참고가 될 수 있는 잡담(메뉴 이름이 섞인 말)은 근거로 남긴다
            event["is_reference"] = bool(_menu_text(text))
            events.append(event)
            continue

        if event_type in ("cancel", "confirm"):
            event["target_hint"] = _menu_text(text)
            event["options"] = _options(text)
            events.append(event)
            continue

        # add · update — 항목을 만든다
        reference = _has(text, _REFERENCE)
        if reference:
            event["is_reference"] = True
            # 앞선 메시지를 근거로 끌어온다 — 남의 메시지에서 메뉴를 가져왔다
            previous = [e for e in events if e.get("is_reference") or e["items"]
                        or e["event_type"] == "ignore"]
            if previous:
                event["evidence_message_ids"] = [previous[-1]["message_id"], message["message_id"]]
        event["items"] = build_items(text, message["sender"])

        events.append(event)

    return events


def build_items(text: str, person: str) -> list[dict]:
    """주문 문장 하나 → 항목 목록 (add · update). 규칙 해석과 LLM 해석이 **같은 함수**로 항목을 만든다.

    LLM 은 메뉴 · 사이즈 · 옵션 표현을 원문 그대로 옮길 뿐이고(D-11 classify 규칙 7),
    옵션 값 · 모호 표시 · 온도 단서 · 수량 점검은 여기 규칙이 정한다 — 두 경로의 판정이 갈라지지 않게.
    """
    quantity = parse_quantity(text)
    menu_text = _menu_text(text)
    options = _options(text)
    ambiguous = _ambiguous(text)
    traits = _traits(text)
    custom = _custom_request(text)

    # 수량이 0 · 음수 · 과다면 값을 고치지 않고 묻는다. 저장은 양수만 되므로(D-07 CHECK)
    # 0 · 음수는 1 을 임시로 두고, 확인 전까지 금액을 내지 않는다(ambiguous_options)
    quantity_ambiguous = quantity_issue(quantity)
    if quantity_ambiguous:
        ambiguous = {**ambiguous, "quantity": f'"{quantity}잔"'}
        quantity = quantity if quantity > 0 else 1

    # 두 온도가 함께 있으면 메뉴 매칭은 온도를 뺀 표현으로 하고 후보를 둘 다 띄운다
    conflict = temperature_conflict(order_text(text))
    if conflict:
        ambiguous = {**ambiguous, "temperature": f'"{conflict}"'}

    reference = _has(text, _REFERENCE)

    # 표시용(menu_text)과 매칭용(match_text)을 나눈다.
    # 조사를 떼어낸 문자열을 그대로 보여주면 "그럼 난 그걸 하나"처럼 원문이 망가진다 —
    # 확인 필요 항목은 **원문 조각을 따옴표째 유지**해야 한다(P-06)
    item = {
        "person": person,
        # 항상 원문 조각을 담는다. 메뉴가 잡히면 화면은 menu_name 을 쓰므로
        # 이 값은 **메뉴가 안 잡힌 항목에서만 보이고**, 그때 조사를 떼어낸 문자열을
        # 보여주면 원문이 망가진다("그럼 난 그걸 하나")
        "menu_text": _display_text(text),
        "match_text": menu_text or text.strip(),
        "options": options,
        # 메뉴에 그 종류가 없는 옵션을 기타 요청으로 옮길 때 원문 조각을 쓴다(저장하지 않는다)
        "option_words": option_words(text),
        "ambiguous_options": ambiguous,
        "custom_request": custom,
        "quantity": quantity,
        "traits": traits,
        "reference": reference,
        "temperature_base": without_temperature(menu_text) if conflict else None,
        "temperature_hint": None if conflict else temperature_hint(order_text(text), options),
    }

    # "2잔, 하나는 샷 추가" — 수량 중 하나만 다른 옵션을 받는 경우
    split = re.search(r"(?:하나|한\s*잔|1잔)(?:는|은)?\s*(.{0,12}?)(?:추가|로|으로)", text)
    if quantity > 1 and not quantity_ambiguous and split and _options(split.group(0)):
        extra = _options(split.group(0))
        # 문장 전체에서 읽은 옵션에는 "하나는 샷 추가"의 샷이 이미 섞여 있다.
        # 기본 행에서 빼지 않으면 **2잔 모두 샷 추가**가 되어 금액이 틀린다
        base_options = {k: v for k, v in options.items() if k not in extra}
        return [{**item, "quantity": quantity - 1, "options": base_options},
                {**item, "quantity": 1, "options": {**base_options, **extra}}]
    return [item]


def from_contract(events: list[dict], messages: list[dict], *,
                  known_ids: set[str] | None = None) -> list[dict]:
    """주문 해석 서비스가 돌려준 `OrderEvents`(계약) → 업무 층 이벤트 (D-12 N-03).

    - 경로 · 주문자 · 근거 · 대상은 계약 값을 쓴다 (LLM 이 정하는 것)
    - 항목은 `build_items` 규칙이 만든다 — 메시지에서 이벤트가 하나면 **원문 그대로**, 둘 이상이면
      계약의 항목 표현을 이어 붙인 문장으로. 규칙 해석과 같은 함수라 옵션 · 모호 판정이 갈라지지 않는다
    - 묶음 밖 message_id(앞 메시지를 다시 만든 것)는 버린다 — 같은 주문이 두 번 생긴다(SC-5)
    """
    by_id = {m["message_id"]: m for m in messages}
    known = set(known_ids or ()) | set(by_id)
    per_message: dict[str, int] = {}
    for event in events:
        per_message[event["message_id"]] = per_message.get(event["message_id"], 0) + 1

    out: list[dict] = []
    for event in events:
        message = by_id.get(event["message_id"])
        if message is None:
            continue
        text = message["text"]
        person = event.get("person") or message["sender"]
        # 근거에는 **그 이벤트의 메시지가 늘 들어간다** — 모델이 앞 메시지만 적어 낸 일이 있었다
        # (이지은 대리 주문의 근거가 담당자 공지 m1, 이수빈 대리 주문의 근거가 m17 · 2026-11-05 5차 보고).
        # 앞 메시지는 참조 주문("저도 그걸로")일 때만 근거가 된다
        evidence = [mid for mid in event.get("evidence_message_ids") or []
                    if mid in known and mid != message["message_id"]] if event.get("is_reference") else []
        evidence = sorted(set(evidence) | {message["message_id"]},
                          key=lambda mid: (by_id.get(mid) or {}).get("seq") or 0)
        converted = {
            "message_id": message["message_id"],
            "seq": message.get("seq") or message.get("order"),
            "person": person,
            "event_type": event["event_type"],
            "_raw_text": text,
            "evidence_message_ids": evidence or [message["message_id"]],
            "is_reference": bool(event.get("is_reference")),
            "agent": "intake",
            "items": [],
            "target_message_id": event.get("target_message_id"),
            "target_unclear": bool(event.get("target_unclear")),
            "person_unclear": bool(event.get("person_unclear")),
        }
        kind = event["event_type"]
        if kind == "ignore":
            converted["is_reference"] = converted["is_reference"] or bool(_menu_text(text))
        elif kind in ("cancel", "confirm"):
            converted["target_hint"] = _menu_text(text)
            converted["options"] = _options(text)
        else:
            if per_message[message["message_id"]] == 1 or not event.get("items"):
                items = build_items(text, person)
            else:
                items = []
                for draft in event["items"]:
                    words = [draft.get("temperature_text"), draft.get("menu_text"),
                             draft.get("size_text"), *(draft.get("option_texts") or [])]
                    phrase = " ".join(w for w in words if w) + f" {draft.get('quantity') or 1}잔"
                    items += build_items(phrase, person)
            # LLM 이 읽은 기타 요청(휘핑 빼기 · 빨대 등)을 잃지 않는다 — 규칙의 기타 요청 어휘에 없는 표현도
            # LLM 은 custom_request 로 넘긴다. 규칙으로 항목을 다시 만들면 이 칸이 비어 요청이 조용히 사라졌다
            drafts = event.get("items") or []
            for index, item in enumerate(items):
                item["reference"] = item["reference"] or converted["is_reference"]
                item["person_unclear"] = converted["person_unclear"]
                draft = drafts[0] if len(drafts) == 1 else (drafts[index] if len(drafts) == len(items) else None)
                llm_custom = llm_request(text, draft) if draft else None
                if not item.get("custom_request") and llm_custom:
                    item["custom_request"] = llm_custom
                    # 요청 문구("휘핑 빼")가 메뉴 표현에 남으면 엄격 매칭이 확정하지 않는다 — LLM 이 떼어 준 메뉴 표현으로
                    # 맞춘다. 엄격 매칭은 그대로라 메뉴판 밖 단어가 남으면 여전히 확정하지 않는다(SC-8)
                    if draft.get("menu_text"):
                        item["match_text"] = _menu_text(draft["menu_text"]) or draft["menu_text"]
            converted["items"] = items
        out.append(converted)
    return out
