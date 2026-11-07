"""공유 내용 생성 — 내용은 서버가 만든다. 화면이 조립하지 않는다 (API-033).

공유 카드 이미지(PNG)는 생성 후 응답으로만 보낸다 — 파일 · DB 에 두지 않는다.
보관할 이유가 없다. 같은 버전이면 다시 만든다 (D-07 2.4).

개인별 금액을 넣지 않는다 (D-07 D-5).
"""

import datetime
import re

from app.services import share_image
from app.services.chat_parse import UNNAMED

FOOT = "Moa · 비공식 시연 서비스"
NOTICE = "참고용 금액이에요. 실제 결제 금액과 다를 수 있어요"

BRAND_HEAD = {"starbucks": "스타벅스", "generic": "일반 카페"}

# 폰트가 없어 그릴 수 없을 때만 쓰는 문구 — 이미지 기능 자체는 구현돼 있다
IMAGE_UNAVAILABLE_TEXT = "이미지는 아직 만들지 못했어요. 텍스트 복사로 공유해 주세요"


def _spec_text(group: dict) -> str:
    """메뉴명 뒤에 붙는 사양 — 카드 · 이미지용 한 줄. 복사 텍스트와 **같은 옵션 규칙**(_split_options)을 쓴다:
    기본값은 빼고 "샷 샷 추가"처럼 겹치지 않는다(2026-11-06)."""
    size, extras = _split_options(group)
    return " ".join(([size] if size else []) + extras)


def _split_options(group: dict) -> tuple[str | None, list[str]]:
    """옵션 칩을 (사이즈, 나머지 옵션 문구 목록)으로 나눈다."""
    size, extras = None, []
    for option in group.get("options") or []:
        value = option.get("value")
        if not value:
            continue
        label = option.get("label")
        if label == "사이즈":
            size = value
        elif (label, value) in _DEFAULT_OPTION_TEXT:
            continue
        elif not label or label == "우유" or label in value:
            # "샷 추가" · "시럽 추가" · "두유"처럼 값만으로 읽힌다 — "샷 샷 추가"가 되지 않게
            extras.append(value)
        else:
            extras.append(f"{label} {value}")
    return size, extras


# 기본값은 주문하는 쪽에 알릴 말이 아니다 — "얼음 보통 · 개인컵 미사용"이 줄마다 쌓이지 않게 텍스트에서 뺀다
_DEFAULT_OPTION_TEXT = {("얼음", "보통"), ("샷", "기본"), ("원두", "시그니처"), ("개인컵", "미사용"),
                        ("시럽", "없음"), ("우유", "일반 우유")}


# 기타 요청을 단톡방용 짧은 문구로 줄이는 규칙. **확실할 때만 줄이고 아니면 원문 구절 그대로 둔다** —
# 잘못 줄이면 매장에 다른 주문이 들어간다. 요청 원문은 고치지 않는다(DB · 주문서는 그대로)
# 구절 나누기: 쉼표 · 세미콜론 · 줄바꿈 · 문장 끝 마침표(숫자 사이 "1.5" 는 자르지 않는다)
_CLAUSE_SPLIT = re.compile(r"\s*(?:[,，;\n]|(?<!\d)[.。](?!\d))\s*")
# 한 구절 안의 "~주시고 ~" — 나눈 조각이 **전부** 규칙에 맞을 때만 나눈다("빨대 주시고 …"가 "빨대"로 남지 않게)
_CONNECTIVE_SPLIT = re.compile(r"\s*(?:해\s?주시고|주시고|하시고|해\s?주고)\s+")
_TOPICS = ("빨대", "휘핑", "얼음", "텀블러", "개인컵", "개인 컵")
_NEGATED_CUP = re.compile(r"안\s*가져|없어|못\s*가져|안\s*써|안\s*쓰|미사용|말고")
_SHORT_RULES = [
    (re.compile(r"빨대\S*\s*(?:는|은)?\s*(?:빼|없이|제외|필요\s*없|안\s*주|주지\s*마)"), "빨대 제외"),
    (re.compile(r"휘핑\S*\s*(?:는|은)?\s*(?:빼|없이|제외|필요\s*없)"), "휘핑 제외"),
    (re.compile(r"얼음\S*\s*(?:은|는)?\s*(?:컵\s*)?(?:반만|절반|반\s*정도)"), "얼음 절반만"),
    (re.compile(r"텀블러|개인\s?컵"), "개인컵 사용"),
]


def _shorten(clause: str) -> str | None:
    """구절 하나를 짧은 문구로. 확실하지 않으면 None — 주제어가 둘 이상이거나 부정이 섞이면 줄이지 않는다."""
    if sum(word in clause for word in _TOPICS if word != "개인 컵") != 1:
        return None
    for pattern, short in _SHORT_RULES:
        if pattern.search(clause):
            if short == "개인컵 사용" and _NEGATED_CUP.search(clause):
                return None
            return short
    return None


def _request_lines(custom_request: str | None) -> list[str]:
    out = []
    for clause in _CLAUSE_SPLIT.split(custom_request or ""):
        clause = clause.strip()
        if not clause:
            continue
        parts = [p for p in _CONNECTIVE_SPLIT.split(clause) if p.strip()]
        shorts = [_shorten(p) for p in parts]
        if len(parts) > 1 and all(shorts):
            out += shorts
        else:
            out.append(_shorten(clause) or clause)
    return out


def _details(extras: list[str], custom_request: str | None) -> list[str]:
    """`-` 줄로 붙일 항목 — 사이즈 외 옵션 + 기타 요청 구절. 같은 말은 한 번만."""
    out: list[str] = []
    for text in extras + _request_lines(custom_request):
        text = text.strip()
        if text and text not in out:
            out.append(text)
    return out


def _compact_people(people: list[str]) -> list[str]:
    """주문자 목록 — 이름 없는 참여자는 "이름 없음, 이름 없음"으로 늘어놓지 않고 "이름 없음 2명"으로 묶는다.
    카드 · 이미지 · 복사 텍스트가 같이 쓴다."""
    named = [p for p in people if p and p != UNNAMED]
    unnamed = len(people) - len(named)
    if unnamed:
        named.append(f"{UNNAMED} {unnamed}명" if unnamed > 1 else UNNAMED)
    return named


def build_card(sheet: dict, *, now: datetime.datetime | None = None, show_names: bool = True) -> dict:
    """공유 카드 구조. 화면과 이미지 생성이 같은 값을 쓴다.

    show_names=False 면 묶음마다의 이름 줄을 뺀다 — 메뉴 · 수량 · 요청 · 합계만 남는다(2026-11-04 · 사용자 요청 #8).
    머리줄의 "N명"은 인원 수라 이름이 아니므로 남긴다.

    이름이 없는 대화(참여자가 `이름 없음` 하나뿐)는 **요청 없이도** 이름 줄을 뺀다 —
    "이름 없음 · 이름 없음"을 단톡방에 올리는 것은 이름 표시가 아니다(2026-11-05 사용자 결정 1번).
    """
    now = now or _now_kst()
    # 이름이 하나도 없는 대화는 몇 명인지 모른다 — 인원을 말하지 않고 잔 수만 쓴다(projection.chat_intro 와 같은 결정)
    nameless = not _has_real_names(sheet)
    if show_names and nameless:
        show_names = False
    brand_head = BRAND_HEAD.get(sheet["brand"]["id"], "카페")
    summary = sheet["summary"]
    totals = sheet["totals"]

    lines = []
    for group in sheet["groups"]:
        spec = _spec_text(group)
        size, extras = _split_options(group)
        lines.append({
            "menu_text": f'{group["menu_text"]} {spec}'.strip(),
            # 복사 텍스트용 — 메뉴명 · 사이즈 · 나머지 옵션 · 요청을 따로 둔다(2026-11-06 사용자 요청)
            "menu_name": group["menu_text"],
            "size": size,
            "details": _details(extras, group.get("custom_request")),
            "quantity": group["quantity"],
            "people": _compact_people(group["people"]) if show_names else [],
            "custom_request": group["custom_request"],
        })

    total_text = (f'합계 {totals["cups"]}잔'
                  if totals["estimated_total"] is None
                  else f'합계 {totals["cups"]}잔 · 예상 {totals["estimated_total"]:,}원')

    return {
        "brand_head": brand_head,
        "people_count": None if nameless else summary["people"],
        "cups": summary["cups"],
        "estimated_total": totals["estimated_total"],
        "head": f"커피 주문 · {brand_head}",
        "sub": f'{now.month}/{now.day} {now:%H:%M} 기준 · {_count_text(None if nameless else summary["people"], summary["cups"])}',
        "lines": lines,
        "total_text": total_text,
        "notice": NOTICE,
        "foot": FOOT,
    }


def _has_real_names(sheet: dict) -> bool:
    """이름이라고 할 만한 주문자가 하나라도 있는가 — `이름 없음`은 이름이 아니다."""
    return any(name and name != UNNAMED
               for group in sheet.get("groups") or [] for name in group.get("people") or [])


# 공유 카드 · 파일 이름의 "기준 시각"은 한국 시간이다. 컨테이너 시계는 UTC 라 그대로 쓰면 오전 11시 주문이
# "02:07 기준"으로 나갔다(2026-11-04 전체 흐름 확인). 한국은 서머타임이 없어 고정 +9 로 충분하다 — tzdata 가 없는
# slim 이미지에서도 돈다
KST = datetime.timezone(datetime.timedelta(hours=9))


def _now_kst() -> datetime.datetime:
    return datetime.datetime.now(KST)


def _count_text(people: int | None, cups: int) -> str:
    return f"{cups}잔" if people is None else f"{people}명 · {cups}잔"


TEXT_NOTICE = "※ 금액은 참고용이며 실제 결제 금액과 다를 수 있습니다."


def render_text(card: dict) -> str:
    """복사용 텍스트. 공유 카드와 같은 자료에서 만들고, 단톡방에 붙여 읽기 좋은 번호 목록으로 쓴다
    (2026-11-06 사용자 요청 — 메뉴 · 사이즈 · 주문자 · 요청을 줄로 나눈다)."""
    count = _count_text(card["people_count"], card["cups"])
    total = card["estimated_total"]
    out = [f'[{card["brand_head"]} 주문 정리]', "", f"주문 현황: {count}"]
    if total is not None:
        out.append(f"예상 결제 금액: {total:,}원")
    for index, line in enumerate(card["lines"], start=1):
        size = f' ({line["size"]})' if line.get("size") else ""
        out += ["", f'{index}. {line["menu_name"]}{size} × {line["quantity"]}잔']
        if line["people"]:
            out.append("주문자: " + ", ".join(line["people"]))
        out += [f"-{detail}" for detail in line["details"]]
    out += ["", f"총 {count}" + (f" | 예상 {total:,}원" if total is not None else "")]
    if total is not None:
        out += ["", TEXT_NOTICE]
    return "\n".join(out)


def image_filename(brand_id: str, *, now: datetime.datetime | None = None) -> str:
    now = now or _now_kst()
    return f'주문_{BRAND_HEAD.get(brand_id, "카페")}_{now:%Y%m%d_%H%M}.png'


def view(sheet: dict, *, fmt: str = "text", now: datetime.datetime | None = None,
         show_names: bool = True) -> dict:
    """API-033 — 공유 내용 생성 응답.

    `image_available` 은 **한글 폰트가 있는지**로 정한다(share_image.available()). 폰트가 없는
    환경에서 참으로 내보내면 글자가 두부(□)로 찍힌 그림이 단톡방에 올라간다 — 그때는 거짓과
    대체 문구를 함께 주고 화면이 [텍스트 복사]만 쓰게 한다.
    """
    card = build_card(sheet, now=now, show_names=show_names)
    shown = show_names and _has_real_names(sheet)
    return {
        "format": fmt,
        "show_names": shown,
        "content": render_text(card),
        "card": card,
        "image_filename": image_filename(sheet["brand"]["id"], now=now),
        "image_available": share_image.available(),
        "image_unavailable_text": IMAGE_UNAVAILABLE_TEXT,
    }


def render_image(sheet: dict, *, now: datetime.datetime | None = None, show_names: bool = True) -> bytes:
    """공유 카드 PNG. 텍스트와 **같은 카드 구조**에서 그린다."""
    return share_image.render(build_card(sheet, now=now, show_names=show_names))


def content_hash(content: str) -> str:
    """공유한 내용과 같은지 판정하는 값 (share_history.content_hash)."""
    from app import ids

    return ids.content_hash(content)
