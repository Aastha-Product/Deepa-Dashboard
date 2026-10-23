"""Minimal Supabase Storage client for the private sap-exports bucket (server side only)."""
import os

import httpx

BUCKET = "sap-exports"


class StorageError(Exception):
    pass


def _settings():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise StorageError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set to use Supabase Storage")
    return url, key


def _headers(key: str) -> dict:
    headers = {"apikey": key}
    if key.startswith("eyJ"):  # legacy JWT service_role key; new sb_secret_ keys go in apikey only
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _client(client):
    return client or httpx.Client(timeout=30)


def list_folders(client: httpx.Client = None) -> list:
    """Top-level folder names in the bucket (one folder per export)."""
    url, key = _settings()
    c = _client(client)
    r = c.post(f"{url}/storage/v1/object/list/{BUCKET}", headers=_headers(key),
               json={"prefix": "", "limit": 1000, "offset": 0, "sortBy": {"column": "name", "order": "desc"}})
    if r.status_code != 200:
        raise StorageError(f"listing {BUCKET} failed: HTTP {r.status_code} {r.text[:200]}")
    # Folders come back with id = null; files have an id.
    return sorted((o["name"] for o in r.json() if o.get("id") is None), reverse=True)


def download_text(path: str, client: httpx.Client = None) -> str:
    url, key = _settings()
    r = _client(client).get(f"{url}/storage/v1/object/{BUCKET}/{path}", headers=_headers(key))
    if r.status_code != 200:
        raise StorageError(f"downloading {path} failed: HTTP {r.status_code}")
    return r.content.decode("utf-8-sig")


def upload_text(path: str, text: str, client: httpx.Client = None) -> None:
    url, key = _settings()
    headers = {**_headers(key), "Content-Type": "text/csv", "x-upsert": "true"}
    r = _client(client).post(f"{url}/storage/v1/object/{BUCKET}/{path}", headers=headers,
                             content=text.encode("utf-8"))
    if r.status_code not in (200, 201):
        raise StorageError(f"uploading {path} failed: HTTP {r.status_code} {r.text[:200]}")
