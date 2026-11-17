"""Pricing logic for RKJ WhatsApp bot.

RULE (per Rohan, July 2026): the customer-quoted range is ALWAYS the ERP
MRP +/-10%, for both DIAMOND and GOLD pieces, regardless of the day's
gold rate. Example: DT00001 has MRP 490,188 -> quote ~₹4.41–5.39 lakh.

The daily gold rate (data/rates.json) is kept only as a CROSS-CHECK: if
the live-computed gold value drifts far from the MRP, a warning is
logged so RKJ knows the ERP price list needs a refresh. It is never
used for the customer quote.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RATES_FILE = DATA_DIR / "rates.json"

RANGE_PCT = 0.10  # +/-10% presentation band


class RateSource:
    """Interface. Swap in an API-backed source later without touching callers."""

    def get_rate(self, karat: int) -> float:
        raise NotImplementedError

    def as_of(self) -> str:
        raise NotImplementedError


class StaticConfigRateSource(RateSource):
    """Staff-updated daily gold rates, INR per gram, keyed by karat."""

    def __init__(self, path: Path = RATES_FILE):
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text())

    def get_rate(self, karat: int) -> float:
        rates = self._load()["rates_per_gram"]
        rate = rates.get(str(int(karat)))
        if rate is None:
            raise KeyError(f"No gold rate configured for {karat}K")
        return float(rate)

    def as_of(self) -> str:
        return self._load().get("updated_at", "unknown")


rate_source: RateSource = StaticConfigRateSource()


def compute_gold_price(karat: int, net_wt: float, making_pct: float | None,
                       stone_amt: float | None) -> float:
    rate = rate_source.get_rate(karat)
    return rate * net_wt * (1 + (making_pct or 0.0) / 100.0) + (stone_amt or 0.0)


def format_inr(amount: float) -> str:
    """Indian notation: <1L plain rupees, then lakh, then crore."""
    if amount >= 1e7:
        v = amount / 1e7
        return f"₹{v:.2f}".rstrip("0").rstrip(".") + " crore"
    if amount >= 1e5:
        v = amount / 1e5
        return f"₹{v:.2f}".rstrip("0").rstrip(".") + " lakh"
    # round to nearest 500 for a clean spoken figure
    r = int(round(amount / 500.0) * 500)
    return f"₹{r:,}"


def format_range(price: float, pct: float = RANGE_PCT) -> str:
    lo, hi = price * (1 - pct), price * (1 + pct)
    return f"{format_inr(lo)} – {format_inr(hi)}"


log = logging.getLogger("rkj.pricing")

# Cross-check: warn if live gold value drifts more than this from MRP
CROSSCHECK_DRIFT = 0.25
_warned_skus: set = set()  # warn once per SKU per process, don't flood logs


def piece_price(row: dict) -> tuple[float, str, str]:
    """Return (numeric_price, range_string, caveat) for a products row (dict).

    Quoted range is MRP +/-10% for ALL pieces. numeric_price is for
    internal filtering only and must never be shown to the customer.
    """
    mrp = float(row["mrp"] or row.get("indicative_price") or 0)

    if row["item_type"] == "GOLD":
        # cross-check only — never used for the quote
        try:
            live = compute_gold_price(row["karat"], row["net_wt"],
                                      row["making_pct"], row["stone_amt"])
            sku = row.get("sku")
            if (mrp and sku not in _warned_skus
                    and abs(live - mrp) / mrp > CROSSCHECK_DRIFT):
                _warned_skus.add(sku)
                log.warning("MRP drift on %s: mrp=%.0f live-computed=%.0f "
                            "(ERP price list may be stale)", sku, mrp, live)
            if not mrp:  # safety net for missing MRP
                mrp = live
        except Exception:
            pass
        caveat = ("Final price depends on the day's gold rate and exact "
                  "weight; confirmed at the showroom.")
    else:
        caveat = "Final pricing is confirmed at the showroom."

    return mrp, format_range(mrp), caveat
