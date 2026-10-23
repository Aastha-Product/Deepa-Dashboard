"""Versioned, signed-off thresholds (components map step 0).

Raghav writes knowledge/thresholds.yaml and forecasting_rules.md; Deepa signs off.
Every distinct set of values (plus the rules file) is stored once as a version.
The job refuses to run unless the version matching the current files is signed off.
"""
import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from job import config


class ThresholdsError(Exception):
    pass


class ThresholdsNotSigned(ThresholdsError):
    pass


INT, NUM, BOOL, STR, MONTHS = "int", "number", "bool", "str", "months"

REQUIRED_KEYS = {
    "rules.high_demand.short_window_weeks": INT,
    "rules.high_demand.long_window_weeks": INT,
    "rules.high_demand.long_window_includes_short": BOOL,
    "rules.high_demand.threshold_pct": NUM,
    "rules.high_demand.reorder_multiplier": NUM,
    "rules.cover_windows.standard.min_weeks": NUM,
    "rules.cover_windows.standard.max_weeks": NUM,
    "rules.cover_windows.high_perishability.min_weeks": NUM,
    "rules.cover_windows.high_perishability.max_weeks": NUM,
    "rules.festival.months": MONTHS,
    "rules.festival.uplift_pct": NUM,
    "rules.festival.apply_before_threshold_checks": BOOL,
    "rules.reorder_qty.base": STR,
    "rules.reorder_qty.top_up_to_min_cover": BOOL,
    "rules.reorder_qty.cap_at_max_cover": BOOL,
    "exceptions.conflicting_flags": BOOL,
    "exceptions.new_sku_min_history_weeks": INT,
    "exceptions.zero_demand": BOOL,
    "exceptions.distributor_swing_pct": NUM,
    "exceptions.distributor_swing_enabled": BOOL,
    "data_checks.stale_after_hours": NUM,
}
VALUE_SECTIONS = ("rules", "exceptions", "data_checks")


@dataclass(frozen=True)
class ThresholdSet:
    values: dict          # dotted key -> value
    written_by: str
    sign_off_by: str
    rules_file_sha: str
    content_hash: str


def _flatten(node, prefix=""):
    out = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, path + "."))
        else:
            out[path] = value
    return out


def _type_ok(kind, value):
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == BOOL:
        return isinstance(value, bool)
    if kind == STR:
        return isinstance(value, str) and value.strip() != ""
    if kind == MONTHS:
        return (isinstance(value, list) and value
                and all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12 for m in value)
                and len(set(value)) == len(value))
    raise ValueError(kind)


def calendar_festival_months(csv_path: Path) -> set:
    """Festival months from the calendar CSV; a month must be flagged the same way every year."""
    seen = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            month = int(row["Month"])
            flag = row["Is_Festival_Month"].strip().upper() == "TRUE"
            if seen.setdefault(month, flag) != flag:
                raise ThresholdsError(f"festival calendar flags month {month} differently across years")
    return {m for m, flag in seen.items() if flag}


def validate(doc: dict, calendar_months: set | None) -> list:
    errors = []
    if not isinstance(doc, dict):
        return ["thresholds file is not a mapping"]
    for field in ("written_by", "sign_off_by", "rules_file"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            errors.append(f"missing '{field}'")

    flat = _flatten({s: doc.get(s) or {} for s in VALUE_SECTIONS})
    for key, kind in REQUIRED_KEYS.items():
        if key not in flat:
            errors.append(f"missing key {key}")
        elif not _type_ok(kind, flat[key]):
            errors.append(f"{key} must be {kind}, got {flat[key]!r}")
    for key in sorted(set(flat) - set(REQUIRED_KEYS)):
        errors.append(f"unknown key {key}")
    if errors:
        return errors

    hd = "rules.high_demand."
    if not 0 < flat[hd + "short_window_weeks"] < flat[hd + "long_window_weeks"]:
        errors.append("short window must be positive and shorter than the long window")
    if flat[hd + "threshold_pct"] <= 0:
        errors.append("high-demand threshold must be above 0%")
    if flat[hd + "reorder_multiplier"] < 1:
        errors.append("high-demand reorder multiplier must be at least 1")
    for window in ("standard", "high_perishability"):
        lo = flat[f"rules.cover_windows.{window}.min_weeks"]
        hi = flat[f"rules.cover_windows.{window}.max_weeks"]
        if not 0 < lo < hi:
            errors.append(f"{window} window needs 0 < min_weeks < max_weeks (got {lo}, {hi})")
    if flat["rules.festival.uplift_pct"] < 0:
        errors.append("festival uplift cannot be negative")
    if flat["rules.festival.apply_before_threshold_checks"] is not True:
        errors.append("festival uplift must apply before threshold checks (rule 4)")
    if flat["rules.reorder_qty.base"] != "standard_reorder_qty":
        errors.append("reorder_qty.base must be standard_reorder_qty")
    if flat["exceptions.new_sku_min_history_weeks"] < flat[hd + "long_window_weeks"]:
        errors.append("new-SKU history must be at least the long window")
    if flat["exceptions.distributor_swing_pct"] <= 0:
        errors.append("distributor swing threshold must be above 0%")
    if flat["data_checks.stale_after_hours"] <= 0:
        errors.append("stale_after_hours must be above 0")
    if calendar_months is not None and set(flat["rules.festival.months"]) != calendar_months:
        errors.append(f"festival months {sorted(flat['rules.festival.months'])} don't match "
                      f"the festival calendar {sorted(calendar_months)}")
    return errors


def read(thresholds_file: Path = None, calendar_csv: Path = None) -> ThresholdSet:
    thresholds_file = Path(thresholds_file or config.THRESHOLDS_FILE)
    calendar_csv = Path(calendar_csv or config.FESTIVAL_CALENDAR_CSV)
    doc = yaml.safe_load(thresholds_file.read_text(encoding="utf-8"))
    errors = validate(doc, calendar_festival_months(calendar_csv))
    if errors:
        raise ThresholdsError("thresholds.yaml is invalid:\n  - " + "\n  - ".join(errors))

    rules_path = thresholds_file.parent / doc["rules_file"]
    if not rules_path.is_file():
        raise ThresholdsError(f"rules file not found: {rules_path}")
    # Normalise line endings so a Windows checkout and Vercel (Linux) hash the same file identically.
    rules_text = rules_path.read_bytes().replace(b"\r\n", b"\n")
    rules_sha = hashlib.sha256(rules_text).hexdigest()

    values = _flatten({s: doc[s] for s in VALUE_SECTIONS})
    payload = json.dumps({"values": values, "rules_file_sha": rules_sha,
                          "written_by": doc["written_by"], "sign_off_by": doc["sign_off_by"]},
                         sort_keys=True, separators=(",", ":"))
    return ThresholdSet(values, doc["written_by"], doc["sign_off_by"], rules_sha,
                        hashlib.sha256(payload.encode()).hexdigest())


def load(conn, ts: ThresholdSet) -> tuple:
    """Store the version if it's new. Returns (version_id, created). Caller commits."""
    row = conn.execute("SELECT version_id FROM threshold_versions WHERE content_hash = %s",
                       (ts.content_hash,)).fetchone()
    if row:
        return row[0], False
    version_id = conn.execute(
        """INSERT INTO threshold_versions
               (content_hash, written_by, sign_off_by, rules_file_sha, values_json)
           VALUES (%s, %s, %s, %s, %s) RETURNING version_id""",
        (ts.content_hash, ts.written_by, ts.sign_off_by, ts.rules_file_sha,
         json.dumps(ts.values, sort_keys=True))).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO thresholds (version_id, key, value) VALUES (%s, %s, %s)",
                        [(version_id, k, json.dumps(v)) for k, v in sorted(ts.values.items())])
    return version_id, True


def signer_name(sign_off_by: str) -> str:
    return sign_off_by.split(",")[0].strip()


def sign(conn, version_id: int, by: str, actor: str = None) -> None:
    """Record the one-time sign-off. Only the signer named in the file may sign. Caller commits.

    `actor` is the dashboard login that clicked Sign off; it's stored next to the signer's name.
    """
    row = conn.execute("SELECT sign_off_by, signed_off_by FROM threshold_versions WHERE version_id = %s",
                       (version_id,)).fetchone()
    if not row:
        raise ThresholdsError(f"version {version_id} not found")
    required, already = row
    if already:
        raise ThresholdsError(f"version {version_id} is already signed off by {already}")
    if by.strip().casefold() != signer_name(required).casefold():
        raise ThresholdsError(f"version {version_id} must be signed off by {signer_name(required)}")
    signed = signer_name(required) + (f" ({actor})" if actor else "")
    conn.execute("UPDATE threshold_versions SET signed_off_by = %s, signed_off_at = now() WHERE version_id = %s",
                 (signed, version_id))


def require_signed(conn, ts: ThresholdSet = None) -> tuple:
    """Return (version_id, values) for the current files, or raise ThresholdsNotSigned."""
    ts = ts or read()
    row = conn.execute("SELECT version_id, signed_off_by FROM threshold_versions WHERE content_hash = %s",
                       (ts.content_hash,)).fetchone()
    if not row:
        raise ThresholdsNotSigned("the current thresholds and rules files are not loaded; "
                                  "run `python -m job.thresholds load`")
    if not row[1]:
        raise ThresholdsNotSigned(f"thresholds version {row[0]} is not signed off by "
                                  f"{signer_name(ts.sign_off_by)}; the job will not run")
    return row[0], ts.values


def _status(conn) -> str:
    rows = conn.execute("""SELECT version_id, left(content_hash, 12), loaded_at, signed_off_by, signed_off_at
                           FROM threshold_versions ORDER BY version_id""").fetchall()
    if not rows:
        return "No threshold versions loaded."
    lines = [f"{'ver':>3}  {'hash':12}  {'loaded':16}  sign-off"]
    for vid, h, loaded, by, at in rows:
        signed = f"{by} at {at:%Y-%m-%d %H:%M}" if by else "NOT SIGNED"
        lines.append(f"{vid:>3}  {h}  {loaded:%Y-%m-%d %H:%M}  {signed}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m job.thresholds")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="check thresholds.yaml and the festival calendar (no database)")
    sub.add_parser("load", help="store the current files as a version if new")
    p_sign = sub.add_parser("sign", help="record the sign-off for a version")
    p_sign.add_argument("--version", type=int, required=True)
    p_sign.add_argument("--by", required=True)
    sub.add_parser("status", help="list versions and their sign-off")
    sub.add_parser("check", help="exit 1 unless the current files are signed off")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "validate":
            ts = read()
            print(f"OK: {len(ts.values)} values, hash {ts.content_hash[:12]}")
            return 0

        from job import db
        with db.connect() as conn:
            if args.cmd == "load":
                version_id, created = load(conn, read())
                conn.commit()
                print(f"{'Loaded new' if created else 'Already loaded as'} version {version_id}")
            elif args.cmd == "sign":
                sign(conn, args.version, args.by)
                conn.commit()
                print(f"Version {args.version} signed off by {args.by}")
            elif args.cmd == "status":
                print(_status(conn))
            elif args.cmd == "check":
                version_id, _ = require_signed(conn)
                print(f"OK: running on signed version {version_id}")
        return 0
    except ThresholdsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
