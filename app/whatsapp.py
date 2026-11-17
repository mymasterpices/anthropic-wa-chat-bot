"""Meta WhatsApp Cloud API client (send text, image cards, team notify)."""

from __future__ import annotations

import logging

import httpx

from . import config

log = logging.getLogger("rkj.whatsapp")


async def _post(payload: dict) -> dict:
    url = f"{config.GRAPH_URL}/{config.PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()


async def send_text(to: str, body: str) -> str:
    """Returns the WhatsApp message id (used to resolve quoted replies)."""
    data = await _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096], "preview_url": False},
    })
    return (data.get("messages") or [{}])[0].get("id", "")


async def send_image_card(to: str, image_url: str, caption: str) -> str:
    """Product card: image message with caption. Returns the message id."""
    data = await _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption[:1024]},
    })
    return (data.get("messages") or [{}])[0].get("id", "")


async def notify_team(summary: str) -> None:
    """Escalation ping. Note: the Cloud API cannot post to WhatsApp groups,
    so each number in TEAM_NOTIFY_NUMBERS gets an individual message.
    (Recipients must have messaged the business number within 24h, or this
    needs a pre-approved template — see README, 'Escalation delivery'.)"""
    for number in config.TEAM_NOTIFY_NUMBERS:
        try:
            await send_text(number, f"🔔 RKJ bot escalation\n{summary}")
        except Exception:  # never let a notify failure break the customer flow
            log.exception("Team notify to %s failed", number)
