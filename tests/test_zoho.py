import httpx
import pytest

from job import zoho

SKUS = {"SKU001", "SKU004"}


@pytest.fixture
def zoho_env(monkeypatch):
    for k, v in {"ZOHO_CLIENT_ID": "id", "ZOHO_CLIENT_SECRET": "secret",
                 "ZOHO_REFRESH_TOKEN": "refresh", "ZOHO_ORGANIZATION_ID": "60001"}.items():
        monkeypatch.setenv(k, v)


ITEMS = [{"item_id": 11, "sku": "SKU001"}, {"item_id": 44, "sku": "SKU004"}, {"item_id": 99, "sku": "OTHER"}]


def fake_zoho(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/oauth/v2/token":
        assert request.url.host == "accounts.zoho.in"
        return httpx.Response(200, json={"access_token": "tok"})
    assert request.url.host == "www.zohoapis.in"
    assert request.headers["Authorization"] == "Zoho-oauthtoken tok"
    assert request.method == "GET"                       # read-only: never POST/PUT to Inventory
    assert "filter_by" not in request.url.params          # not documented by Zoho; open POs are picked in code
    page = int(request.url.params.get("page", 1))
    if path == "/inventory/v1/items":
        return httpx.Response(200, json={"items": ITEMS})
    if path == "/inventory/v1/purchaseorders":
        rows = ([{"purchaseorder_id": "1", "purchaseorder_number": "PO-1", "status": "issued"},
                 {"purchaseorder_id": "9", "purchaseorder_number": "PO-9", "status": "draft"}] if page == 1 else
                [{"purchaseorder_id": "2", "purchaseorder_number": "PO-2", "status": "Partially_Received"},
                 {"purchaseorder_id": "3", "purchaseorder_number": "PO-3", "status": "billed"}])
        return httpx.Response(200, json={"purchaseorders": rows, "page_context": {"has_more_page": page == 1}})
    if path == "/inventory/v1/purchaseorders/1":
        return httpx.Response(200, json={"purchaseorder": {"purchaseorder_number": "PO-1", "date": "2026-10-15",
            "line_items": [{"item_id": 11, "quantity": 300}, {"item_id": 99, "quantity": 50}]}})
    if path == "/inventory/v1/purchaseorders/2":
        return httpx.Response(200, json={"purchaseorder": {"purchaseorder_number": "PO-2",
            "line_items": [{"item_id": 44, "quantity": 150, "quantity_received": 100},
                           {"item_id": 11, "quantity": 20, "quantity_received": 20}]}})
    raise AssertionError(f"unexpected call {request.method} {request.url}")


def test_not_configured(monkeypatch):
    for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORGANIZATION_ID"):
        monkeypatch.delenv(k, raising=False)
    r = zoho.fetch_open_pos(SKUS)
    assert r.status == zoho.NOT_CONFIGURED and r.open_qty_by_sku() is None
    assert zoho.check() == (False, "not connected: Zoho credentials aren't set in Vercel")


def test_open_pos_mapped_to_skus_and_netted(zoho_env):
    client = httpx.Client(transport=httpx.MockTransport(fake_zoho))
    r = zoho.fetch_open_pos(SKUS, client)
    assert r.status == zoho.OK
    assert r.open_qty_by_sku() == {"SKU001": 300, "SKU004": 50}    # draft and billed POs are ignored
    assert {p.po_number for p in r.open_pos} == {"PO-1", "PO-2"}


def test_check_reads_one_item(zoho_env):
    client = httpx.Client(transport=httpx.MockTransport(fake_zoho))
    assert zoho.check(client) == (True, "connected (read-only)")


def test_failure_is_unknown_not_zero(zoho_env):
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    r = zoho.fetch_open_pos(SKUS, client)
    assert r.status == zoho.UNAVAILABLE and r.open_qty_by_sku() is None
    assert zoho.check(client)[0] is False
