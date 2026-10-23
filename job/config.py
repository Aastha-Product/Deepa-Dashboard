import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """Read KEY=value lines from .env into the environment; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(ENV_FILE)

KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", ROOT / "knowledge"))
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"

THRESHOLDS_FILE = KNOWLEDGE_DIR / "thresholds.yaml"
FESTIVAL_CALENDAR_CSV = DATA_DIR / "seed" / "festival_calendar.csv"


PASSWORD_PLACEHOLDER = "REPLACE_WITH_DB_PASSWORD"


def database_configured() -> bool:
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    return bool(url) and PASSWORD_PLACEHOLDER not in url


# libpq rejects query parameters it doesn't know (Supabase's Vercel integration adds e.g. supa=...).
_LIBPQ_PARAMS = {"sslmode", "sslrootcert", "connect_timeout", "application_name", "options", "target_session_attrs"}


def _clean(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k in _LIBPQ_PARAMS]
    if not any(k == "sslmode" for k, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def database_url() -> str:
    # DATABASE_URL locally; POSTGRES_URL (pooler) when set by Supabase's Vercel integration.
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; add the Supabase pooler connection string to .env")
    if PASSWORD_PLACEHOLDER in url:
        raise RuntimeError(f"DATABASE_URL still contains {PASSWORD_PLACEHOLDER}; put the Supabase database password in .env")
    return _clean(url)
