"""Dashboard pages and forms, rendered against stand-in data built from the real seed run (no database)."""
import csv
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import auth, main, queries
from job import config, explain, ingest, rules, thresholds

SEED = config.DATA_DIR / "seed"
RUN_AS_OF = datetime(2026, 10, 19, 6, 0, tzinfo=ingest.IST)


def _seed_lines():
    with open(SEED / "sku_master.csv", newline="", encoding="utf-8") as f:
        master = {r["SKU_ID"]: r for r in csv.DictReader(f)}
    dist = {"DIST001": "NutriFlow Distributors", "DIST002": "GrainPath Logistics",
            "DIST003": "PeakSource Supply", "DIST004": "Meridian Trade Co."}
    export = ingest.from_folder(SEED, set(master), "2026-10-19_0600")
    t = thresholds.read().values
    lines = []
    for sku, m in master.items():
        r = rules.evaluate(rules.SkuInput(sku, m["Perishability"], int(m["Standard_Reorder_Qty"]),
                                          export.sales[sku], export.on_hand[sku], 0), RUN_AS_OF.date(), t)
        status = "awaiting_gate1" if r.exception_type else ("awaiting_gate2" if r.action in ("reorder", "pause") else "no_action")
        lines.append({**r.__dict__, "run_id": 1, "sku_name": m["SKU_Name"], "category": m["Category"],
                      "perishability": m["Perishability"], "standard_reorder_qty": int(m["Standard_Reorder_Qty"]),
                      "distributor_id": m["Distributor_ID"], "distributor_name": dist[m["Distributor_ID"]],
                      "reason": rules.template_reason(r), "reason_source": "template", "needs_judgment": bool(r.exception_type),
                      "why_unusual": None, "line_status": status, "final_qty": r.qty, "po_number": None,
                      **{f"gate{g}_{k}": None for g in (1, 2) for k in ("decision", "qty", "reason", "by", "at")}})
    order = lambda l: (0 if l["exception_type"] else 1 if l["flag_stockout"] else 2 if l["flag_overstock"] else 4,
                       (l["cover_weeks"] / l["min_weeks"]) if l["flag_stockout"] else
                       -(l["cover_weeks"] / l["max_weeks"]) if l["flag_overstock"] else l["cover_weeks"])
    return sorted(lines, key=order), export


LINES, EXPORT = _seed_lines()
RUN = {"run_id": 1, "run_as_of": RUN_AS_OF, "status": "ok", "status_detail": "Zoho: not_configured.", "shadow_mode": True,
       "data_as_of": EXPORT.data_as_of, "data_age_hours": 6.0, "festival_month": True, "uplift": 1.25,
       "export_id": EXPORT.export_id, "zoho_status": "not_configured", "reasons_source": "template", "triggered_by": "cron"}


class FakeConn:
    def __init__(self, recorded):
        self.recorded = recorded

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def commit(self): self.recorded.append("commit")

    def execute(self, sql, params=()):
        rows = [(l["sku_id"], l["sku_name"]) for l in sorted(LINES, key=lambda l: l["sku_id"])]
        return type("R", (), {"fetchall": lambda s: rows, "__iter__": lambda s: iter(rows)})()


@pytest.fixture
def app_client(monkeypatch):
    recorded = []
    monkeypatch.setattr(main.db, "connect", lambda: FakeConn(recorded))
    monkeypatch.setattr(queries, "run", lambda conn, run_id=None: RUN)
    monkeypatch.setattr(queries, "latest_run", lambda conn: RUN)
    monkeypatch.setattr(queries, "recent_runs", lambda conn, limit=20: [RUN])
    monkeypatch.setattr(queries, "all_runs", lambda conn, limit=15: [RUN])
    monkeypatch.setattr(queries, "filter_options", lambda conn: {"distributors": [], "categories": ["Snack"]})

    def lines(conn, run_id, flag=None, **f):
        rows = LINES
        if flag == "exception":
            rows = [l for l in rows if l["exception_type"]]
        return rows

    monkeypatch.setattr(queries, "lines", lines)
    monkeypatch.setattr(queries, "line", lambda conn, run_id, sku_id: next(l for l in LINES if l["sku_id"] == sku_id))
    monkeypatch.setattr(queries, "sku", lambda conn, sku_id: {**next(l for l in LINES if l["sku_id"] == sku_id),
                                                              "unit_of_measure": "Units"})
    monkeypatch.setattr(queries, "weekly_sales", lambda conn, export_id, sku_id:
                        [{"iso_week": w, "units_sold": u} for w, u in sorted(EXPORT.sales[sku_id].items())])
    monkeypatch.setattr(queries, "sku_history", lambda conn, sku_id, limit=20:
                        [{**next(l for l in LINES if l["sku_id"] == sku_id), "run_as_of": RUN_AS_OF, "shadow_mode": True}])
    monkeypatch.setattr(queries, "impact", lambda conn: {
        "stats": {"all_decisions": 4, "accepted": 3, "overridden": 1, "live_decisions": 0, "shadow_decisions": 4},
        "totals": {"stockout_incidents": 0, "units_written_off": 12},
        "counts": {"ok_runs": 1, "shadow_runs": 1, "stopped_runs": 0}, "most_overridden": [], "outcomes": []})
    monkeypatch.setattr(queries, "record_decision", lambda conn, **kw: recorded.append(("decision", kw)))
    monkeypatch.setattr(queries, "log_write_off", lambda conn, **kw: recorded.append(("write_off", kw)))
    c = TestClient(main.app, follow_redirects=False)
    c.recorded = recorded
    return c


def as_user(monkeypatch, role):
    monkeypatch.setenv("DP_DEV_USER", f"{role}@example.com,{role.title()},{role}")
    monkeypatch.delenv("VERCEL", raising=False)


def test_not_signed_in_goes_to_login(app_client, monkeypatch):
    monkeypatch.delenv("DP_DEV_USER", raising=False)
    r = app_client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Email me a sign-in link" in app_client.get("/login").text


def test_dev_login_never_works_on_vercel(app_client, monkeypatch):
    as_user(monkeypatch, "raghav")
    monkeypatch.setenv("VERCEL", "1")
    assert app_client.get("/").status_code == 303


def test_summary_tab_matches_golden_run(app_client, monkeypatch):
    as_user(monkeypatch, "deepa")
    html = app_client.get("/").text
    for n, label in [(15, "SKUs checked"), (5, "Stockout risk"), (2, "Overstock risk"), (1, "High demand"),
                     (1, "Exceptions for Raghav"), (5, "Reorder lines"), (8, "OK, no action")]:
        assert f'<div class="n' in html and f">{n}</div><div class=\"l\">{label}" in html
    assert "SHADOW MODE" in html and "Festival month: +25% uplift applied" in html
    assert html.index("SKU001") < html.index("SKU013") < html.index("SKU006") < html.index("SKU002")
    assert "Reorder <b>161</b>" in html and "topped up" in html


def test_exceptions_tab_and_gate1_only_for_raghav(app_client, monkeypatch):
    as_user(monkeypatch, "procurement")
    html = app_client.get("/exceptions").text
    assert "SKU001" in html and "Conflicting flags" in html and "Record decision" not in html
    as_user(monkeypatch, "raghav")
    html = app_client.get("/exceptions").text
    assert "Record decision" in html and "Accept 750" in html and "+21.4%" in html


def test_procurement_tab_lists_reorders_and_pauses(app_client, monkeypatch):
    as_user(monkeypatch, "procurement")
    html = app_client.get("/procurement").text
    for sku in ("SKU001", "SKU003", "SKU004", "SKU011", "SKU013", "SKU006", "SKU007"):
        assert sku in html
    assert "SKU002" not in html and "Waiting on Gate 1" in html and "Topped up" in html
    assert html.count('value="approve"') == 6     # SKU001 waits on Gate 1


def test_sku_tab_draws_chart_without_in_progress_week(app_client, monkeypatch):
    as_user(monkeypatch, "viewer")
    html = app_client.get("/sku/SKU001").text
    assert "<svg" in html and "12-week window" in html and "last 4 weeks" in html
    assert "2026-W27" in html and "2026-W42" in html and "+20% line 258" in html


def test_impact_tab(app_client, monkeypatch):
    as_user(monkeypatch, "deepa")
    html = app_client.get("/impact").text
    assert "75%" in html and "25%" in html and "₹18–22L" in html and "Draft · Deepa to confirm" in html
    assert "Raghav or procurement can log write-offs" in html


def test_gate1_change_needs_reason_and_qty(app_client, monkeypatch):
    as_user(monkeypatch, "raghav")
    form = {"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "change", "final_qty": "600", "reason": ""}
    r = app_client.post("/decide", data=form)
    assert r.status_code == 303 and "kind=err" in r.headers["location"] and "reason+is+required" in r.headers["location"]
    r = app_client.post("/decide", data={**form, "reason": "Promo ends this week"})
    assert "kind=ok" in r.headers["location"]
    kind, kw = app_client.recorded[0]
    assert kind == "decision" and kw["final_qty"] == 600 and kw["gate"] == 1 and kw["shadow"] is True
    assert kw["decided_by"] == "raghav@example.com"


def test_gate1_accept_uses_rules_qty(app_client, monkeypatch):
    as_user(monkeypatch, "raghav")
    app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "accept"})
    assert app_client.recorded[0][1]["final_qty"] == 750


def test_gate2_by_wrong_role_is_forbidden(app_client, monkeypatch):
    as_user(monkeypatch, "raghav")
    r = app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU013", "gate": 2, "decision": "approve"})
    assert r.status_code == 403 and app_client.recorded == []


def test_gate2_approve(app_client, monkeypatch):
    as_user(monkeypatch, "procurement")
    r = app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU013", "gate": 2, "decision": "approve"})
    assert "kind=ok" in r.headers["location"] and app_client.recorded[0][1]["final_qty"] == 161


def test_gate2_refuses_line_still_at_gate1(app_client, monkeypatch):
    as_user(monkeypatch, "procurement")
    r = app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 2, "decision": "approve"})
    assert "kind=err" in r.headers["location"] and app_client.recorded == []


def test_write_off_validation(app_client, monkeypatch):
    as_user(monkeypatch, "procurement")
    r = app_client.post("/outcomes", data={"sku_id": "SKU007", "iso_week": "W43", "units": 5})
    assert "kind=err" in r.headers["location"]
    r = app_client.post("/outcomes", data={"sku_id": "SKU007", "iso_week": "2026-W43", "units": 5})
    assert "kind=ok" in r.headers["location"] and app_client.recorded[0][0] == "write_off"


def test_admin_only_for_raghav_and_it(app_client, monkeypatch):
    as_user(monkeypatch, "deepa")
    assert app_client.get("/admin").status_code == 403
    as_user(monkeypatch, "it")
    assert "Upload an SAP export" in app_client.get("/admin").text
