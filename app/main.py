"""FastAPI app on Vercel: the cron endpoint (map step 2) and Deepa's dashboard (steps 9-13).

Deepa Nair (Head of Operations) checks the dashboard after each Mon + Thu run. She decides the exceptions
(Gate 1), approves the reorder and pause lines (Gate 2), signs off the rules and logs write-offs.
"""
import csv
import hmac
import io
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import auth, connections, queries
from job import db, ingest, run as job_run, storage, thresholds

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
app = FastAPI(title="Freshroots Demand Planning", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- template filters -------------------------------------------------------------------------

def _ist(value):
    if not value:
        return "—"
    return value.astimezone(ingest.IST).strftime("%a %d %b %Y, %H:%M") if isinstance(value, datetime) else str(value)


def _num(value, digits=1):
    return "—" if value is None else f"{float(value):,.{digits}f}"


templates.env.filters.update(
    ist=_ist,
    day=lambda v: v.astimezone(ingest.IST).strftime("%a %d %b") if isinstance(v, datetime) else str(v or "—"),
    fmt1=lambda v: _num(v, 1),
    fmtw=lambda v: _num(v, 1),
    wk=lambda v: "—" if v is None else f"{float(v):g}",      # window limits: 6, 14, 3, 4
    pct=lambda v: "—" if v is None else f"{float(v):+.1f}%",
    num=lambda v: "—" if v is None else f"{int(v):,}",
    rate=lambda n, d: "—" if not d else f"{100 * n / d:.0f}%",
    short=lambda name: (name or "").replace("Freshroots ", ""),
)


# --- helpers ----------------------------------------------------------------------------------

def _page(request, name, conn, tab, run, **ctx):
    params = request.query_params
    pending = _pending(_lines(conn, run["run_id"])) if run else {"gate1": 0, "gate2": 0}
    return templates.TemplateResponse(request, name, {
        "user": auth.current_user(), "tab": tab, "run": run, "latest": queries.latest_run(conn),
        "pending": pending, "run_param": "run" in params, "not_connected": connections.missing_count(),
        "flash": params.get("msg"), "flash_kind": params.get("kind", "ok"), **ctx})


def _pending(lines):
    return {"gate1": sum(l["line_status"] == "awaiting_gate1" for l in lines),
            "gate2": sum(l["line_status"] == "awaiting_gate2" for l in lines)}


def _selected_run(conn, request):
    rid = request.query_params.get("run")
    return queries.run(conn, int(rid)) if rid and rid.isdigit() else queries.run(conn)


def _back(path, msg, kind="ok", **extra):
    return RedirectResponse(f"{path}?{urlencode({**extra, 'msg': msg, 'kind': kind})}", status_code=303)


LARGE_ORDER_MULTIPLE = 2      # dashboard check only: an order of 2x standard or more gets a deliberate look


def _enrich(l):
    demand = l.get("demand_weekly")
    l["days_left"] = round(l["on_hand"] * 7 / demand) if demand else None      # on-hand stock only
    qty = l.get("final_qty") if l.get("final_qty") is not None else l.get("qty", 0)
    l["large_order"] = l["action"] == "reorder" and bool(l.get("standard_reorder_qty"))         and qty >= LARGE_ORDER_MULTIPLE * l["standard_reorder_qty"]
    return l


def _lines(conn, run_id, **f):
    return [_enrich(dict(l)) for l in queries.lines(conn, run_id, **f)]


def _flagged(l):
    return bool(l["flag_stockout"] or l["flag_overstock"] or l["flag_high_demand"] or l["exception_type"])


def _changes(lines, prev_lines):
    """What changed since the previous run, for Deepa's twice-weekly check."""
    if prev_lines is None:
        return None
    before = {l["sku_id"]: _flagged(l) for l in prev_lines}
    now = {l["sku_id"]: _flagged(l) for l in lines}
    return {
        "new": [l for l in lines if now[l["sku_id"]] and not before.get(l["sku_id"], False)],
        "cleared": [l for l in prev_lines if before[l["sku_id"]] and not now.get(l["sku_id"], False)],
        "still": sum(now[s] and before.get(s, False) for s in now),
    }


def _pipeline(run, latest, lines, version, last_export):
    """SAP data -> plan run -> rules signed -> decisions -> Zoho export, each with a status."""
    if not run:
        return []
    age = run.get("data_age_hours")
    to_decide = [l for l in lines if l["exception_type"] or l["action"] in ("reorder", "pause")]
    waiting = sum(l["line_status"] in ("awaiting_gate1", "awaiting_gate2") for l in to_decide)
    ready = _approved_reorders(lines)
    stopped = latest and latest["status"] != "ok" and latest["run_id"] > run["run_id"]
    exported = last_export and last_export["lines"] >= len(ready) and ready
    flagged = sum(_flagged(l) for l in lines)
    return [
        {"label": "SAP data", "status": "done" if age is not None and age <= 48 else "block", "href": "/admin",
         "value": f"{age:.0f} h old" if age is not None else "—", "detail": f"stock as of {run['data_as_of']}"},
        {"label": "Plan run", "status": "block" if stopped else "done", "href": "/",
         "value": f"{flagged} of {len(lines)} flagged", "detail": "last run stopped" if stopped else f"run {run['run_id']}"},
        {"label": "Rules sign-off", "status": "done" if version and version.get("signed_off_by") else "block",
         "href": "/impact", "value": f"version {version['version_id']}" if version else "not loaded",
         "detail": "signed off" if version and version.get("signed_off_by") else "runs stop until signed"},
        {"label": "Your decisions", "status": "wait" if waiting else "done", "href": "/decisions",
         "value": f"{len(to_decide) - waiting} of {len(to_decide)} decided", "detail": f"{waiting} waiting" if waiting else "all decided"},
        {"label": "Zoho export", "status": "done" if exported else ("wait" if ready else ""), "href": "/decisions#zoho",
         "value": f"{last_export['lines']} lines" if exported else (f"{len(ready)} ready" if ready else "nothing yet"),
         "detail": "PO raised by hand in Zoho" if exported else ("download the CSV" if ready else "approve lines first")},
    ]


def _briefing(lines, changes):
    """The few things to look at first, from the numbers only (no AI, nothing invented)."""
    out = []
    stock = sorted((l for l in lines if l["flag_stockout"] and l["days_left"] is not None), key=lambda l: l["days_left"])
    if stock:
        low = [l for l in stock if l["days_left"] == stock[0]["days_left"]]
        names = " and ".join(l["sku_name"].replace("Freshroots ", "") for l in low)
        open_ = sum(l["line_status"] in ("awaiting_gate1", "awaiting_gate2") for l in low)
        out.append({"title": f"{names}: {stock[0]['days_left']} days of stock on hand",
                    "detail": f"{low[0]['cover_weeks']:.1f} weeks of cover against a {low[0]['min_weeks']:g}-week minimum. "
                              + ("Reorder waiting for you." if open_ else "Reorder approved; raise the PO in Zoho."),
                    "href": "/decisions" if open_ else "/decisions#zoho", "kind": "so"})
    for l in (l for l in lines if l["flag_high_demand"]):
        out.append({"title": f"{l['sku_name'].replace('Freshroots ', '')} is up {l['change_pct']:.0f}%",
                    "detail": f"Last 4 weeks {l['last4_avg']:.0f}/wk against {l['avg12']:.0f}/wk over 12 weeks, "
                              f"so the reorder is 1.5x standard ({l['qty']} units).",
                    "href": f"/sku/{l['sku_id']}", "kind": "hd"})
    over = [l for l in lines if l["flag_overstock"]]
    if over:
        excess = sum(max(l["on_hand"] - l["max_weeks"] * l["demand_weekly"], 0) for l in over if l["demand_weekly"])
        out.append({"title": f"Pause {len(over)} order{'s' if len(over) != 1 else ''}: about {excess:,.0f} units above the maximum",
                    "detail": "; ".join(f"{l['sku_name'].replace('Freshroots ', '')} {l['cover_weeks']:.1f} w (max {l['max_weeks']:g})"
                                        for l in over) + ".", "href": "/?show=overstock", "kind": "ov"})
    large = [l for l in lines if l["large_order"]]
    if large:
        out.append({"title": f"{len(large)} large order{'s' if len(large) != 1 else ''} to check",
                    "detail": ", ".join(f"{l['sku_id']} {l['final_qty']} ({l['final_qty'] / l['standard_reorder_qty']:.1f}x standard)"
                                        for l in large) + ". Approve these one by one.", "href": "/decisions", "kind": "ex"})
    if changes and changes["new"]:
        out.append({"title": f"{len(changes['new'])} SKU{'s' if len(changes['new']) != 1 else ''} newly flagged since the last run",
                    "detail": ", ".join(l["sku_id"] for l in changes["new"]) + ".", "href": "/?show=action", "kind": "ex"})
    return out[:4]


# --- cron + health ----------------------------------------------------------------------------

def _check_cron_secret(authorization: str | None) -> None:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/health")
def health():
    """Up, plus which connections are configured (booleans only, no details leaked)."""
    q = connections.quick()
    status = {"ok": True, "cron_secret": q["cron"], "zoho": q["zoho"], "claude": q["claude"], "email": q["email"]}
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
        status["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("health: database check failed: %s", type(exc).__name__)
        status["db"] = "unavailable"
    return status


@app.get("/api/run")
def scheduled_run(authorization: str | None = Header(default=None)):
    """Called by Vercel cron (Mon + Thu 06:00 IST) with Authorization: Bearer <CRON_SECRET>."""
    _check_cron_secret(authorization)
    with db.connect() as conn:
        summary = job_run.execute_and_notify(conn, triggered_by="cron")
    return summary.as_dict()


# --- Deepa's pages ----------------------------------------------------------------------------

FILTERS = {"all": "All SKUs", "action": "Needs action", "stockout": "Stockout risk", "overstock": "Overstock risk",
           "ok": "OK"}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    show = request.query_params.get("show", "all")
    with db.connect() as conn:
        run = _selected_run(conn, request)
        lines = _lines(conn, run["run_id"]) if run else []
        prev = queries.previous_run(conn, run["run_id"]) if run else None
        changes = _changes(lines, _lines(conn, prev["run_id"]) if prev else None)
        shown = {
            "action": [l for l in lines if l["line_status"] in ("awaiting_gate1", "awaiting_gate2")],
            "stockout": [l for l in lines if l["flag_stockout"]],
            "overstock": [l for l in lines if l["flag_overstock"]],
            "ok": [l for l in lines if not _flagged(l)],
        }.get(show, lines)
        ts = thresholds.read()
        group = lambda l: ("exception" if l["exception_type"] else "stockout" if l["flag_stockout"] else
                           "overstock" if l["flag_overstock"] else "high_demand" if l["flag_high_demand"] else "ok")
        groups = {g: sum(group(l) == g for l in lines) for g in ("exception", "stockout", "overstock", "high_demand", "ok")}
        to_decide = [l for l in lines if l["exception_type"] or l["action"] in ("reorder", "pause")]
        decided = sum(l["line_status"] not in ("awaiting_gate1", "awaiting_gate2") for l in to_decide)
        version = queries.threshold_version(conn, ts.content_hash)
        pipeline = _pipeline(run, queries.latest_run(conn), lines, version,
                             queries.last_zoho_export(conn, run["run_id"]) if run else None)
        return _page(request, "overview.html", conn, "overview", run, lines=shown, all_lines=lines,
                     pipeline=pipeline, briefing=_briefing(lines, changes),
                     groups=groups, decided=decided, decision_total=len(to_decide),
                     t=queries.tiles(lines), changes=changes, prev=prev, show=show if show in FILTERS else "all",
                     filters=FILTERS, runs=queries.recent_runs(conn), impact=queries.impact(conn),
                     version=version)


@app.get("/decisions", response_class=HTMLResponse)
def decisions(request: Request):
    with db.connect() as conn:
        run = _selected_run(conn, request)
        lines = _lines(conn, run["run_id"]) if run else []
        return _page(request, "decisions.html", conn, "decisions", run,
                     exceptions=[l for l in lines if l["exception_type"]],
                     approvals=[l for l in lines if l["line_status"] == "awaiting_gate2"],
                     decided=[l for l in lines if l["line_status"] in ("approved", "po_recorded", "overridden")],
                     export_ready=_approved_reorders(lines),
                     last_export=queries.last_zoho_export(conn, run["run_id"]) if run else None)


# Old tab addresses still work.
@app.get("/exceptions")
@app.get("/procurement")
def _old_tabs(request: Request):
    q = request.url.query
    return RedirectResponse("/decisions" + (f"?{q}" if q else ""), status_code=307)


@app.get("/sku/{sku_id}", response_class=HTMLResponse)
def sku_detail(request: Request, sku_id: str):
    with db.connect() as conn:
        sku = queries.sku(conn, sku_id)
        if not sku:
            raise HTTPException(status_code=404, detail="Unknown SKU")
        run = _selected_run(conn, request)
        line = queries.line(conn, run["run_id"], sku_id) if run else None
        line = _enrich(dict(line)) if line else None
        weeks = queries.weekly_sales(conn, run["export_id"], sku_id) if run else []
        names = [w["iso_week"] for w in weeks if w["iso_week"] < _week_of(run)] if run else []
        window = names[-12:]
        t = thresholds.read().values
        return _page(request, "sku.html", conn, "sku", run, sku=sku, sku_id=sku_id, l=line,
                     weeks=[w for w in weeks if w["iso_week"] in names], window=window, last4=window[-4:],
                     threshold_pct=t["rules.high_demand.threshold_pct"], history=queries.sku_history(conn, sku_id),
                     all_skus=_all_skus(conn))


def _week_of(run):
    y, w, _ = run["run_as_of"].astimezone(ingest.IST).date().isocalendar()
    return f"{y}-W{w:02d}"


def _all_skus(conn):
    return [dict(sku_id=r[0], sku_name=r[1]) for r in conn.execute("SELECT sku_id, sku_name FROM sku_master ORDER BY sku_id")]


@app.get("/impact", response_class=HTMLResponse)
def impact(request: Request):
    ts = thresholds.read()
    with db.connect() as conn:
        return _page(request, "impact.html", conn, "impact", queries.run(conn), impact=queries.impact(conn),
                     all_skus=_all_skus(conn), ts=ts, version=queries.threshold_version(conn, ts.content_hash),
                     signer=thresholds.signer_name(ts.sign_off_by))


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    with db.connect() as conn:
        return _page(request, "admin.html", conn, "admin", queries.run(conn), recent=queries.all_runs(conn),
                     checks=connections.full(conn))


# --- rules sign-off (map step 0) ---------------------------------------------------------------

@app.post("/thresholds/sign")
def sign_thresholds(content_hash: str = Form(...)):
    ts = thresholds.read()
    if content_hash != ts.content_hash:
        return _back("/impact", "The rules changed while the page was open. Review them again before signing.", "err")
    with db.connect() as conn:
        version_id, _ = thresholds.load(conn, ts)
        try:
            thresholds.sign(conn, version_id, thresholds.signer_name(ts.sign_off_by), actor="dashboard")
        except thresholds.ThresholdsError as exc:
            return _back("/impact", str(exc), "err")
        conn.commit()
    return _back("/impact", f"Rules version {version_id} signed off. The next run will use it.")


# --- decisions: Gate 1 (exceptions) and Gate 2 (approvals), both by Deepa ---------------------

@app.post("/decide")
def decide(run_id: int = Form(...), sku_id: str = Form(...), gate: int = Form(...), decision: str = Form(...),
           final_qty: str = Form(""), reason: str = Form("")):
    user = auth.current_user()
    reason = reason.strip()
    with db.connect() as conn:
        line = queries.line(conn, run_id, sku_id)
        run = queries.run(conn, run_id)
        if not line or not run:
            return _back("/decisions", "That line no longer exists.", "err")
        expected = "awaiting_gate1" if gate == 1 else "awaiting_gate2"
        if line["line_status"] != expected:
            return _back("/decisions", f"{sku_id} isn't waiting for that decision any more.", "err", run=run_id)
        allowed = {1: ("accept", "change", "override"), 2: ("approve", "change")}[gate]
        if decision not in allowed:
            return _back("/decisions", "Unknown decision.", "err", run=run_id)
        if decision in ("change", "override") and not reason:
            return _back("/decisions", f"A reason is required to {decision} {sku_id}.", "err", run=run_id)
        if decision == "change":
            if not final_qty.strip().isdigit():
                return _back("/decisions", f"Enter the new quantity for {sku_id}.", "err", run=run_id)
            qty = int(final_qty)
        elif decision == "override":
            qty = 0
        else:
            qty = line["final_qty"] if gate == 2 else line["qty"]
        queries.record_decision(conn, run_id=run_id, sku_id=sku_id, gate=gate, decided_by=user.email,
                                decision=decision, final_qty=qty, reason=reason, shadow=run["shadow_mode"])
        # Deepa decides both gates, so accepting or changing an exception also approves its line.
        # Nothing to approve when the outcome orders nothing (an override, or a qty of 0 on a reorder).
        also_approved = gate == 1 and decision in ("accept", "change") and (qty > 0 or line["action"] == "pause")
        if also_approved:
            queries.record_decision(conn, run_id=run_id, sku_id=sku_id, gate=2, decided_by=user.email,
                                    decision="approve", final_qty=qty, reason="", shadow=run["shadow_mode"])
        conn.commit()
    what = {"accept": "accepted", "change": "changed", "override": "overridden", "approve": "approved"}[decision]
    if also_approved:
        what += f" and approved ({'pause next order' if line['action'] == 'pause' else f'order {qty}'})"
    shadow = " Shadow mode: logged only, nothing is ordered." if run["shadow_mode"] else ""
    return _back("/decisions", f"{sku_id} {what}.{shadow}", run=run_id)


@app.post("/decide/approve-all")
def approve_all(run_id: int = Form(...)):
    """Approve every line that's ready (past Gate 1, not yet approved) at the recommended quantity."""
    user = auth.current_user()
    with db.connect() as conn:
        run = queries.run(conn, run_id)
        if not run:
            return _back("/decisions", "That run no longer exists.", "err")
        ready = [l for l in _lines(conn, run_id) if l["line_status"] == "awaiting_gate2" and not l["large_order"]]
        for l in ready:
            queries.record_decision(conn, run_id=run_id, sku_id=l["sku_id"], gate=2, decided_by=user.email,
                                    decision="approve", final_qty=l["final_qty"], reason="", shadow=run["shadow_mode"])
        conn.commit()
    return _back("/decisions", f"Approved {len(ready)} line{'s' if len(ready) != 1 else ''} as recommended."
                               " Large orders, if any, are left for you to approve one by one.", run=run_id)


# --- Zoho export of approved reorder lines (map step 12; the PO is still raised by hand) ------

ZOHO_COLUMNS = ["SKU", "Item Name", "Quantity", "Linked Distributor ID", "Linked Distributor", "Approved At (IST)",
                "Approved By", "Freshroots Run", "Note"]


def _approved_reorders(lines):
    return [l for l in lines if l["line_status"] in ("approved", "po_recorded") and l["action"] != "pause"
            and (l["final_qty"] or 0) > 0]


@app.get("/decisions/zoho.csv")
def zoho_export(request: Request):
    with db.connect() as conn:
        run = _selected_run(conn, request)
        if not run:
            raise HTTPException(status_code=404, detail="No completed run")
        rows = _approved_reorders(_lines(conn, run["run_id"]))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(ZOHO_COLUMNS)
        for l in rows:
            w.writerow([l["sku_id"], l["sku_name"], l["final_qty"], l["distributor_id"], l["distributor_name"],
                        _ist(l["gate2_at"]), l["gate2_by"], run["run_id"],
                        "shadow mode: for checking only" if run["shadow_mode"] else ""])
        queries.record_zoho_export(conn, run_id=run["run_id"], lines=len(rows),
                                   units=sum(l["final_qty"] for l in rows), exported_by=auth.current_user().email)
        conn.commit()
    name = f"freshroots_run{run['run_id']}_zoho_po_lines.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --- outcomes (map step 13) -------------------------------------------------------------------

@app.post("/outcomes")
def log_outcome(sku_id: str = Form(...), iso_week: str = Form(...), units: int = Form(...), note: str = Form("")):
    if not ingest.WEEK_RE.match(iso_week.strip()) or units <= 0:
        return _back("/impact", "Enter a week like 2026-W43 and a positive number of units.", "err")
    with db.connect() as conn:
        queries.log_write_off(conn, sku_id=sku_id, iso_week=iso_week.strip(), units=units,
                              logged_by=auth.current_user().email, note=note)
        conn.commit()
    return _back("/impact", f"Logged a write-off of {units} units for {sku_id}.")


# --- exports and manual runs ------------------------------------------------------------------

@app.post("/admin/upload")
async def upload_export(sales: UploadFile = File(...), inventory: UploadFile = File(...)):
    sales_text = (await sales.read()).decode("utf-8-sig")
    inv_text = (await inventory.read()).decode("utf-8-sig")
    with db.connect() as conn:
        skus = {r[0] for r in conn.execute("SELECT sku_id FROM sku_master")}
    try:
        export = ingest.parse("upload", "upload", sales_text, inv_text, skus)
    except ingest.ExportError as exc:
        return _back("/admin", f"Export rejected: {exc}", "err")
    export_id = datetime.now(ingest.IST).strftime("%Y-%m-%d_%H%M")
    storage.upload_text(f"{export_id}/{ingest.SALES_FILE}", sales_text)
    storage.upload_text(f"{export_id}/{ingest.INVENTORY_FILE}", inv_text)
    return _back("/admin", f"Uploaded export {export_id} (stock as of {export.data_as_of}). Click Run now to use it.")


@app.post("/admin/run")
def run_now(replay_as_of: str = Form("")):
    """Run on the newest export. replay_as_of (demo only) sets the run clock, e.g. 2026-10-19T06:00."""
    clock = None
    if replay_as_of.strip():
        try:
            clock = datetime.fromisoformat(replay_as_of.strip())
        except ValueError:
            return _back("/admin", "Replay time must look like 2026-10-19T06:00.", "err")
        clock = clock if clock.tzinfo else clock.replace(tzinfo=ingest.IST)
    with db.connect() as conn:
        s = job_run.execute_and_notify(conn, triggered_by="manual: Deepa Nair" + (" (replay)" if clock else ""),
                                       run_as_of=clock)
    if s.status == "ok":
        return _back("/", f"Run {s.run_id} finished: {s.counts.get('exceptions', 0)} exception(s) and "
                          f"{s.counts.get('reorder_lines', 0)} reorder line(s) to review.")
    return _back("/admin", f"Run {s.run_id} {s.status.replace('_', ' ')}: {s.detail}", "err")
