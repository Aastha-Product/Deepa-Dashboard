import json

import pytest
from fastapi.testclient import TestClient

from app import main
from job import run as job_run


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret-for-tests")
    calls = []

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(main.db, "connect", lambda: FakeConn())
    monkeypatch.setattr(job_run, "execute",
                        lambda conn, triggered_by: calls.append(triggered_by) or job_run.RunSummary(7, "ok"))
    c = TestClient(main.app)
    c.calls = calls
    return c


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["cron_secret"] is True and body["db"] in ("ok", "unavailable")
    assert set(body) == {"ok", "cron_secret", "zoho", "claude", "email", "db"}


@pytest.mark.parametrize("header", [None, "Bearer wrong", "s3cret-for-tests"])
def test_run_rejects_missing_or_wrong_secret(client, header):
    r = client.get("/api/run", headers={"Authorization": header} if header else {})
    assert r.status_code == 401 and client.calls == []


def test_run_with_cron_secret(client):
    r = client.get("/api/run", headers={"Authorization": "Bearer s3cret-for-tests"})
    assert r.status_code == 200 and r.json()["run_id"] == 7 and client.calls == ["cron"]


def test_run_refuses_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET")
    assert client.get("/api/run", headers={"Authorization": "Bearer "}).status_code == 401


def test_cron_schedule_is_monday_thursday_0600_ist():
    cfg = json.loads(open("vercel.json", encoding="utf-8").read())
    assert cfg["crons"] == [{"path": "/api/run", "schedule": "30 0 * * 1,4"}]   # 00:30 UTC = 06:00 IST
