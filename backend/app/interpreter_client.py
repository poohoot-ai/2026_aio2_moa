"""주문 해석 서비스 호출 — DEP-001 의 호출부 (D-02 6절).

지키는 것
  - 제한시간 5초(PRD 5.6). 넘으면 DEP_TIMEOUT
  - **재시도 · 대체 전환을 여기서 하지 않는다.** 재시도는 Recovery 한 곳에서만 센다(교안코드규약 4.14 ·
    D-03 1.3). 여기서도 세면 실제 호출 횟수를 모르고 비용 사고로 직결된다
  - **결과를 우리 계약으로 다시 검증한다**(F-011). 서비스 · 모델을 믿지 않는다 — F-5 탐지 지점
  - 어느 서비스 · 모델이 답했는지 llm_history 에 남긴다(교안 4.14 규약 2)
  - **보내기 직전 요청 본문 전체의 PII 를 가린다**(D-13 · D-10 4절). LLM 으로 나가는 유일한 경계가 여기다 —
    호출부가 가렸는지에 기대지 않는다

에이전트(Intake · Menu · Validation)에 배선하는 것은 D-13 ~ D-15 다. 지금은 이 모듈만 있다.
"""

import datetime as dt
import time

import httpx
from pydantic import BaseModel, ValidationError

from app import config, logging_setup
from app.contracts import TASK_CONTRACTS
from app.tools.pii import mask_payload


# DEP-001 task → 호출하는 에이전트. llm_history.agent CHECK(intake · menu · validation)와 같은 값이어야 한다 —
# 다른 값을 넣으면 INSERT 가 실패하고, 이력 실패는 삼키므로 **기록이 조용히 0건**이 된다
TASK_AGENT = {"extract_events": "intake", "normalize_menu": "menu", "review": "validation"}


class InterpreterError(RuntimeError):
    """의존 서비스 실패. fault_code 는 P-03 7절 장애 코드다 — 사건 레코드에 그대로 들어간다."""

    def __init__(self, fault_code: str, detail: str, *, service: str,
                 missing_fields: list[str] | None = None):
        super().__init__(detail)
        self.fault_code = fault_code
        self.service = service
        self.missing_fields = missing_fields or []


def service_url(service: str) -> str:
    return {"primary": config.ORDER_INTERPRETER_PRIMARY_URL,
            "backup": config.ORDER_INTERPRETER_BACKUP_URL}.get(service, "")


def interpret(task: str, messages: list[dict], context: dict, *, service: str = "primary",
              trace_id: str | None = None, session_id: str | None = None,
              client: httpx.Client | None = None, timeout: float | None = None) -> BaseModel:
    """한 번 부르고, 계약으로 검증한 결과를 돌려준다. 실패는 InterpreterError 하나다."""
    contract = TASK_CONTRACTS[task]
    url = service_url(service)
    if not url:
        raise InterpreterError("UNKNOWN", f"ORDER_INTERPRETER_{service.upper()}_URL 미설정",
                               service=service)
    (messages, context), found = mask_payload([messages, context])
    pii_masked = sorted(set(found))     # 종류만 남긴다 — 원문은 로그에도 쓰지 않는다
    payload = {"task": task, "messages": messages, "context": context,
               "schema": contract.model_json_schema(), "trace_id": trace_id}

    started = time.perf_counter()
    try:
        if client is not None:
            res = client.post(f"{url}/interpret", json=payload,
                              timeout=timeout or config.ORDER_INTERPRETER_TIMEOUT_SEC)
        else:
            res = httpx.post(f"{url}/interpret", json=payload,
                             timeout=timeout or config.ORDER_INTERPRETER_TIMEOUT_SEC)
    except httpx.TimeoutException as exc:
        raise InterpreterError("DEP_TIMEOUT", f"{service} {config.ORDER_INTERPRETER_TIMEOUT_SEC}초 초과",
                               service=service) from exc
    except httpx.HTTPError as exc:
        raise InterpreterError("DEP_HTTP_5XX", f"{service} 연결 실패: {exc}",
                               service=service) from exc
    duration_ms = int((time.perf_counter() - started) * 1000)

    if res.status_code == 504:
        raise InterpreterError("DEP_TIMEOUT", f"{service} 제공자 제한시간 초과", service=service)
    if res.status_code >= 500:
        raise InterpreterError("DEP_HTTP_5XX", f"{service} HTTP {res.status_code}", service=service)
    if res.status_code >= 400:
        raise InterpreterError("UNKNOWN", f"{service} HTTP {res.status_code}: {res.text[:200]}",
                               service=service)

    body = res.json()
    try:
        parsed = contract.model_validate(body.get("result"))
    except ValidationError as exc:
        missing = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()
                          if e["type"] == "missing"})
        _save(task, body, service, duration_ms, trace_id, session_id, valid=False, missing=missing,
              pii_masked=pii_masked)
        raise InterpreterError("AGENT_OUTPUT_INVALID", f"{service} 계약 위반: {len(exc.errors())}건",
                               service=service, missing_fields=missing) from exc

    _save(task, body, service, duration_ms, trace_id, session_id, valid=True, missing=[],
          pii_masked=pii_masked)
    return parsed


def _circuit_record(service: str, *, success: bool) -> None:
    """Persist real interpreter outcomes; database absence keeps local/dev calls usable."""
    if not config.db_configured():
        return
    from app import db

    key = f"order_interpreter_{service}"
    if success:
        sql = ("INSERT INTO healing.circuit_state(dependency_key,state,consecutive_failures,cooldown_until,updated_at) "
               "VALUES (%s,'up',0,NULL,NOW()) ON CONFLICT(dependency_key) DO UPDATE SET "
               "state='up',consecutive_failures=0,cooldown_until=NULL,updated_at=NOW()")
        params = (key,)
    else:
        sql = ("INSERT INTO healing.circuit_state(dependency_key,state,consecutive_failures,cooldown_until,updated_at) "
               "VALUES (%s,'degraded',1,NULL,NOW()) ON CONFLICT(dependency_key) DO UPDATE SET "
               "consecutive_failures=healing.circuit_state.consecutive_failures+1, "
               "state=CASE WHEN healing.circuit_state.consecutive_failures+1>=3 THEN 'circuit_open' ELSE 'degraded' END, "
               "cooldown_until=CASE WHEN healing.circuit_state.consecutive_failures+1>=3 "
               "THEN NOW()+INTERVAL '60 seconds' ELSE healing.circuit_state.cooldown_until END,updated_at=NOW()")
        params = (key,)
    try:
        db.execute(sql, params, role="healing")
    except Exception:  # Circuit telemetry must never break an order request.
        logging_setup.log("circuit_update_failed", level="warning", dependency=key)


def interpret_resilient(task: str, messages: list[dict], context: dict, *, service: str = "primary",
                        trace_id: str | None = None, session_id: str | None = None,
                        client: httpx.Client | None = None, attempt_observer=None) -> BaseModel:
    """One primary call and at most one opposite-interpreter switch within a 15s total budget."""
    started = time.monotonic()
    preferred = service
    alternate = "backup" if preferred == "primary" else "primary"
    attempt_no = 0

    def observed_call(target_service: str, timeout: float) -> BaseModel:
        nonlocal attempt_no
        attempt_no += 1
        call_started = time.monotonic()
        # 지연 임포트 — _save 가 history 를 그렇게 가져오는 것과 같은 이유다(순환 임포트 회피)
        from app.services import metrics
        try:
            result = interpret(task, messages, context, service=target_service, trace_id=trace_id,
                               session_id=session_id, client=client, timeout=timeout)
        except InterpreterError as exc:
            # 5.6.1 감지 메트릭 — 업무 층은 관측한 사실만 적는다(PRD 5.3 복구 경계).
            # 사건을 만들지 않는다 — Monitor 가 이 행들을 세어 판정한다
            metrics.observe_interpreter(exc.fault_code, target_service,
                                        int((time.monotonic() - call_started) * 1000),
                                        trace_id=trace_id)
            if attempt_observer:
                try:
                    attempt_observer({"attempt_no": attempt_no, "service": target_service,
                                      "outcome": "실패", "fault_code": exc.fault_code,
                                      "duration_ms": int((time.monotonic() - call_started) * 1000)})
                except Exception:
                    pass
            raise
        metrics.observe_interpreter(None, target_service,
                                    int((time.monotonic() - call_started) * 1000), trace_id=trace_id)
        if attempt_observer:
            try:
                attempt_observer({"attempt_no": attempt_no, "service": target_service,
                                  "outcome": "성공", "duration_ms": int((time.monotonic() - call_started) * 1000)})
            except Exception:
                pass
        return result
    if _circuit_blocked(preferred):
        if not service_url(alternate) or _circuit_blocked(alternate):
            raise InterpreterError("UNKNOWN", "both interpreter circuits are unavailable", service=preferred)
        preferred, alternate = alternate, preferred
    try:
        result = observed_call(preferred, min(5.0, 15.0))
        _circuit_record(preferred, success=True)
        return result
    except InterpreterError as exc:
        if exc.fault_code not in ("DEP_HTTP_5XX", "DEP_TIMEOUT"):
            raise
        _circuit_record(preferred, success=False)
        alternate_url = service_url(alternate)
        if not alternate_url or _circuit_blocked(alternate):
            raise
        remaining = 15.0 - (time.monotonic() - started)
        if remaining <= 0.05:
            raise
        try:
            result = observed_call(alternate, min(5.0, remaining))
        except InterpreterError:
            _circuit_record(alternate, success=False)
            raise
        _circuit_record(alternate, success=True)
        return result


def circuit_usable(state: str | None, cooldown_until=None, *, now=None) -> bool:
    """회로 한 행을 보고 **지금 이 서비스를 쓸 수 있는가**.

    두 곳이 회로를 각자 해석하면 한쪽만 고쳐도 다른 쪽에서 조용히 깨진다 —
    실측 2026-11-06: `_circuit_blocked` 와 `_pick_service` 가 서로 다른 규칙을 써서
    1차가 영구 차단됐다. 규칙은 여기 하나다.

      up · degraded      쓴다
      down · probing     쓰지 않는다 (probing 은 healing 이 시험 중이다 — 끼어들지 않는다)
      circuit_open       냉각이 지났으면 **시험 요청 1회**를 보낸다 (PRD 5.6 F-4 "60초 후 시험 요청 1회").
                         성공하면 _circuit_record 가 state='up' 으로 닫고, 실패하면 냉각이 다시 걸린다
    """
    if state in ("down", "probing"):
        return False
    if state != "circuit_open":
        return True
    if cooldown_until is None:
        return False                 # 열어 둘 근거가 없다 — _circuit_record 는 열 때 항상 넣는다
    moment = now or (dt.datetime.now(cooldown_until.tzinfo) if cooldown_until.tzinfo
                     else dt.datetime.now())
    return cooldown_until <= moment


def _circuit_blocked(service: str) -> bool:
    if not config.db_configured():
        return False
    try:
        from app import db
        row = db.fetch_one("SELECT state,cooldown_until FROM healing.circuit_state "
                           "WHERE dependency_key=%s", (f"order_interpreter_{service}",), role="healing")
        if not row:
            return False
        return not circuit_usable(row["state"], row.get("cooldown_until"))
    except Exception:  # Database health is handled by its own fallback path.
        return False


def _save(task, body, service, duration_ms, trace_id, session_id, *, valid, missing,
          pii_masked=None) -> None:
    """llm_history — 실패해도 삼킨다(비치명). 규칙 원천(모델 'rules')은 LLM 호출이 아니므로 남기지 않는다."""
    if body.get("model") in (None, "rules"):
        if not valid:
            logging_setup.log("schema_invalid", level="warning", trace_id=trace_id,
                              agent=TASK_AGENT[task], task=task, service=service,
                              missing_fields=missing)
        return
    usage = body.get("usage") or {}
    if config.db_configured():
        from app.services import history

        history.save_llm_call(trace_id=trace_id or "", agent=TASK_AGENT[task], task=task,
                              service=service, model=body.get("model"), duration_ms=duration_ms,
                              schema_valid=valid, session_id=session_id,
                              input_tokens=usage.get("input_tokens"),
                              output_tokens=usage.get("output_tokens"),
                              missing_fields=missing,
                              prompt_version=body.get("prompt_version") or "unknown",
                              pii_masked=pii_masked)
    elif not valid:
        logging_setup.log("schema_invalid", level="warning", trace_id=trace_id, agent=TASK_AGENT[task],
                          task=task, service=service, missing_fields=missing)
