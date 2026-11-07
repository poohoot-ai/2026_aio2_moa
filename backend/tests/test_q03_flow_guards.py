# Q-03 — 답장 · 공유 · 권한 · 지출 한도 · DB 폴백 · 배포 구성
#          (Q-01 TC-060 · TC-061 · TC-063 · TC-077 · TC-079 · TC-113 · TC-124 · TC-133)
import datetime
import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config, db, envelope, graph
from app.contracts import OrderEvents
from app.graph import nodes
from app.main import app
from app.services import changes, completion, incidents, menu as menu_service
from app.services import order_sheet, share, suggestions
from tests import q03_fakes as F

client = TestClient(app, raise_server_exceptions=False)
TOKEN = {"X-Session-Token": "tok_q03"}
REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def error_of(res):
    return res.json()["error"]


@pytest.fixture
def owned(monkeypatch):
    """토큰이 F.SESSION 을 가리키는 상태 — 기존 계약 테스트와 같은 방식(sessions.load)."""
    from app.services import sessions

    monkeypatch.setattr(sessions, "load",
                        lambda sid, tok: dict(F.SESSION) if sid == "ORD-0001" else None)


def fake_events(monkeypatch, events):
    """해석 서비스를 고정한다. events = [(message_id, event_type, items)]."""
    from app import interpreter_client

    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(nodes, "_pick_service", lambda: "primary")
    base = {"evidence_message_ids": [], "target_message_id": None, "reason": "r", "person": None,
            "person_unclear": False, "target_unclear": False, "is_reference": False}
    monkeypatch.setattr(interpreter_client, "interpret", lambda task, messages, context, **k:
                        OrderEvents.model_validate({"events": [
                            {**base, "message_id": mid, "evidence_message_ids": [mid],
                             "event_type": etype, "items": items}
                            for mid, etype, items in events]}))


# --- TC-060 답장 분석 — 제안만 만들고 주문은 그대로 ----------------------------------------
def test_reply_analysis_only_makes_suggestions(monkeypatch):
    store = F.Store()
    F.install(monkeypatch, store)
    persisted = []
    monkeypatch.setattr(suggestions, "persist",
                        lambda session, messages, event_types:
                        persisted.append(event_types) or {"suggestions": [{"suggestion_id": "sg_1"}]})
    fake_events(monkeypatch, [("reply1", "update", [{"menu_text": "", "temperature_text": None,
                                                     "size_text": None, "quantity": 1,
                                                     "option_texts": ["얼음 많이"],
                                                     "custom_request": None}]),
                              ("reply2", "confirm", [])])

    out = graph.run("reply", dict(F.SESSION), trace_id="tr_Q03",
                    messages=[{"seq": 1, "sender": "이지연", "text": "얼음 많이로 바꿔주세요"},
                              {"seq": 2, "sender": "박서연", "text": "네 맞아요"}])

    assert [e["route"] for e in out["targets"]["ordered"]] == ["update", "confirm"]
    assert persisted == [{0: "update", 1: "confirm"}]      # 경로는 그래프가 정하고 저장은 제안만
    assert store.items == []                               # 주문 항목은 건드리지 않는다
    assert not [s for s in store.sql if "UPDATE app.order_item" in s]


def test_confirm_reply_makes_no_suggestion(monkeypatch):
    store = F.Store()
    F.install(monkeypatch, store)
    seen = {}
    monkeypatch.setattr(suggestions, "persist",
                        lambda session, messages, event_types: seen.setdefault("types", event_types)
                        or {"suggestions": []})
    fake_events(monkeypatch, [("reply1", "confirm", [])])

    out = graph.run("reply", dict(F.SESSION), trace_id="tr_Q03",
                    messages=[{"seq": 1, "sender": "박서연", "text": "네 그대로 좋아요"}])

    assert seen["types"] == {0: "confirm"}
    assert [e["route"] for e in out["targets"]["ordered"]] == ["confirm"]


# --- TC-061 반영 대기 제안이 남으면 공유 차단 → [반영] → 공유 -------------------------------
def test_pending_suggestion_blocks_share_until_it_is_decided(owned, monkeypatch):
    gate = {"can_share": False, "blocked_reason_text": "반영할지 정하지 않은 제안이 있어요"}
    monkeypatch.setattr(order_sheet, "can_share", lambda session_id: gate)

    blocked = client.get("/api/sessions/ORD-0001/share", headers=TOKEN)
    assert blocked.status_code == 409
    assert error_of(blocked)["code"] == "SHARE_BLOCKED"
    assert "제안" in error_of(blocked)["user_message"]

    # ② [반영] — 제안을 반영하면 대기가 0 이 된다
    monkeypatch.setattr(suggestions, "decide",
                        lambda session, suggestion_id, decision, *, expected_version:
                        {"applied": decision == "apply", "version": 2})
    monkeypatch.setattr(suggestions, "pending_count", lambda session_id: 0)
    monkeypatch.setattr(db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)
    decided = client.post("/api/sessions/ORD-0001/suggestions/sg_1/decision",
                          json={"decision": "apply"}, headers=TOKEN)
    assert decided.status_code == 200
    assert decided.json()["data"]["pending_count"] == 0

    # ③ 공유 — 이제 열린다
    gate.update(can_share=True, blocked_reason_text=None)
    monkeypatch.setattr(order_sheet, "view", lambda session: {"totals": {"estimated_total": 9000}})
    monkeypatch.setattr(share, "view", lambda sheet, fmt="text", **_: {"content": "주문서", "format": fmt})
    assert client.get("/api/sessions/ORD-0001/share", headers=TOKEN).status_code == 200


def test_decision_takes_only_apply_or_ignore(owned):
    res = client.post("/api/sessions/ORD-0001/suggestions/sg_1/decision",
                      json={"decision": "maybe"}, headers=TOKEN)
    assert error_of(res)["code"] == "BAD_REQUEST"


# --- TC-063 공유 완료 → completed · share_history 1행 ---------------------------------------
def test_share_complete_records_history_and_completes_the_session(monkeypatch):  # noqa: C901
    store = F.Store()
    F.install(monkeypatch, store)
    monkeypatch.setattr(order_sheet, "can_share", lambda session_id: {"can_share": True})
    monkeypatch.setattr(order_sheet, "view", lambda session: {"totals": {"estimated_total": 9000}})
    monkeypatch.setattr(share, "view", lambda sheet, fmt="text", **_: {"content": "주문서 본문"})

    class Cur:
        rowcount = 1

        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            store.sql.append(" ".join(sql.split()))

        def fetchone(self):
            return {"version": 3}

        def fetchall(self):
            return []

    import contextlib

    @contextlib.contextmanager
    def one_cursor(role="app"):
        yield Cur()

    monkeypatch.setattr(db, "transaction", one_cursor)
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None, **k: {
        "share_count": 1, "completed_at": datetime.datetime(2026, 11, 3, 23, 40)})

    done = completion.complete(dict(F.SESSION), "copy_text")

    assert done["status"] == "completed"
    assert done["share_count"] == 1

    sql = " ".join(store.sql)
    assert "INSERT INTO app.share_history" in sql
    assert "status = 'completed'" in sql
    assert sql.count("INSERT INTO app.share_history") == 1       # 1행만


def test_share_complete_is_refused_when_the_gate_is_closed(monkeypatch):
    monkeypatch.setattr(order_sheet, "can_share",
                        lambda session_id: {"can_share": False, "blocked_reason_text": "확인 필요가 남았어요"})

    with pytest.raises(completion.NotShareable):
        completion.complete(dict(F.SESSION), "copy_text")


# --- TC-077 장애 주입은 lab 에서만 ----------------------------------------------------------
def test_fault_injection_is_refused_outside_lab(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "prod")
    writes = []
    monkeypatch.setattr(db, "transaction", lambda *a, **k: writes.append(1))

    result = incidents.inject("F-1", "inject")

    assert result["ok"] is False
    assert "실습" in result["user_message"]
    assert writes == []                                   # 주입 행 0건


def test_fault_injection_api_answers_403_lab_only(monkeypatch):
    from app.services import ops_auth

    monkeypatch.setattr(config, "ENVIRONMENT", "prod")
    monkeypatch.setattr(ops_auth, "verify", lambda token: True)

    res = client.post("/api/ops/faults", json={"code": "F-1", "action": "inject"},
                      headers={"Authorization": "Bearer ops_test"})

    assert res.status_code == 403
    assert error_of(res)["code"] == "LAB_ONLY"             # 404 로 숨기지 않는다


# --- TC-079 디버그 API 는 플래그가 켜져 있을 때만 ------------------------------------------
def test_debug_api_is_absent_when_the_flag_is_off():
    assert config.ENABLE_DEBUG_API is False                # 테스트 기준 환경
    assert client.get("/api/debug/sessions/ORD-0001/trace", headers=TOKEN).status_code == 404
    assert client.post("/api/debug/boom", headers=TOKEN).status_code == 404


def test_debug_routes_exist_only_in_the_debug_router():
    """플래그를 켜면 붙는 경로가 실제로 그 라우터에 있다 — 404 가 '구현 안 됨'이 아님을 보인다."""
    from app.api import debug

    paths = {r.path for r in debug.router.routes}
    assert paths == {"/api/debug/sessions/{session_id}/trace", "/api/debug/boom"}


# --- TC-113 지출 한도 도달 → 재시도 없이 규칙 대체 ------------------------------------------
def test_spend_limit_is_not_retried_and_the_batch_falls_back_to_rules(monkeypatch):
    from app import interpreter_client

    monkeypatch.setattr(config, "ORDER_INTERPRETER_PRIMARY_URL", "http://primary")
    monkeypatch.setattr(nodes, "_pick_service", lambda: "primary")
    calls = []

    def quota_exhausted(task, messages, context, **k):
        calls.append(1)
        raise interpreter_client.InterpreterError("limit_reached", "지출 한도", service="primary")

    monkeypatch.setattr(interpreter_client, "interpret", quota_exhausted)
    targets = F.chat(["[김민수] 아아 톨 한 잔이요."])
    out = nodes.intake_node({"session": dict(F.SESSION), "trace_id": "tr_Q03", "existing": [],
                             "batch": {"batch_no": 1, "prev": [], "targets": targets,
                                       "use_llm": True, "service": "primary"}})

    assert calls == [1]                                    # 재시도 0회
    assert out["events"][0]["event_type"] == "add"          # 규칙 해석으로 대체
    assert out["degraded"][0]["reason"] == "limit_reached"


def test_degraded_batch_is_recorded_for_the_operator(monkeypatch):
    """O-6 처리 기록 — 담당자 문구에 장애 코드 문자열은 넣지 않는다."""
    store = F.Store()
    F.install(monkeypatch, store)
    recorded = []
    monkeypatch.setattr(changes, "record", lambda cur, session_id, **k: recorded.append(k))

    nodes.output_guard_node({"session": dict(F.SESSION), "source": "snapshot", "rows": [],
                             "targets": {"ordered": []}, "result": {}, "trace_id": "tr_Q03",
                             "degraded": [{"reason": "DEP_TIMEOUT", "messages": 4}]})

    assert len(recorded) == 1
    assert recorded[0]["method"] == "auto_recovery"
    assert "DEP_TIMEOUT" not in recorded[0]["before_text"] + recorded[0]["after_text"]


# --- TC-124 DB 끊김 — 메뉴는 캐시로 답한다 (F-6) -------------------------------------------
def test_menu_catalog_falls_back_to_the_cache_when_the_db_is_down(monkeypatch):
    monkeypatch.setitem(menu_service._CACHE, (F.BRAND, F.MENU_VERSION), F.catalog())
    monkeypatch.setattr(menu_service, "_CACHE_TTL_SEC", 0)       # 캐시를 만료시켜 다시 읽게 한다
    monkeypatch.setattr(menu_service, "_load",
                        lambda brand_id, menu_version: (_ for _ in ()).throw(db.DbUnavailable("down")))

    cat = menu_service.catalog(F.BRAND, F.MENU_VERSION)

    assert cat["stale"] is True
    assert cat["by_id"]["sb_004"]["name"] == "아이스 아메리카노"      # 메뉴 조회는 계속 된다


def test_without_a_cache_the_db_failure_surfaces(monkeypatch):
    """캐시도 없으면 숨기지 않는다 — 호출자가 DB_UNAVAILABLE 로 답한다."""
    monkeypatch.setattr(menu_service, "_CACHE", {})
    monkeypatch.setattr(menu_service, "_load",
                        lambda brand_id, menu_version: (_ for _ in ()).throw(db.DbUnavailable("down")))

    with pytest.raises(db.DbUnavailable):
        menu_service.catalog(F.BRAND, F.MENU_VERSION)


# --- TC-133 배포 compose — 외부 포트 1개 · restart 정책 ------------------------------------
# PRD 5.4: 공용 스택(docker-compose.core.yml) + 앱 스택 A/B(docker-compose.app.yml 을 -p moa-a · moa-b 로 두 번)
def compose(name):
    return yaml.safe_load((REPO / name).read_text(encoding="utf-8"))


CORE = "docker-compose.core.yml"
APP = "docker-compose.app.yml"


def test_deploy_compose_opens_only_the_gateway_port():
    core, app = compose(CORE)["services"], compose(APP)["services"]

    published = {name: svc.get("ports") for name, svc in {**core, **app}.items() if svc.get("ports")}
    assert published == {"gateway": ["8501:8501"]}           # 밖으로 여는 포트는 게이트웨이 하나다
    assert app["backend"].get("expose") == ["8000"]
    assert app["frontend"].get("expose") == ["8501"]


def test_work_containers_do_not_restart_themselves():
    """R-14 — Docker 가 먼저 되살리면 F-3 탐지 · 복구를 잴 수 없다."""
    core, app = compose(CORE)["services"], compose(APP)["services"]

    assert app["backend"]["restart"] == "no"
    assert core["interpreter-primary"]["restart"] == "no"
    assert core["interpreter-backup"]["restart"] == "unless-stopped"
    assert app["frontend"]["restart"] == "unless-stopped"


def test_interpreter_source_defaults_to_llm():
    services = compose(CORE)["services"]

    for name in ("interpreter-primary", "interpreter-backup"):
        assert services[name]["environment"]["INTERPRETER_SOURCE"] == "${INTERPRETER_SOURCE:-llm}"


def test_db_lives_only_in_the_core_stack():
    """앱 파일에 db 를 두면 프로젝트(moa-a · moa-b)마다 DB 가 따로 생겨 전환 뒤 세션 복원이 깨진다."""
    assert "db" in compose(CORE)["services"]
    assert "db" not in compose(APP)["services"]
    assert compose(CORE)["volumes"]["pgdata"]["name"] == "moa_pgdata"   # 옛 단일 스택 볼륨을 이어 쓴다


def test_app_stacks_stay_apart():
    """frontend 는 자기 스택 네트워크에만 — 같은 스택의 backend 만 보인다."""
    app = compose(APP)

    assert app["services"]["frontend"]["networks"] == ["default"]
    assert app["networks"]["default"]["name"] == "${COMPOSE_PROJECT_NAME}_default"
    assert "container_name" not in app["services"]["backend"]   # 이름은 compose 가 moa-<a|b>-backend-1 로 정한다


def test_docker_control_goes_only_through_the_socket_proxy():
    """PRD 5.2 — healing 은 Docker 소켓에 직접 붙지 않고, 소켓 프록시는 healing 만 닿는 내부 네트워크에 둔다."""
    core = compose(CORE)
    services = core["services"]

    mounts = {name: [v for v in svc.get("volumes", []) if "docker.sock" in v] for name, svc in services.items()}
    assert {name for name, v in mounts.items() if v} == {"socket-proxy"}
    assert services["socket-proxy"]["networks"] == ["ctl"]
    assert core["networks"]["ctl"]["internal"] is True
    assert "ctl" in services["healing"]["networks"]
    assert services["healing"]["environment"]["DOCKER_HOST"] == "tcp://socket-proxy:2375"


def test_every_container_has_a_memory_limit():
    """PRD 9.3 — 한 컨테이너가 메모리를 다 써 같은 호스트의 다른 스택까지 죽이지 않게 한다."""
    services = {**compose(CORE)["services"], **compose(APP)["services"]}

    assert {name for name, svc in services.items() if not svc.get("mem_limit")} == set()


def test_every_container_caps_its_docker_log():
    """docker logs(json-file)에 크기 제한이 없으면 요청마다 쌓여 서버 디스크를 계속 쓴다."""
    services = {**compose(CORE)["services"], **compose(APP)["services"]}

    for name, svc in services.items():
        opts = (svc.get("logging") or {}).get("options") or {}
        assert opts.get("max-size") and opts.get("max-file"), name


def test_logs_survive_container_recreation():
    """PRD 9.3 로그 14일 — 배포가 컨테이너를 다시 만들어도 로그가 남게 호스트 폴더에 마운트한다."""
    core, app = compose(CORE)["services"], compose(APP)["services"]

    for svc in (app["backend"], core["interpreter-primary"], core["interpreter-backup"], core["healing"]):
        assert any(v.startswith("./logs/") and v.split(":")[1] == "/app/logs" for v in svc.get("volumes", []))
    assert "${COMPOSE_PROJECT_NAME}" in next(v for v in app["backend"]["volumes"] if "/app/logs" in v)  # A · B 따로


def test_log_files_rotate_daily_and_keep_14_days(tmp_path):
    from app import logging_setup

    handler = logging_setup._daily_file(tmp_path / "service.log")
    assert handler.when == "MIDNIGHT" and handler.backupCount == 14 and handler.utc
    handler.close()


def test_dev_overrides_open_ports_only_on_localhost():
    core_dev = compose("docker-compose.core.dev.yml")["services"]
    app_dev = compose("docker-compose.app.dev.yml")["services"]

    # DB는 호스트 개발 도구용으로 127.0.0.1 에만 연다. 5434는 이미 점유된 5433과 충돌하지 않는다.
    assert core_dev["db"]["ports"] == ["127.0.0.1:5434:5432"]
    for svc in [*core_dev.values(), *app_dev.values()]:
        assert all(p.startswith("127.0.0.1:") for p in svc["ports"])
