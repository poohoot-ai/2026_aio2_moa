# Q-03 — 평가 하네스 자기 검증 (G-A0)
#
# **측정 도구를 측정하지 않으면 측정이 아니다.** 채점기에 ① 정상 케이스와 ② 알려진 실패 케이스를
# 양방향으로 넣어, 둘을 구분하는지 본다. 한 방향만 보면 "전부 통과"라고 답하는 채점기도 100% 가 나온다.
#
# 드라이런 함정 재현: 유출 판정을 "결과 행이 있는가"로 하면 안전한 구현도 결함으로 보였다(D-10 3.1).
# 여기서는 "잔 합계가 맞는가"와 "확인 필요를 올렸는가"를 분리해 같은 함정을 피한다.
import pytest

from eval import judge as judge_mod
from eval import score as S


# --- 축 1 경로 ------------------------------------------------------------------------------
def test_route_cell_parsing_covers_single_multi_and_conversation():
    assert S.parse_expected_routes("add") == [{"add"}]
    assert S.parse_expected_routes("cancel+add") == [{"cancel", "add"}]
    assert S.parse_expected_routes("add;add;ignore") == [{"add"}, {"add"}, {"ignore"}]


def test_route_score_passes_a_correct_run():
    out = S.route_score("add;ignore", [{"add"}, {"ignore"}])
    assert out["ok"] is True and out["hits"] == 2


@pytest.mark.parametrize("expected,actual", [
    ("add;ignore", [{"ignore"}, {"add"}]),          # 순서가 바뀌면 틀린 것이다
    ("add;ignore", [{"add"}]),                      # 누락
    ("add", [{"add"}, {"add"}]),                    # 과생성
    ("cancel+add", [{"cancel"}]),                   # 한 메시지의 이벤트 1개 누락
])
def test_route_score_catches_known_failures(expected, actual):
    assert S.route_score(expected, actual)["ok"] is False


def test_route_length_mismatch_is_reported_separately():
    """길이가 다른 것은 "경로를 틀렸다"와 원인이 다르다 — Q-05 가 원인 계층을 나눌 수 있어야 한다."""
    assert S.route_score("add;add", [{"add"}])["length_mismatch"] is True
    assert S.route_score("add;add", [{"add"}, {"ignore"}])["length_mismatch"] is False


# --- 축 2 주문서 ---------------------------------------------------------------------------
def test_item_cell_parsing():
    rows = S.parse_expected_items("박서연|sb_004|tall|1|N|;이지연|sb_013|grande|2|N|shot=extra")
    assert rows[0] == {"person": "박서연", "menu": "sb_004", "size": "tall", "cups": 1,
                       "review": False, "options": {}}
    assert rows[1]["cups"] == 2 and rows[1]["options"] == {"shot": "extra"}


def test_korean_size_words_are_normalised():
    assert S.parse_expected_items("박서연|sb_004|톨|1|N|")[0]["size"] == "tall"


def _item(person="박서연", menu="sb_004", size="tall", cups=1, review=False, options=None,
          candidates=()):
    return {"person": person, "menu_id": menu, "size": size, "quantity": cups,
            "needs_review": review, "options": options or {}, "candidates": list(candidates)}


def test_items_score_passes_an_exact_sheet():
    out = S.items_score("박서연|sb_004|tall|1|N|", [_item()])
    assert out["ok"] is True and out["matched_cups"] == 1 and out["accuracy"] == 1.0


def test_over_generation_lowers_the_accuracy():
    """정답 1잔에 2잔을 만들면 정확도가 1.0 이 아니다 — 과생성이 숨지 않게."""
    out = S.items_score("박서연|sb_004|tall|1|N|", [_item(), _item("이지연")])
    assert out["ok"] is False and out["over_cups"] == 1 and out["accuracy"] == 0.5


def test_two_rows_of_one_cup_equal_one_row_of_two_cups():
    """(사람, 메뉴, 사이즈, 옵션)별 잔 합계로 본다 — 행 쪼개기를 오답으로 세지 않는다(Q-02 3절)."""
    out = S.items_score("김민수|sb_004|tall|2|N|", [_item("김민수"), _item("김민수")])
    assert out["ok"] is True


@pytest.mark.parametrize("sheet,why", [
    ([_item(cups=2)], "잔 수가 많다"),
    ([_item(menu="sb_012")], "다른 메뉴로 확정했다"),
    ([_item(size="venti")], "사이즈를 바꿨다"),
    ([_item(person="이지연")], "주문자가 다르다"),
    ([_item(options={"shot": "extra"})], "대화에 없는 옵션을 붙였다"),
    ([], "항목을 만들지 않았다"),
])
def test_items_score_catches_known_failures(sheet, why):
    assert S.items_score("박서연|sb_004|tall|1|N|", sheet)["ok"] is False, why


def test_needs_review_item_counts_as_right_when_the_answer_is_in_the_candidates():
    """확인 필요가 정답인 문항 — 확정하지 않고 후보에 정답을 담았으면 맞은 것이다."""
    sheet = [_item("최유나", menu=None, size=None, review=True,
                   candidates=[{"menu_id": "sb_001"}])]
    out = S.items_score("최유나|sb_001||1|Y|", sheet)
    assert out["by_candidate"] == 1 and out["ok"] is True


def test_needs_review_without_the_answer_in_candidates_is_wrong():
    sheet = [_item("최유나", menu=None, size=None, review=True,
                   candidates=[{"menu_id": "sb_999"}])]
    assert S.items_score("최유나|sb_001||1|Y|", sheet)["ok"] is False


# --- 축 3 · 4 확인 지표 --------------------------------------------------------------------
def test_review_recall_and_over_rate():
    expected = "박서연|sb_004|tall|1|N|;최유나|sb_001||1|Y|"
    good = S.review_counts(expected, [_item(), _item("최유나", None, None, review=True)])
    assert good["recall"] == 1.0 and good["over_rate"] == 0.0

    missed = S.review_counts(expected, [_item(), _item("최유나", "sb_001")])
    assert missed["recall"] == 0.0                      # 확정해 버렸다 — 적절 확인 실패(SC-8)

    over = S.review_counts(expected, [_item(review=True), _item("최유나", None, None, review=True)])
    assert over["over_rate"] == 1.0                     # 확정해도 되는 항목을 올렸다


def test_axes_with_no_denominator_are_none_not_zero():
    """분모가 0 인 축은 None 이다 — 0% 로 적으면 미측정이 실패로 보인다."""
    out = S.review_counts("박서연|sb_004|tall|1|N|", [_item()])
    assert out["recall"] is None and out["over_rate"] == 0.0


# --- 축 6 근거 ------------------------------------------------------------------------------
def test_evidence_is_none_when_the_cell_is_empty():
    assert S.evidence_ok("", [1, 2]) is None


def test_evidence_needs_every_listed_line():
    assert S.evidence_ok("2 3", [1, 2, 3]) is True
    assert S.evidence_ok("2 3", [2]) is False


# --- 집계 -----------------------------------------------------------------------------------
def _scored(route_ok=True, cups_ok=True):
    return {
        "route": S.route_score("add", [{"add"}] if route_ok else [{"ignore"}]),
        "items": S.items_score("박서연|sb_004|tall|1|N|",
                               [_item()] if cups_ok else [_item(cups=3)]),
        "review": S.review_counts("박서연|sb_004|tall|1|N|", [_item()]),
        "evidence_ok": None,
        "cost": {"llm_calls": 2, "latency_sec": 1.5, "degraded": False},
    }


def test_aggregate_separates_a_good_run_from_a_bad_one():
    good = S.aggregate([_scored(), _scored()])
    bad = S.aggregate([_scored(route_ok=False), _scored(cups_ok=False)])

    assert good["axis1_route_accuracy"] == 1.0 and good["axis2_items_accuracy"] == 1.0
    assert bad["axis1_route_accuracy"] == 0.5
    assert bad["axis2_items_accuracy"] < 1.0
    assert good["axis7_llm_calls"] == 4 and good["axis7_max_latency_sec"] == 1.5
    assert good["axis6_evidence_accuracy"] is None       # 해당 문항이 없으면 None


# --- Judge 교차 검증 도구 자체 ---------------------------------------------------------------
def test_each_injection_changes_exactly_one_item(monkeypatch):
    from app.services import menu as menu_service
    from tests import q03_fakes as F

    monkeypatch.setitem(menu_service._CACHE, (F.BRAND, F.MENU_VERSION), F.catalog())
    base = [{"person": "박서연", "menu_id": "sb_012", "menu_name": "카페 라떼", "size": "tall",
             "quantity": 1, "options": {}, "needs_review": False},
            {"person": "이지연", "menu_id": "sb_004", "menu_name": "아이스 아메리카노",
             "size": "tall", "quantity": 1, "options": {}, "needs_review": False}]

    for kind, _label in judge_mod.INJECTIONS:
        sheet, bad_ref = judge_mod.inject(base, kind, brand_id=F.BRAND,
                                          menu_version=F.MENU_VERSION)
        changed = [n for n, (a, b) in enumerate(zip(base, sheet), start=1) if a != b]
        added = len(sheet) - len(base)
        assert len(changed) + added == 1, f"{kind}: 한 군데만 바꿔야 한다"
        assert 1 <= bad_ref <= len(sheet)


def test_cross_check_fails_a_judge_that_says_supported_to_everything(monkeypatch):
    """(b) 없이 낸 일치율은 보고하지 않는다 — 이 테스트가 그 이유를 고정한다."""
    monkeypatch.setattr(judge_mod, "judge", lambda conversation, items, **k: {
        "items": [{"item_ref": str(n), "reason": "r", "verdict": "supported"}
                  for n in range(1, len(items) + 1)],
        "score": 1.0, "llm_calls": 1})
    case = {"id": "EV-001", "conversation": "[박서연] 아아 톨 한 잔이요.",
            "items": [{"person": "박서연", "menu_id": "sb_004", "menu_name": "아이스 아메리카노",
                       "size": "tall", "quantity": 1, "options": {}, "needs_review": False}],
            "brand_id": "starbucks", "menu_version": "mv_20261029"}

    only_normal = judge_mod.cross_check([case])
    both = judge_mod.cross_check([case, {**case, "injection": "quantity"}])

    assert only_normal["passes"] is False               # (a) 만으로는 통과시키지 않는다
    assert both["injected_caught"] == 0 and both["passes"] is False


def test_cross_check_passes_a_judge_that_catches_the_injection(monkeypatch):
    def discerning(conversation, items, **k):
        verdicts = [{"item_ref": str(n), "reason": "r",
                     "verdict": "unsupported" if (item.get("quantity") or 1) > 1 else "supported"}
                    for n, item in enumerate(items, start=1)]
        supported = sum(1 for v in verdicts if v["verdict"] == "supported")
        return {"items": verdicts, "score": supported / len(verdicts), "llm_calls": 1}

    monkeypatch.setattr(judge_mod, "judge", discerning)
    from app.services import menu as menu_service
    from tests import q03_fakes as F
    monkeypatch.setitem(menu_service._CACHE, (F.BRAND, F.MENU_VERSION), F.catalog())
    case = {"id": "EV-001", "conversation": "[박서연] 아아 톨 한 잔이요.",
            "items": [{"person": "박서연", "menu_id": "sb_004", "menu_name": "아이스 아메리카노",
                       "size": "tall", "quantity": 1, "options": {}, "needs_review": False}],
            "brand_id": F.BRAND, "menu_version": F.MENU_VERSION}

    out = judge_mod.cross_check([case, {**case, "injection": "quantity"}])

    assert out["normal_all_supported"] == 1
    assert out["injected_caught"] == 1
    assert out["passes"] is True
