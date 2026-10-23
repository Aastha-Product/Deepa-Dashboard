import sys

import psycopg

from job import config


def connect() -> psycopg.Connection:
    # Supabase's transaction-mode pooler doesn't support prepared statements.
    return psycopg.connect(config.database_url(), prepare_threshold=None)


def migration_files() -> list:
    return sorted(config.MIGRATIONS_DIR.glob("*.sql"))


def init(conn: psycopg.Connection) -> list:
    """Apply every Supabase migration in order. Each is written to be safe to re-run."""
    files = migration_files()
    for path in files:
        conn.execute(path.read_text(encoding="utf-8"))
    return files


if __name__ == "__main__":
    if sys.argv[1:] != ["init"]:
        sys.exit("usage: python -m job.db init")
    with connect() as conn:
        applied = init(conn)
    print("Applied: " + ", ".join(p.name for p in applied))
