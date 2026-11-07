-- =====================================================================
-- 02_schema_app.sql — app 스키마 · 테이블 18개 · 인덱스 (D-07 4절 · 5.1)
-- 공통 규약(D-07 3절): 식별자 TEXT · 시각 TIMESTAMPTZ · 금액 INTEGER(미정은 NULL)
--                      열거는 TEXT + CHECK (ENUM 금지) · 물리 삭제 금지
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS app;

-- ---------- 4.1 order_session (증분 1) ----------
CREATE TABLE IF NOT EXISTS app.order_session (
  session_id          TEXT        PRIMARY KEY,
  token_hash          TEXT        NOT NULL UNIQUE,
  brand_id            TEXT        NOT NULL CHECK (brand_id IN ('starbucks','generic')),
  brand_source        TEXT        NOT NULL CHECK (brand_source IN ('request','chat','default','unsupported','asked')),
  brand_evidence_text TEXT,
  brand_store_name    TEXT,
  menu_version        TEXT        NOT NULL,
  status              TEXT        NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending','running','recovering','needs_review',
                                                    'awaiting_confirmation','completed','failed')),
  version             INTEGER     NOT NULL DEFAULT 1,
  completed_at        TIMESTAMPTZ,
  share_count         INTEGER     NOT NULL DEFAULT 0,
  is_verification     BOOLEAN     NOT NULL DEFAULT false,
  checkpoint          TEXT        CHECK (checkpoint IN ('created','chat_read','menu_matched','validated','settled')),
  checkpoint_at       TIMESTAMPTZ,
  work_lock_id        TEXT,
  lock_acquired_at    TIMESTAMPTZ,
  lock_expires_at     TIMESTAMPTZ,
  trace_id            TEXT        NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.2 snapshot (증분 1) ----------
CREATE TABLE IF NOT EXISTS app.snapshot (
  snapshot_id      TEXT        PRIMARY KEY,
  session_id       TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  snapshot_no      INTEGER     NOT NULL,
  raw_text         TEXT        NOT NULL,
  content_hash     TEXT        NOT NULL,
  source           TEXT        DEFAULT 'paste' CHECK (source IN ('paste','file','sample')),
  message_count    INTEGER     NOT NULL DEFAULT 0,
  person_count     INTEGER     NOT NULL DEFAULT 0,
  unreadable_lines JSONB       DEFAULT '[]'::jsonb,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.3 message (증분 1) ----------
CREATE TABLE IF NOT EXISTS app.message (
  message_id  TEXT        PRIMARY KEY,
  snapshot_id TEXT        NOT NULL REFERENCES app.snapshot(snapshot_id) ON DELETE CASCADE,
  seq         INTEGER     NOT NULL,
  sender      TEXT        NOT NULL,
  text        TEXT        NOT NULL,
  is_reply    BOOLEAN     NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.11 메뉴 4종 (증분 2) — order_item.menu_id FK 때문에 먼저 만든다 ----------
CREATE TABLE IF NOT EXISTS app.menu (
  menu_id      TEXT        PRIMARY KEY,
  brand_id     TEXT        NOT NULL CHECK (brand_id IN ('starbucks','generic')),
  menu_version TEXT        NOT NULL,
  category     TEXT        NOT NULL,
  name         TEXT        NOT NULL,
  base_price   INTEGER     NOT NULL,
  trait_tags   TEXT[]      NOT NULL DEFAULT '{}',
  is_active    BOOLEAN     NOT NULL DEFAULT true,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (brand_id, menu_version, name)
);

CREATE TABLE IF NOT EXISTS app.menu_alias (
  alias    TEXT NOT NULL,
  menu_id  TEXT NOT NULL REFERENCES app.menu(menu_id) ON DELETE CASCADE,
  brand_id TEXT NOT NULL CHECK (brand_id IN ('starbucks','generic')),
  PRIMARY KEY (alias, menu_id)
);

CREATE TABLE IF NOT EXISTS app.menu_option (
  option_id    TEXT    PRIMARY KEY,
  menu_id      TEXT    REFERENCES app.menu(menu_id) ON DELETE CASCADE,
  category     TEXT,
  option_group TEXT    NOT NULL CHECK (option_group IN ('size','ice','shot','bean','cup','syrup','milk')),
  option_value TEXT    NOT NULL,
  value_label  TEXT    NOT NULL,
  extra_price  INTEGER NOT NULL DEFAULT 0,
  is_default   BOOLEAN NOT NULL DEFAULT false
);

-- ★ D-02 API-012 는 매장 되묻기 응답으로 status='awaiting_brand_answer' 를 돌려주는데
--   D-07 4.1 의 CHECK 목록에 그 값이 없다. 되묻기 대기 세션을 저장할 상태가 없다는 뜻이다.
--   P-03 4.2 는 "PENDING — 되묻기가 뜨면 담당자 답 후 RUNNING"이라고만 적어 대기 상태에
--   이름을 주지 않았고, D-07 의 brand_source 에는 이미 'asked'(답을 받은 뒤)가 있다 —
--   되묻기 흐름을 전제하면서 대기 상태만 빠진 것이다. D-02 가 쓰는 이름으로 맞춘다.
ALTER TABLE app.order_session DROP CONSTRAINT IF EXISTS order_session_status_check;
ALTER TABLE app.order_session ADD CONSTRAINT order_session_status_check
  CHECK (status IN ('pending','awaiting_brand_answer','running','recovering','needs_review',
                    'awaiting_confirmation','completed','failed'));

-- ★ D-07 4.11 표에 없는 컬럼이다. 없으면 매장 기준별 사이즈 용어를 구분할 수 없다.
--   menu_id 를 NULL 로 둔 "분류 전체 공통" 행이 두 매장에 동시에 적용돼
--   스타벅스 세션에 `레귤러` · `라지`가 함께 뜬다. U-13 이 요구하는 "용어 충돌 1건"이
--   바로 이 경우다. menu_alias 가 같은 이유로 brand_id 를 이미 갖고 있다(4.11) —
--   D-07 의 내부 불일치이므로 menu_alias 쪽에 맞춘다.
ALTER TABLE app.menu_option
  ADD COLUMN IF NOT EXISTS brand_id TEXT
    CHECK (brand_id IS NULL OR brand_id IN ('starbucks','generic'));

-- ★ 옵션 종류 2개 추가 (2026-11-04 · 스타벅스_메뉴별_옵션정리.xlsx).
--   bean = 원두(시그니처 · 블론드 · 디카페인), cup = 개인컵 여부.
--   스타벅스는 size · ice · shot · bean · cup 다섯 종류만 DB 로 관리하고, 그 밖의 요청(시럽 · 우유 ·
--   휘핑 등)은 기타 요청(custom_request)으로 보낸다. syrup · milk 는 일반 카페가 계속 쓴다.
--   위 CREATE TABLE 의 CHECK 는 이미 만들어진 표에 적용되지 않으므로 제약을 바꿔 끼운다
ALTER TABLE app.menu_option DROP CONSTRAINT IF EXISTS menu_option_option_group_check;
ALTER TABLE app.menu_option ADD CONSTRAINT menu_option_option_group_check
  CHECK (option_group IN ('size','ice','shot','bean','cup','syrup','milk'));

CREATE TABLE IF NOT EXISTS app.brand_alias (
  alias             TEXT PRIMARY KEY,
  resolved_brand_id TEXT NOT NULL CHECK (resolved_brand_id IN ('starbucks','generic')),
  kind              TEXT NOT NULL CHECK (kind IN ('supported','generic_expr','unsupported')),
  display_name      TEXT NOT NULL
);

-- ---------- 4.4 order_event (증분 1) ----------
CREATE TABLE IF NOT EXISTS app.order_event (
  event_id             TEXT        PRIMARY KEY,
  session_id           TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  snapshot_id          TEXT        NOT NULL REFERENCES app.snapshot(snapshot_id) ON DELETE CASCADE,
  message_id           TEXT        NOT NULL REFERENCES app.message(message_id) ON DELETE CASCADE,
  evidence_message_ids TEXT[]      NOT NULL DEFAULT '{}',
  event_type           TEXT        NOT NULL CHECK (event_type IN ('add','update','cancel','confirm','ignore')),
  target_event_id      TEXT        REFERENCES app.order_event(event_id) ON DELETE SET NULL,
  is_reference         BOOLEAN     NOT NULL DEFAULT false,
  agent                TEXT        NOT NULL DEFAULT 'intake',
  service              TEXT        CHECK (service IN ('primary','backup')),
  trace_id             TEXT        NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ★ Handoff 저장본 (2026-11-04 · PRD 5.2 Handoff 개정 · D-12 10.4)
--   N-04 가 Intake 결과(LLM 해석) 이벤트 전체를 여기 먼저 저장하고 checkpoint = chat_read 를 남긴다.
--   그 뒤에 프로세스가 죽으면 이 저장본으로 이어 간다 — LLM 을 다시 부르지 않고, 같은 이벤트로 같은 결과가 나온다.
--   예전 행(값 NULL)은 재개 대상이 아니다. 기존 DB 에도 붙도록 ALTER 로 둔다
ALTER TABLE app.order_event ADD COLUMN IF NOT EXISTS payload JSONB;

-- ---------- 4.5 order_item (증분 1 → 2에서 메뉴 연결) ----------
CREATE TABLE IF NOT EXISTS app.order_item (
  item_id            TEXT        PRIMARY KEY,
  session_id         TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  source_event_id    TEXT        REFERENCES app.order_event(event_id) ON DELETE SET NULL,
  person             TEXT        NOT NULL,
  menu_id            TEXT        REFERENCES app.menu(menu_id) ON DELETE RESTRICT,
  menu_text          TEXT        NOT NULL,
  options            JSONB       NOT NULL DEFAULT '{}'::jsonb,
  ambiguous_options  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  custom_request     TEXT,
  quantity           INTEGER     NOT NULL DEFAULT 1 CHECK (quantity > 0),
  unit_price         INTEGER,
  estimated_price    INTEGER,
  validation_status  TEXT        NOT NULL DEFAULT 'needs_review'
                                 CHECK (validation_status IN ('passed','needs_review','failed')),
  rule_verdict       TEXT        NOT NULL DEFAULT 'undecided'
                                 CHECK (rule_verdict IN ('passed','failed','undecided')),
  llm_review_verdict TEXT        CHECK (llm_review_verdict IN ('agree','disagree','unavailable')),
  review_reason      TEXT        CHECK (review_reason IN ('ambiguous_spec','trait_expression','reference_menu',
                                                          'unregistered_menu','ambiguous_target','ambiguous_person')),
  review_reason_text TEXT,
  removed            BOOLEAN     NOT NULL DEFAULT false,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ★ 대리 주문 주문자 확인 (2026-11-04 · P-03 INT-002 · D-12 10.5)
--   person_check = {"said": "서연이", "candidates": ["박서연"]} — 담당자가 확인 질문에서 고르기 전까지 남는다.
--   고르면 person 을 바꾸고 NULL 로 비운다. 사유 값 ambiguous_person 을 더한다(CHECK 는 바꿔 끼운다)
ALTER TABLE app.order_item ADD COLUMN IF NOT EXISTS person_check JSONB;
ALTER TABLE app.order_item DROP CONSTRAINT IF EXISTS order_item_review_reason_check;
ALTER TABLE app.order_item ADD CONSTRAINT order_item_review_reason_check
  CHECK (review_reason IN ('ambiguous_spec','trait_expression','reference_menu',
                           'unregistered_menu','ambiguous_target','ambiguous_person'));

-- ---------- 4.6 review_question (증분 3) ----------
CREATE TABLE IF NOT EXISTS app.review_question (
  question_id   TEXT        PRIMARY KEY,
  session_id    TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  item_id       TEXT        NOT NULL REFERENCES app.order_item(item_id) ON DELETE CASCADE,
  seq           INTEGER     NOT NULL,
  reason        TEXT        NOT NULL,
  question_text TEXT        NOT NULL,
  options       JSONB       NOT NULL DEFAULT '[]'::jsonb,
  sort_basis    TEXT        CHECK (sort_basis IN ('trait_tags','alias_score','reference_message')),
  status        TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','voided')),
  chosen_value  TEXT,
  answered_at   TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.8 suggestion (증분 3) ----------
CREATE TABLE IF NOT EXISTS app.suggestion (
  suggestion_id    TEXT        PRIMARY KEY,
  session_id       TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  item_id          TEXT        REFERENCES app.order_item(item_id) ON DELETE SET NULL,
  reply_message_id TEXT        REFERENCES app.message(message_id) ON DELETE SET NULL,
  person           TEXT,
  quote            TEXT        NOT NULL,
  change_text      TEXT        NOT NULL,
  change_json      JSONB,
  kind             TEXT        NOT NULL CHECK (kind IN ('change','ambiguous')),
  question_id      TEXT        REFERENCES app.review_question(question_id) ON DELETE SET NULL,
  status           TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','ignored')),
  decided_at       TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.7 change_log (증분 3) ----------
CREATE TABLE IF NOT EXISTS app.change_log (
  change_id     TEXT        PRIMARY KEY,
  session_id    TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  item_id       TEXT        REFERENCES app.order_item(item_id) ON DELETE SET NULL,
  who           TEXT,
  method        TEXT        NOT NULL CHECK (method IN ('chat_cancel','question_answer','chat_edit',
                                                       'reply_applied','auto_recovery')),
  method_label  TEXT        NOT NULL,
  before_text   TEXT,
  after_text    TEXT,
  before_json   JSONB,
  after_json    JSONB,
  suggestion_id TEXT        REFERENCES app.suggestion(suggestion_id) ON DELETE SET NULL,
  incident_id   TEXT,
  undo_token    TEXT        UNIQUE,
  undone_at     TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.9 settlement_line (증분 3 · 저장하되 미노출) ----------
CREATE TABLE IF NOT EXISTS app.settlement_line (
  session_id      TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  session_version INTEGER     NOT NULL,
  person          TEXT        NOT NULL,
  cups            INTEGER     NOT NULL,
  amount          INTEGER     NOT NULL,
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (session_id, session_version, person)
);

-- ---------- 4.10 share_history (증분 3) ----------
CREATE TABLE IF NOT EXISTS app.share_history (
  share_id        TEXT        PRIMARY KEY,
  session_id      TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  session_version INTEGER     NOT NULL,
  method          TEXT        NOT NULL CHECK (method IN ('copy_text','save_image')),
  content_hash    TEXT,
  shared_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.12 idempotency_key (증분 3) ----------
CREATE TABLE IF NOT EXISTS app.idempotency_key (
  key           TEXT        PRIMARY KEY,
  session_id    TEXT        REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  endpoint      TEXT        NOT NULL,
  response_json JSONB       NOT NULL,
  status_code   INTEGER     NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ NOT NULL
);

-- ---------- 4.13 이력 2종 (증분 1 · 교안코드규약 4.12 승계) ----------
CREATE TABLE IF NOT EXISTS app.request_history (
  id          BIGSERIAL   PRIMARY KEY,
  method      TEXT        NOT NULL,
  path        TEXT        NOT NULL,
  status      INTEGER     NOT NULL,
  duration_ms INTEGER     NOT NULL,
  error       TEXT,
  trace_id    TEXT,
  session_id  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.llm_history (
  id               BIGSERIAL   PRIMARY KEY,
  trace_id         TEXT        NOT NULL,
  session_id       TEXT,
  agent            TEXT        NOT NULL CHECK (agent IN ('intake','menu','validation')),
  task             TEXT        NOT NULL CHECK (task IN ('extract_events','normalize_menu','review')),
  service          TEXT        NOT NULL CHECK (service IN ('primary','backup')),
  model            TEXT        NOT NULL,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  duration_ms      INTEGER     NOT NULL,
  schema_valid     BOOLEAN     NOT NULL,
  missing_fields   TEXT[]      NOT NULL DEFAULT '{}',
  prompt_version   TEXT,
  regenerate_count INTEGER     NOT NULL DEFAULT 0,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------- 4.21 intake_conflict (증분 3 · 교안규약 4.6) ----------
CREATE TABLE IF NOT EXISTS app.intake_conflict (
  conflict_id           TEXT        PRIMARY KEY,
  session_id            TEXT        NOT NULL REFERENCES app.order_session(session_id) ON DELETE CASCADE,
  person                TEXT,
  item_id               TEXT        REFERENCES app.order_item(item_id) ON DELETE SET NULL,
  conflicting_event_ids TEXT[]      NOT NULL,
  conflict_kind         TEXT        NOT NULL CHECK (conflict_kind IN ('same_item_opposite_change','duplicate_add',
                                                                      'cancel_after_cancel','target_ambiguous')),
  resolved_question_id  TEXT        REFERENCES app.review_question(question_id) ON DELETE SET NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- 5.1 인덱스 (app)
-- =====================================================================
CREATE INDEX IF NOT EXISTS idx_session_status     ON app.order_session (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_checkpoint ON app.order_session (checkpoint_at)
  WHERE status IN ('running','recovering');
CREATE INDEX IF NOT EXISTS idx_session_lock       ON app.order_session (lock_expires_at)
  WHERE work_lock_id IS NOT NULL;

CREATE INDEX        IF NOT EXISTS idx_snapshot_hash ON app.snapshot (content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshot_no    ON app.snapshot (session_id, snapshot_no);

CREATE INDEX IF NOT EXISTS idx_message_seq ON app.message (snapshot_id, seq);

CREATE INDEX IF NOT EXISTS idx_event_session ON app.order_event (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_message ON app.order_event (message_id);

CREATE INDEX IF NOT EXISTS idx_item_session  ON app.order_item (session_id, removed);
CREATE INDEX IF NOT EXISTS idx_item_review   ON app.order_item (session_id, validation_status)
  WHERE validation_status = 'needs_review';
CREATE INDEX IF NOT EXISTS idx_item_group    ON app.order_item (session_id, menu_id, options, custom_request);
CREATE INDEX IF NOT EXISTS idx_item_rule_gap ON app.order_item (session_id)
  WHERE validation_status = 'passed' AND rule_verdict <> 'passed';

CREATE INDEX IF NOT EXISTS idx_question_open ON app.review_question (session_id, seq)
  WHERE status = 'open';

CREATE INDEX        IF NOT EXISTS idx_change_session ON app.change_log (session_id, created_at DESC);
-- 2026-11-05 4차 보고 1번 — 채팅으로 **주문을 새로 넣는** 경로가 생겼다 (edits.add_from_chat).
-- O-6 변경 이력이 "채팅으로 수정"과 "채팅으로 추가"를 구분해야 한다 — 되돌리기 동작이 다르다
-- (수정은 값 복원 · 추가는 빼기). 이미 만들어진 표에는 CREATE TABLE 의 CHECK 가 적용되지
-- 않으므로 위 4.1 · 4.5 와 같은 방식으로 제약을 바꿔 끼운다.
ALTER TABLE app.change_log DROP CONSTRAINT IF EXISTS change_log_method_check;
ALTER TABLE app.change_log ADD CONSTRAINT change_log_method_check
  CHECK (method IN ('chat_cancel','question_answer','chat_edit','chat_add',
                    'reply_applied','auto_recovery'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_undo_token      ON app.change_log (undo_token)
  WHERE undo_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_suggestion_pending ON app.suggestion (session_id)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_share_session ON app.share_history (session_id, shared_at DESC);

CREATE INDEX IF NOT EXISTS idx_menu_lookup  ON app.menu (brand_id, menu_version, is_active);
CREATE INDEX IF NOT EXISTS idx_menu_traits  ON app.menu USING GIN (trait_tags);
CREATE INDEX IF NOT EXISTS idx_alias_lookup ON app.menu_alias (brand_id, alias);
CREATE INDEX IF NOT EXISTS idx_option_menu  ON app.menu_option (menu_id, option_group);
CREATE INDEX IF NOT EXISTS idx_option_brand ON app.menu_option (brand_id, option_group);

CREATE INDEX IF NOT EXISTS idx_idem_expire ON app.idempotency_key (expires_at);

CREATE INDEX IF NOT EXISTS idx_req_created ON app.request_history (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_req_trace   ON app.request_history (trace_id);

CREATE INDEX IF NOT EXISTS idx_llm_trace       ON app.llm_history (trace_id);
CREATE INDEX IF NOT EXISTS idx_llm_service     ON app.llm_history (service, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_schema_fail ON app.llm_history (agent, prompt_version, created_at DESC)
  WHERE schema_valid = false;

CREATE INDEX IF NOT EXISTS idx_conflict_session ON app.intake_conflict (session_id, created_at);
