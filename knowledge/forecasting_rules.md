# Freshroots Forecasting Rules

**Written by:** Raghav Kulkarni, Senior Demand Planner
**Sign-off:** Deepa Nair, Head of Operations (recorded in Postgres by `python -m job.thresholds sign`, not in this file)
**Numbers:** every number below lives in `thresholds.yaml`. The engine reads that file, never this one. Editing either file creates a new version that needs sign-off again.

These rules cover about 80–85% of Raghav's monthly recommendations. The other 15–20% (new product launches, unusual distributor behaviour, one-off market events) stay with Raghav's judgment and go to him as exceptions.

## The five documented rules (Case 01, Demand Planning brief)

1. **High demand.** If an SKU's sales in the last 4 weeks are more than 20% above its 12-week average, flag it as high-demand and recommend a reorder at 1.5× the standard quantity.
2. **Stockout risk.** If current stock falls below 6 weeks of average weekly sales, flag it as stockout risk and recommend an immediate reorder.
3. **Overstock risk.** If current stock exceeds 14 weeks of average weekly sales, flag it as overstock risk and recommend pausing the next order.
4. **Festival months.** In October, November and March, apply a 25% demand uplift before any threshold checks.
5. **High-perishability SKUs** follow tighter windows: a maximum of 4 weeks of stock and a minimum of 3 weeks.

## How the engine applies them (order is fixed in code)

1. Take the latest 12 complete ISO weeks of sales. The last 4 of those are the "last 4 weeks".
2. 12-week average = mean of all 12 weeks, including the last 4. *(Pending Raghav's confirmation, key `rules.high_demand.long_window_includes_short`.)*
3. Festival uplift first: in a festival month, weekly demand = 12-week average × 1.25.
4. Weeks of cover = (units on hand + open PO units) ÷ weekly demand.
5. Window: high-perishability SKUs use min 3 / max 4 weeks; all others use min 6 / max 14.
6. Flags: high demand (rule 1), stockout risk (cover below min), overstock risk (cover above max).
7. Quantity: see below.

## Added rules: need Raghav's sign-off

These aren't in Raghav's written logic. They were agreed in the dashboard plan (23 Oct 2026) and need his sign-off before go-live.

- **A. Reorder quantity for a stockout** (rule 2 says "immediate reorder" but gives no quantity). Start from the standard reorder quantity, or 1.5× for high demand. If that still leaves cover under the minimum, top up to reach the minimum. Never order above the maximum.
- **B. Exceptions go to Raghav** instead of getting a final number:
  - conflicting flags (high demand together with stockout or overstock)
  - an SKU with fewer than 12 weeks of history (new SKU)
  - zero demand (cover can't be computed)
  - a distributor's orders swinging by more than 30%. This one is switched off until the SAP export includes distributor-level sales.
- **C. Stale data stops the run.** If the export is more than 48 hours old, there are no recommendations and IT is alerted.
