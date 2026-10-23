import json
from types import SimpleNamespace

import pytest

from job import explain, rules


def rec(sku, **kw):
    base = dict(history_weeks=16, last4_avg=100.0, avg12=100.0, change_pct=0.0, uplift=1.25,
                demand_weekly=125.0, on_hand=300, open_po_qty=0, cover_weeks=2.4, min_weeks=6, max_weeks=14)
    base.update(kw)
    return rules.Recommendation(sku, **base)


RECS = [
    rec("SKU001", flag_high_demand=True, flag_stockout=True, action="reorder", qty=750, qty_basis="1.5x",
        cover_after=6.1, exception_type="conflicting_flags"),
    rec("SKU013", flag_stockout=True, action="reorder", qty=161, qty_basis="topup", cover_after=6.0),
    rec("SKU002", cover_weeks=11.5),
]


class FakeClient:
    def __init__(self, payload=None, stop_reason="end_turn", error=None):
        self.calls = []
        self.payload, self.stop_reason, self.error = payload, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])


def answer(*items):
    return {"reasons": [dict(sku_id=s, reason=r, needs_judgment=j, why_unusual=w) for s, r, j, w in items]}


def test_claude_reasons_used_for_flagged_only():
    client = FakeClient(answer(
        ("SKU001", "Sales jumped about 21% and cover is 3.4 w, so both rules fire.", True, "step up at W39"),
        ("SKU013", "Cover 3.2 w is under the 6 w minimum.", False, "")))
    out, source, _ = explain.explain(RECS, {}, {}, client)
    assert source == "claude"
    assert out["SKU001"].source == "claude" and out["SKU001"].needs_judgment and "W39" in out["SKU001"].why_unusual
    assert out["SKU002"].source == "template"          # unflagged rows never go to Claude
    sent = json.loads(client.calls[0]["messages"][0]["content"])["skus"]
    assert {r["sku_id"] for r in sent} == {"SKU001", "SKU013"}


def test_request_shape():
    client = FakeClient(answer())
    explain.explain(RECS, {}, {}, client)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "qty" not in json.dumps(call["output_config"]["format"]["schema"])   # no quantity field
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]


def test_reason_quoting_another_quantity_is_dropped():
    client = FakeClient(answer(("SKU013", "Reorder 400 units to be safe.", False, ""),
                               ("SKU001", "Order 750 now; both rules fire.", True, "")))
    out, source, _ = explain.explain(RECS, {}, {}, client)
    assert out["SKU013"].source == "template" and out["SKU001"].source == "claude"
    assert source == "mixed"


def test_unknown_sku_and_overlong_reason():
    client = FakeClient(answer(("SKU999", "x", False, ""), ("SKU013", "A" * 500, False, "")))
    out, _, _ = explain.explain(RECS, {}, {}, client)
    assert "SKU999" not in out
    assert len(out["SKU013"].reason) == 200


@pytest.mark.parametrize("client", [
    FakeClient(answer(), stop_reason="refusal"),
    FakeClient("not json"),
    FakeClient(error=RuntimeError("network down")),
])
def test_failures_fall_back_to_templates(client):
    out, source, detail = explain.explain(RECS, {}, {}, client)
    assert source == "template" and "Claude unavailable" in detail
    assert all(e.source == "template" for e in out.values())
    assert out["SKU013"].reason == rules.template_reason(RECS[1])


def test_no_api_key_uses_templates(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out, source, detail = explain.explain(RECS, {}, {})
    assert source == "template" and "ANTHROPIC_API_KEY" in detail
