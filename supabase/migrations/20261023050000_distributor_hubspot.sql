-- Each distributor's HubSpot company record, so the dashboard can link to it. Safe to re-run.
-- Filled once the companies exist in HubSpot; empty means "not linked yet".
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS hubspot_company_id TEXT;
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS hubspot_url TEXT;

-- v_lines selects r.* and named columns, so it has to be recreated to expose the link.
DROP VIEW IF EXISTS v_lines;
CREATE VIEW v_lines WITH (security_invoker = true) AS
SELECT
    r.*,
    m.sku_name, m.category, m.perishability, m.standard_reorder_qty, m.distributor_id, d.distributor_name,
    d.hubspot_url AS distributor_hubspot_url,
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
