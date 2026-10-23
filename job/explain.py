"""Claude reviews exceptions and writes a one-line reason per recommendation (map steps 6-7).

Claude only writes text. The output schema has no quantity field, a reason that quotes a different
order quantity is thrown away, and any failure (no key, refusal, error, bad output) falls back to
the template reason from job.rules, so the run always completes.
"""
import json
import logging
import os
import re

from pydantic import BaseModel, ValidationError

from job import rules

log = logging.getLogger(__name__)

MODEL = os.environ.get("DP_CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("DP_CLAUDE_EFFORT", "low")
MAX_REASON = 200

SYSTEM = """You explain stock recommendations for Freshroots, an FMCG distributor in Maharashtra and Goa.
The numbers were computed by Raghav Kulkarni's documented forecasting rules. You never change them.

For each SKU in the input, write one plain-English sentence (at most 200 characters) for Deepa Nair,
the Head of Operations who reviews each run, saying why the rules flagged it. Use only numbers that appear in the input.
Never suggest a different quantity and never invent sales, prices, competitors or events.

Set needs_judgment to true when the row is an exception, or when the weekly sales look like something the
rules don't cover: a sudden step up or down, a new product, one distributor changing behaviour, or a one-off
event. Then say in why_unusual what in the numbers looks unusual (for example "sales stepped up from about
190 to 260 per week at W39"). Otherwise set needs_judgment to false and why_unusual to an empty string."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "needs_judgment": {"type": "boolean"},
                    "why_unusual": {"type": "string"},
                },
                "required": ["sku_id", "reason", "needs_judgment", "why_unusual"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reasons"],
    "additionalProperties": False,
}


class SkuReason(BaseModel):
    sku_id: str
    reason: str
    needs_judgment: bool
    why_unusual: str


class Reasons(BaseModel):
    reasons: list[SkuReason]


class Explained:
    def __init__(self, reason, source, needs_judgment=False, why_unusual=""):
        self.reason, self.source = reason, source
        self.needs_judgment, self.why_unusual = needs_judgment, why_unusual


ORDER_QTY = re.compile(r"\b(?:re)?order(?:ing)?\s+(?:of\s+)?(\d[\d,]*)", re.I)


def quotes_other_qty(text: str, qty: int) -> bool:
    return any(int(n.replace(",", "")) != qty for n in ORDER_QTY.findall(text))


def _row(r: rules.Recommendation, name: str, weekly: dict) -> dict:
    rd = lambda v, n=1: None if v is None else round(v, n)
    return {
        "sku_id": r.sku_id, "name": name, "exception_type": r.exception_type,
        "flags": [f for f, on in (("high_demand", r.flag_high_demand), ("stockout_risk", r.flag_stockout),
                                  ("overstock_risk", r.flag_overstock)) if on],
        "last4_avg": rd(r.last4_avg), "avg12": rd(r.avg12), "change_pct": rd(r.change_pct),
        "festival_uplift": r.uplift, "weekly_demand": rd(r.demand_weekly), "on_hand": r.on_hand,
        "open_po_units": r.open_po_qty, "cover_weeks": rd(r.cover_weeks), "min_weeks": r.min_weeks,
        "max_weeks": r.max_weeks, "action": r.action, "qty": r.qty, "qty_basis": r.qty_basis,
        "cover_after": rd(r.cover_after), "weekly_sales": {w: weekly[w] for w in sorted(weekly)[-16:]},
    }


def _call_claude(rows: list, client) -> Reasons:
    kwargs = dict(
        model=MODEL, max_tokens=8000, system=SYSTEM,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": json.dumps({"skus": rows}, separators=(",", ":"))}],
    )
    if os.environ.get("DP_CLAUDE_FALLBACKS", "1") == "1":
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    response = client.beta.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    if not text:
        raise RuntimeError("Claude returned no text")
    return Reasons.model_validate_json(text)


def explain(recs: list, names: dict, sales: dict, client=None) -> tuple:
    """Return ({sku_id: Explained}, source, detail). source is claude / template / mixed."""
    out = {r.sku_id: Explained(rules.template_reason(r), "template") for r in recs}
    flagged = [r for r in recs if r.flagged]
    if not flagged:
        return out, "template", "nothing flagged"
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return out, "template", "ANTHROPIC_API_KEY not set"
        import anthropic
        client = anthropic.Anthropic(timeout=120, max_retries=2)

    try:
        result = _call_claude([_row(r, names.get(r.sku_id, ""), sales.get(r.sku_id, {})) for r in flagged], client)
    except (ValidationError, Exception) as exc:  # noqa: BLE001 - any failure falls back to templates
        log.warning("Claude reasons unavailable: %s", exc)
        return out, "template", f"Claude unavailable: {str(exc)[:200]}"

    by_sku = {r.sku_id: r for r in flagged}
    used = 0
    for item in result.reasons:
        r = by_sku.get(item.sku_id)
        reason = " ".join(item.reason.split())[:MAX_REASON]
        if r is None or not reason or quotes_other_qty(reason, r.qty):
            continue
        out[r.sku_id] = Explained(reason, "claude", item.needs_judgment or bool(r.exception_type),
                                  " ".join(item.why_unusual.split())[:MAX_REASON])
        used += 1
    source = "claude" if used == len(flagged) else ("mixed" if used else "template")
    return out, source, f"Claude explained {used} of {len(flagged)} flagged SKUs"
