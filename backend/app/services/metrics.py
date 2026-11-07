"""5.6.1 유형별 감지 메트릭 — Monitor 가 읽는 재료를 업무 층이 적는다 (평가 P3-2).

경계(PRD 5.3 v1.1 "복구 경계"): 업무 층은 **관측한 사실만** 적는다.
사건 생성 · 복구 판단 · 완료 선언은 장애 대응 층이 한다 — 여기서 하지 않는다.

기록 실패는 삼킨다. 메트릭 때문에 주문 요청이 깨지면 본말이 뒤집힌다.
F-6(저장소 장애) 중에는 DB 가 없어 기록 자체가 불가능하므로, 그 구간의 사건은
healing 이 메모리에 들고 있다가 DB 가 돌아온 뒤에 적는다.
"""

from app import config, db, logging_setup

# healing.runtime_metric.metric_key 의 CHECK 제약과 **같은 목록**이어야 한다 (03_schema_healing.sql 4.20).
# 제약에 없는 키를 보내면 INSERT 가 깨지는데, 기록 실패는 삼키므로 조용히 비어 있게 된다 —
# 그래서 여기서 먼저 걸러 ValueError 로 알린다
METRIC_KEYS = frozenset({
    "conn_fail_count", "conn_latency_ms", "db_query_p95_ms", "db_pool_usage",
    "dep_5xx_ratio", "dep_latency_ms", "schema_invalid_count", "checkpoint_age_sec",
    "timeout_count", "agent_error_count", "restart_count",
    "db_connection_error", "slow_query_count",
    "schema_error_rate", "validation_fail_rate",
    "injection_detect_count", "circuit_open_count", "fallback_ratio",
})

# 장애 코드 -> 그 장애를 드러내는 메트릭 (PRD 5.6.1)
#   DEP_TIMEOUT          timeout_count       호출 5초 초과            F-2
#   DEP_HTTP_5XX         dep_5xx_ratio       5xx 연속 2회             F-1
#   AGENT_OUTPUT_INVALID schema_error_rate   스키마 검증 실패 1회     F-5
_FAULT_METRIC = {
    "DEP_TIMEOUT": "timeout_count",
    "DEP_HTTP_5XX": "dep_5xx_ratio",
    "AGENT_OUTPUT_INVALID": "schema_error_rate",
}

_INSERT = ("INSERT INTO healing.runtime_metric(metric_key,target,value,threshold,breached,trace_id) "
           "VALUES (%s,%s,%s,%s,%s,%s)")


def record(metric_key: str, target: str, value, *, threshold=None, breached: bool = False,
           trace_id: str | None = None) -> None:
    """관측 한 건을 남긴다. 한 행 = 한 관측이고, 판정 조건(연속 2회 등)은 Monitor 가 센다."""
    if metric_key not in METRIC_KEYS:
        raise ValueError(f"runtime_metric 에 없는 메트릭 키: {metric_key}")
    if not config.db_configured():
        return
    try:
        db.execute(_INSERT, (metric_key, target, value, threshold, breached, trace_id), role="healing")
    except Exception:      # noqa: BLE001 — 메트릭 기록은 비치명이다. F-6 중에는 DB 자체가 없다
        logging_setup.log("schema_invalid", level="warning", trace_id=trace_id,
                          detail="metric_record_failed", metric_key=metric_key, target=target)


def observe_interpreter(fault_code: str | None, service: str, duration_ms: int, *,
                        trace_id: str | None = None) -> None:
    """주문 해석 서비스 호출 한 건의 관측 (F-1 · F-2 · F-5).

    `fault_code` 가 None 이면 성공이다. 성공도 남긴다 — 지연은 장애가 아니어도 추세가 필요하고,
    PRD 5.6.1 API 판정 조건이 "호출 5초 초과"라서 예외가 나지 않은 초과도 넘은 것으로 표시한다.
    """
    target = f"order_interpreter_{service}"
    limit_ms = int(config.ORDER_INTERPRETER_TIMEOUT_SEC * 1000)
    record("dep_latency_ms", target, duration_ms, threshold=limit_ms,
           breached=duration_ms > limit_ms, trace_id=trace_id)
    key = _FAULT_METRIC.get(fault_code or "")
    if key:
        record(key, target, 1, threshold=1, breached=True, trace_id=trace_id)
