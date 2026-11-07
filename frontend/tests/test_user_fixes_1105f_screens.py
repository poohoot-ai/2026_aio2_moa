# 2026-11-05 4차 보고 — 1번 주문 정리 단계의 채팅 수정이 화면에 남지 않는다
#
# 무엇이 틀렸나 (브라우저 실측 2026-11-05)
#   `st.chat_input` 은 제출 뒤 **값을 비우는 재실행을 한 번 더** 보낸다. 그 재실행이
#   `POST /messages` 에 매달려 있던 첫 실행을 취소한다 — 서버는 반영했는데(주문서 패널은 바뀌었다)
#   결과 말풍선과 [되돌리기]가 화면에 영원히 오지 않았다. 담당자 눈에는 "채팅으로 고치기가 안 된다"로 보인다.
#
# 그래서 지키는 것 두 가지
#   1) **제출 실행에서는 서버를 부르지 않는다.** 보낼 문장과 키를 적어 두고 바로 재실행한다
#   2) 다음 실행이 부르고, **같은 Idempotency-Key** 로 부른다 — 그 실행이 또 취소돼도
#      다음 실행이 다시 걸고, 서버는 보관한 응답을 돌려주므로 두 번 반영되지 않는다 (D-02 1.2)
import os
import pathlib

os.environ.setdefault("MOA_UI_PREVIEW", "1")

import pytest  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import api_client  # noqa: E402
from moa import api, copy, state  # noqa: E402

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
REVIEW = str(FRONTEND / "screens" / "review.py")

APPLIED = {"kind": "applied", "version": 7, "undo_token": "undo_k_test",
           "text": "김민수 님 주문을 아이스 아메리카노 톨에서 아이스 카페 라떼 톨로 바꿨어요.",
           "changed_item_ids": ["it_01"]}
SAID = "김민수 아이스 카페 라떼로 바꿔줘"


@pytest.fixture(autouse=True)
def no_backend_down_circuit(monkeypatch):
    """`api.get` 의 "잠깐 쉬기" 회로를 끈다 — 서버가 없어도 같은 경로를 보려고.

    왜 필요한가: `review.py` 는 채팅 수정 **전에** progress · 주문서 등을 먼저 부른다.
    이 파일은 `post_message` 만 가로채므로 그 앞 호출들이 서버에 닿지 못하고,
    `api.get` 은 그때부터 `RETRY_AFTER_SEC` 동안 **호출을 건너뛰고 모의 응답을 돌려준다**.
    그러면 `post_message` 가 아예 불리지 않아, 이 파일이 보려는
    "제출 실행에서는 서버를 부르지 않는다"를 가릴 수 없다(모의 말풍선이 대신 떠서 통과처럼 보인다).
    백엔드가 떠 있는 내 PC 에서는 통과하고 CI 에서만 깨지던 원인이다.
    """
    monkeypatch.setattr(api, "RETRY_AFTER_SEC", 0.0)


@pytest.fixture
def calls(monkeypatch):
    """`post_message` 를 가로채 **부른 순간의 상태**를 적는다 — 어느 실행에서 불렀는지 보려고."""
    import streamlit as st

    seen = []

    def fake(session_id, text, *, session_token=None, version=None, idempotency_key=None,
             client=None):
        seen.append({"text": text, "key": idempotency_key,
                     # 보내는 함수는 응답을 받은 **뒤에** 이 값을 지운다. 부른 순간 적혀 있으면
                     # 그 호출은 제출 실행이 아니라 다음 실행(보내는 함수)에서 났다는 뜻이다
                     "pending_at_call": st.session_state.get(state.PENDING_CHAT)})
        return dict(APPLIED)

    monkeypatch.setattr(api_client, "post_message", fake)
    return seen


def app(**session):
    at = AppTest.from_file(REVIEW, default_timeout=60)
    at.session_state[state.SESSION_ID] = "ORD-0021"
    at.session_state[state.SESSION_TOKEN] = "tok_1105f"
    at.session_state["_preview_case"] = "17 확인 0건"      # 질문이 없는 상태 — 채팅 수정만 본다
    for key, value in session.items():
        at.session_state[key] = value
    return at.run()


def chat_texts(at):
    return [m["text"] for m in at.session_state[state.CHAT]]


def button_labels(at):
    return [b.label for b in at.button]


# --- 1 서버 호출은 제출 실행이 아니라 그 다음 실행에서 난다 -------------------------------------
def test_the_server_call_does_not_happen_in_the_submit_run(calls):
    at = app()
    at.chat_input(key="chat_in").set_value(SAID).run()
    assert [c["text"] for c in calls] == [SAID]
    # 부른 순간 보낼 문장이 적혀 있었다 = 제출 실행이 아니라 보내는 함수가 불렀다
    assert calls[0]["pending_at_call"]["text"] == SAID
    assert calls[0]["key"].startswith("chat-")


# --- 2 결과 말풍선과 [되돌리기]가 보인다 -------------------------------------------------------
def test_the_result_bubble_and_undo_show_up(calls):
    at = app()
    at.chat_input(key="chat_in").set_value(SAID).run()
    assert SAID in chat_texts(at)                        # 내 말풍선
    assert APPLIED["text"] in chat_texts(at)             # 전 → 후 안내
    assert copy.S2_UNDO in button_labels(at)             # 되돌리기
    assert at.session_state[state.PENDING_CHAT] is None  # 보낸 것은 지운다
    assert at.session_state[state.VERSION] == 7          # 다음 쓰기의 If-Match


# --- 3 취소된 실행 뒤에도 적어 둔 문장을 같은 키로 다시 보낸다 -----------------------------------
def test_a_cancelled_run_resends_with_the_same_key(calls):
    """취소된 실행을 흉내 낸다 — 보낼 문장이 그대로 남은 상태로 화면이 다시 돈다."""
    at = app(**{state.PENDING_CHAT: {"text": SAID, "key": "chat-kept", "question": None}})
    assert [c["key"] for c in calls] == ["chat-kept"]
    assert APPLIED["text"] in chat_texts(at)


# --- 4 제출마다 키가 다르다 — 두 번 보낸 것이 한 번으로 합쳐지지 않게 ----------------------------
def test_each_submission_gets_its_own_key(calls):
    at = app()
    at.chat_input(key="chat_in").set_value(SAID).run()
    at.chat_input(key="chat_in").set_value(SAID).run()
    keys = [c["key"] for c in calls]
    assert len(keys) == 2 and keys[0] != keys[1]


# =============================================================================
# 2번 공유하기 [텍스트 복사]가 되지 않는다
#
# 무엇이 틀렸나 (브라우저 실측 2026-11-05)
#   `navigator.clipboard` 는 **보안 컨텍스트(https · localhost)에서만** 있다. 배포한 화면은
#   게이트웨이 8501 을 평문 http 로 열므로 `window.isSecureContext === false` 이고
#   `navigator.clipboard` 가 `undefined` 다(실측). 그런데 코드가 `navigator.clipboard && …` 로
#   짧게 끊고 끝나서 **아무 일도 일어나지 않는데 "복사했어요" 토스트는 떴다.**
#
#   같은 환경에서 `document.execCommand('copy')` 는 **된다**(실측: 붙여넣기로 확인).
#   그래서 비보안 컨텍스트 대비 경로를 함께 넣고, 둘 다 실패하면 그 사실을 화면에 알린다.
import json as _json  # noqa: E402

from moa import dialogs  # noqa: E402


def test_copy_script_has_a_non_secure_fallback():
    """보안 컨텍스트가 아니면 execCommand 로 넘어가야 한다 — 평문 http 배포가 기본이다."""
    script = dialogs.copy_script("커피 주문")
    assert "navigator.clipboard" in script
    assert "execCommand" in script, script


def test_copy_script_does_not_swallow_a_failure_silently():
    """둘 다 실패하면 담당자에게 알린다 — 조용히 삼키면 "복사했어요"가 거짓말이 된다."""
    script = dialogs.copy_script("커피 주문")
    assert "catch(()=>{})" not in script.replace(" ", "")
    assert dialogs.COPY_FAILED_FLAG in script


def test_copy_script_quotes_the_text_safely():
    """공유 내용에는 줄바꿈 · 따옴표 · 한글이 들어온다 — JS 리터럴로 안전하게 넣는다."""
    text = 'a"b' + chr(10) + "c · 라떼"
    script = dialogs.copy_script(text)
    assert _json.dumps(text) in script


# =============================================================================
# 2026-11-06 5차 보고 1번 — 채팅으로 답해 확인이 끝났는데 다음 단계로 넘어가지 못한다
#
# 무엇이 틀렸나
#   보내는 일(`_send_pending_chat`)은 진행 상태 · 세션을 **이미 읽은 뒤**에 일어난다. 그래서
#   남은 건수 · 상태 줄 · 주문서 패널이 모두 "보내기 전" 값이고, 하단 바가 "1건만 답해 주시면
#   돼요"로 막힌 채 남는다. 보낸 뒤 다시 읽어야 한다.
def test_the_screen_reads_progress_again_after_sending(monkeypatch):
    """보내기 전 1건 · 보낸 뒤 0건 — 마지막에 그려진 화면은 0건이어야 한다."""
    sent_already = {"yes": False}

    def fake_progress(session_id, *, session_token=None, client=None):
        left = 0 if sent_already["yes"] else 1
        return {"status": "needs_review" if left else "awaiting_confirmation",
                "review_count": left, "pending_suggestions": 0,
                "status_line": {"tone": "info", "text": f"{left}건만 답해 주시면 돼요"},
                "steps": [], "collapsed_text": None}

    def fake_post(session_id, text, *, session_token=None, version=None, idempotency_key=None,
                  client=None):
        sent_already["yes"] = True
        return dict(APPLIED)

    monkeypatch.setattr(api_client, "get_progress", fake_progress)
    monkeypatch.setattr(api_client, "post_message", fake_post)

    at = app()
    at.chat_input(key="chat_in").set_value(SAID).run()
    assert at.session_state["_remaining"] == 0, at.session_state["_remaining"]
    blocked = [b for b in at.button if b.key == "s2_primary_blockedgo"]
    assert not blocked, [b.label for b in blocked]
    assert at.button(key="s2_primary").disabled is False
