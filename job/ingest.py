"""Read and check one SAP export (components map steps 1-3).

An export is a folder with two CSVs, as IT's scheduled SAP export produces them:
    sales_weekly.csv        SKU_ID, Week, Units_Sold
    inventory_snapshot.csv  SKU_ID, Location, Units_On_Hand, As_Of_Date
In production the folder lives in the Supabase Storage bucket `sap-exports/<export_id>/`;
locally (tests, demo) it can be any directory, e.g. data/seed.
"""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from job import storage

IST = timezone(timedelta(hours=5, minutes=30))
SALES_FILE = "sales_weekly.csv"
INVENTORY_FILE = "inventory_snapshot.csv"
SALES_COLUMNS = ["SKU_ID", "Week", "Units_Sold"]
INVENTORY_COLUMNS = ["SKU_ID", "Location", "Units_On_Hand", "As_Of_Date"]
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class ExportError(Exception):
    """The export can't be used; the run stops and IT is alerted."""


@dataclass
class Export:
    export_id: str
    source: str
    sales: dict            # sku_id -> {iso_week: units}
    on_hand: dict          # sku_id -> units (summed across locations)
    locations: dict        # sku_id -> {location: units}
    data_as_of: date


def _rows(text: str, expected: list, name: str) -> list:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in expected if c not in header]
    if missing:
        raise ExportError(f"{name} is missing column(s): {', '.join(missing)}")
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]


def _int(value: str, where: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise ExportError(f"{where}: '{value}' is not a whole number") from None
    if n < 0:
        raise ExportError(f"{where}: negative units ({n})")
    return n


def parse(export_id: str, source: str, sales_text: str, inventory_text: str, known_skus: set) -> Export:
    sales = {}
    for i, row in enumerate(_rows(sales_text, SALES_COLUMNS, SALES_FILE), start=2):
        sku, week = row["SKU_ID"], row["Week"]
        if sku not in known_skus:
            raise ExportError(f"{SALES_FILE} line {i}: unknown SKU {sku}")
        if not WEEK_RE.match(week):
            raise ExportError(f"{SALES_FILE} line {i}: week '{week}' is not like 2026-W42")
        weeks = sales.setdefault(sku, {})
        if week in weeks:
            raise ExportError(f"{SALES_FILE} line {i}: duplicate {sku} {week}")
        weeks[week] = _int(row["Units_Sold"], f"{SALES_FILE} line {i}")

    locations, as_of = {}, set()
    for i, row in enumerate(_rows(inventory_text, INVENTORY_COLUMNS, INVENTORY_FILE), start=2):
        sku = row["SKU_ID"]
        if sku not in known_skus:
            raise ExportError(f"{INVENTORY_FILE} line {i}: unknown SKU {sku}")
        try:
            as_of.add(date.fromisoformat(row["As_Of_Date"]))
        except ValueError:
            raise ExportError(f"{INVENTORY_FILE} line {i}: As_Of_Date '{row['As_Of_Date']}' is not YYYY-MM-DD") from None
        locs = locations.setdefault(sku, {})
        if row["Location"] in locs:
            raise ExportError(f"{INVENTORY_FILE} line {i}: duplicate {sku} at {row['Location']}")
        locs[row["Location"]] = _int(row["Units_On_Hand"], f"{INVENTORY_FILE} line {i}")

    if not sales:
        raise ExportError(f"{SALES_FILE} has no rows")
    if not locations:
        raise ExportError(f"{INVENTORY_FILE} has no rows")
    if len(as_of) != 1:
        raise ExportError(f"{INVENTORY_FILE} mixes stock dates: {', '.join(sorted(map(str, as_of)))}")
    no_stock = sorted(set(sales) - set(locations))
    if no_stock:
        raise ExportError(f"no stock row for {', '.join(no_stock)}")

    on_hand = {sku: sum(locs.values()) for sku, locs in locations.items()}
    return Export(export_id, source, sales, on_hand, locations, as_of.pop())


def from_folder(folder: Path, known_skus: set, export_id: str = None) -> Export:
    folder = Path(folder)
    return parse(export_id or folder.name, str(folder),
                 (folder / SALES_FILE).read_text(encoding="utf-8-sig"),
                 (folder / INVENTORY_FILE).read_text(encoding="utf-8-sig"), known_skus)


def latest_from_storage(known_skus: set, client=None) -> Export:
    folders = storage.list_folders(client)
    if not folders:
        raise ExportError(f"no exports in the {storage.BUCKET} bucket yet")
    export_id = folders[0]
    try:
        sales = storage.download_text(f"{export_id}/{SALES_FILE}", client)
        inventory = storage.download_text(f"{export_id}/{INVENTORY_FILE}", client)
    except storage.StorageError as exc:
        raise ExportError(f"export {export_id}: {exc}") from None
    return parse(export_id, f"{storage.BUCKET}/{export_id}", sales, inventory, known_skus)


def data_age_hours(data_as_of: date, run_as_of: datetime) -> float:
    """Hours between the stock snapshot (start of its day, IST) and the run."""
    snapshot = datetime.combine(data_as_of, time(0, 0), tzinfo=IST)
    return round((run_as_of - snapshot).total_seconds() / 3600, 1)


def save(conn, export: Export) -> bool:
    """Store the export once. Returns False if this export_id is already loaded. Caller commits."""
    if conn.execute("SELECT 1 FROM sap_exports WHERE export_id = %s", (export.export_id,)).fetchone():
        return False
    weeks = {w for s in export.sales.values() for w in s}
    conn.execute("""INSERT INTO sap_exports (export_id, source, data_as_of, sku_count, week_count)
                    VALUES (%s, %s, %s, %s, %s)""",
                 (export.export_id, export.source, export.data_as_of, len(export.locations), len(weeks)))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO sales_weekly (export_id, sku_id, iso_week, units_sold) VALUES (%s, %s, %s, %s)",
                        [(export.export_id, sku, w, u) for sku, weeks_ in export.sales.items()
                         for w, u in sorted(weeks_.items())])
        cur.executemany("""INSERT INTO inventory_snapshot (export_id, sku_id, location, units_on_hand, as_of_date)
                           VALUES (%s, %s, %s, %s, %s)""",
                        [(export.export_id, sku, loc, u, export.data_as_of)
                         for sku, locs in export.locations.items() for loc, u in sorted(locs.items())])
    return True
