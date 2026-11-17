#!/usr/bin/env python3
"""Look up the exact, unprocessed sheet row(s) for an order ID or phone
number — no interpretation applied, just what's literally stored.

Usage:
    python check_order_raw.py --order RK0019
    python check_order_raw.py --phone 9811690123
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import orders  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--order", help="Order ID, e.g. RK0019")
    p.add_argument("--phone", help="Phone number, any format")
    args = p.parse_args()

    if not args.order and not args.phone:
        p.print_help()
        sys.exit(1)

    if args.order:
        rows = orders.raw_rows_for_order(args.order)
        print(f"--- Raw row(s) for order {args.order!r} ---")
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        if len(rows) > 1:
            print(f"\n⚠️  {len(rows)} rows found for this order ID — check for duplicates.")

    if args.phone:
        rows = orders.raw_rows_for_phone(args.phone)
        print(f"--- Raw row(s) for phone {args.phone!r} ---")
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        if len(rows) > 1:
            print(f"\n⚠️  {len(rows)} rows found for this phone number — "
                  f"the sheet has more than one order saved against it.")


if __name__ == "__main__":
    main()
