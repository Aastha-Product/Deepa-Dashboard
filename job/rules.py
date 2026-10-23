"""Data prep and Raghav's rules, in a fixed order (components map steps 4-5).

Every number in a recommendation is computed here. The AI step only explains them.
Thresholds come from the signed-off version (job.thresholds); nothing is hard-coded.
"""
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

EPS = 1e-9


@dataclass
class SkuInput:
    sku_id: str
    perishability: str          # Low / High
    standard_qty: int
    weekly_sales: dict          # iso_week -> units
    on_hand: int
    open_po_qty: int | None     # None = Zoho unavailable


@dataclass
class Recommendation:
    sku_id: str
    history_weeks: int
    last4_avg: float | None
    avg12: float | None
    change_pct: float | None
    uplift: float
    demand_weekly: float | None
    on_hand: int
    open_po_qty: int | None
    cover_weeks: float | None
    min_weeks: float
    max_weeks: float
    flag_high_demand: bool = False
    flag_stockout: bool = False
    flag_overstock: bool = False
    action: str = "none"        # reorder / pause / none / review
    qty: int = 0
    qty_basis: str = "none"     # 1.5x / std / topup / none
    cover_after: float | None = None
    exception_type: str | None = None
    window_weeks: list = field(default_factory=list)   # the 12 weeks used, oldest first

    @property
    def flagged(self) -> bool:
        return self.flag_high_demand or self.flag_stockout or self.flag_overstock or bool(self.exception_type)

    @property
    def next_gate(self) -> int | None:
        if self.exception_type:
            return 1
        return 2 if self.action in ("reorder", "pause") else None


def week_end(iso_week: str) -> date:
    year, week = iso_week.split("-W")
    return date.fromisocalendar(int(year), int(week), 7)


def complete_weeks(weekly_sales: dict, run_date: date) -> list:
    """Weeks that ended before the run date, oldest first."""
    return sorted(w for w in weekly_sales if week_end(w) < run_date)


def festival_uplift(run_date: date, t: dict) -> float:
    if run_date.month in t["rules.festival.months"]:
        return 1 + t["rules.festival.uplift_pct"] / 100
    return 1.0


def window_for(perishability: str, t: dict) -> tuple:
    kind = "high_perishability" if perishability == "High" else "standard"
    return (t[f"rules.cover_windows.{kind}.min_weeks"], t[f"rules.cover_windows.{kind}.max_weeks"])


def evaluate(s: SkuInput, run_date: date, t: dict) -> Recommendation:
    short_n = t["rules.high_demand.short_window_weeks"]
    long_n = t["rules.high_demand.long_window_weeks"]
    lo, hi = window_for(s.perishability, t)
    uplift = festival_uplift(run_date, t)                       # rule 4: uplift first
    weeks = complete_weeks(s.weekly_sales, run_date)
    stock = s.on_hand + (s.open_po_qty or 0)                    # open POs counted before any reorder

    rec = Recommendation(s.sku_id, len(weeks), None, None, None, uplift, None, s.on_hand, s.open_po_qty,
                         None, lo, hi)

    if len(weeks) < t["exceptions.new_sku_min_history_weeks"]:
        rec.action, rec.exception_type = "review", "new_sku"    # not enough history: a judgment call for Deepa
        return rec

    window = weeks[-long_n:]
    short = window[-short_n:]
    long_weeks = window if t["rules.high_demand.long_window_includes_short"] else weeks[-(long_n + short_n):-short_n]
    rec.window_weeks = window
    rec.last4_avg = sum(s.weekly_sales[w] for w in short) / len(short)
    rec.avg12 = sum(s.weekly_sales[w] for w in long_weeks) / len(long_weeks)

    if rec.avg12 <= 0:
        rec.action, rec.exception_type = "review", "zero_demand"
        return rec

    rec.change_pct = (rec.last4_avg / rec.avg12 - 1) * 100
    rec.demand_weekly = rec.avg12 * uplift
    rec.cover_weeks = stock / rec.demand_weekly

    rec.flag_high_demand = rec.last4_avg > rec.avg12 * (1 + t["rules.high_demand.threshold_pct"] / 100) + EPS
    rec.flag_stockout = rec.cover_weeks < lo - EPS
    rec.flag_overstock = rec.cover_weeks > hi + EPS

    if t["exceptions.conflicting_flags"] and rec.flag_high_demand and (rec.flag_stockout or rec.flag_overstock):
        rec.exception_type = "conflicting_flags"

    if rec.flag_overstock:
        rec.action = "review" if rec.exception_type else "pause"     # rule 3: pause the next order
        return rec

    if rec.flag_high_demand or rec.flag_stockout:
        base = int(round(s.standard_qty * t["rules.high_demand.reorder_multiplier"])) if rec.flag_high_demand \
            else s.standard_qty
        qty, basis = base, "1.5x" if rec.flag_high_demand else "std"
        if rec.flag_stockout and t["rules.reorder_qty.top_up_to_min_cover"]:
            need = math.ceil(lo * rec.demand_weekly - stock - EPS)
            if need > qty:
                qty, basis = need, "topup"
        if t["rules.reorder_qty.cap_at_max_cover"]:
            qty = min(qty, max(math.floor(hi * rec.demand_weekly - stock + EPS), 0))
        if qty > 0:
            rec.action, rec.qty, rec.qty_basis = "reorder", qty, basis
            rec.cover_after = (stock + qty) / rec.demand_weekly
    return rec


def template_reason(r: Recommendation) -> str:
    """Plain reason built from the numbers only; used when the AI step is off or fails."""
    if r.exception_type == "new_sku":
        return f"Only {r.history_weeks} weeks of sales history; needs a judgment call."
    if r.exception_type == "zero_demand":
        return "No sales in the last 12 weeks, so weeks of cover can't be worked out; needs a judgment call."
    parts = []
    if r.flag_high_demand:
        parts.append(f"Last 4 weeks {r.last4_avg:.0f}/wk are {r.change_pct:+.0f}% vs the 12-week average {r.avg12:.0f}/wk")
    if r.flag_stockout:
        parts.append(f"cover {r.cover_weeks:.1f} w is below the {r.min_weeks:g} w minimum")
    if r.flag_overstock:
        parts.append(f"cover {r.cover_weeks:.1f} w is above the {r.max_weeks:g} w maximum")
    if not parts:
        return f"Cover {r.cover_weeks:.1f} w is inside the {r.min_weeks:g}-{r.max_weeks:g} w window."
    text = "; ".join(parts)
    if r.action == "reorder":
        how = {"1.5x": "1.5x the standard quantity", "std": "the standard quantity",
               "topup": f"topped up to reach {r.min_weeks:g} w"}[r.qty_basis]
        text += f". Reorder {r.qty} ({how}), giving {r.cover_after:.1f} w."
    elif r.action == "pause":
        text += ". Pause the next order."
    if r.exception_type == "conflicting_flags":
        text += " Conflicting flags: needs a judgment call."
    return text[0].upper() + text[1:]
