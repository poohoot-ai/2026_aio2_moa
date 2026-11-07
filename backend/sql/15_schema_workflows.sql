-- Lab workflow execution and audit. Additive migration: never clears existing app/healing data.
CREATE TABLE IF NOT EXISTS healing.workflow_run (
  run_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('quick','full','custom')),
  template_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','preflight','running','stop_requested','cleaning',
                      'completed','stopped','failed','needs_operator_action','invalid')),
  phase TEXT NOT NULL DEFAULT 'queued',
  environment TEXT NOT NULL CHECK (environment = 'lab'),
  cases JSONB NOT NULL,
  total_iterations INTEGER NOT NULL CHECK (total_iterations BETWEEN 1 AND 25),
  current_iteration INTEGER NOT NULL DEFAULT 0,
  cancel_requested BOOLEAN NOT NULL DEFAULT false,
  idempotency_key TEXT UNIQUE,
  policy_version TEXT NOT NULL DEFAULT 'chat-plan-2026-11-06',
  preflight JSONB NOT NULL DEFAULT '{}'::jsonb,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_one_active
  ON healing.workflow_run ((true))
  WHERE status IN ('queued','preflight','running','stop_requested','cleaning');
CREATE INDEX IF NOT EXISTS idx_workflow_run_recent
  ON healing.workflow_run (created_at DESC);

CREATE TABLE IF NOT EXISTS healing.workflow_iteration (
  run_id TEXT NOT NULL REFERENCES healing.workflow_run(run_id) ON DELETE CASCADE,
  iteration_no INTEGER NOT NULL,
  fault_type TEXT NOT NULL CHECK (fault_type IN ('F-1','F-2','F-3','F-4','F-5','F-6')),
  target TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','PASS','FAIL','INVALID','AWAITING_APPROVAL','NEEDS_REVIEW')),
  phase TEXT NOT NULL DEFAULT 'queued',
  injection_id TEXT,
  incident_id TEXT,
  synthetic_requests INTEGER NOT NULL DEFAULT 0,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  mttd_ms INTEGER,
  mttr_ms INTEGER,
  PRIMARY KEY (run_id, iteration_no)
);

CREATE TABLE IF NOT EXISTS healing.workflow_event (
  event_id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES healing.workflow_run(run_id) ON DELETE CASCADE,
  iteration_no INTEGER,
  event_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_workflow_event_run
  ON healing.workflow_event (run_id, event_id);

CREATE TABLE IF NOT EXISTS healing.workflow_worker (
  worker_id TEXT PRIMARY KEY,
  last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  proxy_ok BOOLEAN NOT NULL DEFAULT false,
  allowlist JSONB NOT NULL DEFAULT '[]'::jsonb,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS healing.workflow_lease (
  lease_name TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES healing.workflow_run(run_id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL,
  lease_until TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE healing.fault_injection
  ADD COLUMN IF NOT EXISTS run_id TEXT,
  ADD COLUMN IF NOT EXISTS iteration_no INTEGER;
ALTER TABLE healing.incident
  ADD COLUMN IF NOT EXISTS run_id TEXT;

-- Older fault/incident rows can have a run_id from an earlier, non-workflow
-- execution. Keep those audit rows, but remove the invalid association before
-- adding the new foreign keys. The columns are nullable by design.
UPDATE healing.fault_injection fi
   SET run_id = NULL
 WHERE fi.run_id IS NOT NULL
   AND NOT EXISTS (
     SELECT 1 FROM healing.workflow_run wr WHERE wr.run_id = fi.run_id
   );
UPDATE healing.incident i
   SET run_id = NULL
 WHERE i.run_id IS NOT NULL
   AND NOT EXISTS (
     SELECT 1 FROM healing.workflow_run wr WHERE wr.run_id = i.run_id
   );

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fault_injection_workflow_run_fk') THEN
    ALTER TABLE healing.fault_injection ADD CONSTRAINT fault_injection_workflow_run_fk
      FOREIGN KEY (run_id) REFERENCES healing.workflow_run(run_id) ON DELETE SET NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'incident_workflow_run_fk') THEN
    ALTER TABLE healing.incident ADD CONSTRAINT incident_workflow_run_fk
      FOREIGN KEY (run_id) REFERENCES healing.workflow_run(run_id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_fault_injection_run ON healing.fault_injection (run_id, injected_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_workflow_run ON healing.incident (run_id, detected_at DESC)
  WHERE run_id IS NOT NULL;
