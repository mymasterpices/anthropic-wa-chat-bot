#!/usr/bin/env python3
"""Export a lead sheet from the bot's conversations.

Usage:  python export_leads.py            -> leads.xlsx (or leads.csv)
        python export_leads.py out.xlsx   -> custom filename

One row per customer: status (Hot/Warm/New), contact dates, pieces shown,
escalations, last message. Data comes from data/sessions.db.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import sessions  # noqa: E402

COLUMNS = ["Phone", "Status", "First contact", "Last contact", "Messages",
           "Pieces shown", "Escalations", "Escalation reasons",
           "Latest escalation note", "Last customer message"]


def main() -> None:
    rows = sessions.leads_summary()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("leads.xlsx")

    if out.suffix == ".xlsx":
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
        except ImportError:
            print("openpyxl not installed — writing CSV instead "
                  "(pip install openpyxl for Excel).")
            out = out.with_suffix(".csv")
        else:
            wb = Workbook(); ws = wb.active; ws.title = "Leads"
            ws.append(COLUMNS)
            for c in ws[1]:
                c.font = Font(bold=True)
                c.fill = PatternFill("solid", fgColor="E8EEF4")
            for r in rows:
                ws.append([r[c] for c in COLUMNS])
            widths = [16, 24, 18, 18, 10, 34, 12, 22, 40, 40]
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
            ws.freeze_panes = "A2"
            wb.save(out)
            print(f"{len(rows)} leads -> {out}")
            return

    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader(); w.writerows(rows)
    print(f"{len(rows)} leads -> {out}")


if __name__ == "__main__":
    main()
