# 에이전트 서버(FastAPI) 호출 모음 — 화면은 이 파일을 통해서만 서버와 통신한다
#
# 계약의 출처는 D-02 API스펙 v1.0이다. 엔드포인트 이름 · 필드 이름을 여기서 지어내지 않는다.
#
# 이 파일이 지키는 것 (P-06 프론트엔드 원칙 · D-02 1절)
#   - LLM · DB를 직접 부르지 않는다. 서버 API만 호출한다
#   - 금액 계산 · 공유 가능 조건 판정 · 채팅 입력 해석을 하지 않는다
#   - 세션 관리 토큰은 URL이 아니라 `X-Session-Token` 헤더로 보낸다 (D-02 1.1)
#   - 운영 대시보드 토큰은 `Authorization: Bearer`로 보낸다
#   - 쓰기 요청에는 `If-Match: {version}`을 붙인다 (D-02 2.5 낙관적 잠금)
#   - 응답 포장은 `data` · `error` 둘 중 하나다 (D-02 1절 · 2.2)
import os

import httpx

# 로컬: localhost:8000 / compose: http://backend:8000 / 배포: 실제 주소
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
TIMEOUT = 10.0
# 쓰기 요청은 서버가 LLM 해석 · 원격 DB 왕복을 거친다(실측 채팅 반영 11초). 10초로 끊으면 **서버는 반영했는데
# 화면만 실패**로 보이고, 다시 누르면 두 번 반영된다 — 읽기보다 길게 기다린다
WRITE_TIMEOUT = 30.0


class ApiError(Exception):
    """서버가 `error`로 답했을 때 던진다.

    `user_message`만 화면에 보여준다. `detail`은 로그 · 운영 대시보드 몫이다
    (D-02 2.2 — 기술 문구가 담당자 화면에 뜨는 것이 가장 흔한 누출 경로다).
    """

    def __init__(self, code, user_message, *, detail=None, field=None, retryable=False, status=None):
        super().__init__(code)
        self.code = code
        self.user_message = user_message
        self.detail = detail
        self.field = field
        self.retryable = retryable
        self.status = status


class ApiUnreachable(ApiError):
    """서버에 닿지 못해 응답 자체가 없을 때 던진다."""

    def __init__(self, detail=None):
        super().__init__("UNREACHABLE", "잠시 후 다시 눌러 주세요", detail=detail, retryable=True)


def _headers(session_token=None, ops_token=None, version=None, idempotency_key=None):
    headers = {}
    if session_token:
        headers["X-Session-Token"] = session_token
    if ops_token:
        headers["Authorization"] = f"Bearer {ops_token}"
    if version is not None:
        headers["If-Match"] = str(version)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _send(method, path, *, client=None, **kwargs):
    # client는 테스트에서 가짜 서버를 끼워 넣을 때만 넘긴다
    if client is not None:
        return client.request(method, path, **kwargs)
    timeout = TIMEOUT if method == "GET" else WRITE_TIMEOUT
    with httpx.Client(base_url=BACKEND_URL, timeout=timeout) as c:
        return c.request(method, path, **kwargs)


def _unwrap(res):
    """`{"data": ...}`를 벗겨 돌려주고, `{"error": ...}`면 ApiError를 던진다."""
    try:
        body = res.json()
    except ValueError:
        raise ApiError("BAD_RESPONSE", "잠시 후 다시 눌러 주세요", detail="response is not json", status=res.status_code)

    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise ApiError(
            err.get("code", "UNKNOWN"),
            err.get("user_message", "잠시 후 다시 눌러 주세요"),
            detail=err.get("detail"),
            field=err.get("field"),
            retryable=bool(err.get("retryable")),
            status=res.status_code,
        )

    if res.status_code >= 400:
        raise ApiError("HTTP_ERROR", "잠시 후 다시 눌러 주세요", detail=f"status={res.status_code}", status=res.status_code)

    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body  # /health 는 `/api` 밖이라 포장이 없다 (D-02 API-001)


def request(method, path, *, client=None, session_token=None, ops_token=None, version=None,
            idempotency_key=None, **kwargs):
    """공통 호출 — 성공하면 `data`를, 실패하면 ApiError를 돌려준다."""
    headers = _headers(session_token, ops_token, version, idempotency_key)
    if headers:
        kwargs["headers"] = {**headers, **kwargs.pop("headers", {})}
    try:
        res = _send(method, path, client=client, **kwargs)
    except httpx.HTTPError as e:
        raise ApiUnreachable(detail=str(e))
    return _unwrap(res)


# --- 4.0 공통 · 상태 ---------------------------------------------------------
def get_health(client=None):
    """API-001 — 상태 확인. `status`는 ok · degraded · down 3분기다."""
    return request("GET", "/health", client=client)


def is_backend_alive(client=None):
    """서버가 일할 수 있는 상태인지. 화면 배관 확인용이며 담당자 화면에 띄우지 않는다."""
    try:
        return get_health(client=client).get("status") in ("ok", "degraded")
    except ApiError:
        return False


def get_meta(client=None):
    """API-002 — 기준 정보(메뉴 기준일 · 가격 고지 문구 · 매장 기준 라벨 · 정책 수치)."""
    return request("GET", "/api/meta", client=client)


# --- 4.1 담당자 — 입력과 분석 (SCR-001 → SCR-002) ----------------------------
def get_sample_conversation(client=None):
    """API-010 — 예시 대화 조회(`예시로 채워보기`)."""
    return request("GET", "/api/sample-conversation", client=client)


def preview_conversation(raw_text=None, file=None, client=None):
    """API-011 — 대화 미리보기 파싱. 형식만 보고 LLM은 호출하지 않는다.

    `file`은 `(파일이름, 바이트)` 짝이다. 확장자 제한은 걸지 않고 **서버가 판정한다**
    (P-06 Streamlit 구현 대응 — 제한을 걸면 설계 문구가 아니라 Streamlit 오류가 뜬다).
    """
    if file is not None:
        return request("POST", "/api/conversations/preview", client=client,
                       files={"file": (file[0], file[1])})
    return request("POST", "/api/conversations/preview", client=client, json={"raw_text": raw_text})


def create_session(raw_text, source="paste", client=None):
    """API-012 — 분석 요청. 매장 기준은 서버가 규칙으로 판별한다.

    `brand_id`를 보내지 않는다 — 화면에 브랜드 선택 UI가 없다(PRD 2.4.1 · FR-001).
    매장 요청은 `raw_text` 안의 마지막 한 줄이며 별도 필드가 아니다.
    """
    return request("POST", "/api/sessions", client=client, json={"raw_text": raw_text, "source": source})


def submit_brand_answer(session_id, answer, store_name=None, *, session_token=None, client=None):
    """API-013 — 매장 되묻기 답 제출. 되묻기는 분석 전 1회다."""
    payload = {"answer": answer}
    if store_name:
        payload["store_name"] = store_name
    return request("POST", f"/api/sessions/{session_id}/brand-answer", client=client,
                   session_token=session_token, json=payload)


def get_progress(session_id, *, session_token=None, client=None):
    """API-014 — 분석 진행 상태(진행 4단계 · 상태 줄 문구). 1초 주기 폴링."""
    return request("GET", f"/api/sessions/{session_id}/progress", client=client, session_token=session_token)


def get_session(session_id, *, session_token=None, client=None):
    """API-015 — 분석 결과 조회(주문서 패널 투영)."""
    return request("GET", f"/api/sessions/{session_id}", client=client, session_token=session_token)


# --- 4.2 담당자 — 검토 · 수정 (SCR-002) --------------------------------------
def get_questions(session_id, *, session_token=None, client=None):
    """API-020 — 확인 질문 큐. 한 번에 하나만 묻는다."""
    return request("GET", f"/api/sessions/{session_id}/questions", client=client, session_token=session_token)


def answer_question(session_id, question_id, answer, *, session_token=None, version=None, client=None):
    """API-021 — 확인 질문 답변(빠른 답 버튼)."""
    return request("POST", f"/api/sessions/{session_id}/questions/{question_id}/answer", client=client,
                   session_token=session_token, version=version, json={"answer": answer})


def answer_question_text(session_id, question_id, text, *, session_token=None, version=None,
                         idempotency_key=None, client=None):
    """API-021 (text) — 질문이 열려 있을 때 채팅으로 친 문장을 답으로 보낸다. 해석은 서버가 한다.

    서버가 답으로 읽지 못하면 422(NOT_APPLICABLE) — 화면은 그때 채팅 수정(API-022)으로 넘긴다.
    """
    return request("POST", f"/api/sessions/{session_id}/questions/{question_id}/answer", client=client,
                   session_token=session_token, version=version,
                   idempotency_key=idempotency_key, json={"text": text})


def post_message(session_id, text, *, session_token=None, version=None, idempotency_key=None,
                 client=None):
    """API-022 — 채팅 입력 해석. 입력 문장을 그대로 보낸다.

    해석은 서버가 한다 — 화면은 받은 "전 → 후" 안내와 되돌리기 토큰만 표시한다
    (P-06 프론트엔드 원칙).

    `idempotency_key` 를 받는다 — 채팅 입력은 **한 번 보낸 것을 다시 보낼 수 있다.**
    `st.chat_input` 이 값을 비우는 재실행으로 보내던 실행이 취소되면 다음 실행이 다시 걸고,
    키가 같으면 서버가 보관한 응답을 돌려주므로 주문이 두 번 바뀌지 않는다 (D-02 1.2 · 4차 보고 1번).
    """
    return request("POST", f"/api/sessions/{session_id}/messages", client=client,
                   session_token=session_token, version=version,
                   idempotency_key=idempotency_key, json={"text": text})


def patch_item(session_id, item_id, changes, *, session_token=None, version=None, client=None):
    """API-023 — 행 편집(사양 · 옵션 · 기타 요청 · 메뉴 바꾸기 · 빼기)."""
    return request("PATCH", f"/api/sessions/{session_id}/items/{item_id}", client=client,
                   session_token=session_token, version=version, json=changes)


def undo(session_id, undo_token, *, session_token=None, client=None):
    """API-024 — 되돌리기(직전 1회)."""
    return request("POST", f"/api/sessions/{session_id}/undo", client=client,
                   session_token=session_token, json={"undo_token": undo_token})


def get_evidence(session_id, *, item_id=None, question_id=None, session_token=None, client=None):
    """API-025 — 근거 메시지 조회(`원본 보기` · `대화 보기`)."""
    params = {k: v for k, v in (("item_id", item_id), ("question_id", question_id)) if v}
    return request("GET", f"/api/sessions/{session_id}/evidence", client=client,
                   session_token=session_token, params=params)


def search_menus(query, *, session_token=None, client=None):
    """API-026 — 메뉴 검색(메뉴 찾기 모달). 매장 기준은 세션에서 주입된다."""
    return request("GET", "/api/menus/search", client=client, session_token=session_token,
                   params={"q": query})


def change_brand(session_id, brand_id, *, session_token=None, version=None, client=None):
    """API-027 — 매장 기준 바꾸기 실행([새로 정리])."""
    return request("POST", f"/api/sessions/{session_id}/brand-change", client=client,
                   session_token=session_token, version=version, json={"brand_id": brand_id})


# --- 4.3 담당자 — 주문서 · 공유 (SCR-005) ------------------------------------
def get_order_sheet(session_id, *, session_token=None, client=None):
    """API-030 — 산출물 조회(묶음 목록 · 합계 · 공유 가능 여부)."""
    return request("GET", f"/api/sessions/{session_id}/order-sheet", client=client, session_token=session_token)


def analyze_replies(session_id, raw_text, *, apply=False, session_token=None, client=None):
    """API-031 — 답장 대화 분석 → 변경 제안.

    `apply=True` 면 분명한 변경은 서버가 바로 반영하고 불분명한 것은 확인 질문으로 올린다
    (2026-11-05 보고 11번 — 담당자가 제안마다 [반영]을 누르지 않는다).
    """
    return request("POST", f"/api/sessions/{session_id}/replies/analyze", client=client,
                   session_token=session_token, json={"raw_text": raw_text, "apply": bool(apply)})


def decide_suggestion(session_id, suggestion_id, decision, *, session_token=None, version=None, client=None):
    """API-032 — 제안 반영 · 무시."""
    return request("POST", f"/api/sessions/{session_id}/suggestions/{suggestion_id}/decision", client=client,
                   session_token=session_token, version=version, json={"decision": decision})


def get_share(session_id, fmt="text", *, names=True, session_token=None, client=None):
    """API-033 — 공유 내용 생성. 내용은 서버가 만든다 — 화면이 조립하지 않는다.

    `names=False`면 이름 없이 메뉴 · 수량 · 요청 · 합계만 받는다(이름 노출 선택 · 2026-11-04).

    `fmt="image"`는 `image/png` 본문이라 포장이 없으므로 바이트를 그대로 돌려준다.
    """
    if fmt == "image":
        headers = _headers(session_token=session_token)
        try:
            res = _send("GET", f"/api/sessions/{session_id}/share", client=client,
                        params={"format": "image", "names": _flag(names)}, headers=headers)
        except httpx.HTTPError as e:
            raise ApiUnreachable(detail=str(e))
        if res.status_code >= 400:
            return _unwrap(res)
        return res.content
    return request("GET", f"/api/sessions/{session_id}/share", client=client,
                   session_token=session_token, params={"format": "text", "names": _flag(names)})


def _flag(value) -> str:
    """쿼리의 참/거짓은 FastAPI 가 읽는 소문자 문자열로 보낸다."""
    return "true" if value else "false"


def complete_share(session_id, method, *, session_token=None, client=None):
    """API-034 — 공유 완료 기록(= COMPLETED). 완료 처리는 서버가 한다.

    여러 번 와도 안전하다(D-02 1.2) — 복사 · 저장할 때마다 완료 시각이 덧붙는다.
    """
    return request("POST", f"/api/sessions/{session_id}/share/complete", client=client,
                   session_token=session_token, json={"method": method})


# --- 4.4 운영 대시보드 (SCR-006 · O-0 ~ O-6) ---------------------------------
def ops_login(password, client=None):
    """API-040 — 운영 대시보드 인증. 비밀번호 비교는 서버가 한다.

    화면은 받은 토큰만 보관하고 비밀번호를 저장 · 기록하지 않는다(P-06 O-0 검증 위치).
    """
    return request("POST", "/api/ops/login", client=client, json={"password": password})


def ops_logout(*, ops_token=None, client=None):
    """API-041 — 인증 해제([나가기])."""
    return request("POST", "/api/ops/logout", client=client, ops_token=ops_token)


def ops_incidents(*, ops_token=None, limit=50, client=None):
    """API-042 — 사건 목록(O-1)."""
    return request("GET", "/api/ops/incidents", client=client, ops_token=ops_token, params={"limit": limit})


def ops_incident(incident_id, *, ops_token=None, client=None):
    """API-043 — 사건 상세(O-2 · 타임라인 · 근거)."""
    return request("GET", f"/api/ops/incidents/{incident_id}", client=client, ops_token=ops_token)


def ops_decide(incident_id, decision, *, ops_token=None, client=None):
    """API-044 — 승인 · 거부(O-4). 첫 결정만 반영된다."""
    return request("POST", f"/api/ops/incidents/{incident_id}/decision", client=client,
                   ops_token=ops_token, json={"decision": decision})


def ops_status(*, ops_token=None, client=None):
    """API-045 — 지금 상태 · 회로 상태(O-3)."""
    return request("GET", "/api/ops/status", client=client, ops_token=ops_token)


def ops_metrics(*, ops_token=None, client=None):
    """API-046 — 지표(O-5 · MTTD · MTTR · 성공률 · 에스컬레이션)."""
    return request("GET", "/api/ops/metrics", client=client, ops_token=ops_token)


def ops_trials(*, ops_token=None, client=None):
    """장애 시험 결과(블록 D · F-1 ~ F-6 × 회차) — 고도화안 G-3 · G-4. 서버 미구현 구간이다.

    응답 모양: `{"total_runs": 25, "rows": [{"fault": "F-1", "cells": [{"result": "auto", "sec": 41}, ...]}],
    "f4": {"cells": [{"result": "switched"}, ...]}}` — result 는 auto · approved · failed · invalid ·
    running · pending (F-4 는 switched · not_switched · pending).
    """
    return request("GET", "/api/ops/trials", client=client, ops_token=ops_token)


def ops_orders(*, ops_token=None, limit=50, client=None):
    """API-047 — 주문 처리 기록 목록(O-6)."""
    return request("GET", "/api/ops/orders", client=client, ops_token=ops_token, params={"limit": limit})


def ops_order(order_id, *, ops_token=None, client=None):
    """API-048 — 주문 처리 기록 상세(O-6 펼침)."""
    return request("GET", f"/api/ops/orders/{order_id}", client=client, ops_token=ops_token)


def ops_fault(code, action="inject", *, ops_token=None, client=None):
    """API-049 — 장애 주입 · 해제(F-1 ~ F-6 · 실습 전용)."""
    return request("POST", "/api/ops/faults", client=client, ops_token=ops_token,
                   json={"code": code, "action": action})


def ops_workflow_preflight(*, mode="quick", fault_type="F-1", target=None, ops_token=None, client=None):
    return request("POST", "/api/ops/workflows/preflight", client=client, ops_token=ops_token,
                   json={"mode": mode, "fault_type": fault_type, "target": target})


def ops_workflow_create(*, mode="quick", fault_type="F-1", target=None, repetitions=1,
                        injection_seconds=1, idempotency_key=None, ops_token=None, client=None):
    return request("POST", "/api/ops/workflows", client=client, ops_token=ops_token,
                   idempotency_key=idempotency_key,
                   json={"mode": mode, "fault_type": fault_type, "target": target,
                         "repetitions": repetitions, "injection_seconds": injection_seconds})


def ops_workflows(*, ops_token=None, client=None):
    return request("GET", "/api/ops/workflows", client=client, ops_token=ops_token)


def ops_workflow(run_id, *, ops_token=None, client=None):
    return request("GET", f"/api/ops/workflows/{run_id}", client=client, ops_token=ops_token)


def ops_workflow_stop(run_id, *, ops_token=None, client=None):
    return request("POST", f"/api/ops/workflows/{run_id}/stop", client=client, ops_token=ops_token)


def ops_workflow_export(run_id, fmt="json", *, ops_token=None, client=None):
    headers = _headers(ops_token=ops_token)
    try:
        res = _send("GET", f"/api/ops/workflows/{run_id}/export", client=client,
                    params={"format": fmt}, headers=headers)
    except httpx.HTTPError as exc:
        raise ApiUnreachable(detail=str(exc))
    if res.status_code >= 400:
        _unwrap(res)
    return res.content
