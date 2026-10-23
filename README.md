# Freshroots Demand Planning Engine

MESA AI-Native Track, Term 2, Case 01 ("Deepa's 90-Day Mandate"): the Demand Planning build, on Supabase + Vercel + Zoho.
Plan: `../03-build-implementation-plan.md` · Components map: `Freshroots_Components_Map_Demand_Planning.pdf`

**Status:**
- P0 (setup) and P1 (knowledge files + thresholds) are done.
- P2 onwards isn't built yet.

## Layout
| Path | What |
|---|---|
| `knowledge/` | Map step 0: `forecasting_rules.md` (Raghav's 5 rules + added rules A–C), `festival_calendar.md`, `thresholds.yaml` (every number the engine uses) |
| `data/seed/` | The 5 seed CSVs, renamed: sku_master, inventory_snapshot, distributors, festival_calendar, sales_weekly |
| `data/inbox/` | Where IT's scheduled SAP export lands (not committed) |
| `job/` | Python job. So far: `config.py`, `db.py`, `thresholds.py` |
| `supabase/` | Supabase project config (`config.toml`) and `migrations/`. The Supabase GitHub integration applies these to the project; `python -m job.db init` applies them by hand. So far: versioned thresholds with sign-off, row-level security on |

## Stack
- **Supabase:** Postgres, a Storage bucket for SAP exports, and Auth for dashboard sign-in.
- **Vercel:** one Python (FastAPI) app hosting the Mon + Thu cron endpoint and the dashboard (P6–P9).
- **Zoho Inventory:** open POs, read-only, India data centre (P3).
- **Claude API:** reasons and exception review (P5).
- **SMTP:** summary email and IT alert (P8).

No Docker, no Metabase. Until P6, the job runs locally from a virtual environment.

## Run locally
```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # then add the Supabase pooler connection string as DATABASE_URL
.venv\Scripts\python -m job.db init
.venv\Scripts\python -m job.thresholds validate
.venv\Scripts\python -m job.thresholds load
.venv\Scripts\python -m job.thresholds status
.venv\Scripts\python -m job.thresholds sign --version 1 --by "Deepa Nair"   # Deepa only
.venv\Scripts\python -m job.thresholds check
.venv\Scripts\python -m pytest -q
```

## Supabase + GitHub
- **Project:** `Deepa-Dashboard` (ref `tjhulermfpkbanrwesmy`, region ap-southeast-2), in Aastha-Product's Org.
- **Integration:** linked to this repo, working directory `.`, deploy to production from `main` (on since 23 Oct 2026).
- **Schema changes:** a new file in `supabase/migrations/`, named `YYYYMMDDHHMMSS_name.sql`. Never edit a migration that has already been applied.
- **GitHub integration:** in Supabase, go to Project Settings → Integrations → GitHub, choose this repo, set the Supabase directory to `supabase`, and turn on deploy to production for `main`. Migrations then apply on every merge to `main`.
- **Secrets:** the job's database connection string and service role key go in `.env` (local) and Vercel environment variables. They never go in the repo.

## Thresholds and sign-off (map step 0)
- Raghav edits `knowledge/thresholds.yaml` and `forecasting_rules.md`.
- `load` stores the values as a new **version** only when something changed. Values and the rules file are hashed; comments and whitespace are ignored.
- Loaded versions can't be edited or deleted; database triggers enforce this.
- Only the signer named in the file (**Deepa Nair**) can sign a version, and only once.
- `check` fails unless the version matching the current files is signed off. The job will call this before every run.
- `validate` also checks that the festival months in `thresholds.yaml` match `data/seed/festival_calendar.csv`.

## Still pending sign-off (not blocking the build)
- **Raghav:** added rule A (stockout top-up, `rules.reorder_qty`) and `rules.high_demand.long_window_includes_short`.
- **Deepa:** the sign-off itself. It's not done on her behalf; version 1 stays unsigned until she signs.

## Secrets
`.env` is git-ignored and never leaves the machine running the job. `.env.example` lists the variable names. `config.py` reads `.env` automatically; real environment variables take priority.
