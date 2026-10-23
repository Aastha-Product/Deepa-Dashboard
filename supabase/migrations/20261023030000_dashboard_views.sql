-- P7: views the dashboard reads. security_invoker = true, so the public API (anon/authenticated)
-- still sees nothing through them; only the server, which owns the tables, can read them.

-- One row per SKU per run, with the latest Gate 1 / Gate 2 decision and where the line stands.
CREATE OR REPLACE VIEW v_lines WITH (security_invoker = true) AS
SELECT
    r.*,
    m.sku_name, m.category, m.perishability, m.standard_reorder_qty, m.distributor_id, d.distributor_name,
    g1.decision AS gate1_decision, g1.final_qty AS gate1_qty, g1.reason AS gate1_reason,
    g1.decided_by AS gate1_by, g1.decided_at AS gate1_at,
    g2.decision AS gate2_decision, g2.final_qty AS gate2_qty, g2.reason AS gate2_reason,
    g2.decided_by AS gate2_by, g2.decided_at AS gate2_at,
    po.po_number,
    COALESCE(g2.final_qty, g1.final_qty, r.qty) AS final_qty,
    CASE
        WHEN r.exception_type IS NOT NULL AND g1.decision IS NULL THEN 'awaiting_gate1'
        WHEN g1.decision = 'override' THEN 'overridden'
        WHEN (r.action = 'pause' OR COALESCE(g1.final_qty, r.qty) > 0) AND g2.decision IS NULL THEN 'awaiting_gate2'
        WHEN g2.decision IS NOT NULL AND po.po_number IS NOT NULL THEN 'po_recorded'
        WHEN g2.decision IS NOT NULL THEN 'approved'
        ELSE 'no_action'
    END AS line_status,
    CASE
        WHEN r.exception_type IS NOT NULL THEN 0
        WHEN r.flag_stockout THEN 1
        WHEN r.flag_overstock THEN 2
        WHEN r.flag_high_demand THEN 3
        ELSE 4
    END AS sort_group,
    CASE
        WHEN r.flag_stockout THEN r.cover_weeks / NULLIF(r.min_weeks, 0)
        WHEN r.flag_overstock THEN -(r.cover_weeks / NULLIF(r.max_weeks, 0))
        ELSE r.cover_weeks
    END AS sort_key
FROM sku_recommendations r
JOIN sku_master m ON m.sku_id = r.sku_id
JOIN distributors d ON d.distributor_id = m.distributor_id
LEFT JOIN LATERAL (
    SELECT * FROM decisions x WHERE x.run_id = r.run_id AND x.sku_id = r.sku_id AND x.gate = 1
    ORDER BY x.decided_at DESC, x.decision_id DESC LIMIT 1) g1 ON true
LEFT JOIN LATERAL (
    SELECT * FROM decisions x WHERE x.run_id = r.run_id AND x.sku_id = r.sku_id AND x.gate = 2
    ORDER BY x.decided_at DESC, x.decision_id DESC LIMIT 1) g2 ON true
LEFT JOIN LATERAL (
    SELECT min(op.po_number) AS po_number FROM open_pos op
    WHERE g2.decided_at IS NOT NULL AND op.sku_id = r.sku_id AND op.run_id > r.run_id
      AND (op.po_date IS NULL OR op.po_date >= (g2.decided_at AT TIME ZONE 'Asia/Kolkata')::date)) po ON true;

-- Decision counts for the impact tab (acceptance and override rates).
CREATE OR REPLACE VIEW v_decision_stats WITH (security_invoker = true) AS
SELECT
    count(*) FILTER (WHERE NOT shadow)                                          AS live_decisions,
    count(*) FILTER (WHERE shadow)                                              AS shadow_decisions,
    count(*)                                                                    AS all_decisions,
    count(*) FILTER (WHERE decision IN ('accept', 'approve'))                   AS accepted,
    count(*) FILTER (WHERE decision IN ('change', 'override'))                  AS overridden
FROM decisions;

CREATE OR REPLACE VIEW v_outcome_totals WITH (security_invoker = true) AS
SELECT
    count(*) FILTER (WHERE event = 'stockout')                                  AS stockout_incidents,
    COALESCE(sum(units) FILTER (WHERE event = 'write_off'), 0)                  AS units_written_off
FROM outcomes_log;
