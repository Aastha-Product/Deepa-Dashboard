-- Each download of approved reorder lines for Zoho (the PO is still raised by hand). Safe to re-run.
CREATE TABLE IF NOT EXISTS zoho_exports (
    export_id   BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    lines       INTEGER NOT NULL,
    units       INTEGER NOT NULL,
    exported_by TEXT NOT NULL,
    exported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE zoho_exports ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS zoho_exports_run_idx ON zoho_exports (run_id, exported_at DESC);
