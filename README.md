# Freshroots Demand Planning Engine

MESA AI-Native Track, Term 2, Case 01 ("Deepa's 90-Day Mandate"): the Demand Planning build, on Supabase + Vercel + Zoho.
Plan: `../03-build-implementation-plan.md` · Components map: `Freshroots_Components_Map_Demand_Planning.pdf`

**Live:** https://deepa-dashboard.vercel.app (Vercel project `deepa-dashboard`, deployed from `main`)

**Status:** P0–P9 built. Waiting on secrets in Vercel and `.env` (database password, keys) before the first real run. P10 (end-to-end run on Supabase and the demo) is next.

| Phase | What | Where |
|---|---|---|
| P1 | Thresholds, versions, sign-off | `knowledge/`, `job/thresholds.py` |
| P2 | Tables, `sap-exports` bucket, reference data | `supabase/migrations/` |
| P3 | SAP export reader, 48 h check, Zoho (read-only) | `job/ingest.py`, `job/storage.py`, `job/zoho.py` |
| P4 | Rules engine + golden test | `job/rules.py`, `tests/test_rules.py` |
| P5 | Claude reasons (text only, with fallback) | `job/explain.py` |
| P6 | Run orchestration, cron endpoint | `job/run.py`, `app/main.py`, `vercel.json` |
| P7 | Dashboard: sign-in + 5 tabs | `app/`, `app/templates/` |
| P8 | Summary email + IT alert | `job/notify.py` |
| P9 | Gate 1 / Gate 2 forms, write-offs, impact | `app/main.py`, `app/queries.py` |

## Dashboard
The dashboard is **Deepa Nair's** (Head of Operations), who checks it after each Mon + Thu 06:00 run. There's no sign-in and no role picker. Every decision is recorded as "Deepa Nair (dashboard)".

| Page | What Deepa does there |
|---|---|
| **Overview** | Sees what needs her decision, what changed since the last run, every SKU's weeks of cover against its window, and an impact snapshot |
| **Decisions** | Gate 1: accepts, changes or overrides exceptions (a reason is needed to change). Gate 2: approves reorder and pause lines, one by one or all at once. Approved lines are raised in Zoho by hand. |
| **SKU detail** | 16-week sales chart, the rule trace, the reason and the history for one SKU |
| **Impact & rules** | Signs off the rules (map step 0), tracks acceptance and override rates, kill criteria, and logs write-offs |
| **Data & runs** | Uploads an export and runs now (with an optional demo replay time) |

Anyone with the link can use the dashboard, so share it only with Deepa. The scheduled `/api/run` is protected by `CRON_SECRET`.

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

## Connections
**Data & runs → Connections** checks each service live: Supabase database, Supabase storage, the Vercel schedule, Zoho, Claude and email. The header shows "All connections working" or how many aren't set up.

### Connect Zoho Inventory (read-only, India data centre)
1. At https://api-console.zoho.in: **Add Client → Self Client → Create**.
2. **Generate Code** with scopes `ZohoInventory.purchaseorders.READ,ZohoInventory.items.READ,ZohoInventory.settings.READ`, valid for 10 minutes.
3. Run `.venv\Scripts\python tools\zoho_setup.py` in a normal Windows terminal and paste the Client ID, Client Secret and code. Input is hidden. The script finds the organisation, checks it can read items and purchase orders, and writes `.env` and `zoho.vercel.env`.
4. In Vercel → deepa-dashboard → Settings → Environment Variables, click **Import .env**, choose `zoho.vercel.env`, save and redeploy. Then delete `zoho.vercel.env`.

Item SKUs in Zoho must be SKU001…SKU015, because purchase-order lines only carry Zoho's `item_id` and the app maps it to the SKU through the items list.

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
