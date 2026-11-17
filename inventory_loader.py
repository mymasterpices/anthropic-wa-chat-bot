"""RKJ WhatsApp Bot — Inventory Loader
Converts the DIAMOND and GOLD packing-list Excel exports into a clean SQLite DB
that the bot's search_inventory tool queries.

Usage: python inventory_loader.py <diamond.xlsx> <gold.xlsx> <output.db>

Pricing model:
- DIAMOND pieces: mrp is all-in -> used directly for price bands.
- GOLD pieces: price computed at query time = live_rate[karat] * net_wt
  * (1 + making_pct/100) + stone_amt. Reference rates below are only for
  indicative price bands at load time; the server recomputes live.
"""
import sys, re, json, sqlite3
import pandas as pd

# Reference per-gram rates for indicative bands (server overrides with live rates)
REF_RATES = {24: 14600.0, 22: 13454.0, 18: 11241.0, 14: 8813.0}

NAMED_COLLECTIONS = {"REWIND","BLOOM","CITY LIGHT","DRAGON FLY","ESCAPE","RISE","YIN YANG"}
OCCASIONS = {"DAILY WEAR","EVENING","PARTY WEAR","WEDDING"}
SUBCAT_FIX = {"HOLOOW":"HOLLOW","TESSAL":"TASSEL"}
# Authoritative RKJ shape abbreviations (confirmed by Rohan, Jul 2026)
SHAPE_NAMES = {"RD":"Round","EC":"Emerald cut","OV":"Oval","PS":"Pear shape",
    "MQ":"Marquise","RAD":"Radiant","RA":"Radiant","CU":"Cushion",
    "RC":"Rose cut","PR":"Princess",
    # present in data, pending confirmation:
    "MX":"Mixed shapes","TR":"Trillion","AC":"Asscher","HT":"Heart",
    "BG":"Baguette","BR":"Briolette"}

def clean_str(v):
    if pd.isna(v): return None
    s = str(v).strip()
    return s.upper() if s else None

def karat_from(quality, purity):
    q = clean_str(quality)
    if q in ("22","18","14","24"): return int(q)
    if q in ("995","999"): return 24
    if q in ("950","SL"): return None  # platinum/silver — flagged, not karat gold
    for k, p in ((22,91.6),(18,75.0),(18,76.5),(14,58.5),(24,99.5)):
        if purity and abs(purity - p) < 0.6: return k
    return None

def split_collection(v):
    """Returns (collection_name, occasion_tags list)."""
    if not v: return None, []
    parts = [p.strip().upper() for p in str(v).split(",") if p.strip()]
    named = [p for p in parts if p in NAMED_COLLECTIONS]
    occ = [p for p in parts if p in OCCASIONS]
    return (named[0] if named else None), occ

def price_band(price):
    if price is None or price <= 0: return None
    if price < 50000: return "UNDER_50K"
    if price < 100000: return "50K_1L"
    if price < 300000: return "1L_3L"
    if price < 500000: return "3L_5L"
    if price < 1000000: return "5L_10L"
    return "ABOVE_10L"

SCHEMA = """
CREATE TABLE products (
  sku TEXT PRIMARY KEY,
  item_type TEXT NOT NULL,            -- DIAMOND | GOLD
  category TEXT, sub_category TEXT,
  style_code TEXT,
  collection_name TEXT,
  occasion_tags TEXT,                 -- JSON array
  gender TEXT,                        -- LADIES | GENTS | UNISEX
  karat INTEGER, purity REAL,
  gross_wt REAL, net_wt REAL,
  mrp REAL,                           -- all-in for DIAMOND; snapshot metal(+stone) for GOLD
  making_pct REAL,                    -- GOLD only: % on metal value
  stone_amt REAL,                     -- colour-stone value (snapshot)
  total_dia_ct REAL, dia_pcs INTEGER,
  is_set_component INTEGER DEFAULT 0,
  indicative_price REAL,              -- band basis; server recomputes GOLD live
  price_band TEXT,
  quality_grade TEXT,
  image_url TEXT,
  in_stock INTEGER DEFAULT 1
);
CREATE TABLE diamond_lots (
  sku TEXT REFERENCES products(sku),
  shape TEXT, shape_name TEXT, colour TEXT, clarity TEXT,
  size_label TEXT, pcs INTEGER, carat REAL
);
CREATE TABLE stone_lots (
  sku TEXT REFERENCES products(sku),
  pcs INTEGER, carat REAL
);
CREATE INDEX idx_p_cat ON products(item_type, category, sub_category);
CREATE INDEX idx_p_band ON products(price_band);
CREATE INDEX idx_p_style ON products(style_code);
"""

def load_diamond(path, report):
    df = pd.read_excel(path)
    df.columns = [c.strip() for c in df.columns]
    df["piece"] = df["jewel_code"].ffill()
    products, dlots, slots = [], [], []
    for sku, g in df.groupby("piece", sort=False):
        main = g[g["jewel_code"].notna()]
        if main.empty:
            report["diamond_orphan_rows"] += len(g); continue
        m = main.iloc[0]
        cat = clean_str(m["product_category"])
        cat = re.sub(r"^DIAMOND\s+", "", cat) if cat else None
        coll_name, occ = split_collection(m.get("collection"))
        purity = float(m["gold_purity"]) if pd.notna(m["gold_purity"]) else None
        karat = karat_from(m["quality_code"], purity)
        dia_ct = round(float(g["dia_wt"].fillna(0).sum()), 3)
        dia_pcs = int(g["Diamond pcs"].fillna(0).sum())
        stone_amt = float(g["colour_stone_amt"].fillna(0).sum())
        mrp = float(m["mrp"]) if pd.notna(m["mrp"]) else None
        sub = clean_str(m["sub_category"])
        sub = SUBCAT_FIX.get(sub, sub)
        products.append(dict(sku=sku, item_type="DIAMOND", category=cat, sub_category=sub,
            style_code=clean_str(m["Style Code"]), collection_name=coll_name,
            occasion_tags=json.dumps(occ), gender=clean_str(m.get("gender")),
            karat=karat, purity=purity,
            gross_wt=float(m["gross_wt"]) if pd.notna(m["gross_wt"]) else None,
            net_wt=float(m["net_wt"]) if pd.notna(m["net_wt"]) else None,
            mrp=mrp, making_pct=None, stone_amt=stone_amt,
            total_dia_ct=dia_ct, dia_pcs=dia_pcs, is_set_component=0,
            indicative_price=mrp, price_band=price_band(mrp),
            quality_grade=clean_str(m.get("Itemcode")),
            image_url=str(m["Image link"]).strip() if pd.notna(m["Image link"]) else None))
        if mrp is None: report["diamond_missing_mrp"].append(sku)
        if pd.isna(m["Image link"]): report["diamond_missing_image"].append(sku)
        for _, r in g.iterrows():
            if pd.notna(r["dia_wt"]) or pd.notna(r["Diamond pcs"]):
                sh = clean_str(r["Diamond Shape"])
                dlots.append(dict(sku=sku, shape=sh, shape_name=SHAPE_NAMES.get(sh),
                    colour=clean_str(r["diamond_colour"]), clarity=clean_str(r["diamond_clarity"]),
                    size_label=clean_str(r["DSize"]),
                    pcs=int(r["Diamond pcs"]) if pd.notna(r["Diamond pcs"]) else None,
                    carat=float(r["dia_wt"]) if pd.notna(r["dia_wt"]) else None))
            if pd.notna(r.get("colour_stone_wt")):
                slots.append(dict(sku=sku,
                    pcs=int(r["stone pcs"]) if pd.notna(r.get("stone pcs")) else None,
                    carat=float(r["colour_stone_wt"])))
    return products, dlots, slots

def load_gold(path, report):
    df = pd.read_excel(path)
    df.columns = [c.strip() for c in df.columns]
    products, slots = [], []
    for _, m in df.iterrows():
        sku = clean_str(m["jewel_code"])
        cat_raw = clean_str(m["product_category"]) or ""
        cat = re.sub(r"^GOLD\s+", "", cat_raw)
        is_set = 1 if ("OF PS" in cat_raw or "OF SET" in cat_raw) else 0
        coll_name, occ = split_collection(m.get("collection"))
        purity = float(m["gold_purity"]) if pd.notna(m["gold_purity"]) else None
        karat = karat_from(m["quality_code"], purity)
        net = float(m["net_wt"]) if pd.notna(m["net_wt"]) else None
        making = float(m["Making New"]) if pd.notna(m.get("Making New")) else None
        stone_amt = float(m["colour_stone_amt"]) if pd.notna(m["colour_stone_amt"]) else 0.0
        # indicative price from reference rates; server recomputes with live rate
        ind = None
        if net and karat and REF_RATES.get(karat):
            ind = REF_RATES[karat] * net * (1 + (making or 0)/100) + stone_amt
        sub = clean_str(m["sub_category"])
        sub = SUBCAT_FIX.get(sub, sub)
        g = clean_str(m.get("gender"))
        products.append(dict(sku=sku, item_type="GOLD", category=cat, sub_category=sub,
            style_code=None, collection_name=coll_name, occasion_tags=json.dumps(occ),
            gender=g, karat=karat, purity=purity,
            gross_wt=float(m["gross_wt"]) if pd.notna(m["gross_wt"]) else None,
            net_wt=net, mrp=float(m["mrp"]) if pd.notna(m["mrp"]) else None,
            making_pct=making, stone_amt=stone_amt, total_dia_ct=0, dia_pcs=0,
            is_set_component=is_set, indicative_price=ind, price_band=price_band(ind),
            quality_grade=clean_str(m.get("Itemcode")),
            image_url=str(m["Image Link"]).strip() if pd.notna(m["Image Link"]) else None))
        if making is None: report["gold_missing_making"].append(sku)
        if karat is None: report["gold_unresolved_karat"].append(sku)
        if pd.notna(m["colour_stone_wt"]):
            slots.append(dict(sku=sku, pcs=None, carat=float(m["colour_stone_wt"])))
    return products, slots

def main(diamond_path, gold_path, out_db):
    report = {"diamond_orphan_rows": 0, "diamond_missing_mrp": [],
              "diamond_missing_image": [], "gold_missing_making": [],
              "gold_unresolved_karat": []}
    dp, dl, ds = load_diamond(diamond_path, report)
    gp, gs = load_gold(gold_path, report)
    con = sqlite3.connect(out_db)
    con.executescript("DROP TABLE IF EXISTS products; DROP TABLE IF EXISTS diamond_lots; DROP TABLE IF EXISTS stone_lots;")
    con.executescript(SCHEMA)
    cols = list(dp[0].keys())
    con.executemany(f"INSERT INTO products ({','.join(cols)}) VALUES ({','.join(':'+c for c in cols)})", dp + gp)
    if dl: con.executemany("INSERT INTO diamond_lots (sku,shape,shape_name,colour,clarity,size_label,pcs,carat) VALUES (:sku,:shape,:shape_name,:colour,:clarity,:size_label,:pcs,:carat)", dl)
    if ds or gs: con.executemany("INSERT INTO stone_lots (sku,pcs,carat) VALUES (:sku,:pcs,:carat)", ds + gs)
    con.commit()
    # validation summary
    n = con.execute("SELECT item_type, COUNT(*) FROM products GROUP BY item_type").fetchall()
    bands = con.execute("SELECT price_band, COUNT(*) FROM products GROUP BY price_band ORDER BY 2 DESC").fetchall()
    nocat = con.execute("SELECT COUNT(*) FROM products WHERE category IS NULL OR sub_category IS NULL").fetchone()[0]
    nogender = con.execute("SELECT COUNT(*) FROM products WHERE gender IS NULL").fetchone()[0]
    print("=== LOAD REPORT ===")
    print("Pieces by type:", dict(n))
    print("Diamond lots:", len(dl), "| Stone lots:", len(ds)+len(gs))
    print("Price bands:", dict(bands))
    print("Missing category/sub_category:", nocat, "| Missing gender:", nogender)
    for k, v in report.items():
        print(f"{k}: {len(v) if isinstance(v, list) else v}", (v[:8] if isinstance(v, list) and v else ""))
    con.close()

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
