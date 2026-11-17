"""Daily leads backup: build leads.xlsx and email it every 24 hours.

Configure via env (see .env.example): SMTP_HOST/PORT/USER/PASS,
LEADS_EMAIL_TO, LEADS_REPORT_HOUR (IST). If SMTP is not configured the
loop stays idle and logs a notice — the bot itself is unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, sessions

log = logging.getLogger("rkj.reporting")
IST = ZoneInfo("Asia/Kolkata")

COLUMNS = ["Phone", "Status", "First contact", "Last contact", "Messages",
           "Pieces shown", "Escalations", "Escalation reasons",
           "Latest escalation note", "Last customer message"]


def build_leads_xlsx(path: Path) -> int:
    """Write the lead sheet to `path`; returns the number of leads."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    rows = sessions.leads_summary()
    wb = Workbook(); ws = wb.active; ws.title = "Leads"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8EEF4")
    for r in rows:
        ws.append([r[c] for c in COLUMNS])
    for i, w in enumerate([16, 24, 18, 18, 10, 34, 12, 22, 40, 40], 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"
    wb.save(path)
    return len(rows)


def smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS
                and config.LEADS_EMAIL_TO)


def send_leads_email() -> int:
    """Build and email the lead sheet. Returns number of leads sent."""
    if not smtp_configured():
        log.info("SMTP not configured — skipping leads email")
        return -1
    today = datetime.now(IST)
    path = Path(f"/tmp/rkj-leads-{today:%Y-%m-%d}.xlsx")
    n = build_leads_xlsx(path)

    msg = EmailMessage()
    msg["Subject"] = f"RKJ bot leads backup — {today:%d %b %Y} ({n} customers)"
    msg["From"] = config.SMTP_USER
    msg["To"] = ", ".join(config.LEADS_EMAIL_TO)
    msg.set_content(
        f"Daily backup of all leads from the RKJ WhatsApp concierge.\n"
        f"{n} customers in total. Sheet attached.\n\n"
        f"Hot = asked for the team (call first). Warm = pieces shown. "
        f"New = enquiry only.\n\n— RKJ Concierge Bot")
    msg.add_attachment(
        path.read_bytes(), maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name)

    ctx = ssl.create_default_context()
    if int(config.SMTP_PORT) == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
    else:  # 587 STARTTLS
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
            s.starttls(context=ctx)
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
    log.info("Leads email sent: %d leads to %s", n, config.LEADS_EMAIL_TO)
    return n


async def daily_leads_loop() -> None:
    """Background task: email the lead sheet every day at LEADS_REPORT_HOUR IST."""
    if not smtp_configured():
        log.info("Daily leads email disabled (SMTP not configured)")
        return
    while True:
        now = datetime.now(IST)
        target = now.replace(hour=config.LEADS_REPORT_HOUR, minute=0,
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        log.info("Next leads email at %s", target)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await asyncio.to_thread(send_leads_email)
        except Exception:
            log.exception("Daily leads email failed; retrying tomorrow")
