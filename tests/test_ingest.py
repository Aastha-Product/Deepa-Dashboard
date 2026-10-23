import csv
from datetime import date, datetime

import pytest

from job import config, ingest

SEED = config.DATA_DIR / "seed"


@pytest.fixture(scope="module")
def skus():
    with open(SEED / "sku_master.csv", newline="", encoding="utf-8") as f:
        return {r["SKU_ID"] for r in csv.DictReader(f)}


def texts():
    return ((SEED / ingest.SALES_FILE).read_text(encoding="utf-8"),
            (SEED / ingest.INVENTORY_FILE).read_text(encoding="utf-8"))


def test_seed_export_parses(skus):
    e = ingest.from_folder(SEED, skus, export_id="seed-2026-10-19")
    assert e.data_as_of == date(2026, 10, 19)
    assert len(e.sales) == 15 and len(e.on_hand) == 15
    assert sum(len(w) for w in e.sales.values()) == 240
    assert e.on_hand["SKU001"] == 900
    assert e.sales["SKU001"]["2026-W42"] == 259


def test_missing_column(skus):
    sales, inv = texts()
    with pytest.raises(ingest.ExportError, match="missing column.*Units_Sold"):
        ingest.parse("x", "t", sales.replace("Units_Sold", "Units"), inv, skus)


def test_unknown_sku(skus):
    sales, inv = texts()
    with pytest.raises(ingest.ExportError, match="unknown SKU SKU999"):
        ingest.parse("x", "t", sales + "SKU999,2026-W42,5\n", inv, skus)


def test_duplicate_week(skus):
    sales, inv = texts()
    with pytest.raises(ingest.ExportError, match="duplicate SKU001 2026-W42"):
        ingest.parse("x", "t", sales + "SKU001,2026-W42,5\n", inv, skus)


def test_negative_units(skus):
    sales, inv = texts()
    with pytest.raises(ingest.ExportError, match="negative units"):
        ingest.parse("x", "t", sales.replace("SKU001,2026-W42,259", "SKU001,2026-W42,-1"), inv, skus)


def test_bad_week_format(skus):
    sales, inv = texts()
    with pytest.raises(ingest.ExportError, match="not like 2026-W42"):
        ingest.parse("x", "t", sales.replace("SKU001,2026-W42", "SKU001,2026-42"), inv, skus)


def test_mixed_stock_dates(skus):
    sales, inv = texts()
    with pytest.raises(ingest.ExportError, match="mixes stock dates"):
        ingest.parse("x", "t", sales, inv.replace("SKU015,Pune Central Warehouse,360,2026-10-19",
                                                   "SKU015,Pune Central Warehouse,360,2026-10-12"), skus)


def test_missing_stock_row(skus):
    sales, inv = texts()
    inv = "\n".join(l for l in inv.splitlines() if not l.startswith("SKU015"))
    with pytest.raises(ingest.ExportError, match="no stock row for SKU015"):
        ingest.parse("x", "t", sales, inv, skus)


def test_data_age_monday_run_is_fresh():
    run = datetime(2026, 10, 19, 6, 0, tzinfo=ingest.IST)
    assert ingest.data_age_hours(date(2026, 10, 19), run) == 6.0


def test_data_age_four_days_later_is_stale():
    run = datetime(2026, 10, 23, 6, 0, tzinfo=ingest.IST)
    assert ingest.data_age_hours(date(2026, 10, 19), run) == 102.0
