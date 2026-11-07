# SCR-006 운영 대시보드 고도화 — 정본 시안 `Moa_운영대시보드.svg` 24 · 25-1 ~ 25-3 · 26
#
# 앞부분은 Streamlit 없이 도는 순수 함수(moa/ops_view.py) 테스트, 뒷부분은 AppTest 로 화면을 실제로 그린다.
# 숫자는 전부 시안의 값이다 — 눈으로 보지 않아도 어긋나면 걸리게.
import datetime
import os
import pathlib
import re

import pytest

os.environ.setdefault("MOA_UI_PREVIEW", "1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from moa import copy, mock, ops_view as ov, state  # noqa: E402

OPS = str(pathlib.Path(__file__).resolve().parent.parent / "screens" / "ops.py")


# --- D 장애 시험 결과 ------------------------------------------------------------
def test_trial_totals_match_design_24():
    s = ov.trial_summary(mock.ops_scene("approval")["trials"])
    assert (s["ok"], s["valid"], s["pct"]) == (18, 22, 82)
    assert (s["done"], s["total"]) == (23, 25)
    assert [(r["fault"], r["ok"], r["valid"]) for r in s["rows"]] == [
        ("F-1", 3, 4), ("F-2", 5, 5), ("F-3", 3, 5), ("F-5", 4, 4), ("F-6", 3, 4)]
    assert (s["f4"]["ok"], s["f4"]["valid"]) == (2, 2)   # F-4 는 분모 밖 · 별도 줄


def test_trial_totals_after_f1_recovery_25_3():
    s = ov.trial_summary(mock.ops_scene("recovered")["trials"])
    assert (s["ok"], s["valid"], s["pct"], s["done"]) == (19, 23, 83, 24)


def test_running_cell_counts_as_done_not_as_valid():
    s = ov.trial_summary(mock.ops_scene("injected")["trials"])
    assert s["done"] == 24 and s["valid"] == 22


def test_only_cells_over_their_own_target_are_bold():
    html = ov.trials_html(ov.trial_summary(mock.OPS_TRIALS))
    assert "<b>72</b>" in html          # F-5 72초 > 60초
    assert "<b>81</b>" not in html      # F-3 은 목표 90초 — 81초는 통과


def test_grid_shows_strategy_and_detection_metric():
    """평가 질문(PRD 1.2 P3-2) 두 가지 — 복구 전략 4종 · 유형별 감지 메트릭이 화면에 있는가."""
    html = ov.trials_html(ov.trial_summary(mock.OPS_TRIALS))
    assert copy.OD_STRATEGY in html
    for strategy in ("대체 전환", "재시작 · 재개", "재생성 → 대체모델", "재연결 → 캐시"):
        assert strategy in html
    for metric in ("FT-01 timeout_count", "FT-02 agent_error_count",
                   "FT-03 db_connection_error", "FT-04 schema_error_rate"):
        assert metric in html


def test_grid_head_names_the_run():
    """어느 실행의 숫자인지 — 실행 ID가 없으면 25회 측정을 캡처로 증빙할 수 없다."""
    html = ov.trials_html(ov.trial_summary(mock.OPS_TRIALS))
    assert "run_2ec6c62a" in html and "예상 흐름 일치 23/23" in html


def test_grid_head_falls_back_without_run():
    trials = {k: v for k, v in mock.OPS_TRIALS.items() if k != "latest_run"}
    html = ov.trials_html(ov.trial_summary(trials))
    assert "run_" not in html and "23/25회 실시" in html


def test_trials_missing_shows_empty_sentence():
    assert ov.trial_summary(None) is None
    assert copy.OD_EMPTY in ov.trials_html(None)


# --- B 핵심 수치 · 0 과 미측정 구분 --------------------------------------------------
def test_kpis_match_design():
    cards = ov.kpis(mock.OPS_METRICS, ov.trial_summary(mock.OPS_TRIALS))
    got = {k["key"]: (k["value"], k["extra"], k["verdict"]) for k in cards}
    # 측정 원본은 ms다 — 초는 소수 1자리로 쓴다(8.4 와 8.0 이 같아 보이지 않게)
    assert got == {"mttd": ("8.2", None, "pass"), "mttr": ("41.3", "최대 81초", "pass"),
                   "auto": ("18/22", "(82%)", "pass"), "integrity": ("0", None, "pass")}


def test_unmeasured_is_not_zero():
    """측정 전에는 `0` 이 아니라 `미측정` 이다(고도화안 6절 원칙 7) — 판정도 통과로 칠하지 않는다."""
    cards = ov.kpis({"detail": {}}, None)
    assert all(k["value"] is None and k["verdict"] == "none" for k in cards)
    assert copy.OB_UNMEASURED in ov.kpis_html(cards)
    assert copy.OB_PASS not in ov.kpis_html(cards)


def test_kpi_fails_when_over_target():
    cards = ov.kpis({"detail": {"mttd_sec": 16, "mttr_sec": 61, "integrity_issue_count": 1}},
                    {"ok": 7, "valid": 10, "max_sec": 70})
    assert [k["verdict"] for k in cards] == ["fail", "fail", "fail", "fail"]


# --- A 상태 배너 — 우선순위 위에서 하나만 ------------------------------------------
@pytest.mark.parametrize("scene, tone, title, sub", [
    ("approval", "amber", "승인이 필요해요", "원인 미확인 장애(UNKNOWN) · 2분 14초 대기"),
    ("injected", "gray", "주입 후 3초", "탐지 대기 중 · 주문 해석 오류 응답(F-1) · 5회차"),
    ("recovering", "amber", "복구 중", "주문 해석 오류 응답(F-1) · 22초 경과"),
    ("recovered", "green", "정상 처리 중", "대체 해석기를 쓰고 있어요"),
    ("normal", "green", "정상 운영 중", None),
])
def test_banner_sentence_per_design_state(scene, tone, title, sub):
    s = mock.ops_scene(scene)
    b = ov.banner(s["status"], s["incidents"])
    assert (b["tone"], b["title"], b["sub"]) == (tone, title, sub)


def test_banner_alert_line_only_when_given():
    s = mock.ops_scene("approval")
    assert ov.banner(s["status"], s["incidents"])["alert"] == "최근 5분 주문 해석 실패율 12% (기준 10%)"
    n = mock.ops_scene("normal")
    assert ov.banner(n["status"], n["incidents"])["alert"] is None


def test_old_failed_incident_does_not_turn_banner_red():
    """34분 전에 끝난 실패 사건이 목록에 남아 있다고 지금 상태가 빨강이 되지 않는다(시안 26)."""
    s = mock.ops_scene("normal")
    assert any(r["state"] == "failed" for r in s["incidents"]["incidents"])
    assert ov.banner(s["status"], s["incidents"])["tone"] == "green"
    s["incidents"]["incidents"][-1] = {**s["incidents"]["incidents"][-1], "open": True}
    assert ov.banner(s["status"], s["incidents"])["title"] == copy.OA_FAILED


# --- C 지금 요청 경로 ----------------------------------------------------------------
def test_route_rule_fallback_note_when_no_interpreter():
    r = ov.route(mock.ops_scene("approval")["status"])
    interp = dict(r["groups"])["해석기"]
    assert [(x["name"], x["text"]) for x in interp] == [("1차", "차단 · 42초 후 시험"), ("대체", "차단 · 응답 없음")]
    assert r["note"][1] == copy.OC_NOTE_RULE


def test_route_probing_shows_streak():
    r = ov.route(mock.ops_scene("recovered")["status"])
    primary = dict(r["groups"])["해석기"][0]
    assert (primary["tone"], primary["text"]) == ("probing", "복귀 확인 2/3")
    assert r["note"][1] == copy.OC_NOTE_PROBING.format(need=3)


def test_route_without_replicas_is_not_invented():
    """G-1 — 서버가 복제본 상태를 주지 않으면 `상태 미수집` 이다. `사용 중` 으로 지어내지 않는다."""
    status = {k: v for k, v in mock.OPS_STATUS.items() if k != "replicas"}
    gateway = dict(ov.route(status)["groups"])["게이트웨이"]
    assert {x["text"] for x in gateway} == {copy.OC_UNKNOWN}


def test_route_shows_replica_switch_seconds():
    """SC-12 — 전환 초는 서버가 줄 때만 쓴다. 없으면 칸을 비운다."""
    gateway = dict(ov.route(mock.OPS_STATUS)["groups"])["게이트웨이"]
    assert [x.get("note") for x in gateway] == [None, copy.OC_SWITCH.format(sec=11)]
    bare = {**mock.OPS_STATUS,
            "replicas": [{"key": "app_a", "name": "앱 A", "state": "up", "active": True}]}
    assert dict(ov.route(bare)["groups"])["게이트웨이"][0].get("note") is None


def test_route_reads_current_server_shape():
    """지금 서버(services/incidents.status_view) 응답 모양 그대로도 그린다."""
    server = {"services": [
        {"name": "주문 해석 1차", "key": "order_interpreter_primary", "state": "blocked",
         "state_text": "차단 · 12초 후 시험", "active": False, "healthy_streak": 0},
        {"name": "주문 해석 대체", "key": "order_interpreter_backup", "state": "up",
         "state_text": "정상", "active": True, "healthy_streak": 0},
        {"name": "메뉴 저장소", "key": "menu_store", "state": "up", "state_text": "정상 · 캐시 폴백",
         "active": False, "healthy_streak": 0}],
        "active_interpreter": "order_interpreter_backup", "cache_fallback_active": True}
    groups = dict(ov.route(server)["groups"])
    assert [x["text"] for x in groups["해석기"]] == ["차단 · 12초 후 시험", "사용 중"]
    assert groups["메뉴 저장소"][0]["text"] == copy.OC_CACHE


# --- E 최근 사건 ---------------------------------------------------------------------
def test_incident_rows_match_design_24():
    rows = ov.incident_rows(mock.ops_scene("approval")["incidents"])
    assert [(r["name"], r["code"], r["ago"], r["elapsed"]) for r in rows] == [
        # UNKNOWN 은 F 코드가 아니다 — 원인 미확인 에스컬레이션(PRD SC-6)
        ("원인 미확인 장애", "UNKNOWN", "2분 전", "대기 중"),
        ("처리 프로세스 중단", "F-3", "12분 전", "27초"),
        ("DB 연결 끊김", "F-6", "34분 전", "64초")]


def test_incident_rows_from_server_shape_use_korean_names():
    """서버는 `fault` 없이 장애 코드 · 시각 글자만 준다 — 이름은 쉬운 말, 시간은 지난 시간으로."""
    now = datetime.datetime(2026, 11, 6, 14, 12, 30)
    rows = ov.incident_rows({"incidents": [
        {"incident_id": "INC-1", "fault_code": "DEP_TIMEOUT", "state": "recovered", "state_text": "복구 성공",
         "detected_at_text": "14:02:11", "resolved_at_text": "14:03:05"}]}, now)
    assert (rows[0]["name"], rows[0]["code"], rows[0]["ago"], rows[0]["elapsed"]) == (
        "해석 응답 지연", "F-2", "10분 전", "54초")


def test_duration_text():
    assert [ov.duration_text(v) for v in (8, 64, 99, 148, 120, None)] == [
        "8초", "64초", "99초", "2분 28초", "2분", "-"]


def test_timeline_matches_design_26():
    tl = ov.timeline(mock.OPS_INCIDENT_DETAIL["INC-0006"])
    assert (tl["det"], tl["start"], tl["end"], tl["scale"], tl["lines"]) == (8, 9, 27, 90, [15, 60])


def test_timeline_human_wait_segment_and_f3_target():
    tl = ov.timeline(mock.OPS_INCIDENT_DETAIL["INC-0007"])
    assert tl["human"] == (9, 143) and tl["lines"] == [15, 90] and tl["end"] == 148


def test_detail_cards_from_server_shape():
    """서버 상세(목록형 timeline · verification, 최상위 attempts)도 카드 4장으로 그린다 — 원시 JSON 0건."""
    server = {"incident_id": "INC-9", "fault_code": "WORKER_DOWN", "strategy": "restart_resume",
              "timeline": [{"key": "injected", "at_text": "13:39:50"}, {"key": "detected", "at_text": "13:39:58"},
                           {"key": "recovery_started", "at_text": "13:39:59"},
                           {"key": "resolved", "at_text": "13:40:17"}, {"key": "failed", "at_text": None}],
              "diagnosis": {"rule": "WORKER_DOWN", "reason": "체크포인트 멈춤"},
              "attempts": [{"attempt_no": 1, "action": "restart", "result": "fail"},
                           {"attempt_no": 2, "action": "restart", "result": "ok"}],
              "verification": [{"check_name": "health", "passed": True, "verdict": "passed"},
                               {"check_name": "resume", "passed": True, "verdict": "passed"}]}
    assert ov.detail_cards(server) == [("진단", "체크포인트 멈춤"), ("전략", "워커 재시작"),
                                       ("시도", "시도 2회 → 2회째 성공"), ("검증", "검증 2/2 통과")]
    assert (ov.timeline(server)["det"], ov.timeline(server)["end"]) == (8, 27)
    assert "{" not in ov.incident_detail_html(server)



def test_row_elapsed_equals_timeline_end():
    """목록의 걸린 시간과 펼친 타임라인의 정상화 · 실패 시각은 같은 숫자다(주입 0초 기준)."""
    for scene in ("normal", "recovered"):
        s = mock.ops_scene(scene)
        for r in s["incidents"]["incidents"]:
            tl = ov.timeline(s["details"][r["incident_id"]])
            assert tl["end"] == r["elapsed_sec"], r["incident_id"]

# --- 화면 렌더 (AppTest) ----------------------------------------------------------
def _render(case, **session):
    at = AppTest.from_file(OPS, default_timeout=60)
    at.session_state[state.OPS_AUTHED] = True
    at.session_state[state.OPS_TOKEN] = "preview-ops-token"
    at.session_state["_preview_case"] = case
    for key, value in session.items():
        at.session_state[key] = value
    return at.run()


def _text(at):
    body = "\n".join(getattr(e, "body", "") for e in at.get("html"))
    body = re.sub(r"<style\b.*?</style>", "", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


@pytest.mark.parametrize("case, expected", [
    # 격자 머리는 실행 ID를 밝힌다 — 어느 실행의 숫자인지 모르면 캡처가 증빙이 되지 않는다
    ("24 승인 필요", ["승인이 필요해요", "승인 필요 1건", "18/22",
                   "최근 실행 run_2ec6c62a · 격자 23/25칸 · 예상 흐름 일치 23/23", "규칙 해석으로 처리 중이에요",
                   "복구 전략", "재연결 → 캐시", "FT-03 db_connection_error", "전환 11초 · 목표 15초"]),
    ("25-1 주입 직후", ["주입 후 3초", "지금 승인할 일이 없어요", "진행 중",
                    "최근 실행 run_2ec6c62a · 격자 24/25칸 · 예상 흐름 일치 23/23"]),
    ("25-2 복구 중", ["복구 중", "22초 경과", "22초째", "차단 · 20초 후 시험"]),
    ("25-3 복구 완료", ["정상 처리 중", "대체 해석기를 쓰고 있어요", "19/23", "복귀 확인 2/3"]),
    ("26 정상 운영", ["정상 운영 중", "승인 후 복구", "2분 28초"]),
])
def test_each_design_state_renders(case, expected):
    at = _render(case)
    assert not at.exception
    text = _text(at)
    for word in expected:
        assert word in text, word


def test_approval_buttons_only_when_waiting():
    assert {"apr_INC-0007", "rej_INC-0007"} <= {b.key for b in _render("24 승인 필요").button}
    assert "apr_INC-0007" not in {b.key for b in _render("26 정상 운영").button}


def test_approve_moves_preview_to_normal():
    at = _render("24 승인 필요")
    next(b for b in at.button if b.key == "apr_INC-0007").click().run()
    assert not at.exception
    assert "정상 운영 중" in _text(at)


def test_orders_are_folded_until_opened():
    at = _render("26 정상 운영")
    assert "ORD-0012" not in _text(at) and "오늘 3건" in _text(at)
    next(b for b in at.button if b.key == "hit_ord_toggle").click().run()
    assert "ORD-0012" in _text(at)


def test_incident_row_opens_timeline_cards():
    at = _render("26 정상 운영")
    next(b for b in at.button if b.key == "hit_inc_INC-0006").click().run()
    text = _text(at)
    assert not at.exception
    for word in ("주입 0초", "워커 응답 없음 2회 연속", "워커 재시작", "헬스체크 3/3 · 유실 0"):
        assert word in text, word


def test_a_run_without_a_verdict_does_not_claim_zero_matches():
    """막 시작한 실행은 판정이 없다 — `일치 0/0` 은 숫자가 아니라 착각을 만든다."""
    trials = {**mock._trials("done"),
              "latest_run": {"run_id": "run_new", "ok": 0, "valid": 0,
                             "avg_mttd_ms": None, "avg_mttr_ms": None}}
    html = ov.trials_html(ov.trial_summary(trials))
    assert "run_new" in html
    assert "일치" not in html
    assert "격자" in html
