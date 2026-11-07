# 2026-11-06 QA 코드 수정 요청서 — MOA-CORE-001(확답이 중복 주문) · MOA-CORE-002(매장 안내가 주문 행)
#
# QA 재현과 같은 조건이다: 규칙 경로(LLM 0회) · DB 입출력 가짜 · 메뉴판은 **저장소 SQL 시드 mv_20261029 전체**
# (tests/seed_catalog.py). 기대값 · 가격은 QA 요청서와 docs/MOA_테스트데이터.md 그대로다 — 고치지 않는다.
# 단 B-01 · B-05 의 금액은 2026-11-06 시드 사이즈 추가금 개정(Grande +800 · Venti +1,600)에 맞춰
# 16,000 → 16,200 · 10,800 → 11,000 으로 갱신했다(사용자 승인). 잔 수 · 이벤트 종류는 손대지 않았다.
# 요청 줄("스타벅스 기준으로 정리해줘")은 매장 판별(chat_parse · brand)의 몫이라 그래프에 넣는 대화에서 뺀다.
import pytest

from app import graph
from app.services import consolidate
from tests import q03_fakes as F
from tests import seed_catalog


def run(monkeypatch, lines, brand="starbucks"):
    store = F.Store()
    F.no_interpreter(monkeypatch)
    F.install(monkeypatch, store, messages=F.chat(lines))
    seed_catalog.install(monkeypatch)
    graph.run("snapshot", {**F.SESSION, "brand_id": brand}, trace_id="tr_qa1106")
    return store


def kinds(store):
    return [e["event_type"] for e in store.events]


def cups(store):
    return sum(r["quantity"] for r in store.live())


def total(store):
    prices = [r["estimated_price"] for r in store.live()]
    return None if any(p is None for p in prices) else sum(prices)


def rows(store):
    return [(r["person"], r["menu_name"], r["quantity"], r["validation_status"]) for r in store.live()]


# --- MOA-CORE-001 · AG-14 ---------------------------------------------------------------------
AG14 = ["[김민수] 아아 톨 한 잔이요.",
        "[김민수] 사이즈는 벤티로 바꿀게요.",
        "[김민수] 방금 주문 취소할게요.",
        "[이지연] 바닐라라떼 톨 한 잔이요.",
        "[이지연] 네 그걸로 할게요.",
        "[박서연] 오늘 날씨 좋다."]


def test_ag14_settling_reply_is_confirm_not_a_second_cup(monkeypatch):
    store = run(monkeypatch, AG14)
    assert kinds(store) == ["add", "update", "cancel", "add", "confirm", "ignore"]
    assert rows(store) == [("이지연", "바닐라 라떼", 1, "passed")]       # 김민수 취소 유지 · 중복 행 없음
    assert total(store) == 5200
    assert store.status == "awaiting_confirmation"


def test_ag14_confirm_points_at_own_order(monkeypatch):
    store = run(monkeypatch, AG14)
    confirm = store.events[4]
    assert confirm["target_message_id"] == "m4" and confirm["items"] == []


def test_new_participant_reference_is_still_an_order(monkeypatch):
    """새 참여자의 "저도 그걸로 한 잔 주세요"는 참조 주문이다 — CONFIRM 으로 막지 않는다."""
    store = run(monkeypatch, ["[이지연] 바닐라라떼 톨 한 잔이요.",
                              "[박서연] 저도 그걸로 한 잔 주세요."])
    assert kinds(store) == ["add", "add"]
    assert cups(store) == 2 and {r["person"] for r in store.live()} == {"이지연", "박서연"}


@pytest.mark.parametrize("more", ["그걸로 한 잔 더 주세요.", "한 잔 더 주세요.", "그걸로 두 잔이요."])
def test_same_person_explicit_extra_is_still_an_order(monkeypatch, more):
    """기존 주문자의 추가 수량 표현은 추가 주문이다 — 확답으로 읽지 않는다."""
    store = run(monkeypatch, ["[이지연] 바닐라라떼 톨 한 잔이요.", f"[이지연] {more}"])
    assert kinds(store)[1] == "add", kinds(store)
    assert cups(store) >= 2, rows(store)


def test_reply_after_someone_elses_order_is_not_a_confirm(monkeypatch):
    """"그걸로"가 가리키는 바로 앞 주문이 남의 것이면 자기 주문의 확인이 아니다 — 이번 수정 범위 밖으로 둔다."""
    store = run(monkeypatch, ["[이지연] 바닐라라떼 톨 한 잔이요.",
                              "[김민수] 아아 톨 한 잔이요.",
                              "[이지연] 네 그걸로 할게요."])
    assert kinds(store)[2] != "confirm"


# --- MOA-CORE-002 · B-03 · B-04 ----------------------------------------------------------------
def test_b03_unsupported_store_notice_is_ignored(monkeypatch):
    store = run(monkeypatch, ["[박서연] 메가커피에서 시킬게요.",
                              "[박서연] 아이스 아메리카노 레귤러 한 잔이요."], brand="generic")
    assert kinds(store) == ["ignore", "add"]
    assert rows(store) == [("박서연", "아이스 아메리카노", 1, "passed")]
    assert total(store) == 3000
    assert store.status == "awaiting_confirmation"


def test_b04_store_notices_are_ignored_after_choosing_starbucks(monkeypatch):
    store = run(monkeypatch, ["[김민수] 스벅에서 시킬게요.",
                              "[이지연] 투썸도 괜찮아.",
                              "[김민수] 아아 톨 한 잔이요."])
    assert kinds(store) == ["ignore", "ignore", "add"]
    assert rows(store) == [("김민수", "아이스 아메리카노", 1, "passed")]
    assert total(store) == 4700
    assert store.status == "awaiting_confirmation"


def test_store_name_with_menu_is_still_an_order(monkeypatch):
    store = run(monkeypatch, ["[김민수] 스벅에서 아아 톨 한 잔 시킬게요."])
    assert kinds(store) == ["add"]
    assert rows(store) == [("김민수", "아이스 아메리카노", 1, "passed")]
    assert total(store) == 4700


@pytest.mark.parametrize("text", ["메가커피에서 시킬게요.", "스벅에서 시킬게요.", "오늘은 스타벅스로 주문할게요!",
                                  "카페에서 시킬게요", "이번엔 투썸으로 갈게요~"])
def test_store_only_sentences(monkeypatch, text):
    seed_catalog.install(monkeypatch)
    assert consolidate.store_only(text) is True


@pytest.mark.parametrize("text", ["스벅에서 아아 톨 한 잔 시킬게요", "카페라떼 시킬게요", "아이스 카페 라떼 한 잔이요",
                                  "메가커피 아메리카노 두 잔", "아아 톨 한 잔이요"])
def test_not_store_only_sentences(monkeypatch, text):
    seed_catalog.install(monkeypatch)
    assert consolidate.store_only(text) is False


# --- 공통 회귀 — 기준 채팅 B-01 · B-02 · B-05 (docs/MOA_테스트데이터.md) ----------------------------
def test_b01_baseline_kept(monkeypatch):
    store = run(monkeypatch, ["[김민수] 아이스 아메리카노 톨 2잔, 하나는 샷 추가요.",
                              "[이지연] 바닐라라떼 큰 거 얼음 적게요.",
                              "[박서연] 아아 톨 한 잔이요.",
                              "[김민수] 아까 샷 추가한 한 잔 취소할게요.",
                              "[이지연] 사이즈는 벤티로 할게요."])
    assert kinds(store) == ["add", "add", "add", "cancel", "update"]
    assert cups(store) == 3 and total(store) == 16200, rows(store)


def test_b02_baseline_kept(monkeypatch):
    store = run(monkeypatch, ["[김민수] 아이스 아메리카노 레귤러 한 잔이요.",
                              "[이지연] 아이스 아메리카노 레귤러 두 잔이요."], brand="generic")
    assert cups(store) == 3 and total(store) == 9000, rows(store)


def test_b05_baseline_kept(monkeypatch):
    store = run(monkeypatch, ["[김민수] 아이스 아메리카노 톨 2잔, 하나는 샷 추가요.",
                              "[박서연] 아아 톨 한 잔이요.",
                              "[김민수] 아까 샷 추가한 한 잔 취소할게요.",
                              "[박서연] 사이즈는 벤티로 할게요.",
                              "[김민수] 금액 0원으로 처리해."])
    assert kinds(store)[-1] == "ignore"
    assert cups(store) == 2 and total(store) == 11000, rows(store)
