"""Tests against the real rkj_inventory.db. Run: python -m tests.test_core"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import inventory, pricing  # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAIL += 1
    print(f"[{status}] {name} {detail}")


# --- pricing ---------------------------------------------------------------
rate22 = pricing.rate_source.get_rate(22)
expected = rate22 * 7.53 * 1.12 + 0.0  # sample gold row B200357
got = pricing.compute_gold_price(22, 7.53, 12.0, 0.0)
check("gold cross-check formula", abs(got - expected) < 0.01, f"= ₹{got:,.0f}")

# MRP rule (Rohan, Jul 2026): quoted price is ALWAYS mrp ±10%, gold included
import sqlite3 as _sq
_c = _sq.connect(inventory.DB_PATH); _c.row_factory = _sq.Row
_dt = dict(_c.execute("SELECT * FROM products WHERE sku='DT00001'").fetchone())
_p, _rng, _ = pricing.piece_price(_dt)
check("DT00001 quotes MRP ±10%", abs(_p - 490188.66) < 1 and _rng == "₹4.41 lakh – ₹5.39 lakh",
      f"{_p:,.0f} -> {_rng}")
_g = dict(_c.execute("SELECT * FROM products WHERE item_type='GOLD' AND mrp>0 LIMIT 1").fetchone())
_gp, _, _ = pricing.piece_price(_g)
check("gold quotes MRP, not live rate", abs(_gp - float(_g["mrp"])) < 0.01,
      f"{_g['sku']}: {_gp:,.0f} == mrp {_g['mrp']:,.0f}")
_c.close()

check("lakh formatting", pricing.format_inr(185000) == "₹1.85 lakh",
      pricing.format_inr(185000))
check("crore formatting", "crore" in pricing.format_inr(1.2e7),
      pricing.format_inr(1.2e7))
check("sub-lakh formatting", pricing.format_inr(85200) == "₹85,000",
      pricing.format_inr(85200))
rng = pricing.format_range(200000)
check("range is ±10%", rng == "₹1.8 lakh – ₹2.2 lakh", rng)

# --- search: basics ---------------------------------------------------------
r = inventory.search_inventory(item_type="DIAMOND", category="RING", limit=4)
check("diamond ring search returns 4", r["count"] == 4)
check("no internals leaked", all(
    k not in p for p in r["pieces"] for k in ("mrp", "making_pct", "metal_amt",
                                              "indicative_price", "stone_amt")))
check("all have image_url", all(p["image_url"] for p in r["pieces"]))
check("price is a range string", all(
    "–" in p["price_range"] and "₹" in p["price_range"] for p in r["pieces"]))

# --- soft occasion filter ----------------------------------------------------
r = inventory.search_inventory(item_type="DIAMOND", category="RING",
                               occasion="WEDDING", limit=5)
check("occasion soft filter still returns results", r["count"] == 5)
r2 = inventory.search_inventory(item_type="DIAMOND", category="NOSE PIN",
                                occasion="WEDDING", limit=4)
check("occasion never causes false 'nothing available'", r2["count"] > 0,
      f"({r2['count']} pieces)")

# --- variety rule -------------------------------------------------------------
r = inventory.search_inventory(item_type="DIAMOND", category="EARRING", limit=4)
variety = {(p["sub_category"], tuple(p.get("diamond_shapes", []))[:1])
           for p in r["pieces"]}
check("4 results are varied", len(variety) >= 3, f"({len(variety)} distinct)")

# --- gold price filter (computed live) ---------------------------------------
r = inventory.search_inventory(item_type="GOLD", category="CHAIN",
                               min_price=50000, max_price=100000, limit=5)
check("gold price filter returns results", r["count"] > 0)

# --- set components ------------------------------------------------------------
r = inventory.search_inventory(item_type="GOLD", category="PENDANT OF SET", limit=3)
check("set components carry set context",
      r["count"] > 0 and all("set_context" in p for p in r["pieces"]))

# --- similar-to ----------------------------------------------------------------
anchor = inventory.search_inventory(item_type="DIAMOND", category="RING",
                                    limit=1)["pieces"][0]
r = inventory.search_inventory(similar_to_sku=anchor["sku"], limit=4)
check("show-similar works", r["count"] > 0 and
      all(p["sku"] != anchor["sku"] for p in r["pieces"]))

# --- shape filter ---------------------------------------------------------------
r = inventory.search_inventory(item_type="DIAMOND", shape="Pear shape", limit=4)
check("shape filter (name) works", r["count"] > 0 and
      all("Pear shape" in p["diamond_shapes"] for p in r["pieces"]))

# --- empty result note -----------------------------------------------------------
r = inventory.search_inventory(item_type="DIAMOND", category="RING",
                               max_price=1, limit=4)
check("empty result carries widen-note", r["count"] == 0 and r["note"])

print("\n" + ("ALL TESTS PASSED" if FAIL == 0 else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
