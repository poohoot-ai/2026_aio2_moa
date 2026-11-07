-- =====================================================================
-- 04_views.sql — 뷰 3개 (D-07 6.1 · 6.2)
-- 집계값을 테이블로 저장하지 않는다 (D-1 · D-10)
-- =====================================================================

-- 주문서 묶음 (D-1: 저장하지 않고 질의 시 집계)
--
-- 묶는 기준은 **메뉴 + 사양 + 옵션 + 기타 요청**이다 (D-02 API-030).
-- `menu_text`(사람이 쓴 원문 조각)는 기준이 아니다 — 기준에 넣으면 같은 메뉴 · 같은 옵션인데
-- 말투가 달라 따로 묶인다(김민수 "아이스 아메리카노 톨 2잔, 하나는 샷 추가요" · 정하준
-- "아이스 아메리카노" → 2묶음, 2026-11-03 확인). 정본 시안 케이스 18 "같은 메뉴 여러 명"이
-- 성립하지 않는다. 메뉴가 **정해지지 않은 행**만 원문으로 구분한다 — 그때는 원문이 유일한
-- 구분 수단이고, 서로 다른 미정 주문이 한 줄로 합쳐지면 안 된다.
CREATE OR REPLACE VIEW app.v_order_group AS
SELECT session_id, menu_id,
       MIN(menu_text)                      AS menu_text,
       options, custom_request,
       SUM(quantity)                       AS quantity,
       SUM(estimated_price)                AS estimated_amount,
       ARRAY_AGG(person  ORDER BY person)  AS people,
       ARRAY_AGG(item_id ORDER BY item_id) AS item_ids,
       BOOL_OR(estimated_price IS NULL)    AS has_undecided
FROM app.order_item
WHERE removed = false
GROUP BY session_id, menu_id,
         CASE WHEN menu_id IS NULL THEN menu_text END,
         options, custom_request;

-- 정산 정합성 (SC-5: 개인 합계의 합 = 전체 금액)
CREATE OR REPLACE VIEW app.v_settlement_check AS
SELECT s.session_id, s.version,
       (SELECT SUM(amount) FROM app.settlement_line l
         WHERE l.session_id = s.session_id AND l.session_version = s.version) AS person_total,
       (SELECT SUM(estimated_price) FROM app.order_item i
         WHERE i.session_id = s.session_id AND i.removed = false)             AS item_total,
       (SELECT COUNT(*) FROM app.order_item i
         WHERE i.session_id = s.session_id AND i.removed = false
           AND i.estimated_price IS NULL)                                     AS undecided_count
FROM app.order_session s;

-- 지표 (D-10: 집계값을 저장하지 않는다)
CREATE OR REPLACE VIEW healing.v_metrics AS
SELECT
  -- MTTD = 탐지 − **장애 시작**. 주입 장애는 injected_at, 저절로 난 장애는 first_signal_at
  -- (첫 위반 관측 시각)을 쓴다. 둘 다 없으면 NULL 로 두고 평균에서 빠진다 — 0 을 지어내지 않는다.
  -- 이 COALESCE 가 없던 동안 mttd_sec 는 늘 NULL 이었다(2026-11-07 실측 · 사건 12건).
  AVG(EXTRACT(EPOCH FROM (i.detected_at - COALESCE(i.injected_at, f.injected_at, i.first_signal_at))))
                                                                                  AS mttd_sec,
  -- MTTR 은 PRD SC-3 정의(recovered_at - injected_at)를 따른다 (v1.9 · R-20).
  -- 시작점은 MTTD 와 같은 기준이어야 한다 — 주입 시각 > 첫 징후 > 탐지 시각 순으로 찾는다
  AVG(EXTRACT(EPOCH FROM (i.resolved_at
        - COALESCE(i.injected_at, f.injected_at, i.first_signal_at, i.detected_at))))
                                                                                  AS mttr_sec,
  AVG(CASE WHEN i.status = 'recovered' THEN 1.0 ELSE 0.0 END)
    FILTER (WHERE i.is_false_positive = false)                                    AS recovery_success_rate,
  COUNT(*) FILTER (WHERE i.escalated_at IS NOT NULL)                              AS escalation_count,
  COUNT(*) FILTER (WHERE i.is_false_positive)::numeric
    / NULLIF(COUNT(*), 0)                                                         AS false_positive_rate,
  AVG((SELECT COUNT(*) FROM healing.recovery_attempt a
        WHERE a.incident_id = i.incident_id))                                     AS avg_attempts,
  COUNT(*)                                                                        AS incident_count,
  -- 종전 MTTR(탐지 ~ 정상화, 승인 대기 제외)은 참고용 — CREATE OR REPLACE VIEW 는 새 칸을 끝에만 붙일 수 있다
  AVG(EXTRACT(EPOCH FROM (i.resolved_at - i.detected_at)) - i.paused_ms / 1000.0) AS mttr_excl_pause_sec
FROM healing.incident i
LEFT JOIN healing.fault_injection f ON f.incident_id = i.incident_id
WHERE i.is_verification_run = false;        -- 검증용 실행 제외

-- 뷰 권한 — GRANT ON ALL TABLES 는 이 파일 이전에 돌았으므로 따로 준다
GRANT SELECT ON app.v_order_group, app.v_settlement_check TO app_rw, healing_rw;
GRANT SELECT ON healing.v_metrics                         TO app_rw, healing_rw;
