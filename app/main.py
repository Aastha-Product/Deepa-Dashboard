"""FastAPI app deployed on Vercel: the cron endpoint (map step 2) and the dashboard (steps 9-13)."""
import hmac
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, queries
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


def _weeks_compact(value):
    """Window limits: 6, 14, 3, 4 (or 3.5) rather than 6.00."""
    return "—" if value is None else f"{float(value):g}"


templates.env.filters.update(
    ist=_ist,
    fmt1=lambda v: _num(v, 1),
    fmtw=lambda v: _num(v, 1),
    wk=_weeks_compact,
    pct=lambda v: "—" if v is None else f"{float(v):+.1f}%",
    num=lambda v: "—" if v is None else f"{int(v):,}",
    rate=lambda n, d: "—" if not d else f"{100 * n / d:.0f}%",
)


# --- helpers ----------------------------------------------------------------------------------

@app.exception_handler(auth.NotSignedIn)
def _to_login(request: Request, exc):
    return RedirectResponse("/login", status_code=303)


def _page(request, name, conn, user, tab, run, **ctx):
    params = request.query_params
    return templates.TemplateResponse(request, name, {
        "user": user, "tab": tab, "run": run, "latest": queries.latest_run(conn),
        "run_param": "run" in params, "flash": params.get("msg"), "flash_kind": params.get("kind", "ok"), **ctx})


def _selected_run(conn, request):
    rid = request.query_params.get("run")
    return queries.run(conn, int(rid)) if rid and rid.isdigit() else queries.run(conn)


def _back(path, msg, kind="ok", **extra):
    return RedirectResponse(f"{path}?{urlencode({**extra, 'msg': msg, 'kind': kind})}", status_code=303)


# --- cron + health ----------------------------------------------------------------------------

def _check_cron_secret(authorization: str | None) -> None:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/run")
def scheduled_run(authorization: str | None = Header(default=None)):
    """Called by Vercel cron (Mon + Thu 06:00 IST) with Authorization: Bearer <CRON_SECRET>."""
    _check_cron_secret(authorization)
    with db.connect() as conn:
        summary = job_run.execute_and_notify(conn, triggered_by="cron")
    return summary.as_dict()


# --- sign-in ----------------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    err = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {"message": err, "kind": "err"} if err else {})


@app.post("/login", response_class=HTMLResponse)
def login_send(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    try:
        with db.connect() as conn:
            known = auth.lookup_user(conn, email) is not None
        if known:
            auth.send_login_link(email, str(request.base_url).rstrip("/") + "/auth/callback")
    except Exception as exc:  # noqa: BLE001
        log.warning("login link failed: %s", exc)
        return templates.TemplateResponse(request, "login.html", {"message": "Couldn't send the link right now. Try again in a minute.", "kind": "err"})
    # Same answer either way, so the page doesn't reveal who has access.
    return templates.TemplateResponse(request, "login.html", {
        "message": "If that email has access, a sign-in link is on its way. It works once.", "kind": "ok"})


@app.get("/auth/callback", response_class=HTMLResponse)
def auth_callback(request: Request):
    return templates.TemplateResponse(request, "login.html", {"script": True})


@app.post("/auth/session")
async def auth_session(request: Request):
    body = await request.json()
    email = auth.email_from_access_token(str(body.get("access_token", "")))
    with db.connect() as conn:
        user = auth.lookup_user(conn, email)
    if not user:
        raise HTTPException(status_code=403, detail="This email isn't on the dashboard access list.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, auth.make_cookie(user), max_age=auth.MAX_AGE, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# --- tabs -------------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def summary(request: Request):
    user = auth.current_user(request)
    q = request.query_params
    f = {k: q.get(k) or None for k in ("flag", "perishability", "distributor", "category")}
    with db.connect() as conn:
        run = _selected_run(conn, request)
        lines = queries.lines(conn, run["run_id"]) if run else []
        shown = queries.lines(conn, run["run_id"], **f) if run and any(f.values()) else lines
        return _page(request, "summary.html", conn, user, "summary", run, lines=shown, t=queries.tiles(lines),
                     f=f, runs=queries.recent_runs(conn), options=queries.filter_options(conn))


@app.get("/exceptions", response_class=HTMLResponse)
def exceptions(request: Request):
    user = auth.current_user(request)
    with db.connect() as conn:
        run = _selected_run(conn, request)
        lines = queries.lines(conn, run["run_id"], flag="exception") if run else []
        return _page(request, "exceptions.html", conn, user, "exceptions", run, lines=lines,
                     can_decide=user.role in auth.CAN_GATE1)


@app.get("/procurement", response_class=HTMLResponse)
def procurement(request: Request):
    user = auth.current_user(request)
    with db.connect() as conn:
        run = _selected_run(conn, request)
        lines = queries.lines(conn, run["run_id"]) if run else []
        lines = [l for l in lines if l["line_status"] not in ("no_action", "overridden")
                 and (l["action"] in ("reorder", "pause") or (l["gate1_qty"] or 0) > 0)]
        return _page(request, "procurement.html", conn, user, "procurement", run, lines=lines,
                     can_decide=user.role in auth.CAN_GATE2)


@app.get("/sku/{sku_id}", response_class=HTMLResponse)
def sku_detail(request: Request, sku_id: str):
    user = auth.current_user(request)
    with db.connect() as conn:
        sku = queries.sku(conn, sku_id)
        if not sku:
            raise HTTPException(status_code=404, detail="Unknown SKU")
        run = _selected_run(conn, request)
        line = queries.line(conn, run["run_id"], sku_id) if run else None
        weeks = queries.weekly_sales(conn, run["export_id"], sku_id) if run else []
        names = [w["iso_week"] for w in weeks if w["iso_week"] < _week_of(run)] if run else []
        window = names[-12:]
        t = thresholds.read().values
        return _page(request, "sku.html", conn, user, "sku", run, sku=sku, sku_id=sku_id, l=line,
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
    user = auth.current_user(request)
    with db.connect() as conn:
        return _page(request, "impact.html", conn, user, "impact", queries.run(conn), impact=queries.impact(conn),
                     all_skus=_all_skus(conn), can_log=user.role in auth.CAN_LOG_OUTCOME)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    user = auth.current_user(request)
    auth.require(user, auth.CAN_RUN)
    with db.connect() as conn:
        return _page(request, "admin.html", conn, user, "admin", queries.run(conn), recent=queries.all_runs(conn))


# --- gates (map steps 10-11) ------------------------------------------------------------------

@app.post("/decide")
def decide(request: Request, run_id: int = Form(...), sku_id: str = Form(...), gate: int = Form(...),
           decision: str = Form(...), final_qty: str = Form(""), reason: str = Form("")):
    user = auth.current_user(request)
    auth.require(user, auth.CAN_GATE1 if gate == 1 else auth.CAN_GATE2)
    page = "/exceptions" if gate == 1 else "/procurement"
    reason = reason.strip()
    with db.connect() as conn:
        line = queries.line(conn, run_id, sku_id)
        run = queries.run(conn, run_id)
        if not line or not run:
            return _back(page, "That line no longer exists.", "err")
        expected = "awaiting_gate1" if gate == 1 else "awaiting_gate2"
        if line["line_status"] != expected:
            return _back(page, f"{sku_id} isn't waiting for Gate {gate} any more.", "err", run=run_id)
        allowed = {1: ("accept", "change", "override"), 2: ("approve", "change")}[gate]
        if decision not in allowed:
            return _back(page, "Unknown decision.", "err", run=run_id)
        if decision in ("change", "override") and not reason:
            return _back(page, f"A reason is required to {decision} {sku_id}.", "err", run=run_id)
        if decision == "change":
            if not final_qty.strip().isdigit():
                return _back(page, f"Enter the new quantity for {sku_id}.", "err", run=run_id)
            qty = int(final_qty)
        elif decision == "override":
            qty = 0
        else:
            qty = line["final_qty"] if gate == 2 else line["qty"]
        queries.record_decision(conn, run_id=run_id, sku_id=sku_id, gate=gate, decided_by=user.email,
                                decision=decision, final_qty=qty, reason=reason, shadow=run["shadow_mode"])
        conn.commit()
    shadow = " (shadow mode: logged only)" if run["shadow_mode"] else ""
    return _back(page, f"Gate {gate}: {decision} recorded for {sku_id}{shadow}.", run=run_id)


# --- outcomes (map step 13) -------------------------------------------------------------------

@app.post("/outcomes")
def log_outcome(request: Request, sku_id: str = Form(...), iso_week: str = Form(...), units: int = Form(...),
                note: str = Form("")):
    user = auth.current_user(request)
    auth.require(user, auth.CAN_LOG_OUTCOME)
    if not ingest.WEEK_RE.match(iso_week.strip()) or units <= 0:
        return _back("/impact", "Enter a week like 2026-W43 and a positive number of units.", "err")
    with db.connect() as conn:
        queries.log_write_off(conn, sku_id=sku_id, iso_week=iso_week.strip(), units=units, logged_by=user.email, note=note)
        conn.commit()
    return _back("/impact", f"Logged a write-off of {units} units for {sku_id}.")


# --- exports and manual runs ------------------------------------------------------------------

@app.post("/admin/upload")
async def upload_export(request: Request, sales: UploadFile = File(...), inventory: UploadFile = File(...)):
    user = auth.current_user(request)
    auth.require(user, auth.CAN_RUN)
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
def run_now(request: Request):
    user = auth.current_user(request)
    auth.require(user, auth.CAN_RUN)
    with db.connect() as conn:
        s = job_run.execute_and_notify(conn, triggered_by=f"manual:{user.email}")
    if s.status == "ok":
        return _back("/", f"Run {s.run_id} finished. {s.counts.get('exceptions', 0)} exceptions, "
                          f"{s.counts.get('reorder_lines', 0)} reorder lines.")
    return _back("/admin", f"Run {s.run_id} {s.status.replace('_', ' ')}: {s.detail}", "err")
