import pytest

from job import notify
from job.run import RunSummary

OK = RunSummary(12, "ok", "Zoho: ok. Reasons: Claude explained 7 of 7 flagged SKUs.", True,
                {"skus": 15, "stockout": 5, "overstock": 2, "high_demand": 1, "exceptions": 1, "reorder_lines": 5, "ok": 8},
                ["SKU001"], [("SKU001", 750), ("SKU013", 161)])
STALE = RunSummary(13, "stopped_stale", "Data is 102 h old (stock as of 2026-10-19); limit is 48 h.")


@pytest.fixture
def smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout): sent.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent.append(("tls",))
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, msg): sent.append(("msg", msg))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "bot", "SMTP_PASSWORD": "x",
                 "MAIL_FROM": "bot@example.com", "MAIL_TO_SUMMARY": "raghav@x.in, proc@x.in, deepa@x.in",
                 "MAIL_TO_IT_ALERT": "it@x.in"}.items():
        monkeypatch.setenv(k, v)
    return sent


def test_summary_email(smtp):
    assert notify.after_run(OK) == "sent to 3"
    msg = smtp[-1][1]
    assert msg["To"] == "raghav@x.in, proc@x.in, deepa@x.in"
    assert "[SHADOW MODE]" in msg["Subject"] and "5 stockout" in msg["Subject"]
    body = msg.get_body(("plain",)).get_content()
    assert "SKU001 750, SKU013 161" in body and "Exceptions to decide: SKU001" in body


def test_stale_run_alerts_it(smtp):
    assert notify.after_run(STALE) == "sent to 1"
    msg = smtp[-1][1]
    assert msg["To"] == "it@x.in" and msg["Subject"].startswith("ACTION NEEDED") and "data too old" in msg["Subject"]


def test_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert notify.after_run(OK) == "email not configured"


def test_send_failure_never_raises(smtp, monkeypatch):
    def boom(*a, **k): raise OSError("connection refused")
    monkeypatch.setattr(notify.smtplib, "SMTP", boom)
    assert notify.after_run(OK).startswith("email failed")
