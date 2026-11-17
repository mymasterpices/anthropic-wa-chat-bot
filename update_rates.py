#!/usr/bin/env python3
"""Daily gold-rate update for RKJ staff.

Usage:
    python update_rates.py 22=7350 18=6010 14=4680 24=8020
(any subset of karats; INR per gram)
"""

import json
import sys
from datetime import date
from pathlib import Path

RATES_FILE = Path(__file__).parent / "data" / "rates.json"


def main() -> None:
    args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    updates = {k: float(v) for k, v in args.items() if k in {"14", "18", "22", "24"}}
    if not updates:
        print(__doc__)
        sys.exit(1)
    data = json.loads(RATES_FILE.read_text())
    data["rates_per_gram"].update(updates)
    data["updated_at"] = date.today().isoformat()
    RATES_FILE.write_text(json.dumps(data, indent=2))
    print(f"Updated ({data['updated_at']}):")
    for k, v in sorted(data["rates_per_gram"].items(), reverse=True):
        print(f"  {k}K  ₹{v:,.0f}/g")


if __name__ == "__main__":
    main()
