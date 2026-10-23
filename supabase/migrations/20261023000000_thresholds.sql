-- P1: versioned thresholds with sign-off. Later phases add the other tables in new migrations.
-- Applied by the Supabase GitHub integration on merge to main, or by `python -m job.db init`.
-- Safe to run more than once.

CREATE TABLE IF NOT EXISTS threshold_versions (
    version_id     SERIAL PRIMARY KEY,
    content_hash   TEXT NOT NULL UNIQUE,
    written_by     TEXT NOT NULL,
    sign_off_by    TEXT NOT NULL,
    rules_file_sha TEXT NOT NULL,
    values_json    JSONB NOT NULL,
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    signed_off_by  TEXT,
    signed_off_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS thresholds (
    version_id INTEGER NOT NULL REFERENCES threshold_versions(version_id),
    key        TEXT NOT NULL,
    value      JSONB NOT NULL,
    PRIMARY KEY (version_id, key)
);

-- No policies: Supabase's public API (anon / authenticated keys) can't read or write these.
-- The server job connects as the table owner, which RLS doesn't restrict.
ALTER TABLE threshold_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE thresholds ENABLE ROW LEVEL SECURITY;

-- A loaded version never changes. The only allowed update is a one-time sign-off.
CREATE OR REPLACE FUNCTION guard_threshold_versions() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'threshold versions cannot be deleted';
    END IF;
    IF OLD.signed_off_by IS NOT NULL THEN
        RAISE EXCEPTION 'version % is already signed off', OLD.version_id;
    END IF;
    IF NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.values_json IS DISTINCT FROM OLD.values_json
       OR NEW.rules_file_sha IS DISTINCT FROM OLD.rules_file_sha
       OR NEW.written_by IS DISTINCT FROM OLD.written_by
       OR NEW.sign_off_by IS DISTINCT FROM OLD.sign_off_by
       OR NEW.loaded_at IS DISTINCT FROM OLD.loaded_at THEN
        RAISE EXCEPTION 'threshold version % is immutable; load a new version instead', OLD.version_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION guard_thresholds() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'thresholds rows are immutable; load a new version instead';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_guard_threshold_versions ON threshold_versions;
CREATE TRIGGER trg_guard_threshold_versions
    BEFORE UPDATE OR DELETE ON threshold_versions
    FOR EACH ROW EXECUTE FUNCTION guard_threshold_versions();

DROP TRIGGER IF EXISTS trg_guard_thresholds ON thresholds;
CREATE TRIGGER trg_guard_thresholds
    BEFORE UPDATE OR DELETE ON thresholds
    FOR EACH ROW EXECUTE FUNCTION guard_thresholds();
