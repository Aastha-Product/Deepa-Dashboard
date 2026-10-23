-- P2: core tables for the demand planning engine. Safe to run more than once.
-- Every table has row-level security on and no policies: only the server (table owner) reads or writes.

CREATE TABLE IF NOT EXISTS distributors (
    distributor_id   TEXT PRIMARY KEY,
    distributor_name TEXT NOT NULL,
    region           TEXT NOT NULL,
    status           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sku_master (
    sku_id               TEXT PRIMARY KEY,
    sku_name             TEXT NOT NULL,
    category             TEXT NOT NULL,
    perishability        TEXT NOT NULL CHECK (perishability IN ('Low', 'High')),
    standard_reorder_qty INTEGER NOT NULL CHECK (standard_reorder_qty > 0),
    unit_of_measure      TEXT NOT NULL,
    distributor_id       TEXT NOT NULL REFERENCES distributors(distributor_id)
);

CREATE TABLE IF NOT EXISTS festival_calendar (
    year_month  TEXT PRIMARY KEY,               -- YYYY-MM
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    is_festival BOOLEAN NOT NULL
);

-- One row per SAP export (the files IT uploads to the sap-exports bucket).
CREATE TABLE IF NOT EXISTS sap_exports (
    export_id   TEXT PRIMARY KEY,
    source      TEXT NOT NULL,                  -- storage path prefix or local folder
    data_as_of  DATE NOT NULL,                  -- inventory As_Of_Date
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sku_count   INTEGER NOT NULL,
    week_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_weekly (
    export_id  TEXT NOT NULL REFERENCES sap_exports(export_id) ON DELETE CASCADE,
    sku_id     TEXT NOT NULL REFERENCES sku_master(sku_id),
    iso_week   TEXT NOT NULL,                   -- e.g. 2026-W42
    units_sold INTEGER NOT NULL CHECK (units_sold >= 0),
    PRIMARY KEY (export_id, sku_id, iso_week)
);

CREATE TABLE IF NOT EXISTS inventory_snapshot (
    export_id     TEXT NOT NULL REFERENCES sap_exports(export_id) ON DELETE CASCADE,
    sku_id        TEXT NOT NULL REFERENCES sku_master(sku_id),
    location      TEXT NOT NULL,
    units_on_hand INTEGER NOT NULL CHECK (units_on_hand >= 0),
    as_of_date    DATE NOT NULL,
    PRIMARY KEY (export_id, sku_id, location)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id             BIGSERIAL PRIMARY KEY,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    run_as_of          TIMESTAMPTZ NOT NULL,    -- the clock the rules used (RUN_AS_OF replays a past run)
    triggered_by       TEXT NOT NULL,           -- cron / manual / cli
    export_id          TEXT REFERENCES sap_exports(export_id),
    data_as_of         DATE,
    data_age_hours     NUMERIC(8, 1),
    festival_month     BOOLEAN,
    uplift             NUMERIC(6, 3),
    thresholds_version INTEGER REFERENCES threshold_versions(version_id),
    status             TEXT NOT NULL CHECK (status IN ('running', 'ok', 'stopped_stale', 'stopped_unsigned', 'failed')),
    status_detail      TEXT,
    shadow_mode        BOOLEAN NOT NULL DEFAULT true,
    zoho_status        TEXT CHECK (zoho_status IN ('ok', 'unavailable', 'not_configured')),
    reasons_source     TEXT CHECK (reasons_source IN ('claude', 'template', 'mixed'))
);

-- Open purchase orders pulled from Zoho for a run (map step 3).
CREATE TABLE IF NOT EXISTS open_pos (
    run_id        BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    po_number     TEXT NOT NULL,
    sku_id        TEXT NOT NULL,
    qty_open      INTEGER NOT NULL CHECK (qty_open >= 0),
    po_date       DATE,
    expected_date DATE,
    PRIMARY KEY (run_id, po_number, sku_id)
);

-- One row per SKU per run (map step 8).
CREATE TABLE IF NOT EXISTS sku_recommendations (
    run_id            BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    sku_id            TEXT NOT NULL REFERENCES sku_master(sku_id),
    history_weeks     INTEGER NOT NULL,
    last4_avg         NUMERIC(12, 2),
    avg12             NUMERIC(12, 2),
    change_pct        NUMERIC(8, 2),
    uplift            NUMERIC(6, 3) NOT NULL,
    demand_weekly     NUMERIC(12, 2),
    on_hand           INTEGER NOT NULL,
    open_po_qty       INTEGER,                  -- NULL = Zoho unavailable (never assumed 0)
    cover_weeks       NUMERIC(8, 2),
    min_weeks         NUMERIC(6, 2) NOT NULL,
    max_weeks         NUMERIC(6, 2) NOT NULL,
    flag_high_demand  BOOLEAN NOT NULL,
    flag_stockout     BOOLEAN NOT NULL,
    flag_overstock    BOOLEAN NOT NULL,
    action            TEXT NOT NULL CHECK (action IN ('reorder', 'pause', 'none', 'review')),
    qty               INTEGER NOT NULL CHECK (qty >= 0),
    qty_basis         TEXT NOT NULL CHECK (qty_basis IN ('1.5x', 'std', 'topup', 'none')),
    cover_after       NUMERIC(8, 2),
    exception_type    TEXT CHECK (exception_type IN ('conflicting_flags', 'new_sku', 'zero_demand', 'distributor_swing')),
    reason            TEXT,
    reason_source     TEXT CHECK (reason_source IN ('claude', 'template')),
    needs_judgment    BOOLEAN NOT NULL DEFAULT false,
    why_unusual       TEXT,
    PRIMARY KEY (run_id, sku_id)
);

-- Gate 1 (Raghav) and Gate 2 (Procurement). A change or override always needs a reason.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL,
    sku_id      TEXT NOT NULL,
    gate        SMALLINT NOT NULL CHECK (gate IN (1, 2)),
    decided_by  TEXT NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('accept', 'change', 'override', 'approve')),
    final_qty   INTEGER CHECK (final_qty >= 0),
    reason      TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    shadow      BOOLEAN NOT NULL,
    FOREIGN KEY (run_id, sku_id) REFERENCES sku_recommendations(run_id, sku_id) ON DELETE CASCADE,
    CONSTRAINT reason_required CHECK (
        decision IN ('accept', 'approve') OR length(btrim(coalesce(reason, ''))) > 0
    ),
    CONSTRAINT gate_decisions CHECK (
        (gate = 1 AND decision IN ('accept', 'change', 'override'))
        OR (gate = 2 AND decision IN ('approve', 'change'))
    )
);

-- Map step 13: outcomes in units (Decision 3).
CREATE TABLE IF NOT EXISTS outcomes_log (
    outcome_id BIGSERIAL PRIMARY KEY,
    sku_id     TEXT NOT NULL REFERENCES sku_master(sku_id),
    iso_week   TEXT NOT NULL,
    event      TEXT NOT NULL CHECK (event IN ('stockout', 'write_off')),
    units      INTEGER NOT NULL CHECK (units >= 0),
    source     TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    run_id     BIGINT REFERENCES runs(run_id) ON DELETE SET NULL,
    logged_by  TEXT NOT NULL,
    note       TEXT,
    logged_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_users (
    email TEXT PRIMARY KEY CHECK (email = lower(email)),
    name  TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('raghav', 'procurement', 'deepa', 'it', 'viewer'))
);

CREATE INDEX IF NOT EXISTS runs_status_started_idx ON runs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS decisions_run_sku_idx ON decisions (run_id, sku_id, gate, decided_at DESC);
CREATE INDEX IF NOT EXISTS outcomes_sku_week_idx ON outcomes_log (sku_id, iso_week);

ALTER TABLE distributors        ENABLE ROW LEVEL SECURITY;
ALTER TABLE sku_master          ENABLE ROW LEVEL SECURITY;
ALTER TABLE festival_calendar   ENABLE ROW LEVEL SECURITY;
ALTER TABLE sap_exports         ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_weekly        ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_snapshot  ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs                ENABLE ROW LEVEL SECURITY;
ALTER TABLE open_pos            ENABLE ROW LEVEL SECURITY;
ALTER TABLE sku_recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions           ENABLE ROW LEVEL SECURITY;
ALTER TABLE outcomes_log        ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_users           ENABLE ROW LEVEL SECURITY;

-- Private bucket where IT's scheduled SAP export lands (Vercel has no permanent disk).
INSERT INTO storage.buckets (id, name, public)
VALUES ('sap-exports', 'sap-exports', false)
ON CONFLICT (id) DO NOTHING;
