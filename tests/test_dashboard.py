"""Dashboard pages and forms, rendered against stand-in data built from the real seed run (no database)."""
import csv
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, queries
from job import config, ingest, rules, thresholds

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
    monkeypatch.setattr(queries, "threshold_version", lambda conn, h: None)
    monkeypatch.setattr(queries, "previous_run", lambda conn, run_id: None)
    monkeypatch.setattr(queries, "last_zoho_export", lambda conn, run_id: None)
    monkeypatch.setattr(queries, "sales_by_sku", lambda conn, export_id:
                        {s: sorted(w.items()) for s, w in EXPORT.sales.items()})
    monkeypatch.setattr(queries, "data_counts", lambda conn, export_id: {
        "sku_master": 15, "sales_weekly": 240, "inventory_snapshot": 15, "distributors": 4, "festival_calendar": 24})
    monkeypatch.setattr(queries, "distributors", lambda conn: [
        {"distributor_id": d, "distributor_name": n, "region": "Maharashtra", "status": "Active", "hubspot_url": None}
        for d, n in [("DIST001", "NutriFlow Distributors"), ("DIST002", "GrainPath Logistics")]])
    monkeypatch.setattr(queries, "decision_log", lambda conn: [
        {"decided_at": RUN_AS_OF, "run_id": 1, "run_as_of": RUN_AS_OF, "sku_id": "SKU001", "sku_name": "Freshroots Mango Drink 200ml",
         "gate": 1, "decision": "change", "final_qty": 600, "reason": "festival stock already on the way",
         "decided_by": "Deepa Nair (dashboard)", "shadow": True}])
    monkeypatch.setattr(queries, "record_zoho_export", lambda conn, **kw: recorded.append(("zoho_export", kw)))
    monkeypatch.setattr(main.thresholds, "load", lambda conn, ts: (3, True))
    monkeypatch.setattr(main.thresholds, "sign", lambda conn, v, by, actor=None: recorded.append(("sign", v, by, actor)))
    monkeypatch.setattr(queries, "record_decision", lambda conn, **kw: recorded.append(("decision", kw)))
    monkeypatch.setattr(queries, "log_write_off", lambda conn, **kw: recorded.append(("write_off", kw)))
    c = TestClient(main.app, follow_redirects=False)
    c.recorded = recorded
    return c


def rows_in(html):
    return html.count("<svg viewBox=\"0 0 240 26\"")      # one cover bar per SKU row


def test_overview_is_deepas_and_has_no_role_picker(app_client):
    html = app_client.get("/").text
    assert "Deepa Nair, Head of Operations" in html and "Acting as" not in html
    assert app_client.post("/role", data={"role": "raghav"}).status_code in (404, 405)
    assert app_client.get("/login").status_code == 404


def test_overview_needs_your_decision(app_client):
    html = app_client.get("/").text
    assert "Review 7 decisions" in html and '<span class="badge">7</span>' in html
    assert "SKU001" in html and "Exceptions needing your judgment" not in html  # singular: 1 exception
    assert ">1</div><div class=\"l\">Exception needing your judgment" in html
    assert ">6</div><div class=\"l\">Reorder / pause lines to approve" in html
    assert "Rules not signed off" in html


def test_overview_tiles_match_golden_run(app_client):
    html = app_client.get("/").text
    for n, label in [(5, "Stockout risk"), (2, "Overstock risk"), (1, "High demand"), (1, "Exceptions"),
                     (5, "Reorder lines"), (8, "OK, no action")]:
        assert f">{n}</div><div class=\"l\">{label}<" in html
    assert "1,870 units in total" in html                      # 750 + 161 + 500 + 150 + 309
    assert rows_in(html) == 15
    table = html[html.index('id="stock"'):]
    assert table.index("SKU001") < table.index("SKU013") < table.index("SKU006") < table.index("SKU002")
    assert "This is the first run" in html


def test_overview_filters(app_client):
    assert rows_in(app_client.get("/?show=action").text) == 7
    assert rows_in(app_client.get("/?show=stockout").text) == 5
    assert rows_in(app_client.get("/?show=ok").text) == 8


def test_overview_changes_since_last_run(app_client, monkeypatch):
    prev = {**RUN, "run_id": 0}
    before = [{**l, "flag_stockout": False, "exception_type": None, "flag_high_demand": False}
              if l["sku_id"] == "SKU013" else l for l in LINES]
    before = [{**l, "flag_stockout": True} if l["sku_id"] == "SKU002" else l for l in before]
    monkeypatch.setattr(queries, "previous_run", lambda conn, run_id: prev)
    monkeypatch.setattr(queries, "lines", lambda conn, run_id, flag=None, **f: before if run_id == 0 else LINES)
    html = app_client.get("/").text
    assert ">1</div><div class=\"l\">Newly flagged</div><div class=\"s\">SKU013" in html
    assert ">1</div><div class=\"l\">Back to OK</div><div class=\"s\">SKU002" in html
    assert ">6</div><div class=\"l\">Still flagged" in html


def test_decisions_page_has_both_gates(app_client):
    html = app_client.get("/decisions").text
    assert '<span class="num">1</span>Exceptions' in html and '<span class="num">2</span>Approve reorders and pauses' in html
    assert "Record decision" in html and "Accept and approve 750" in html and "+21.4%" in html
    assert html.count('name="gate" value="2"') == 6
    assert "Approve all 6 as recommended" in html and "topped up from 120" in html


def test_old_tab_addresses_redirect(app_client):
    for old in ("/exceptions", "/procurement?run=1"):
        r = app_client.get(old)
        assert r.status_code == 307 and r.headers["location"].startswith("/decisions")


def test_gate1_change_needs_reason_and_qty(app_client):
    form = {"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "change", "final_qty": "600", "reason": ""}
    r = app_client.post("/decide", data=form)
    assert r.status_code == 303 and "kind=err" in r.headers["location"] and "reason+is+required" in r.headers["location"]
    r = app_client.post("/decide", data={**form, "reason": "Promo ends this week"})
    assert "kind=ok" in r.headers["location"]
    kind, kw = app_client.recorded[0]
    assert kind == "decision" and kw["final_qty"] == 600 and kw["gate"] == 1 and kw["shadow"] is True
    assert kw["decided_by"] == "Deepa Nair (dashboard)"
    assert app_client.recorded[1][1]["gate"] == 2 and app_client.recorded[1][1]["decided_by"] == "Deepa Nair (dashboard)"


def decisions_made(client):
    return [(kw["gate"], kw["decision"], kw["final_qty"]) for x in client.recorded if isinstance(x, tuple)
            for kind, kw in [x] if kind == "decision"]


def test_gate1_accept_also_approves_in_one_step(app_client):
    r = app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "accept"})
    assert decisions_made(app_client) == [(1, "accept", 750), (2, "approve", 750)]
    assert "accepted+and+approved+%28order+750%29" in r.headers["location"]


def test_gate1_change_also_approves_the_new_qty(app_client):
    app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "change",
                                     "final_qty": "600", "reason": "Promo ends this week"})
    assert decisions_made(app_client) == [(1, "change", 600), (2, "approve", 600)]


def test_gate1_override_approves_nothing(app_client):
    app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "override",
                                     "reason": "Competitor scheme ending"})
    assert decisions_made(app_client) == [(1, "override", 0)]


def test_gate1_accept_with_no_order_approves_nothing(app_client, monkeypatch):
    new_sku = {**next(l for l in LINES if l["sku_id"] == "SKU001"), "exception_type": "new_sku", "action": "review",
               "qty": 0, "final_qty": 0}
    monkeypatch.setattr(queries, "line", lambda conn, run_id, sku_id: new_sku)
    app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 1, "decision": "accept"})
    assert decisions_made(app_client) == [(1, "accept", 0)]


def test_approved_pause_label(app_client, monkeypatch):
    done = [{**l, "line_status": "approved", "gate2_decision": "approve"} if l["sku_id"] in ("SKU006", "SKU013") else l
            for l in LINES]
    monkeypatch.setattr(queries, "lines", lambda conn, run_id, flag=None, **f: done)
    html = app_client.get("/decisions").text
    assert "Approved · skip the next order" in html and "Approved · raise PO in Zoho" in html
    assert html.count("Approved · skip the next order") == 1


def test_gate2_approve(app_client):
    r = app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU013", "gate": 2, "decision": "approve"})
    assert "kind=ok" in r.headers["location"] and app_client.recorded[0][1]["final_qty"] == 161


def test_gate2_refuses_line_still_at_gate1(app_client):
    r = app_client.post("/decide", data={"run_id": 1, "sku_id": "SKU001", "gate": 2, "decision": "approve"})
    assert "kind=err" in r.headers["location"] and app_client.recorded == []


def test_approve_all_ready_lines(app_client):
    r = app_client.post("/decide/approve-all", data={"run_id": 1})
    assert "Approved+6+lines" in r.headers["location"]
    approved = {kw["sku_id"]: kw["final_qty"] for kind, kw in (x for x in app_client.recorded if isinstance(x, tuple)) if kind == "decision"}
    assert approved == {"SKU013": 161, "SKU011": 500, "SKU004": 150, "SKU003": 309, "SKU006": 0, "SKU007": 0}
    assert all(kw["gate"] == 2 and kw["decision"] == "approve" for kind, kw in (x for x in app_client.recorded if isinstance(x, tuple)) if kind == "decision")


def test_sku_page_draws_chart_and_cover_bar(app_client):
    html = app_client.get("/sku/SKU001").text
    assert "<svg" in html and "12-week window" in html and "last 4 weeks" in html and "+20% line 258" in html
    assert rows_in(html) == 1 and "Decide on Decisions" in html


def test_sign_off(app_client):
    html = app_client.get("/impact").text
    assert "Not signed off" in html and "Runs stop until you sign off" in html
    h = main.thresholds.read().content_hash
    r = app_client.post("/thresholds/sign", data={"content_hash": h})
    assert "kind=ok" in r.headers["location"]
    assert app_client.recorded[0] == ("sign", 3, "Deepa Nair", "dashboard")


def test_sign_off_refuses_stale_page(app_client):
    r = app_client.post("/thresholds/sign", data={"content_hash": "old"})
    assert "kind=err" in r.headers["location"] and app_client.recorded == []


def test_impact_tab(app_client):
    html = app_client.get("/impact").text
    assert "75%" in html and "25%" in html and "₹18–22L" in html and "Draft, yours to confirm" in html


def test_write_off_validation(app_client):
    r = app_client.post("/outcomes", data={"sku_id": "SKU007", "iso_week": "W43", "units": 5})
    assert "kind=err" in r.headers["location"]
    r = app_client.post("/outcomes", data={"sku_id": "SKU007", "iso_week": "2026-W43", "units": 5})
    assert "kind=ok" in r.headers["location"] and app_client.recorded[0][0] == "write_off"
    assert app_client.recorded[0][1]["logged_by"] == "Deepa Nair (dashboard)"


def test_data_and_runs_page(app_client):
    html = app_client.get("/admin").text
    assert "Upload an SAP export" in html and "Run now" in html and "Recent runs" in html


def test_connections_panel(app_client, monkeypatch):
    monkeypatch.setattr(main.connections, "full", lambda conn: [
        {"name": "Supabase database", "status": "ok", "detail": "connected · 15 SKUs in the master", "fix": ""},
        {"name": "Zoho Inventory (open POs)", "status": "missing", "detail": "not connected", "fix": "Run tools/zoho_setup.py"}])
    html = app_client.get("/admin").text
    assert 'id="connections"' in html and "Supabase database" in html and "working" in html
    assert "not set up" in html and "What to do: Run tools/zoho_setup.py" in html


def test_header_counts_missing_connections(app_client, monkeypatch):
    for k in ("CRON_SECRET", "ANTHROPIC_API_KEY", "SMTP_HOST", "ZOHO_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert "4 connections not set up" in app_client.get("/").text
    monkeypatch.setattr(main.connections, "missing_count", lambda: 0)
    assert "All connections working" in app_client.get("/").text


def test_overview_headline_and_progress(app_client):
    html = app_client.get("/").text
    assert "7 of 15 SKUs need attention" in html and "7 decisions waiting" in html
    assert "0 of 7 lines decided this run" in html
    assert "Exception 1" in html and "Stockout risk 4" in html and "Overstock risk 2" in html and "OK 8" in html


def approved(skus):
    return [{**l, "line_status": "approved", "gate2_decision": "approve", "gate2_at": RUN_AS_OF, "gate2_by": "Deepa Nair (dashboard)"}
            if l["sku_id"] in skus else l for l in LINES]


def test_days_of_stock_left(app_client):
    html = app_client.get("/").text
    assert "Days of stock" in html
    # SKU011: 460 on hand / (204.3 x 1.25 / 7 per day) = 13 days; SKU002: 1,200 / (83.8 x 1.25 / 7) = 80 days
    assert '<b style="color:var(--red)">13</b> <span class="mut">days</span>' in html
    assert '<b style="color:inherit">80</b>' in html


def test_large_order_check_and_approve_all_skips_it(app_client, monkeypatch):
    big = [{**l, "qty": 300, "final_qty": 300} if l["sku_id"] == "SKU013" else l for l in LINES]   # 2.5x std 120
    monkeypatch.setattr(queries, "lines", lambda conn, run_id, flag=None, **f: big)
    html = app_client.get("/decisions").text
    assert "large order: 2.5&times; standard" in html and "Approve 5 as recommended" in html
    app_client.post("/decide/approve-all", data={"run_id": 1})
    skus = {kw["sku_id"] for x in app_client.recorded if isinstance(x, tuple) and x[0] == "decision" for kw in [x[1]]}
    assert "SKU013" not in skus and len(skus) == 5
    assert "1 order to check one by one" in app_client.get("/").text


def test_zoho_export_csv(app_client, monkeypatch):
    monkeypatch.setattr(queries, "lines", lambda conn, run_id, flag=None, **f: approved({"SKU013", "SKU003", "SKU006"}))
    html = app_client.get("/decisions").text
    assert "2 approved reorder lines" in html and "Download for Zoho (CSV)" in html
    r = app_client.get("/decisions/zoho.csv?run=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="freshroots_run1_zoho_po_lines.csv"' in r.headers["content-disposition"]
    rows = r.text.strip().splitlines()
    assert rows[0].startswith("SKU,Item Name,Quantity")
    assert len(rows) == 3 and any(row.startswith("SKU013,Freshroots Peanut Butter 300g,161,") for row in rows)
    assert not any(row.startswith("SKU006") for row in rows)          # a pause isn't a PO
    assert ("zoho_export", {"run_id": 1, "lines": 2, "units": 470, "exported_by": "Deepa Nair (dashboard)"}) in app_client.recorded


def test_pipeline_strip_and_briefing(app_client):
    html = app_client.get("/").text
    for label in ("SAP data", "Plan run", "Rules sign-off", "Your decisions", "Zoho export"):
        assert f"</span>{label}</div>" in html
    assert "6 h old" in html and "7 of 15 flagged" in html and "0 of 7 decided" in html and "approve lines first" in html
    assert "Look at these first" in html
    assert "Multigrain Bread 400g and Almond Milk 500ml: 13 days of stock on hand" in html
    assert "Mango Drink 200ml is up 21%" in html
    assert "Pause 2 orders: about 420 units above the maximum" in html


def test_overview_table_has_search_sort_sparklines_and_trend(app_client):
    html = app_client.get("/").text
    assert 'id="sku-q"' in html and html.count('class="sort"') == 5
    assert html.count('aria-label="16 weeks of units sold') == 15
    assert ">+21%</span>" in html                              # SKU001, high demand
    assert "5 of 15 SKUs are below their stock window in a festival month" in html


def test_briefing_cards_approve_in_one_click(app_client):
    html = app_client.get("/").text
    assert "Approve 2 reorders" in html and "Approve 2 pauses" in html
    assert "Approve 750" not in html                           # SKU001 is still an open exception
    r = app_client.post("/decide/approve-selected", data={"run_id": 1, "sku_id": ["SKU006", "SKU007"], "back": "/"})
    assert r.status_code == 303 and r.headers["location"].startswith("/?")
    approved = [x[1] for x in app_client.recorded if isinstance(x, tuple) and x[0] == "decision"]
    assert {d["sku_id"] for d in approved} == {"SKU006", "SKU007"} and all(d["gate"] == 2 for d in approved)


def test_approve_selected_skips_lines_not_waiting_or_needing_a_look(app_client):
    r = app_client.post("/decide/approve-selected", data={"run_id": 1, "sku_id": ["SKU001", "SKU002"],
                                                          "back": "https://example.com"})
    approved = {x[1]["sku_id"] for x in app_client.recorded if isinstance(x, tuple) and x[0] == "decision"}
    assert approved == set() and r.headers["location"].startswith("/decisions?")   # SKU001 is an exception, SKU002 OK


def test_decisions_groups_stockout_first_with_why(app_client, monkeypatch):
    done = [{**l, "line_status": "awaiting_gate2"} if l["exception_type"] else l for l in LINES]
    monkeypatch.setattr(queries, "lines", lambda conn, run_id, flag=None, **f: done)
    html = app_client.get("/decisions").text
    urgent, later = html.index('id="urgent"'), html.index('id="next"')
    assert urgent < html.index("SKU013", urgent) < later < html.index("SKU006", later)
    assert "3.2 weeks of cover, below the 6-week minimum." in html
    assert "above the 20% trigger" in html and 'form="bulk"' in html and "Approve the ticked lines" in html


def test_reorder_left_above_the_maximum_needs_a_look(app_client, monkeypatch):
    over = [{**l, "cover_after": 4.9} if l["sku_id"] == "SKU011" else l for l in LINES]     # perishable, max 4
    monkeypatch.setattr(queries, "lines", lambda conn, run_id, flag=None, **f: over)
    html = app_client.get("/decisions").text
    assert "4.9 w after: above the 4-w max" in html and 'value="SKU011" form="bulk"' not in html
    app_client.post("/decide/approve-all", data={"run_id": 1})
    assert "SKU011" not in {x[1]["sku_id"] for x in app_client.recorded if isinstance(x, tuple) and x[0] == "decision"}


def test_decision_log_csv(app_client):
    r = app_client.get("/decisions/log.csv")
    assert r.status_code == 200 and "freshroots_decision_log.csv" in r.headers["content-disposition"]
    rows = list(csv.reader(r.text.splitlines()))
    assert rows[0] == main.LOG_COLUMNS
    assert rows[1][3:9] == ["SKU001", "Freshroots Mango Drink 200ml", "1 exception", "change", "600",
                            "festival stock already on the way"] and rows[1][-1] == "yes"


def test_rules_routing_and_owners_tables(app_client):
    html = app_client.get("/impact").text
    assert "Raghav's rule 4" in html and "Added rule A · Raghav to confirm" in html
    assert "Order would leave stock above the maximum" in html and "Raghav Kulkarni" in html and "Action owner" in html


def test_data_used_and_distributors(app_client):
    html = app_client.get("/admin").text
    assert "Data used in run 1" in html and ">240</td>" in html and "Zoho Inventory, read-only" in html
    assert "NutriFlow Distributors" in html and "Mango Drink 200ml" in html
