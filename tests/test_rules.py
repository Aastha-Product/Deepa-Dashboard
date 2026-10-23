import csv
from datetime import date

import pytest

from job import config, ingest, rules, thresholds

SEED = config.DATA_DIR / "seed"
RUN_DATE = date(2026, 10, 19)   # Monday run on the seed export; October = festival month


@pytest.fixture(scope="module")
def t():
    return thresholds.read().values


@pytest.fixture(scope="module")
def master():
    with open(SEED / "sku_master.csv", newline="", encoding="utf-8") as f:
        return {r["SKU_ID"]: r for r in csv.DictReader(f)}


@pytest.fixture(scope="module")
def results(t, master):
    export = ingest.from_folder(SEED, set(master))
    out = {}
    for sku, m in master.items():
        s = rules.SkuInput(sku, m["Perishability"], int(m["Standard_Reorder_Qty"]),
                           export.sales[sku], export.on_hand[sku], open_po_qty=0)
        out[sku] = rules.evaluate(s, RUN_DATE, t)
    return out


# Golden test: plan 03 section 7.
GOLDEN = {
    #         flags (hd, so, ov)     cover  action    qty  basis    after  exception
    "SKU001": ((True, True, False), 3.35, "reorder", 750, "1.5x", 6.14, "conflicting_flags"),
    "SKU003": ((False, True, False), 4.36, "reorder", 309, "topup", 6.00, None),
    "SKU004": ((False, True, False), 1.80, "reorder", 150, "std", 3.31, None),
    "SKU011": ((False, True, False), 1.80, "reorder", 500, "std", 3.76, None),
    "SKU013": ((False, True, False), 3.17, "reorder", 161, "topup", 6.00, None),
    "SKU006": ((False, False, True), 22.23, "pause", 0, "none", None, None),
    "SKU007": ((False, False, True), 4.08, "pause", 0, "none", None, None),
}


@pytest.mark.parametrize("sku", sorted(GOLDEN))
def test_golden_flagged(results, sku):
    (hd, so, ov), cover, action, qty, basis, after, exc = GOLDEN[sku]
    r = results[sku]
    assert (r.flag_high_demand, r.flag_stockout, r.flag_overstock) == (hd, so, ov)
    assert r.cover_weeks == pytest.approx(cover, abs=0.01)
    assert (r.action, r.qty, r.qty_basis, r.exception_type) == (action, qty, basis, exc)
    if after is None:
        assert r.cover_after is None
    else:
        assert r.cover_after == pytest.approx(after, abs=0.01)


def test_golden_ok_skus(results):
    ok = {s for s, r in results.items() if not r.flagged}
    assert ok == {"SKU002", "SKU005", "SKU008", "SKU009", "SKU010", "SKU012", "SKU014", "SKU015"}
    assert all(results[s].action == "none" and results[s].qty == 0 for s in ok)
    assert min(results[s].cover_weeks for s in ok) == pytest.approx(6.76, abs=0.01)
    assert max(results[s].cover_weeks for s in ok) == pytest.approx(11.46, abs=0.01)


def test_golden_totals(results):
    assert sum(r.flag_stockout for r in results.values()) == 5
    assert sum(r.flag_overstock for r in results.values()) == 2
    assert sum(r.flag_high_demand for r in results.values()) == 1
    assert sum(bool(r.exception_type) for r in results.values()) == 1
    assert sum(r.action == "reorder" for r in results.values()) == 5


def test_window_is_w31_to_w42(results):
    r = results["SKU001"]
    assert r.window_weeks[0] == "2026-W31" and r.window_weeks[-1] == "2026-W42"
    assert r.uplift == 1.25 and r.avg12 == pytest.approx(214.92, abs=0.01) and r.last4_avg == 261.0


def test_next_gates(results):
    assert results["SKU001"].next_gate == 1
    assert results["SKU013"].next_gate == 2 and results["SKU006"].next_gate == 2
    assert results["SKU002"].next_gate is None


# --- boundaries and edge cases ---

def sku(weekly, on_hand, perish="Low", std=100, open_po=0):
    weeks = [f"2026-W{w:02d}" for w in range(31, 31 + len(weekly))]
    return rules.SkuInput("X", perish, std, dict(zip(weeks, weekly)), on_hand, open_po)


AFTER_W42 = date(2026, 10, 19)      # Oct: festival month
NON_FESTIVAL = date(2026, 12, 21)   # Dec run; weeks up to W51 count


def flat(n=12, v=100):
    return [v] * n


def test_exactly_20_percent_is_not_high_demand(t):
    # 8 weeks at 100 and 4 at 125 -> avg12 = 108.33, last4 = 125 (+15.4%): not high
    assert not rules.evaluate(sku([100] * 8 + [125] * 4, 10_000), AFTER_W42, t).flag_high_demand
    # long window excluding... use exact: avg12 x 1.2 == last4 -> last4 = 1.2 * avg12
    # 8*a + 4*b = 12*m, b = 1.2 m -> 8a = 12m - 4.8m = 7.2m -> a = 0.9 m ; m=100 -> a=90, b=120
    r = rules.evaluate(sku([90] * 8 + [120] * 4, 10_000), AFTER_W42, t)
    assert r.change_pct == pytest.approx(20.0)
    assert not r.flag_high_demand
    r = rules.evaluate(sku([90] * 8 + [121] * 4, 10_000), AFTER_W42, t)
    assert r.flag_high_demand


def test_exactly_6_weeks_is_not_stockout(t):
    # demand 100 x 1.25 = 125/wk; 750 on hand = exactly 6.0 w
    assert not rules.evaluate(sku(flat(), 750), AFTER_W42, t).flag_stockout
    assert rules.evaluate(sku(flat(), 749), AFTER_W42, t).flag_stockout


def test_exactly_4_weeks_perishable_is_not_overstock(t):
    assert not rules.evaluate(sku(flat(), 500, perish="High"), AFTER_W42, t).flag_overstock
    assert rules.evaluate(sku(flat(), 501, perish="High"), AFTER_W42, t).flag_overstock


def test_no_uplift_outside_festival_months(t):
    weekly = flat(n=21)   # W31..W51
    r = rules.evaluate(sku(weekly, 600), NON_FESTIVAL, t)
    assert r.uplift == 1.0 and r.demand_weekly == 100 and r.cover_weeks == 6.0 and not r.flag_stockout


def test_open_pos_count_toward_cover(t):
    r = rules.evaluate(sku(flat(), 400, open_po=400), AFTER_W42, t)
    assert r.cover_weeks == pytest.approx(6.4) and not r.flag_stockout


def test_unknown_open_pos_use_on_hand_only(t):
    r = rules.evaluate(sku(flat(), 400, open_po=None), AFTER_W42, t)
    assert r.open_po_qty is None and r.flag_stockout


def test_perishable_reorder_never_above_max(t):
    # demand 125/wk, max 4 w = 500 units; 100 on hand, std 1000 -> capped at 400
    r = rules.evaluate(sku(flat(), 100, perish="High", std=1000), AFTER_W42, t)
    assert r.action == "reorder" and r.qty == 400 and r.cover_after == pytest.approx(4.0)


def test_new_sku_goes_to_raghav(t):
    r = rules.evaluate(sku(flat(n=8), 100), AFTER_W42, t)
    assert (r.exception_type, r.action, r.qty) == ("new_sku", "review", 0)


def test_zero_demand_goes_to_raghav(t):
    r = rules.evaluate(sku([0] * 12, 100), AFTER_W42, t)
    assert (r.exception_type, r.action) == ("zero_demand", "review")


def test_high_demand_with_overstock_is_conflicting(t):
    r = rules.evaluate(sku([100] * 8 + [200] * 4, 5_000), AFTER_W42, t)
    assert r.flag_high_demand and r.flag_overstock
    assert (r.exception_type, r.action, r.qty) == ("conflicting_flags", "review", 0)


def test_incomplete_current_week_is_ignored(t):
    weekly = flat(n=12)
    weekly.append(5)   # W43 still in progress on Wed 21 Oct
    r = rules.evaluate(sku(weekly, 10_000), date(2026, 10, 21), t)
    assert r.window_weeks[-1] == "2026-W42" and r.avg12 == 100


def test_template_reasons_use_only_computed_numbers(results):
    assert rules.template_reason(results["SKU013"]) == (
        "Cover 3.2 w is below the 6 w minimum. Reorder 161 (topped up to reach 6 w), giving 6.0 w.")
    assert "Conflicting flags" in rules.template_reason(results["SKU001"])
    assert rules.template_reason(results["SKU002"]).startswith("Cover 11.5 w is inside the 6-14 w window")
