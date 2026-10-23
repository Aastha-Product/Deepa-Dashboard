# Freshroots Festival Calendar

Festival months get a **25% demand uplift**, applied before any threshold check (rule 4 in `forecasting_rules.md`).

| Month | Festival month |
|---|---|
| March | Yes |
| October | Yes |
| November | Yes |
| All other months | No |

- **Source data:** `data/seed/festival_calendar.csv` (2025–2026, one row per month).
- **Consistency check:** `python -m job.thresholds validate` checks that the months in this calendar match `rules.festival.months` in `thresholds.yaml`, and fails if they differ.
