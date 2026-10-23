-- One person can hold several roles (e.g. a demo login acting as Raghav, Procurement and Deepa).
-- The dashboard lets them switch role; each decision records who made it and in which gate.
-- Safe to re-run.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'public.app_users'::regclass AND c.contype = 'p'
          AND array_length(c.conkey, 1) = 1
    ) THEN
        ALTER TABLE app_users DROP CONSTRAINT app_users_pkey;
        ALTER TABLE app_users ADD PRIMARY KEY (email, role);
    END IF;
END $$;
