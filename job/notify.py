"""Summary email after a good run, alert after a stopped or failed one (map step 9).

Email is best-effort: if SMTP isn't configured or sending fails, the run result is unchanged and the
dashboard still shows everything. Recipients come from MAIL_TO_SUMMARY (Raghav, Procurement, Deepa)
and MAIL_TO_IT_ALERT (IT, plus Raghav if wanted), comma-separated.
"""
import html
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _recipients(var):
    return [a.strip() for a in os.environ.get(var, "").split(",") if a.strip()]


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_FROM"))


def _send(to: list, subject: str, text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["MAIL_FROM"], ", ".join(to), subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    port = int(os.environ.get("SMTP_PORT", "587"))
    host = os.environ["SMTP_HOST"]
    with (smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)) as s:
        if port != 465:
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def build_summary(summary) -> tuple:
    c, url = summary.counts, os.environ.get("DASHBOARD_URL", "https://deepa-dashboard.vercel.app")
    shadow = " [SHADOW MODE]" if summary.shadow_mode else ""
    subject = (f"Freshroots demand planning run {summary.run_id}{shadow}: {c.get('stockout', 0)} stockout, "
               f"{c.get('overstock', 0)} overstock, {c.get('exceptions', 0)} for Raghav")
    lines = [
        f"Run {summary.run_id} finished.{shadow}",
        f"SKUs checked {c.get('skus', 0)} · stockout risk {c.get('stockout', 0)} · overstock risk {c.get('overstock', 0)}"
        f" · high demand {c.get('high_demand', 0)} · OK {c.get('ok', 0)}",
        f"Exceptions for Raghav (Gate 1): {', '.join(summary.exceptions) or 'none'}",
        "Reorder lines for procurement (Gate 2): " + (", ".join(f"{s} {q}" for s, q in summary.reorders) or "none"),
        summary.detail,
        f"Dashboard: {url}",
    ]
    text = "\n".join(lines)
    body = "".join(f"<p>{html.escape(l)}</p>" for l in lines[:-1]) + f'<p><a href="{html.escape(url)}">Open the dashboard</a></p>'
    return subject, text, body


def build_alert(summary) -> tuple:
    url = os.environ.get("DASHBOARD_URL", "https://deepa-dashboard.vercel.app")
    label = {"stopped_stale": "stopped: data too old", "stopped_unsigned": "stopped: rules not signed off",
             "failed": "failed"}.get(summary.status, summary.status)
    subject = f"ACTION NEEDED: Freshroots demand planning run {summary.run_id} {label}"
    text = f"Run {summary.run_id} {label}. No recommendations were produced.\n\n{summary.detail}\n\nDashboard: {url}/admin"
    body = (f"<p><b>Run {summary.run_id} {html.escape(label)}.</b> No recommendations were produced.</p>"
            f"<p>{html.escape(summary.detail)}</p><p><a href=\"{html.escape(url)}/admin\">Exports &amp; runs</a></p>")
    return subject, text, body


def after_run(summary) -> str:
    """Send the right email for this run. Returns what happened, for logs and the API response."""
    if not configured():
        return "email not configured"
    ok = summary.status == "ok"
    to = _recipients("MAIL_TO_SUMMARY" if ok else "MAIL_TO_IT_ALERT")
    if not to:
        return "no recipients set"
    try:
        _send(to, *(build_summary(summary) if ok else build_alert(summary)))
        return f"sent to {len(to)}"
    except Exception as exc:  # noqa: BLE001 - email never changes the run outcome
        log.warning("email failed: %s", exc)
        return f"email failed: {str(exc)[:120]}"
