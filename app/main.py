"""FastAPI webhook server for the RKJ WhatsApp concierge.

Flow: Meta webhook -> signature check -> dedup -> Claude agent (tools) ->
reply via Cloud API. Strictly responsive: only ever replies to inbound.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from functools import partial

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from fastapi.responses import HTMLResponse

from . import claude_agent, config, inbox, pricing, reporting, sessions, whatsapp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rkj.webhook")

app = FastAPI(title="RKJ WhatsApp Concierge")


@app.on_event("startup")
async def _start_daily_leads():
    import asyncio
    asyncio.create_task(reporting.daily_leads_loop())


@app.get("/health")
async def health():
    return {"ok": True,
            "build": config.BUILD,
            "persona_version": config.PERSONA_VERSION,
            "gold_rates_as_of": pricing.rate_source.as_of()}


@app.get("/webhook")
async def verify(mode: str = Query(None, alias="hub.mode"),
                 token: str = Query(None, alias="hub.verify_token"),
                 challenge: str = Query(None, alias="hub.challenge")):
    """Meta webhook verification handshake."""
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "verification failed")


def _valid_signature(raw: bytes, signature: str | None) -> bool:
    if not config.APP_SECRET:
        log.warning("APP_SECRET not set — skipping signature check (dev only)")
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(config.APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks,
                  x_hub_signature_256: str | None = Header(None)):
    raw = await request.body()
    if not _valid_signature(raw, x_hub_signature_256):
        raise HTTPException(403, "bad signature")

    payload = json.loads(raw)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                background.add_task(_handle_message, msg)
    return {"status": "received"}  # ack fast; Meta retries on non-200


async def _handle_message(msg: dict) -> None:
    msg_id, phone = msg.get("id", ""), msg.get("from", "")
    if not phone or sessions.seen_message(msg_id):
        return

    if msg.get("type") == "text":
        text = msg["text"]["body"]
        # Click-to-WhatsApp ad (Instagram/Facebook): Meta attaches referral
        # data to the first message. Note it for the model + lead attribution.
        ref = msg.get("referral") or {}
        if ref:
            src = ref.get("source_type") or "ad"
            head = (ref.get("headline") or ref.get("body") or "").strip()
            note = f"[Customer arrived via your {src}"
            note += f': "{head[:80]}"' if head else ""
            note += " — treat as a fresh enquiry about that piece/collection] "
            text = note + text
        # WhatsApp quoted reply: tell the model exactly what the customer
        # is replying to (a product card or an earlier message of ours).
        ctx_id = (msg.get("context") or {}).get("id")
        if ctx_id:
            hit = sessions.lookup_sent(ctx_id)
            if hit:
                kind, ref = hit
                if kind == "card":
                    text = (f"[The customer sent this as a reply to the product "
                            f"card of piece {ref}] {text}")
                else:
                    text = (f"[The customer sent this as a reply to your earlier "
                            f"message: \"{ref}\"] {text}")
    else:
        # voice notes, images, stickers etc. — graceful nudge, don't invoke Claude
        await whatsapp.send_text(phone, (
            "Namaste! I can help best over text for now — please type what "
            "you're looking for, and I'll show you some lovely pieces. ✨"))
        return

    try:
        reply = await claude_agent.run_turn(
            phone, text,
            send_card=partial(whatsapp.send_image_card, phone),
            notify_team=whatsapp.notify_team,
        )
        if reply:
            mid = await whatsapp.send_text(phone, reply)
            sessions.record_sent(mid, phone, "text", reply[:120])
    except Exception:
        log.exception("Turn failed for %s", phone)
        try:
            await whatsapp.send_text(phone, (
                "Apologies — something went wrong on our side. Our team will "
                "reach out to you shortly."))
            await whatsapp.notify_team(
                f"⚠️ Bot error handling message from {phone}; please follow up.")
        except Exception:
            log.exception("Error-path send failed for %s", phone)


def _check_token(token: str | None, authorization: str | None = None) -> None:
    ok = config.ADMIN_TOKEN and (
        token == config.ADMIN_TOKEN or authorization == f"Bearer {config.ADMIN_TOKEN}")
    if not ok:
        raise HTTPException(403, "forbidden")


@app.get("/admin/inbox", response_class=HTMLResponse)
async def admin_inbox():
    """Live conversation viewer (the page itself checks the token via the API)."""
    return HTMLResponse(inbox.PAGE)


@app.get("/admin/api/conversations")
async def api_conversations(token: str | None = None):
    _check_token(token)
    return sessions.list_conversations()


@app.get("/admin/api/conversation/{phone}")
async def api_conversation(phone: str, token: str | None = None):
    _check_token(token)
    return sessions.conversation(phone)


@app.get("/admin/leads")
async def leads(authorization: str | None = Header(None)):
    """Lead sheet as CSV. Same token as /admin/rates.
    Usage: curl -H "Authorization: Bearer <ADMIN_TOKEN>" .../admin/leads > leads.csv
    """
    if not config.ADMIN_TOKEN or authorization != f"Bearer {config.ADMIN_TOKEN}":
        raise HTTPException(403, "forbidden")
    import csv
    import io
    rows = sessions.leads_summary()
    buf = io.StringIO()
    cols = ["Phone", "Status", "First contact", "Last contact", "Messages",
            "Pieces shown", "Escalations", "Escalation reasons",
            "Latest escalation note", "Last customer message"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader(); w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")


@app.post("/admin/leads/send")
async def send_leads_now(authorization: str | None = Header(None)):
    """Trigger the leads email immediately (for testing the SMTP setup)."""
    if not config.ADMIN_TOKEN or authorization != f"Bearer {config.ADMIN_TOKEN}":
        raise HTTPException(403, "forbidden")
    import asyncio
    n = await asyncio.to_thread(reporting.send_leads_email)
    if n < 0:
        raise HTTPException(422, "SMTP not configured (set SMTP_* and LEADS_EMAIL_TO)")
    return {"sent": True, "leads": n, "to": config.LEADS_EMAIL_TO}


@app.post("/admin/rates")
async def update_rates(request: Request, authorization: str | None = Header(None)):
    """Staff endpoint to set the day's gold rates.
    Body: {"22": 7350, "18": 6010, "14": 4680, "24": 8020}  (INR per gram)
    """
    if not config.ADMIN_TOKEN or authorization != f"Bearer {config.ADMIN_TOKEN}":
        raise HTTPException(403, "forbidden")
    body = await request.json()
    from datetime import date
    rates = {str(k): float(v) for k, v in body.items() if str(k) in {"14", "18", "22", "24"}}
    if not rates:
        raise HTTPException(422, "no valid karat rates in body")
    current = json.loads(pricing.RATES_FILE.read_text())
    current["rates_per_gram"].update(rates)
    current["updated_at"] = date.today().isoformat()
    pricing.RATES_FILE.write_text(json.dumps(current, indent=2))
    return current
