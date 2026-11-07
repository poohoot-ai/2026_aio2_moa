-- =====================================================================
-- 03_schema_healing.sql — healing 스키마 · 테이블 7개 · 인덱스 (D-07 4.14~4.20 · 5.2)
-- app 스키마를 FK로 참조하지 않는다 (D-07 1.3 — 롤 분리 · trace_id로 잇는다)
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS healing;

-- ---------- 4.14 incident (PRD 7.2 필수 항목 전수) ----------
CREATE TABLE IF NOT EXISTS healing.incident (
  incident_id           TEXT        PRIMARY KEY,
  trace_id              TEXT        NOT NULL,
  fault_code            TEXT        NOT NULL CHECK (fault_code IN ('DEP_HTTP_5XX','DEP_TIMEOUT','WORKER_DOWN',
                                                                   'AGENT_EXCEPTION','AGENT_OUTPUT_INVALID',
                                                                   'REPEATED_FAILURE','DB_UNAVAILABLE',
                                                                   'PROMPT_INJECTION','UNKNOWN')),
  status                TEXT        NOT NULL DEFAULT 'detected'
                                    CHECK (status IN ('detected','recovering','awaiting_approval',
                                                      'manual_check','recovered','failed')),
  status_label          TEXT        NOT NULL,
  injected_at           TIMESTAMPTZ,
  detected_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  recovery_started_at   TIMESTAMPTZ,
  resolved_at           TIMESTAMPTZ,
  failed_at             TIMESTAMPTZ,
  escalated_at          TIMESTAMPTZ,
  paused_ms             INTEGER     NOT NULL DEFAULT 0,
  detection_metric      TEXT        NOT NULL,
  detection_evidence    TEXT[]      NOT NULL DEFAULT '{}',
  diagnosis_rule        TEXT,
  diagnosis_reason      TEXT,
  strategy              TEXT        CHECK (strategy IN ('retry_fallback','restart_resume','regenerate',
                                                        'circuit_open','reconnect_cache','escalate')),
  attempt_count         INTEGER     NOT NULL DEFAULT 0,
  session_id            TEXT,
  snapshot_id           TEXT,
  brand_id              TEXT,
  menu_version          TEXT,
  checkpoint            TEXT,
  completed_version     INTEGER,
  elapsed_ms            INTEGER,
  is_verification_run   BOOLEAN     NOT NULL DEFAULT false,
  is_false_positive     BOOLEAN     NOT NULL DEFAULT false,
  false_positive_reason TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.15 recovery_attempt ----------
CREATE TABLE IF NOT EXISTS healing.recovery_attempt (
  incident_id TEXT        NOT NULL REFERENCES healing.incident(incident_id) ON DELETE CASCADE,
  attempt_no  INTEGER     NOT NULL,
  action      TEXT        NOT NULL CHECK (action IN ('retry','switch_backup','restart','resume',
                                                     'regenerate','reconnect','cache_fallback','probe')),
  wait_ms     INTEGER     NOT NULL DEFAULT 0,
  tool        TEXT,
  tool_args   JSONB,
  result      TEXT        NOT NULL CHECK (result IN ('ok','fail','timeout')),
  error       TEXT,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at    TIMESTAMPTZ,
  PRIMARY KEY (incident_id, attempt_no)
);

-- ---------- 4.16 verification_result (F-019) ----------
CREATE TABLE IF NOT EXISTS healing.verification_result (
  incident_id TEXT        NOT NULL REFERENCES healing.incident(incident_id) ON DELETE CASCADE,
  check_name  TEXT        NOT NULL CHECK (check_name IN ('health_consecutive','request_sample','function_test',
                                                         'integrity','baseline_latency','cache_db_match')),
  expected    TEXT        NOT NULL,
  actual      TEXT        NOT NULL,
  passed      BOOLEAN     NOT NULL,
  verdict     TEXT        CHECK (verdict IN ('pass','fail')),
  verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (incident_id, check_name)
);

-- ---------- 4.17 approval (사건당 1건) ----------
CREATE TABLE IF NOT EXISTS healing.approval (
  incident_id         TEXT        PRIMARY KEY REFERENCES healing.incident(incident_id) ON DELETE CASCADE,
  decision            TEXT        NOT NULL CHECK (decision IN ('approve','reject')),
  approver            TEXT        NOT NULL DEFAULT '운영자',
  note                TEXT,
  recommendation_text TEXT,
  waiting_sec         INTEGER,
  decided_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.18 fault_injection (MTTD 시작점) ----------
CREATE TABLE IF NOT EXISTS healing.fault_injection (
  injection_id TEXT        PRIMARY KEY,
  fault        TEXT        NOT NULL CHECK (fault IN ('F-1','F-2','F-3','F-4','F-5','F-6')),
  action       TEXT        NOT NULL CHECK (action IN ('inject','clear')),
  target       TEXT        NOT NULL,
  mode         TEXT        CHECK (mode IN ('error','delay','invalid','kill','block')),
  injected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  cleared_at   TIMESTAMPTZ,
  incident_id  TEXT,
  environment  TEXT        NOT NULL CHECK (environment = 'lab')
);

-- ---------- 4.19 circuit_state ----------
CREATE TABLE IF NOT EXISTS healing.circuit_state (
  dependency_key        TEXT        PRIMARY KEY CHECK (dependency_key IN ('order_interpreter_primary',
                                                                          'order_interpreter_backup',
                                                                          'menu_store','agent_process')),
  state                 TEXT        NOT NULL CHECK (state IN ('up','degraded','down','circuit_open','probing')),
  consecutive_failures  INTEGER     NOT NULL DEFAULT 0,
  opened_at             TIMESTAMPTZ,
  probe_at              TIMESTAMPTZ,
  cooldown_until        TIMESTAMPTZ,
  cache_fallback_active BOOLEAN     NOT NULL DEFAULT false,
  is_active_route       BOOLEAN     NOT NULL DEFAULT false,
  healthy_streak        INTEGER     NOT NULL DEFAULT 0,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.20 runtime_metric (5.6.1 메트릭 · 보존 24시간) ----------
CREATE TABLE IF NOT EXISTS healing.runtime_metric (
  id          BIGSERIAL   PRIMARY KEY,
  metric_key  TEXT        NOT NULL CHECK (metric_key IN ('conn_fail_count','conn_latency_ms','db_query_p95_ms',
                                                         'db_pool_usage','dep_5xx_ratio','dep_latency_ms',
                                                         'schema_invalid_count','checkpoint_age_sec',
                                                         'timeout_count','agent_error_count','restart_count',
                                                         'db_connection_error','slow_query_count',
                                                         'schema_error_rate','validation_fail_rate',
                                                         'injection_detect_count','circuit_open_count',
                                                         'fallback_ratio')),
  target      TEXT        NOT NULL,
  value       NUMERIC     NOT NULL,
  threshold   NUMERIC,
  breached    BOOLEAN     NOT NULL DEFAULT false,
  incident_id TEXT,
  trace_id    TEXT,
  measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- 5.2 인덱스 (healing)
-- =====================================================================
-- 2026-11-07 추가 — 사건의 **대상**과 **첫 징후 시각**
--
-- target: 없던 동안 중복 사건 판정이 장애 코드만 보고 이뤄졌다. 앱 A 의 F-3 이 열려 있으면
--         앱 B 의 F-3 을 탐지하지 못했고, 닫히지 않은 사건 하나가 그 코드 전체를 막았다.
-- first_signal_at: MTTD(= 탐지 − 장애 발생)의 시작점. 주입 장애는 injected_at 이 있지만
--         저절로 난 장애에는 시작 시각이 없어 MTTD 평균이 구조적으로 늘 비어 있었다.
--         첫 위반 관측(또는 첫 헬스체크 실패) 시각을 적어 둔다 — 지어내지 않고 관측한 값이다.
ALTER TABLE healing.incident ADD COLUMN IF NOT EXISTS target          TEXT;
ALTER TABLE healing.incident ADD COLUMN IF NOT EXISTS first_signal_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_incident_list     ON healing.incident (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_approval ON healing.incident (status)
  WHERE status = 'awaiting_approval';
CREATE INDEX IF NOT EXISTS idx_incident_trace    ON healing.incident (trace_id);
CREATE INDEX IF NOT EXISTS idx_incident_fault    ON healing.incident (fault_code, detected_at DESC)
  WHERE is_verification_run = false;
CREATE INDEX IF NOT EXISTS idx_incident_session  ON healing.incident (session_id);
CREATE INDEX IF NOT EXISTS idx_incident_fp       ON healing.incident (detected_at DESC)
  WHERE is_false_positive = true;

CREATE INDEX IF NOT EXISTS idx_injection_time ON healing.fault_injection (injected_at DESC);

-- 활성 경로는 주문 해석 2계통 중 정확히 하나 (D-07 4.19)
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_route ON healing.circuit_state (is_active_route)
  WHERE is_active_route = true
    AND dependency_key IN ('order_interpreter_primary','order_interpreter_backup');

CREATE INDEX IF NOT EXISTS idx_metric_recent   ON healing.runtime_metric (metric_key, target, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_metric_breached ON healing.runtime_metric (measured_at DESC)
  WHERE breached = true AND incident_id IS NULL;

-- =====================================================================
-- PRD v1.9 (2026-11-05) — 기존 DB 에도 붙도록 ALTER 로 둔다 (02_schema_app.sql 과 같은 방식)
--   장애 코드 2종 추가: AGENT_EXCEPTION(FT-02) · PROMPT_INJECTION(F-7 선택)
--   메트릭 키: 평가 메트릭 8종(FT-01 ~ FT-04 · 프롬프트 공격) + circuit_open_count · fallback_ratio (PRD 5.6.1)
--   recovery_plan: Diagnosis 의 복구 계획(PRD 5.9.3) — 장애 대응 층이 채운다. 지금은 비어 있다
-- =====================================================================
ALTER TABLE healing.incident DROP CONSTRAINT IF EXISTS incident_fault_code_check;
ALTER TABLE healing.incident ADD CONSTRAINT incident_fault_code_check
  CHECK (fault_code IN ('DEP_HTTP_5XX','DEP_TIMEOUT','WORKER_DOWN','AGENT_EXCEPTION','AGENT_OUTPUT_INVALID',
                        'REPEATED_FAILURE','DB_UNAVAILABLE','PROMPT_INJECTION','UNKNOWN'));
ALTER TABLE healing.incident ADD COLUMN IF NOT EXISTS recovery_plan JSONB;

ALTER TABLE healing.runtime_metric DROP CONSTRAINT IF EXISTS runtime_metric_metric_key_check;
ALTER TABLE healing.runtime_metric ADD CONSTRAINT runtime_metric_metric_key_check
  CHECK (metric_key IN ('conn_fail_count','conn_latency_ms','db_query_p95_ms','db_pool_usage','dep_5xx_ratio',
                        'dep_latency_ms','schema_invalid_count','checkpoint_age_sec',
                        'timeout_count','agent_error_count','restart_count','db_connection_error',
                        'slow_query_count','schema_error_rate','validation_fail_rate','injection_detect_count',
                        'circuit_open_count','fallback_ratio'));
