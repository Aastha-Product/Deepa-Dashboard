"""Zoho Inventory, read-only: open purchase orders (map step 3) and PO read-back (step 12).

The OAuth client is created with read-only scopes, so this code cannot create or change a PO.
Freshroots is in India, so the defaults point at Zoho's India data centre (accounts.zoho.in / zohoapis.in).

TODO(verify against live Zoho at P3 sign-off): the status filter value, the per-line received-quantity
field name, and the exact scope names. The parsing below is defensive about all three.
"""
import os
from dataclasses import dataclass
from datetime import date

import httpx

OK, UNAVAILABLE, NOT_CONFIGURED = "ok", "unavailable", "not_configured"


@dataclass
class OpenPO:
    po_number: str
    sku_id: str
    qty_open: int
    po_date: date | None
    expected_date: date | None


@dataclass
class ZohoResult:
    status: str                 # ok / unavailable / not_configured
    open_pos: list
    detail: str = ""

    def open_qty_by_sku(self) -> dict | None:
        """SKU -> open units, or None when Zoho couldn't be read (never assume 0)."""
        if self.status != OK:
            return None
        out = {}
        for po in self.open_pos:
            out[po.sku_id] = out.get(po.sku_id, 0) + po.qty_open
        return out


def _env():
    return {k: os.environ.get(k, "") for k in (
        "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORGANIZATION_ID")}


def configured() -> bool:
    return all(_env().values())


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def access_token(client: httpx.Client) -> str:
    e = _env()
    accounts = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
    r = client.post(f"{accounts}/oauth/v2/token", data={
        "refresh_token": e["ZOHO_REFRESH_TOKEN"], "client_id": e["ZOHO_CLIENT_ID"],
        "client_secret": e["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in body:
        raise RuntimeError(f"Zoho token refresh failed: HTTP {r.status_code} {body.get('error', '')}".strip())
    return body["access_token"]


def _line_open_qty(line: dict) -> int:
    ordered = float(line.get("quantity") or 0)
    received = next((float(line[k]) for k in ("quantity_received", "received_quantity") if line.get(k) is not None), 0.0)
    return max(int(round(ordered - received)), 0)


def fetch_open_pos(known_skus: set, client: httpx.Client = None) -> ZohoResult:
    if not configured():
        return ZohoResult(NOT_CONFIGURED, [], "Zoho credentials are not set")
    api = os.environ.get("ZOHO_API_URL", "https://www.zohoapis.in").rstrip("/")
    org = _env()["ZOHO_ORGANIZATION_ID"]
    c = client or httpx.Client(timeout=30)
    try:
        headers = {"Authorization": f"Zoho-oauthtoken {access_token(c)}"}
        pos, page = [], 1
        while True:
            r = c.get(f"{api}/inventory/v1/purchaseorders", headers=headers,
                      params={"organization_id": org, "filter_by": "Status.Open", "page": page, "per_page": 200})
            r.raise_for_status()
            body = r.json()
            pos.extend(body.get("purchaseorders", []))
            if not body.get("page_context", {}).get("has_more_page"):
                break
            page += 1

        out = []
        for po in pos:
            if str(po.get("status", "open")).lower() not in ("open", "issued", "partially_received"):
                continue
            r = c.get(f"{api}/inventory/v1/purchaseorders/{po['purchaseorder_id']}", headers=headers,
                      params={"organization_id": org})
            r.raise_for_status()
            detail = r.json().get("purchaseorder", {})
            for line in detail.get("line_items", []):
                sku = (line.get("sku") or "").strip()
                qty = _line_open_qty(line)
                if sku in known_skus and qty > 0:
                    out.append(OpenPO(detail.get("purchaseorder_number") or po.get("purchaseorder_number", ""),
                                      sku, qty, _date(detail.get("date")), _date(detail.get("delivery_date"))))
        return ZohoResult(OK, out)
    except Exception as exc:  # any Zoho problem -> "unknown", never 0
        return ZohoResult(UNAVAILABLE, [], str(exc)[:300])
    finally:
        if client is None:
            c.close()


def save(conn, run_id: int, result: ZohoResult) -> None:
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO open_pos (run_id, po_number, sku_id, qty_open, po_date, expected_date)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (run_id, po_number, sku_id) DO UPDATE SET qty_open = open_pos.qty_open + EXCLUDED.qty_open""",
                        [(run_id, p.po_number, p.sku_id, p.qty_open, p.po_date, p.expected_date) for p in result.open_pos])
