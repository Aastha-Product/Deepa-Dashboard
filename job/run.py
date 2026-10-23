"""One forecasting run, start to finish (components map steps 2-9).

    python -m job.run                          # latest export in the sap-exports bucket, clock = now (or RUN_AS_OF)
    python -m job.run --local data/seed --export-id seed-2026-10-19 --as-of 2026-10-19T06:00+05:30
    python -m job.run upload-seed              # put the seed files in the bucket as export 2026-10-19_0600

Order: thresholds signed? -> export read and checked -> fresh (<= 48 h)? -> Zoho open POs -> rules -> Claude
reasons -> save -> summary. A stopped or failed run is still recorded, so the dashboard can show why.
"""
import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from job import config, explain, ingest, rules, storage, store, thresholds, zoho

log = logging.getLogger(__name__)
SEED_EXPORT_ID = "2026-10-19_0600"


@dataclass
class RunSummary:
    run_id: int
    status: str
    detail: str = ""
    shadow_mode: bool = True
    counts: dict = field(default_factory=dict)
    exceptions: list = field(default_factory=list)
    reorders: list = field(default_factory=list)
    email: str = ""

    def as_dict(self):
        return self.__dict__.copy()


def run_clock() -> datetime:
    value = os.environ.get("RUN_AS_OF", "").strip()
    return datetime.fromisoformat(value) if value else datetime.now(ingest.IST)


def _setting(conn, key, default):
    row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def _start(conn, run_as_of, triggered_by, shadow) -> int:
    run_id = conn.execute("""INSERT INTO runs (run_as_of, triggered_by, status, shadow_mode)
                             VALUES (%s, %s, 'running', %s) RETURNING run_id""",
                          (run_as_of, triggered_by, shadow)).fetchone()[0]
    conn.commit()
    return run_id


def _finish(conn, run_id, status, detail, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(f"UPDATE runs SET status = %s, status_detail = %s, finished_at = now()"
                 f"{', ' + sets if sets else ''} WHERE run_id = %s",
                 (status, detail[:1000], *fields.values(), run_id))
    conn.commit()


def execute(conn, *, triggered_by: str, run_as_of: datetime = None, local_folder: Path = None,
            export_id: str = None, claude_client=None, zoho_client=None, storage_client=None) -> RunSummary:
    run_as_of = run_as_of or run_clock()
    shadow = _setting(conn, "shadow_mode", "true").strip().lower() != "false"
    run_id = _start(conn, run_as_of, triggered_by, shadow)
    summary = RunSummary(run_id, "running", shadow_mode=shadow)

    try:
        try:
            current = thresholds.read()
            thresholds.load(conn, current)      # store the current files as a version (unsigned if new)
            conn.commit()
            version_id, t = thresholds.require_signed(conn, current)
        except thresholds.ThresholdsError as exc:
            _finish(conn, run_id, "stopped_unsigned", str(exc))
            summary.status, summary.detail = "stopped_unsigned", str(exc)
            return summary

        master = {r[0]: r for r in conn.execute(
            "SELECT sku_id, sku_name, perishability, standard_reorder_qty FROM sku_master").fetchall()}
        try:
            export = (ingest.from_folder(local_folder, set(master), export_id) if local_folder
                      else ingest.latest_from_storage(set(master), storage_client))
        except (ingest.ExportError, storage.StorageError) as exc:
            _finish(conn, run_id, "failed", f"SAP export problem: {exc}", thresholds_version=version_id)
            summary.status, summary.detail = "failed", f"SAP export problem: {exc}"
            return summary

        age = ingest.data_age_hours(export.data_as_of, run_as_of)
        ingest.save(conn, export)
        base = dict(thresholds_version=version_id, export_id=export.export_id, data_as_of=export.data_as_of,
                    data_age_hours=age)
        if age > t["data_checks.stale_after_hours"]:
            detail = (f"Data is {age:.0f} h old (stock as of {export.data_as_of}); limit is "
                      f"{t['data_checks.stale_after_hours']:g} h. No recommendations; IT alerted.")
            _finish(conn, run_id, "stopped_stale", detail, **base)
            summary.status, summary.detail = "stopped_stale", detail
            return summary

        z = zoho.fetch_open_pos(set(master), zoho_client)
        zoho.save(conn, run_id, z)
        open_qty = z.open_qty_by_sku()

        run_date = run_as_of.astimezone(ingest.IST).date()
        recs = []
        for sku in sorted(set(master) & set(export.on_hand)):
            _, _, perish, std = master[sku]
            po = None if open_qty is None else open_qty.get(sku, 0)
            recs.append(rules.evaluate(rules.SkuInput(sku, perish, std, export.sales.get(sku, {}),
                                                      export.on_hand[sku], po), run_date, t))

        explained, reasons_source, reasons_detail = explain.explain(
            recs, {s: m[1] for s, m in master.items()}, export.sales, claude_client)
        store.save_recommendations(conn, run_id, recs, explained)
        store.log_stockouts(conn, run_id, recs, export)

        uplift = recs[0].uplift if recs else rules.festival_uplift(run_date, t)
        detail = f"Zoho: {z.status}{' (' + z.detail + ')' if z.detail else ''}. Reasons: {reasons_detail}."
        _finish(conn, run_id, "ok", detail, **base, festival_month=uplift > 1, uplift=uplift,
                zoho_status=z.status, reasons_source=reasons_source)

        summary.status, summary.detail = "ok", detail
        summary.counts = {
            "skus": len(recs), "stockout": sum(r.flag_stockout for r in recs),
            "overstock": sum(r.flag_overstock for r in recs), "high_demand": sum(r.flag_high_demand for r in recs),
            "exceptions": sum(bool(r.exception_type) for r in recs), "reorder_lines": sum(r.action == "reorder" for r in recs),
            "ok": sum(not r.flagged for r in recs)}
        summary.exceptions = [r.sku_id for r in recs if r.exception_type]
        summary.reorders = [(r.sku_id, r.qty) for r in recs if r.action == "reorder"]
        return summary
    except Exception as exc:
        conn.rollback()
        _finish(conn, run_id, "failed", f"{type(exc).__name__}: {exc}")
        raise


def execute_and_notify(conn, **kwargs) -> RunSummary:
    from job import notify
    summary = execute(conn, **kwargs)
    summary.email = notify.after_run(summary)
    return summary


def upload_seed(export_id: str = SEED_EXPORT_ID) -> None:
    for name in (ingest.SALES_FILE, ingest.INVENTORY_FILE):
        storage.upload_text(f"{export_id}/{name}", (config.DATA_DIR / "seed" / name).read_text(encoding="utf-8"))
    print(f"Uploaded the seed export to {storage.BUCKET}/{export_id}/")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m job.run")
    p.add_argument("command", nargs="?", default="run", choices=["run", "upload-seed"])
    p.add_argument("--local", type=Path, help="read the export from this folder instead of the bucket")
    p.add_argument("--export-id", help="export id to use with --local (default: folder name)")
    p.add_argument("--as-of", help="run clock, e.g. 2026-10-19T06:00+05:30 (default: RUN_AS_OF or now)")
    args = p.parse_args(argv)

    if args.command == "upload-seed":
        upload_seed()
        return 0
    from job import db
    with db.connect() as conn:
        s = execute_and_notify(conn, triggered_by="cli", local_folder=args.local, export_id=args.export_id,
                               run_as_of=datetime.fromisoformat(args.as_of) if args.as_of else None)
    print(json.dumps(s.as_dict(), indent=2, default=str))
    return 0 if s.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
