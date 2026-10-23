"""Write one row per SKU per run (map step 8) and the automatic outcome log entries (step 13)."""


def _round(v, n=2):
    return None if v is None else round(v, n)


def save_recommendations(conn, run_id: int, recs: list, explained: dict) -> None:
    rows = []
    for r in recs:
        e = explained[r.sku_id]
        rows.append((run_id, r.sku_id, r.history_weeks, _round(r.last4_avg), _round(r.avg12), _round(r.change_pct),
                     r.uplift, _round(r.demand_weekly), r.on_hand, r.open_po_qty, _round(r.cover_weeks),
                     r.min_weeks, r.max_weeks, r.flag_high_demand, r.flag_stockout, r.flag_overstock,
                     r.action, r.qty, r.qty_basis, _round(r.cover_after), r.exception_type,
                     e.reason, e.source, e.needs_judgment, e.why_unusual or None))
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO sku_recommendations (
                run_id, sku_id, history_weeks, last4_avg, avg12, change_pct, uplift, demand_weekly, on_hand,
                open_po_qty, cover_weeks, min_weeks, max_weeks, flag_high_demand, flag_stockout, flag_overstock,
                action, qty, qty_basis, cover_after, exception_type, reason, reason_source, needs_judgment, why_unusual)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, rows)


def log_stockouts(conn, run_id: int, recs: list, export) -> int:
    """A run that sees zero stock for an SKU logs a stockout incident for the latest complete week."""
    n = 0
    for r in recs:
        if r.on_hand == 0 and r.window_weeks:
            week = r.window_weeks[-1]
            exists = conn.execute("SELECT 1 FROM outcomes_log WHERE sku_id = %s AND iso_week = %s AND event = 'stockout'",
                                  (r.sku_id, week)).fetchone()
            if not exists:
                conn.execute("""INSERT INTO outcomes_log (sku_id, iso_week, event, units, source, run_id, logged_by, note)
                                VALUES (%s, %s, 'stockout', 0, 'auto', %s, 'system', %s)""",
                             (r.sku_id, week, run_id, f"On hand was 0 in export {export.export_id}"))
                n += 1
    return n
