# 2026-11-04 사용자 보고 수정분 — #5 옵션 기본값 · #6 답장 반영 · #8 이름 없이 공유 (DB 없이 돈다)
#
# #6 의 "같은 세션에서 두 번째 답장 분석이 500" 은 DB 키 충돌이라 실DB 로 본다(tests/test_live_flow.py).
import datetime

from app.services import chat_parse, projection, share, suggestions
from tests import q03_fakes as F


# --- #5 행에 값이 없는 옵션 묶음은 메뉴 기본값이 골라진 상태 ------------------------------------
def test_unset_option_groups_show_menu_default(monkeypatch):
    F.install(monkeypatch, F.Store())
    groups = {g["option_group"]: g for g in projection.option_choices(
        F.BRAND, F.MENU_VERSION, {"menu_id": "sb_004", "options": {"size": "grande"}})}
    assert groups["size"]["current"] == "그란데"                 # 고른 값은 그대로
    assert groups["ice"]["current"] == "보통"                    # 안 고른 묶음은 기본값
    assert groups["shot"]["current"] == "기본"


def test_ambiguous_size_is_not_filled_with_default(monkeypatch):
    """모호한 사이즈("큰 거")는 고른 것이 아니다 — 기본값으로 채우면 확인 없이 톨이 된다."""
    F.install(monkeypatch, F.Store())
    groups = {g["option_group"]: g for g in projection.option_choices(
        F.BRAND, F.MENU_VERSION,
        {"menu_id": "sb_004", "options": {}, "ambiguous_options": {"size": '"큰 거"'}})}
    assert groups["size"]["current"] is None
    assert groups["ice"]["current"] == "보통"


# --- #6 답장 형식 ------------------------------------------------------------------------------
def test_kakao_pc_time_is_not_part_of_the_message():
    parsed = chat_parse.parse("[이지연] [오후 2:31] 그란데로 바꿔주세요")
    assert [(m["sender"], m["text"]) for m in parsed["messages"]] == [("이지연", "그란데로 바꿔주세요")]


def test_plain_bracket_text_is_kept():
    """시각처럼 생기지 않은 대괄호는 본문이다."""
    parsed = chat_parse.parse("[이지연] [급해요] 그란데로")
    assert parsed["messages"][0]["text"] == "[급해요] 그란데로"


def test_reply_without_sender_says_how_to_paste():
    out = suggestions.analyze({"session_id": "ORD-0001"}, "그란데로 바꿔주세요")
    assert out["suggestions"] == [] and out["found_text"] == suggestions.NO_SENDER_TEXT


# --- #8 이름 없이 공유 -------------------------------------------------------------------------
SHEET = {"brand": {"id": "starbucks"}, "summary": {"people": 3, "cups": 3},
         "totals": {"cups": 3, "estimated_total": 14500},
         "groups": [{"menu_text": "아이스 아메리카노", "options": [{"label": "사이즈", "value": "톨"}],
                     "quantity": 2, "people": ["김민수", "박서연"], "custom_request": None},
                    {"menu_text": "카페 라떼", "options": [], "quantity": 1, "people": ["이지연"],
                     "custom_request": "휘핑 빼주세요"}]}
NOW = datetime.datetime(2026, 11, 4, 11, 0, tzinfo=share.KST)


def test_share_with_names_is_unchanged():
    out = share.view(SHEET, now=NOW)
    assert out["show_names"] is True
    assert "주문자: 김민수, 박서연" in out["content"] and "이지연" in out["content"]


def test_share_without_names_keeps_menu_quantity_request_total():
    out = share.view(SHEET, now=NOW, show_names=False)
    text = out["content"]
    for name in ("김민수", "박서연", "이지연"):
        assert name not in text and name not in str(out["card"])
    assert "1. 아이스 아메리카노 (톨) × 2잔" in text and "2. 카페 라떼 × 1잔" in text
    assert "-휘핑 제외" in text and "총 3명 · 3잔 | 예상 14,500원" in text
    assert "3명" in out["card"]["sub"]                          # 인원 수는 이름이 아니다


# --- 2026-11-06 복사 텍스트 번호 목록 — 인원 · 옵션 · 요청이 많을 때 -------------------------------
def _sheet(options, custom, people=("A",)):
    return {"brand": {"id": "starbucks"}, "summary": {"people": len(people), "cups": len(people)},
            "totals": {"cups": len(people), "estimated_total": 5000},
            "groups": [{"menu_text": "카페 라떼", "options": options, "quantity": len(people),
                        "people": list(people), "custom_request": custom}]}


def _text(options=(), custom=None, people=("A",)):
    return share.view(_sheet(list(options), custom, people), now=NOW)["content"]


def test_option_value_that_contains_label_is_not_doubled():
    text = _text([{"label": "샷", "value": "샷 추가"}, {"label": "시럽", "value": "시럽 추가"},
                  {"label": "우유", "value": "두유"}, {"label": "원두", "value": "디카페인"}])
    assert "-샷 추가" in text and "-시럽 추가" in text and "-두유" in text and "-원두 디카페인" in text
    assert "샷 샷" not in text and "시럽 시럽" not in text


def test_default_option_values_are_not_listed():
    text = _text([{"label": "얼음", "value": "보통"}, {"label": "개인컵", "value": "미사용"},
                  {"label": "원두", "value": "시그니처"}])
    assert "보통" not in text and "미사용" not in text and "시그니처" not in text


def test_request_is_shortened_only_when_sure():
    text = _text([{"label": "개인컵", "value": "사용"}],
                 "빨대는 빼 주시고 개인 텀블러에 담아 주세요, 얼음은 컵 반만 채워 주시면 좋겠어요")
    assert text.count("-개인컵 사용") == 1 and "-빨대 제외" in text and "-얼음 절반만" in text
    # 부정 · 두 주제 · 숫자 마침표는 원문 그대로
    assert "-텀블러는 안 가져왔어요" in _text(custom="텀블러는 안 가져왔어요")
    assert "-빨대 주시고 휘핑은 빼주세요" in _text(custom="빨대 주시고 휘핑은 빼주세요")
    assert "-1.5배로 해 주세요" in _text(custom="1.5배로 해 주세요")
    assert "-빨대 안에 넣지 말고 따로" in _text(custom="빨대 안에 넣지 말고 따로")


def test_many_people_and_unnamed_are_listed_compactly():
    people = [f"사람{i}" for i in range(12)]
    assert "주문자: " + ", ".join(people) in _text(people=people)
    assert "주문자: C, 이름 없음 2명" in _text(people=("C", "이름 없음", "이름 없음"))


# --- 2026-11-06 인원 수 · 카드 옵션 ---------------------------------------------------------
def test_unnamed_people_are_counted_per_item_and_names_once():
    from app.services import settlement
    assert settlement.count_people(["C", "이름 없음", "이름 없음", "D"]) == 4
    assert settlement.count_people(["김민수", "김민수", "이지연"]) == 2     # 같은 이름이 두 번 시켜도 1명
    rows = [{"person": "이름 없음", "quantity": 1, "estimated_price": 1}] * 3
    assert settlement.totals(rows)["people"] == 3


def test_nameless_share_says_cups_only():
    sheet = _sheet([], None, people=("이름 없음", "이름 없음"))
    out = share.view(sheet, now=NOW)
    assert "주문 현황: 2잔" in out["content"] and "총 2잔" in out["content"] and "명" not in out["card"]["sub"]


def test_card_uses_same_option_rules_as_text():
    card = share.build_card(_sheet([{"label": "사이즈", "value": "톨"}, {"label": "샷", "value": "샷 추가"},
                                    {"label": "개인컵", "value": "미사용"}], None,
                                   people=("C", "이름 없음", "이름 없음")), now=NOW)
    line = card["lines"][0]
    assert line["menu_text"] == "카페 라떼 톨 샷 추가"
    assert line["people"] == ["C", "이름 없음 2명"]
