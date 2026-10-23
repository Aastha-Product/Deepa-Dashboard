"""Is everything connected? Shown on Data & runs, summarised in the header, and in /api/health.

`quick()` only looks at settings (no network calls), so it's cheap enough for every page.
`full()` actually talks to each service, for the Connections panel.
"""
import os

from job import notify, storage, zoho

OK, MISSING, ERROR = "ok", "missing", "error"


def _item(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def quick() -> dict:
    return {
        "cron": bool(os.environ.get("CRON_SECRET")),
        "zoho": zoho.configured(),
        "claude": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "email": notify.configured() and bool(os.environ.get("MAIL_TO_SUMMARY")),
    }


def missing_count() -> int:
    return sum(not v for v in quick().values())


def full(conn) -> list:
    items = []

    try:
        n = conn.execute("SELECT count(*) FROM sku_master").fetchone()[0]
        items.append(_item("Supabase database", OK, f"connected · {n} SKUs in the master"))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("Supabase database", ERROR, f"can't query: {type(exc).__name__}",
                           "Check the Supabase-Vercel integration (POSTGRES_URL)."))

    try:
        exports = storage.list_folders()
        items.append(_item("Supabase storage", OK, f"connected · {len(exports)} export(s) in sap-exports"
                           + (f", newest {exports[0]}" if exports else "")))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("Supabase storage", ERROR, str(exc)[:140],
                           "Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Vercel."))

    if os.environ.get("CRON_SECRET"):
        items.append(_item("Vercel schedule", OK, "Mon + Thu 06:00 IST (Hobby plan: within that hour)"))
    else:
        items.append(_item("Vercel schedule", MISSING, "CRON_SECRET isn't set, so scheduled runs are refused",
                           "Add CRON_SECRET in Vercel → Environment Variables, then redeploy."))

    ok, detail = zoho.check()
    items.append(_item("Zoho Inventory (open POs)", OK if ok else (MISSING if not zoho.configured() else ERROR), detail,
                       "" if ok else "Run tools/zoho_setup.py once and import zoho.vercel.env in Vercel. "
                                     "Until then, weeks of cover use on-hand stock only."))

    if os.environ.get("ANTHROPIC_API_KEY"):
        items.append(_item("Claude (reasons)", OK, f"key set · model {os.environ.get('DP_CLAUDE_MODEL', 'claude-opus-5-5')}"))
    else:
        items.append(_item("Claude (reasons)", MISSING, "no key: reasons are written by the rules engine",
                           "Add ANTHROPIC_API_KEY in Vercel. Numbers don't change; only the wording of reasons."))

    if notify.configured() and os.environ.get("MAIL_TO_SUMMARY"):
        items.append(_item("Summary email", OK, f"from {os.environ.get('MAIL_FROM')} to {os.environ.get('MAIL_TO_SUMMARY')}"))
    else:
        items.append(_item("Summary email", MISSING, "not set: no email after runs (the dashboard still updates)",
                           "Add SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO_SUMMARY and "
                           "MAIL_TO_IT_ALERT in Vercel."))
    return items
