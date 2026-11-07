"""운영 대시보드 — API-040 ~ 049 (SCR-006 · O-0 ~ O-6).

로그인 외 전부 ops 토큰이 필요하다. 담당자 세션 토큰과 섞이지 않는다 (D-02 1.1).
"""

import csv
import io

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app import envelope, logging_setup
from app.api import deps
from app.api.review import _body
from app.services import incidents, ops_auth, workflows

router = APIRouter(prefix="/api/ops")


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 200))      # D-02 1절 — 기본 50 · 최대 200


@router.post("/login")
async def login(request: Request):
    result = ops_auth.login((await _body(request)).get("password") or "")
    # 결과만 남긴다 — 비밀번호 · 토큰 원문은 로그에 0건 (D-03 1.5 · NFR-003 · R-17)
    logging_setup.log("ops_login", level="info" if result else "warning",
                      trace_id=envelope.current_trace.get(),
                      result="success" if result else "fail")
    if not result:
        raise envelope.ops_auth_failed()
    return envelope.ok(result)


@router.post("/logout")
def logout(token: str = Depends(deps.ops_token)):
    ops_auth.logout(token)
    return envelope.ok({"logged_out": True})


@router.get("/incidents")
def incident_list(limit: int = 50, _: str = Depends(deps.ops_token)):
    return envelope.ok(incidents.incident_list(_limit(limit)))


@router.get("/incidents/{incident_id}")
def incident_detail(incident_id: str, _: str = Depends(deps.ops_token)):
    detail = incidents.incident_detail(incident_id)
    if detail is None:
        raise envelope.not_found("incident")
    return envelope.ok(detail)


@router.post("/incidents/{incident_id}/decision")
async def decide(incident_id: str, request: Request, _: str = Depends(deps.ops_token)):
    body = await _body(request)
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise envelope.bad_request(detail="decision must be approve|reject", field="decision")
    result = incidents.decide(incident_id, decision, note=body.get("note"))
    if not result["ok"]:
        if result.get("already"):
            # 첫 결정만 반영된다 — 기존 결정을 함께 돌려준다 (D-02 1.2)
            raise envelope.conflict("ALREADY_DECIDED", "이미 처리된 요청이에요",
                                    extra={"decision": result["already"]})
        raise envelope.not_found("incident")
    return envelope.ok(result)


@router.get("/status")
def status(_: str = Depends(deps.ops_token)):
    return envelope.ok(incidents.status_view())


@router.get("/metrics")
def metrics(_: str = Depends(deps.ops_token)):
    return envelope.ok(incidents.metrics_view())


@router.get("/orders")
def order_list(limit: int = 50, _: str = Depends(deps.ops_token)):
    return envelope.ok(incidents.order_list(_limit(limit)))


@router.get("/orders/{order_id}")
def order_detail(order_id: str, _: str = Depends(deps.ops_token)):
    return envelope.ok(incidents.order_detail(order_id))


@router.post("/faults")
async def fault(request: Request, _: str = Depends(deps.ops_token)):
    body = await _body(request)
    # 화면은 `code` 를 보낸다. D-02 표기 `fault` 도 받는다
    code = body.get("code") or body.get("fault")
    action = body.get("action") or "inject"
    if action not in ("inject", "clear"):
        raise envelope.bad_request(detail="action must be inject|clear", field="action")
    result = incidents.inject(code, action)
    if not result["ok"]:
        if code in incidents.FAULT_TARGETS or action == "clear":
            # 실습 환경 가드 — 404 로 숨기지 않는다 (D-02 API-049)
            raise envelope.ApiFailure(403, "LAB_ONLY", result["user_message"],
                                      detail="ENVIRONMENT is not lab")
        raise envelope.bad_request(result["user_message"], field="code")
    return envelope.ok(result)


@router.get("/trials")
def trials(_: str = Depends(deps.ops_token)):
    """D 장애 시험 결과 격자 — 유형별 최신 5회차(운영 대시보드 블록 D)."""
    return envelope.ok(workflows.trials_grid())


@router.get("/workflows/templates")
def workflow_templates(_: str = Depends(deps.ops_token)):
    return envelope.ok({"templates": workflows.templates()})


@router.post("/workflows/preflight")
async def workflow_preflight(request: Request, _: str = Depends(deps.ops_token)):
    body = await _body(request)
    # 스레드풀로 넘긴다 — preflight 는 Gateway · 해석기 · A/B backend 를 **동기** httpx 로 부른다.
    # `async def` 안에서 그대로 돌리면 이벤트 루프가 막혀 백엔드가 그 동안 모든 요청을 못 받는다
    # (실측 2026-11-06: 환경 확인 1회에 7초 응답 불가 · 자기 자신을 부르는 A backend 점검이 ReadTimeout).
    check = await run_in_threadpool(workflows.preflight, mode=body.get("mode", "quick"),
                                    fault_type=body.get("fault_type", "F-1"),
                                    target=body.get("target"))
    return envelope.ok(check)


@router.post("/workflows")
async def workflow_create(request: Request,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                          _: str = Depends(deps.ops_token)):
    body = await _body(request)
    try:
        # create_run 도 안에서 preflight 를 부르므로 같은 이유로 스레드풀로 넘긴다
        result = await run_in_threadpool(
            workflows.create_run,
            mode=body.get("mode", "quick"), fault_type=body.get("fault_type", "F-1"),
            target=body.get("target"), repetitions=int(body.get("repetitions", 1)),
            injection_seconds=int(body.get("injection_seconds", 1)),
            idempotency_key=(idempotency_key or "")[:120] or None)
    except PermissionError as exc:
        raise envelope.ApiFailure(403, "LAB_ONLY", str(exc)) from exc
    except ValueError as exc:
        raise envelope.bad_request(detail=str(exc)) from exc
    except RuntimeError as exc:
        raise envelope.conflict("WORKFLOW_ACTIVE", str(exc)) from exc
    if not result.get("ready", True):
        return envelope.ok(result)
    return envelope.ok(result)


@router.get("/workflows")
def workflow_list(limit: int = 20, _: str = Depends(deps.ops_token)):
    return envelope.ok({"runs": workflows.list_runs(limit)})


@router.get("/workflows/{run_id}")
def workflow_get(run_id: str, _: str = Depends(deps.ops_token)):
    result = workflows.get_run(run_id)
    if result is None:
        raise envelope.not_found("workflow")
    return envelope.ok(result)


@router.post("/workflows/{run_id}/stop")
def workflow_stop(run_id: str, _: str = Depends(deps.ops_token)):
    result = workflows.request_stop(run_id)
    if result is None:
        raise envelope.conflict("WORKFLOW_NOT_ACTIVE", "실행 중인 workflow가 아닙니다")
    return envelope.ok(result)


@router.get("/workflows/{run_id}/export")
def workflow_export(run_id: str, format: str = "json", _: str = Depends(deps.ops_token)):
    result = workflows.export_run(run_id)
    if result is None:
        raise envelope.not_found("workflow")
    if format == "json":
        from fastapi.encoders import jsonable_encoder
        from json import dumps
        content = dumps(jsonable_encoder(result), ensure_ascii=False, indent=2)
        return Response(content, media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})
    if format != "csv":
        raise envelope.bad_request(detail="format은 json 또는 csv여야 합니다")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["run_id", "iteration", "fault_type", "target", "status", "detail",
                     "synthetic_requests", "mttd_ms", "mttr_ms", "incident_id", "injection_id"])
    for item in result["iterations"]:
        detail = item.get("result") or {}
        writer.writerow([run_id, item.get("iteration_no"), item.get("fault_type"), item.get("target"),
                         item.get("status"), detail.get("detail", ""), item.get("synthetic_requests", 0),
                         item.get("mttd_ms", ""), item.get("mttr_ms", ""), item.get("incident_id", ""),
                         item.get("injection_id", "")])
    return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'})
