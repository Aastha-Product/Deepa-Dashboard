"""Zoho Inventory, read-only: open purchase orders (map step 3) and PO read-back (step 12).

The OAuth client only has the read scopes below, so this code cannot create or change a PO.
Freshroots is in India, so the defaults point at Zoho's India data centre (accounts.zoho.in / zohoapis.in).

Checked against Zoho's Inventory API docs (Oct 2026):
- scopes: ZohoInventory.purchaseorders.READ and ZohoInventory.items.READ
- purchase-order line items carry `item_id`, `quantity` and `quantity_received`, but no SKU, so SKUs come
  from the items list (`item_id` -> `sku`)
- the list endpoint documents `page` / `per_page` but no status filter, so open POs are picked in code
"""
import os
from dataclasses import dataclass
from datetime import date

import httpx

OK, UNAVAILABLE, NOT_CONFIGURED = "ok", "unavailable", "not_configured"
SCOPES = "ZohoInventory.purchaseorders.READ,ZohoInventory.items.READ"
OPEN_STATUSES = {"open", "issued", "partially_received"}      # not draft, billed, closed or cancelled
PER_PAGE = 200


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


def accounts_url() -> str:
    return os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")


def api_url() -> str:
    return os.environ.get("ZOHO_API_URL", "https://www.zohoapis.in").rstrip("/")


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def access_token(client: httpx.Client) -> str:
    e = _env()
    r = client.post(f"{accounts_url()}/oauth/v2/token", data={
        "refresh_token": e["ZOHO_REFRESH_TOKEN"], "client_id": e["ZOHO_CLIENT_ID"],
        "client_secret": e["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in body:
        raise RuntimeError(f"Zoho token refresh failed: HTTP {r.status_code} {body.get('error', '')}".strip())
    return body["access_token"]


def _pages(c: httpx.Client, path: str, key: str, headers: dict, org: str):
    """Every record from a paginated Zoho list endpoint."""
    page = 1
    while True:
        r = c.get(f"{api_url()}/inventory/v1/{path}", headers=headers,
                  params={"organization_id": org, "page": page, "per_page": PER_PAGE})
        r.raise_for_status()
        body = r.json()
        rows = body.get(key, [])
        yield from rows
        more = body.get("page_context", {}).get("has_more_page")
        if not (more if more is not None else len(rows) == PER_PAGE):
            return
        page += 1


def _line_open_qty(line: dict) -> int:
    ordered = float(line.get("quantity") or 0)
    received = next((float(line[k]) for k in ("quantity_received", "received_quantity") if line.get(k) is not None), 0.0)
    return max(int(round(ordered - received)), 0)


def check(client: httpx.Client = None) -> tuple:
    """(ok, detail) for the Connections panel: can we get a token and read one page of items?"""
    if not configured():
        return False, "not connected: Zoho credentials aren't set in Vercel"
    c = client or httpx.Client(timeout=15)
    try:
        headers = {"Authorization": f"Zoho-oauthtoken {access_token(c)}"}
        r = c.get(f"{api_url()}/inventory/v1/items", headers=headers,
                  params={"organization_id": _env()["ZOHO_ORGANIZATION_ID"], "page": 1, "per_page": 1})
        r.raise_for_status()
        return True, "connected (read-only)"
    except Exception as exc:  # noqa: BLE001
        return False, f"can't reach Zoho: {str(exc)[:120]}"
    finally:
        if client is None:
            c.close()


def fetch_open_pos(known_skus: set, client: httpx.Client = None) -> ZohoResult:
    if not configured():
        return ZohoResult(NOT_CONFIGURED, [], "Zoho credentials are not set")
    org = _env()["ZOHO_ORGANIZATION_ID"]
    c = client or httpx.Client(timeout=30)
    try:
        headers = {"Authorization": f"Zoho-oauthtoken {access_token(c)}"}
        sku_of = {str(i["item_id"]): (i.get("sku") or "").strip() for i in _pages(c, "items", "items", headers, org)}
        out = []
        for po in _pages(c, "purchaseorders", "purchaseorders", headers, org):
            if str(po.get("status", "")).lower() not in OPEN_STATUSES:
                continue
            r = c.get(f"{api_url()}/inventory/v1/purchaseorders/{po['purchaseorder_id']}", headers=headers,
                      params={"organization_id": org})
            r.raise_for_status()
            detail = r.json().get("purchaseorder", {})
            for line in detail.get("line_items", []):
                sku = (line.get("sku") or sku_of.get(str(line.get("item_id")), "")).strip()
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
