import shutil

import pytest
import yaml

from job import config, thresholds as th


@pytest.fixture
def files(tmp_path):
    """Copies of the real knowledge files and calendar that a test can edit."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    for name in ("thresholds.yaml", "forecasting_rules.md"):
        shutil.copy(config.KNOWLEDGE_DIR / name, kdir / name)
    cal = tmp_path / "festival_calendar.csv"
    shutil.copy(config.FESTIVAL_CALENDAR_CSV, cal)
    return kdir / "thresholds.yaml", cal


def edit(path, change):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_real_files_are_valid():
    ts = th.read()
    assert ts.values["rules.high_demand.threshold_pct"] == 20
    assert ts.values["rules.high_demand.reorder_multiplier"] == 1.5
    assert ts.values["rules.cover_windows.standard.min_weeks"] == 6
    assert ts.values["rules.cover_windows.standard.max_weeks"] == 14
    assert ts.values["rules.cover_windows.high_perishability.min_weeks"] == 3
    assert ts.values["rules.cover_windows.high_perishability.max_weeks"] == 4
    assert ts.values["rules.festival.uplift_pct"] == 25
    assert sorted(ts.values["rules.festival.months"]) == [3, 10, 11]
    assert ts.values["data_checks.stale_after_hours"] == 48


def test_calendar_months_from_seed():
    assert th.calendar_festival_months(config.FESTIVAL_CALENDAR_CSV) == {3, 10, 11}


def test_festival_months_must_match_calendar(files):
    yml, cal = files
    edit(yml, lambda d: d["rules"]["festival"].update(months=[10, 11]))
    with pytest.raises(th.ThresholdsError, match="don't match the festival calendar"):
        th.read(yml, cal)


def test_window_min_must_be_below_max(files):
    yml, cal = files
    edit(yml, lambda d: d["rules"]["cover_windows"]["high_perishability"].update(min_weeks=4))
    with pytest.raises(th.ThresholdsError, match="high_perishability window"):
        th.read(yml, cal)


def test_missing_and_unknown_keys_rejected(files):
    yml, cal = files
    def change(d):
        del d["rules"]["high_demand"]["threshold_pct"]
        d["rules"]["high_demand"]["thresold_pct"] = 20
    edit(yml, change)
    with pytest.raises(th.ThresholdsError) as exc:
        th.read(yml, cal)
    assert "missing key rules.high_demand.threshold_pct" in str(exc.value)
    assert "unknown key rules.high_demand.thresold_pct" in str(exc.value)


def test_wrong_type_rejected(files):
    yml, cal = files
    edit(yml, lambda d: d["data_checks"].update(stale_after_hours="48"))
    with pytest.raises(th.ThresholdsError, match="stale_after_hours must be number"):
        th.read(yml, cal)


def test_uplift_must_come_before_checks(files):
    yml, cal = files
    edit(yml, lambda d: d["rules"]["festival"].update(apply_before_threshold_checks=False))
    with pytest.raises(th.ThresholdsError, match="before threshold checks"):
        th.read(yml, cal)


def test_hash_ignores_comments_but_tracks_values(files):
    yml, cal = files
    base = th.read(yml, cal).content_hash
    yml.write_text(yml.read_text(encoding="utf-8") + "\n# a comment\n", encoding="utf-8")
    assert th.read(yml, cal).content_hash == base
    edit(yml, lambda d: d["rules"]["high_demand"].update(threshold_pct=25))
    assert th.read(yml, cal).content_hash != base


def test_hash_tracks_rules_file(files):
    yml, cal = files
    base = th.read(yml, cal).content_hash
    rules = yml.parent / "forecasting_rules.md"
    rules.write_text(rules.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")
    assert th.read(yml, cal).content_hash != base


def test_hash_ignores_line_endings(files):
    yml, cal = files
    base = th.read(yml, cal).content_hash
    rules = yml.parent / "forecasting_rules.md"
    rules.write_bytes(rules.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert th.read(yml, cal).content_hash == base


def test_signer_name():
    assert th.signer_name("Deepa Nair, Head of Operations") == "Deepa Nair"


# --- Postgres: run inside the job container (DATABASE_URL set). Each test rolls back. ---

needs_db = pytest.mark.skipif(not config.database_configured(),
                              reason="DATABASE_URL not set or still has the password placeholder")


@pytest.fixture
def conn():
    from job import db
    c = db.connect()
    db.init(c)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@needs_db
def test_load_is_idempotent_and_versions_on_change(conn, files):
    yml, cal = files
    v1, created = th.load(conn, th.read(yml, cal))
    assert created
    assert th.load(conn, th.read(yml, cal)) == (v1, False)
    edit(yml, lambda d: d["rules"]["cover_windows"]["standard"].update(min_weeks=5))
    v2, created = th.load(conn, th.read(yml, cal))
    assert created and v2 != v1
    stored = conn.execute("SELECT value FROM thresholds WHERE version_id = %s AND key = %s",
                          (v1, "rules.cover_windows.standard.min_weeks")).fetchone()[0]
    assert stored == 6


@needs_db
def test_unsigned_version_blocks_the_job(conn, files):
    yml, cal = files
    ts = th.read(yml, cal)
    with pytest.raises(th.ThresholdsNotSigned, match="not loaded"):
        th.require_signed(conn, ts)
    th.load(conn, ts)
    with pytest.raises(th.ThresholdsNotSigned, match="not signed off by Deepa Nair"):
        th.require_signed(conn, ts)


@needs_db
def test_only_named_signer_can_sign_once(conn, files):
    yml, cal = files
    ts = th.read(yml, cal)
    version, _ = th.load(conn, ts)
    with pytest.raises(th.ThresholdsError, match="must be signed off by Deepa Nair"):
        th.sign(conn, version, "Raghav Kulkarni")
    th.sign(conn, version, "deepa nair")
    assert th.require_signed(conn, ts)[0] == version
    with pytest.raises(th.ThresholdsError, match="already signed off"):
        th.sign(conn, version, "Deepa Nair")


@needs_db
def test_versions_are_immutable(conn, files):
    import psycopg
    yml, cal = files
    version, _ = th.load(conn, th.read(yml, cal))
    conn.execute("SAVEPOINT s")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE thresholds SET value = '7' WHERE version_id = %s", (version,))
    conn.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE threshold_versions SET values_json = '{}' WHERE version_id = %s", (version,))
