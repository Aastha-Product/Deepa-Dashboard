-- P6: switches Deepa or IT can change in Supabase's Table Editor without a redeploy. Safe to re-run.

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    note       TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;

INSERT INTO app_settings (key, value, note) VALUES
    ('shadow_mode', 'true', 'true for the first 3-4 weeks: runs alongside Raghav''s manual forecast, nobody orders from it. Deepa switches it to false at go-live.')
ON CONFLICT (key) DO NOTHING;
