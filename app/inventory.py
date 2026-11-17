"""search_inventory tool implementation over rkj_inventory.db.

Encoded rules (from the data / build brief):
- occasion is a SOFT filter: tagged matches rank first, untagged pieces
  are never excluded, mismatched-tag pieces fill only if needed.
- Price filters: indicative_price for DIAMOND, live-computed for GOLD.
- Results are diversified across sub_category / shape ("4 varied pieces").
- is_set_component=1 pieces carry set context, not offered standalone.
- Never expose: mrp, making_pct, metal internals, cost breakdowns.
- "Show similar": same style_code (diamond) or same sub_category +
  adjacent price band -> pass similar_to_sku.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import pricing

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "rkj_inventory.db"

SHAPE_NAMES = {
    "RD": "Round", "EC": "Emerald cut", "OV": "Oval", "PS": "Pear shape",
    "MQ": "Marquise", "RA": "Radiant", "RAD": "Radiant", "CU": "Cushion",
    "RC": "Rose cut", "PR": "Princess", "MX": "Mixed shapes",
    "TR": "Trillion", "AC": "Asscher", "HT": "Heart", "BG": "Baguette",
    "BR": "Briolette",
}
_NAME_TO_CODE = {v.upper(): k for k, v in SHAPE_NAMES.items()}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _shape_code(shape: str) -> str:
    s = (shape or "").strip().upper()
    return _NAME_TO_CODE.get(s, "RA" if s == "RAD" else s)


def _has_colour_stones(conn, sku: str, stone_amt) -> bool:
    """Colour/other gemstones present: stone value > 0 or stone lots exist."""
    if stone_amt and float(stone_amt) > 0:
        return True
    row = conn.execute(
        "SELECT 1 FROM stone_lots WHERE sku=? AND carat>0 LIMIT 1", (sku,)).fetchone()
    return row is not None


def _shapes_for(conn, sku: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT shape FROM diamond_lots WHERE sku=? AND shape IS NOT NULL",
        (sku,)).fetchall()
    return [SHAPE_NAMES.get(r["shape"], r["shape"]) for r in rows]


def _quality_for(conn, sku: str) -> list[str]:
    """Distinct colour/clarity of the piece's diamond lots, e.g. 'F-G VS'."""
    rows = conn.execute(
        "SELECT DISTINCT colour, clarity FROM diamond_lots WHERE sku=? "
        "AND colour IS NOT NULL", (sku,)).fetchall()
    return [" ".join(filter(None, (r["colour"], r["clarity"]))) for r in rows]


def _occasion_tier(tags_json: str | None, wanted: str | None) -> int:
    """0 = tagged match, 1 = untagged (never exclude!), 2 = tagged mismatch."""
    if not wanted:
        return 0
    try:
        tags = json.loads(tags_json) if tags_json else []
    except (TypeError, ValueError):
        tags = []
    if not tags:
        return 1
    w = wanted.strip().upper()
    return 0 if any(w in t.upper() or t.upper() in w for t in tags) else 2


def _customer_view(conn, row: sqlite3.Row) -> dict:
    """Project a products row to customer-safe fields only."""
    d = dict(row)
    _, price_range, caveat = pricing.piece_price(d)
    out = {
        "sku": d["sku"],
        "item_type": d["item_type"],
        "category": d["category"],
        "sub_category": d["sub_category"],
        "style_code": d["style_code"],
        "collection": d["collection_name"],
        "gender": d["gender"],
        "karat": d["karat"],
        "gross_weight_g": d["gross_wt"],
        "net_weight_g": d["net_wt"],
        "occasions": d["occasion_tags"],
        "price_range": price_range,
        "price_note": caveat,
        "image_url": d["image_url"],
    }
    # Price-breakup fields — shareable with the customer ON REQUEST
    # (policy: Rohan, Jul 2026). Final figures confirmed at the showroom.
    out["breakup"] = {
        "making_charges_pct": d["making_pct"],
        "stone_value_inr": d["stone_amt"] if (d["stone_amt"] or 0) > 0 else None,
        "note": "Share only if the customer asks how the price is made up.",
    }
    if d["item_type"] == "DIAMOND":
        out["total_diamond_carat"] = d["total_dia_ct"]
        out["diamond_pieces"] = d["dia_pcs"]
        out["quality_grade"] = d["quality_grade"]
        out["diamond_shapes"] = _shapes_for(conn, d["sku"])
        out["diamond_colour_clarity"] = _quality_for(conn, d["sku"])
        out["colour_stones"] = _has_colour_stones(conn, d["sku"], d["stone_amt"])
    if d["is_set_component"]:
        out["set_context"] = (
            "This piece is part of a matching set — present it together "
            "with its set, not as a standalone piece.")
    return out


def search_inventory(item_type: str | None = None,
                     category: str | None = None,
                     sub_category: str | None = None,
                     occasion: str | None = None,
                     gender: str | None = None,
                     karat: int | None = None,
                     min_price: float | None = None,
                     max_price: float | None = None,
                     shape: str | None = None,
                     style_code: str | None = None,
                     similar_to_sku: str | None = None,
                     sku: str | None = None,
                     exclude_skus: list | None = None,
                     colour_stones: str | None = None,
                     limit: int = 4) -> dict:
    limit = max(1, min(int(limit or 4), 5))  # persona: max 5 cards/message
    exclude = set(exclude_skus or [])
    conn = _conn()
    try:
        # Direct lookup: price/detail questions about one specific piece.
        # Never blocked by the exclude list.
        if sku:
            row = conn.execute("SELECT * FROM products WHERE sku=?",
                               (sku.strip(),)).fetchone()
            if row:
                return {"count": 1, "pieces": [_customer_view(conn, row)],
                        "note": None}
            return {"count": 0, "pieces": [],
                    "note": f"No piece with SKU {sku}."}

        where, params = ["in_stock=1"], []

        if similar_to_sku:
            anchor = conn.execute("SELECT * FROM products WHERE sku=?",
                                  (similar_to_sku,)).fetchone()
            if anchor:
                same_style = 0
                if anchor["item_type"] == "DIAMOND" and anchor["style_code"]:
                    same_style = conn.execute(
                        "SELECT COUNT(*) FROM products WHERE style_code=? AND sku!=?",
                        (anchor["style_code"], similar_to_sku)).fetchone()[0]
                if same_style:
                    style_code = style_code or anchor["style_code"]
                    item_type = item_type or anchor["item_type"]
                else:
                    # style codes are unique per piece in this export ->
                    # similar = same sub_category + adjacent price band,
                    # widening to category if the sub_category has no siblings
                    siblings = conn.execute(
                        "SELECT COUNT(*) FROM products WHERE sub_category=? "
                        "AND sku!=?", (anchor["sub_category"], similar_to_sku)
                    ).fetchone()[0]
                    if siblings:
                        sub_category = sub_category or anchor["sub_category"]
                    category = category or anchor["category"]
                    item_type = item_type or anchor["item_type"]
                    ap, _, _ = pricing.piece_price(dict(anchor))
                    min_price = min_price if min_price is not None else ap * 0.6
                    max_price = max_price if max_price is not None else ap * 1.5
                where.append("sku != ?")
                params.append(similar_to_sku)

        if item_type:
            where.append("UPPER(item_type)=?")
            params.append(item_type.strip().upper())
        if category:
            # word-boundary match: 'RING' must not match 'EARRING',
            # but 'NECKLACE' should match 'KANTHI NECKLACE'
            c = category.strip().upper()
            where.append("(UPPER(category)=? OR UPPER(category) LIKE ? "
                         "OR UPPER(category) LIKE ? OR UPPER(category) LIKE ?)")
            params.extend([c, f"{c} %", f"% {c}", f"% {c} %"])
        if sub_category:
            where.append("UPPER(sub_category) LIKE ?")
            params.append(f"%{sub_category.strip().upper()}%")
        if gender:
            g = gender.strip().upper()
            g = {"WOMEN": "LADIES", "FEMALE": "LADIES", "MEN": "GENTS",
                 "MALE": "GENTS"}.get(g, g)
            where.append("(gender=? OR gender='UNISEX' OR gender IS NULL)")
            params.append(g)
        if karat:
            where.append("karat=?")
            params.append(int(karat))
        if style_code:
            where.append("style_code=?")
            params.append(style_code)
        if shape:
            where.append("sku IN (SELECT sku FROM diamond_lots WHERE shape=?)")
            params.append(_shape_code(shape))
        if colour_stones:
            stones_clause = ("(COALESCE(stone_amt,0) > 0 OR sku IN "
                             "(SELECT sku FROM stone_lots WHERE carat>0))")
            if colour_stones.strip().lower() == "without":
                where.append(f"NOT {stones_clause}")
            elif colour_stones.strip().lower() == "with":
                where.append(stones_clause)

        rows = conn.execute(
            f"SELECT * FROM products WHERE {' AND '.join(where)}",
            params).fetchall()

        # Price filter (MRP-based)
        candidates, already_shown = [], []
        for r in rows:
            try:
                price, _, _ = pricing.piece_price(dict(r))
            except (KeyError, TypeError):
                continue
            if min_price is not None and price < min_price:
                continue
            if max_price is not None and price > max_price:
                continue
            # never repeat pieces the customer has already seen
            if r["sku"] in exclude:
                already_shown.append((r, price))
                continue
            candidates.append((r, price))

        # Soft occasion ranking, then diversify
        tiers: dict[int, list] = {0: [], 1: [], 2: []}
        for r, price in candidates:
            tiers[_occasion_tier(r["occasion_tags"], occasion)].append((r, price))

        picked, seen_variety = [], set()
        for tier in (0, 1, 2):
            # two passes per tier: first enforce variety, then fill
            pool = tiers[tier]
            # deprioritise set components unless explicitly searched for
            explicit_set = bool(category and "SET" in category.upper())
            if not explicit_set:
                pool = sorted(pool, key=lambda rp: rp[0]["is_set_component"] or 0)
            for enforce_variety in (True, False):
                for r, price in pool:
                    if len(picked) >= limit:
                        break
                    if any(p["sku"] == r["sku"] for p, _ in picked):
                        continue
                    shapes = _shapes_for(conn, r["sku"]) if r["item_type"] == "DIAMOND" else []
                    key = (r["sub_category"], shapes[0] if shapes else None)
                    if enforce_variety and key in seen_variety:
                        continue
                    seen_variety.add(key)
                    picked.append((r, price))
            if len(picked) >= limit:
                break

        results = [_customer_view(conn, r) for r, _ in picked]

        # Circle-back rule: only after 7+ pieces have been shown may
        # previously seen pieces reappear, and only to fill a short result.
        note = None
        if len(results) < limit and len(exclude) >= 7 and already_shown:
            for r, _ in already_shown:
                if len(results) >= limit:
                    break
                v = _customer_view(conn, r)
                v["previously_shown"] = True
                results.append(v)
            note = ("Some pieces were shown to this customer before "
                    "(previously_shown) — only re-present one if nothing "
                    "fresh has appealed to them.")
        if not results:
            note = ("No fresh pieces matched. Widen the search once "
                    "(adjacent category or budget) before telling the "
                    "customer nothing is available.")
        return {"count": len(results), "pieces": results, "note": note}
    finally:
        conn.close()
