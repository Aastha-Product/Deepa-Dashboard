"""FastAPI app deployed on Vercel: the cron endpoint (map step 2) and, from P7, the dashboard."""
import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException

from job import db, run as job_run

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Freshroots Demand Planning", docs_url=None, redoc_url=None, openapi_url=None)


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
        summary = job_run.execute(conn, triggered_by="cron")
    return summary.as_dict()
