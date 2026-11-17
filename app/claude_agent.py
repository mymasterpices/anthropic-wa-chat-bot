"""Claude integration: persona (verbatim) + tools + per-customer history.

Tools exposed to Claude:
- search_inventory : query the live inventory (rules encoded server-side)
- present_pieces   : send selected pieces to the customer as WhatsApp
                     image cards with a caption written by Claude
- escalate_to_team : flag the conversation to the RKJ team with a summary
"""

from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable

import anthropic

from . import config, inventory, sessions

log = logging.getLogger("rkj.agent")

_PERSONA = config.PERSONA_FILE.read_text()  # used verbatim, do not edit

TOOLS = [
    {
        "name": "search_inventory",
        "description": (
            "Search RK Jewellers' live inventory. Call before ANY claim about "
            "available pieces. Prices are returned only as customer-safe "
            "ranges. To answer a question about one specific piece (its "
            "price, weight, carat — e.g. when the customer replies to a "
            "product card), pass its sku for a direct lookup. Results vary "
            "sub-category/shape and automatically EXCLUDE pieces this "
            "customer has already been shown, so 'more options' are always "
            "fresh. For 'show similar', pass similar_to_sku. If nothing "
            "matches, widen once (adjacent category or budget) before "
            "telling the customer. Conversation history notes like "
            "'[showed pieces: SKU1, SKU2]' record what you showed earlier — "
            "when the customer says 'the first one' or 'pehla wala', use "
            "those SKUs with the sku parameter to answer."),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_type": {"type": "string", "enum": ["DIAMOND", "GOLD"]},
                "category": {"type": "string", "description": "e.g. RING, EARRING, CHAIN, BRACELET, PENDANT, NECKLACE, KANTHI NECKLACE, BANGLE, NOSE PIN, MANGALSUTRA"},
                "sub_category": {"type": "string", "description": (
                    "Map the customer's words to these. RING: SOLITAIRE, BAND, COCKTAIL, ETERNITY, CLUSTER, TRADITIONAL, MIDI. "
                    "EARRING: STUDS, JHUMKA, HOOPS, DANGLER, CHAND BALI, CLUSTER, SOLITAIRE. "
                    "NECKLACE: BRIDAL, NON BRIDAL, CHOKER, LINES, LONG CHAIN, TASSEL. "
                    "BRACELET: TENNIS, KADA, CUFF, CUBAN, KIDS. "
                    "PENDANT: RELIGIOUS, ALPHABETS, TENNIS, SOLITAIRE. "
                    "CHAIN: MATAR MALA, NAWABI, ROPE, ITALIAN, HOLLOW, PLAIN. "
                    "BANGLE: BRIDAL, LIGHT, HEAVY, ENAMEL, KIDS. MANGALSUTRA: TRADITIONAL, LONG. "
                    "Examples: 'engagement ring / ek heera' -> SOLITAIRE; 'simple office wear' -> STUDS or LIGHT; "
                    "'bridal set' -> BRIDAL; 'temple style' -> TRADITIONAL; 'kade' -> KADA.")},
                "colour_stones": {"type": "string", "enum": ["with", "without"], "description": (
                    "'without' when the customer wants ONLY diamonds / no coloured "
                    "gemstones (no ruby, emerald, sapphire, polki). 'with' when they "
                    "want colour-stone pieces. ALWAYS pass 'without' when the customer "
                    "says things like 'all diamond', 'sirf diamond', 'no coloured stones'.")},
                "occasion": {"type": "string", "description": "e.g. WEDDING, PARTY WEAR, DAILY WEAR, EVENING. Soft preference, never excludes untagged pieces."},
                "gender": {"type": "string", "enum": ["LADIES", "GENTS", "UNISEX"]},
                "karat": {"type": "integer", "enum": [14, 18, 22, 24]},
                "min_price": {"type": "number", "description": "INR"},
                "max_price": {"type": "number", "description": "INR"},
                "shape": {"type": "string", "description": "Diamond shape, e.g. Round, Oval, Pear shape, Marquise, Emerald cut"},
                "style_code": {"type": "string"},
                "sku": {"type": "string", "description": "Direct lookup of one specific piece — use for price/detail questions about a piece already shown"},
                "similar_to_sku": {"type": "string", "description": "Find pieces similar to this SKU"},
                "limit": {"type": "integer", "description": "1-5, default 4"},
            },
        },
    },
    {
        "name": "present_pieces",
        "description": (
            "Send pieces to the customer as WhatsApp image cards. Each card "
            "shows the piece's photo with your caption — one short, evocative "
            "line in the customer's language (the story of the piece, not a "
            "spec sheet). Use SKUs from search_inventory results only. Max 5 "
            "per message; the standard curation is 4 varied pieces. After "
            "calling this, your text reply should invite a shortlist — do not "
            "repeat the piece details in text."),
        "input_schema": {
            "type": "object",
            "properties": {
                "pieces": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                        "required": ["sku", "caption"],
                    },
                }
            },
            "required": ["pieces"],
        },
    },
    {
        "name": "escalate_to_team",
        "description": (
            "Flag this conversation to the RKJ team (exact pricing/discount "
            "requests, custom design, repair/exchange/buyback, complaints, "
            "bulk/trousseau, customer asks for a person or is frustrated, "
            "payments or suspected fraud, or you are uncertain). Provide a "
            "one-line summary. Continue replying to the customer as the "
            "persona instructs after escalating."),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "summary": {"type": "string", "description": "One line for the team"},
            },
            "required": ["reason", "summary"],
        },
    },
]

# Keyword fallback so critical intents escalate even if the model doesn't call the tool
_ESCALATION_KEYWORDS = ("payment", "paid", "refund", "otp", "fraud", "complaint",
                        "manager", "discount", "best price", "final price")

# Guard: reply promises to show pieces ("let me find...", "dikhata hoon")
# without actually having sent any product cards this turn.
_PROMISE_RE = re.compile(
    r"let me (find|show|share|pull|curate)|i'?ll (find|show|share|pull)"
    r"|one moment|just a moment|dikhat|dikhau|dhundh|nikal", re.IGNORECASE)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


async def run_turn(phone: str, user_text: str,
                   send_card: Callable[[str, str], Awaitable[None]],
                   notify_team: Callable[[str], Awaitable[None]]) -> str:
    """Process one inbound customer message; returns the text reply.

    send_card(image_url, caption) is called for each product card.
    notify_team(summary) is called on escalation.
    """
    sessions.append(phone, "user", user_text, config.PERSONA_VERSION)
    messages = sessions.get_history(phone)

    shown_skus: list[str] = []
    sku_images: dict[str, str] = {}
    escalated = False

    async def _tool_loop() -> str:
        """Run the model+tools until a final text reply; returns the reply."""
        nonlocal escalated
        resp = None
        for _ in range(8):  # tool-use loop guard
            resp = await _client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.MAX_TOKENS,
                system=_PERSONA,
                tools=TOOLS,
                messages=messages,
            )
            if resp.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                result: dict | str
                try:
                    if block.name == "search_inventory":
                        params = dict(block.input)
                        if not params.get("sku"):
                            # fresh options only: exclude everything this
                            # customer has already been shown
                            params["exclude_skus"] = sessions.shown_skus(phone)
                        result = inventory.search_inventory(**params)
                        for p in result.get("pieces", []):
                            sku_images[p["sku"]] = p["image_url"]
                    elif block.name == "present_pieces":
                        # Design protection: hard cap on images per customer
                        already = len(sessions.shown_skus(phone))
                        budget = max(0, config.MAX_CARDS_PER_CUSTOMER - already)
                        sent = []
                        for piece in block.input.get("pieces", [])[:5]:
                            if len(sent) >= budget:
                                break
                            url = sku_images.get(piece["sku"])
                            if not url:
                                continue  # only pieces from real search results
                            caption = f"{piece['caption']}\n\nCode: {piece['sku']}"
                            mid = await send_card(url, caption)
                            sessions.record_sent(mid, phone, "card", piece["sku"])
                            sent.append(piece["sku"])
                        shown_skus.extend(sent)
                        result = {"sent": sent}

                        total = already + len(sent)
                        if total >= config.MAX_CARDS_PER_CUSTOMER:
                            result["limit_reached"] = (
                                f"This customer has now been shown "
                                f"{total} pieces — the maximum. Do NOT send any "
                                "more images. Warmly explain that our team will "
                                "personally take over from here to understand "
                                "their taste properly, and ask for a good time "
                                "to call. Keep it gracious — never suggest "
                                "suspicion.")
                            if not sessions.has_escalation(phone, "image_limit"):
                                summary = (
                                    f"{total} designs shown, nothing shortlisted "
                                    f"— please verify the customer is genuine "
                                    f"before sharing further designs "
                                    f"(customer: {phone})")
                                sessions.log_escalation(
                                    phone, "image_limit",
                                    f"{total} images shown without a selection")
                                await notify_team(f"⚠️ Design-share limit: {summary}")
                                escalated = True
                    elif block.name == "escalate_to_team":
                        summary = (f"{block.input.get('reason', 'escalation')}: "
                                   f"{block.input.get('summary', '')} "
                                   f"(customer: {phone})")
                        sessions.log_escalation(phone, block.input.get("reason", ""),
                                                block.input.get("summary", ""))
                        await notify_team(summary)
                        escalated = True
                        result = {"status": "team notified"}
                    else:
                        result = {"error": f"unknown tool {block.name}"}
                except Exception as exc:
                    log.exception("Tool %s failed", block.name)
                    result = {"error": str(exc)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": tool_results})

        return "".join(b.text for b in resp.content if b.type == "text").strip()

    reply = await _tool_loop()

    # Guard: never send "let me show you some pieces" and then go silent.
    # If the reply promises pieces but no cards were sent this turn, give the
    # model one corrective pass to actually search and present.
    if reply and not shown_skus and _PROMISE_RE.search(reply):
        log.warning("Promise-without-cards from model for %s; forcing redo", phone)
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": (
            "[system correction — the customer does not see this message: your "
            "last reply told the customer you would show them pieces, but you "
            "did not call present_pieces. Do it now: call search_inventory, "
            "then present_pieces with your curated selection, then send a "
            "short reply inviting a shortlist. Do not apologise for a delay.]")})
        redo = await _tool_loop()
        if shown_skus and redo:
            reply = redo

    if not reply:
        reply = ("Apologies, I couldn't process that just now. "
                 "May I connect you with our team?")

    # Safety net: keyword-based escalation if the model didn't call the tool
    if not escalated and any(k in user_text.lower() for k in _ESCALATION_KEYWORDS):
        summary = f"keyword trigger: \"{user_text[:120]}\" (customer: {phone})"
        sessions.log_escalation(phone, "keyword", user_text[:200])
        await notify_team(summary)

    logged = reply + (f"\n[showed pieces: {', '.join(shown_skus)}]" if shown_skus else "")
    sessions.append(phone, "assistant", logged, config.PERSONA_VERSION)
    return reply
