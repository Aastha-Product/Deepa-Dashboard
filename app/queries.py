"""Reads and writes behind the dashboard. Everything goes through the views in the dashboard migration."""
from decimal import Decimal

from psycopg.rows import dict_row

FLAG_FILTERS = {
    "stockout": "flag_stockout", "overstock": "flag_overstock", "high_demand": "flag_high_demand",
    "exception": "exception_type IS NOT NULL",
    "ok": "NOT (flag_stockout OR flag_overstock OR flag_high_demand OR exception_type IS NOT NULL)",
}


def _plain(row):
    # NUMERIC comes back as Decimal; templates mix it with floats, so hand them floats.
    return None if row is None else {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def _all(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return [_plain(r) for r in cur.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return _plain(cur.execute(sql, params).fetchone())


def latest_run(conn):
    return _one(conn, "SELECT * FROM runs WHERE status <> 'running' ORDER BY run_id DESC LIMIT 1")


def run(conn, run_id=None):
    if run_id:
        return _one(conn, "SELECT * FROM runs WHERE run_id = %s AND status = 'ok'", (run_id,))
    return _one(conn, "SELECT * FROM runs WHERE status = 'ok' ORDER BY run_id DESC LIMIT 1")


def all_runs(conn, limit=15):
    return _all(conn, "SELECT * FROM runs ORDER BY run_id DESC LIMIT %s", (limit,))


def previous_run(conn, run_id):
    return _one(conn, "SELECT * FROM runs WHERE status = 'ok' AND run_id < %s ORDER BY run_id DESC LIMIT 1", (run_id,))


def recent_runs(conn, limit=20):
    return _all(conn, "SELECT run_id, run_as_of, status, shadow_mode FROM runs WHERE status = 'ok' "
                      "ORDER BY run_id DESC LIMIT %s", (limit,))


def filter_options(conn):
    return {
        "distributors": _all(conn, "SELECT distributor_id, distributor_name FROM distributors ORDER BY 1"),
        "categories": [r["category"] for r in _all(conn, "SELECT DISTINCT category FROM sku_master ORDER BY 1")],
    }


def lines(conn, run_id, flag=None, perishability=None, distributor=None, category=None):
    where, params = ["run_id = %s"], [run_id]
    if flag in FLAG_FILTERS:
        where.append(FLAG_FILTERS[flag])
    if perishability in ("Low", "High"):
        where.append("perishability = %s"), params.append(perishability)
    if distributor:
        where.append("distributor_id = %s"), params.append(distributor)
    if category:
        where.append("category = %s"), params.append(category)
    return _all(conn, f"SELECT * FROM v_lines WHERE {' AND '.join(where)} ORDER BY sort_group, sort_key, sku_id",
                params)


def tiles(rows):
    return {
        "skus": len(rows),
        "stockout": sum(r["flag_stockout"] for r in rows),
        "overstock": sum(r["flag_overstock"] for r in rows),
        "high_demand": sum(r["flag_high_demand"] for r in rows),
        "exceptions": sum(r["exception_type"] is not None for r in rows),
        "reorder_lines": sum(r["action"] == "reorder" for r in rows),
        "ok": sum(not (r["flag_stockout"] or r["flag_overstock"] or r["flag_high_demand"] or r["exception_type"])
                  for r in rows),
    }


def line(conn, run_id, sku_id):
    return _one(conn, "SELECT * FROM v_lines WHERE run_id = %s AND sku_id = %s", (run_id, sku_id))


def sku(conn, sku_id):
    return _one(conn, """SELECT m.*, d.distributor_name, d.hubspot_url AS distributor_hubspot_url FROM sku_master m
                         JOIN distributors d ON d.distributor_id = m.distributor_id WHERE m.sku_id = %s""", (sku_id,))


def weekly_sales(conn, export_id, sku_id):
    return _all(conn, "SELECT iso_week, units_sold FROM sales_weekly WHERE export_id = %s AND sku_id = %s "
                      "ORDER BY iso_week", (export_id, sku_id))


def sku_history(conn, sku_id, limit=20):
    return _all(conn, """SELECT l.*, r.run_as_of, r.shadow_mode FROM v_lines l JOIN runs r USING (run_id)
                         WHERE l.sku_id = %s ORDER BY l.run_id DESC LIMIT %s""", (sku_id, limit))


def open_pos(conn, run_id):
    return _all(conn, "SELECT * FROM open_pos WHERE run_id = %s ORDER BY sku_id, po_number", (run_id,))


def impact(conn):
    stats = _one(conn, "SELECT * FROM v_decision_stats")
    totals = _one(conn, "SELECT * FROM v_outcome_totals")
    counts = _one(conn, """SELECT count(*) FILTER (WHERE status = 'ok') AS ok_runs,
                                  count(*) FILTER (WHERE status = 'ok' AND shadow_mode) AS shadow_runs,
                                  count(*) FILTER (WHERE status IN ('stopped_stale', 'stopped_unsigned', 'failed'))
                                      AS stopped_runs FROM runs""")
    most_overridden = _all(conn, """SELECT sku_id, count(*) AS n FROM decisions
                                    WHERE decision IN ('change', 'override') GROUP BY sku_id ORDER BY n DESC, sku_id
                                    LIMIT 5""")
    outcomes = _all(conn, "SELECT * FROM outcomes_log ORDER BY logged_at DESC LIMIT 20")
    return {"stats": stats, "totals": totals, "counts": counts, "most_overridden": most_overridden,
            "outcomes": outcomes}


def threshold_version(conn, content_hash):
    return _one(conn, "SELECT version_id, loaded_at, signed_off_by, signed_off_at FROM threshold_versions "
                      "WHERE content_hash = %s", (content_hash,))


def last_zoho_export(conn, run_id):
    return _one(conn, "SELECT * FROM zoho_exports WHERE run_id = %s ORDER BY exported_at DESC LIMIT 1", (run_id,))


def record_zoho_export(conn, *, run_id, lines, units, exported_by):
    conn.execute("INSERT INTO zoho_exports (run_id, lines, units, exported_by) VALUES (%s, %s, %s, %s)",
                 (run_id, lines, units, exported_by))


def record_decision(conn, *, run_id, sku_id, gate, decided_by, decision, final_qty, reason, shadow):
    """Caller commits. The database enforces gate/decision pairs and the required reason."""
    conn.execute("""INSERT INTO decisions (run_id, sku_id, gate, decided_by, decision, final_qty, reason, shadow)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                 (run_id, sku_id, gate, decided_by, decision, final_qty, (reason or "").strip() or None, shadow))


def log_write_off(conn, *, sku_id, iso_week, units, logged_by, note):
    conn.execute("""INSERT INTO outcomes_log (sku_id, iso_week, event, units, source, logged_by, note)
                    VALUES (%s, %s, 'write_off', %s, 'manual', %s, %s)""",
                 (sku_id, iso_week, units, logged_by, (note or "").strip() or None))
