# WhatsApp Concierge Bot

Customer-facing WhatsApp chatbot for RK Jewellers, South Extension Part II,
New Delhi. Handles inquiries, curates pieces from live inventory, shares
price ranges, drives showroom visits. Strictly responsive at launch —
replies to inbound messages only.

## Architecture

Customer (WhatsApp) → Meta Cloud API (webhook) → FastAPI server →
Claude API (persona + `search_inventory` tool) + SQLite inventory →
reply via Meta send-message endpoint. Human handoff on escalation.

```
app/
  main.py          FastAPI webhook (signature verify, dedup, greeting menu, /admin/rates)
  claude_agent.py  Claude tool loop: search_inventory, present_pieces, escalate_to_team
  inventory.py     search_inventory over rkj_inventory.db (all data rules encoded)
  pricing.py       Gold pricing + swappable rate source + lakh formatting
  orders.py        Order Status lookup against a staff Google Sheet
  whatsapp.py      Cloud API client (text, image cards, button menu, team notify)
  sessions.py      Per-phone history, escalation log, webhook dedup (SQLite)
  config.py        Env config
data/
  rkj_inventory.db 5,396 pieces (2,126 diamond + 3,270 gold)
  rates.json       Daily gold rates — STAFF-UPDATED, see below
  sessions.db      Created at runtime
persona/rkj-whatsapp-persona.md   System prompt — used VERBATIM, do not edit casually
templates/meta_templates.md       3 re-engagement templates to submit to Meta EARLY
inventory_loader.py               Re-run when a fresh ERP export arrives
update_rates.py                   Staff CLI for the daily gold rate
tests/test_core.py                17 checks against the real DB
```

## Setup

1. **Meta (manual, Rohan):** Business account → WhatsApp Business API app →
   test phone number (NOT the main line) → note `PHONE_NUMBER_ID`,
   permanent system-user token, app secret.
2. `cp .env.example .env` and fill in every value.
3. `pip install -r requirements.txt`
4. `python update_rates.py 22=7350 18=6010 14=4680 24=8020` — set real rates
   (the shipped rates.json contains placeholders).
5. `uvicorn app.main:app --host 0.0.0.0 --port 8000`
6. In the Meta app dashboard, set the webhook callback to
   `https://<your-host>/webhook` with your `VERIFY_TOKEN`, subscribe to
   `messages`.
7. **Submit the three templates in `templates/meta_templates.md` for Meta
   approval now** — review takes days and re-engagement beyond 24h is
   blocked without them.

Deploy on Railway/Render: point at this repo, set env vars from `.env`,
start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
Note: on ephemeral-disk hosts, put `data/` on a persistent volume
(sessions.db and rates.json must survive restarts).

## Daily gold rate (staff workflow — decided at kickoff)

Rates are a staff-updated config, not an API feed. Each morning:

- `python update_rates.py 22=7350` (any subset of karats, INR/gram), **or**
- `POST /admin/rates` with `Authorization: Bearer <ADMIN_TOKEN>` and body
  `{"22": 7350, "18": 6010}`.

`GET /health` shows when rates were last updated. The rate source is a
swappable class (`pricing.RateSource`) — an API feed can be dropped in
later without touching any other code.

## Pricing rules (critical — encoded in pricing.py)

- **DIAMOND:** `mrp` is all-in. Presented only as ±10% range, never exact.
- **GOLD:** `mrp` in the export is stale. Live:
  `rate[karat] × net_wt × (1 + making_pct/100) + stone_amt`, presented as a
  ±10% range with the day's-gold-rate caveat.
- Customers never see: exact prices, mrp, making_pct, metal internals.
  The tool returns only pre-formatted range strings (lakh notation).

## Escalation (decided at kickoff: WhatsApp ping to the team)

The model calls `escalate_to_team` per the persona triggers; a keyword
fallback (payment/fraud/discount/complaint/manager…) catches misses.
Every escalation is logged in `data/sessions.db` (`escalations` table).

**Delivery caveat:** the Cloud API cannot post to WhatsApp *groups*, so
pings go individually to each number in `TEAM_NOTIFY_NUMBERS`. Team
members must message the bot number once (opens the 24h window) or
you'll need a small approved template for the ping — simplest fix: each
team member sends "hi" to the bot number every morning, or add a
`team_alert` template to the Meta submission batch.

## Greeting menu (General Inquiry / Order Status)

When a customer sends a bare greeting ("hi", "hello", "namaste", "good
morning" — not a full sentence like "hi, looking for a ring") on their
**first-ever message**, or when **re-opening after `GREETING_MENU_GAP_HOURS`
of silence** (default 6h), the bot replies with two WhatsApp reply buttons
instead of going to Claude:

- **General Inquiry** → hands off into the normal Claude concierge flow
  (search_inventory / present_pieces / escalate_to_team), unchanged.
- **Order Status** → looks up the customer's WhatsApp number in the orders
  Google Sheet and replies directly. This never goes through Claude — it's
  a deterministic lookup, so it can't be answered incorrectly by the model
  and Sheet data is never sent to the Anthropic API.

A mid-conversation "hi" (not the first message, not after a long gap) is
left alone — the persona already handles that naturally.

**Order Status data source:** a Sheet maintained by staff, read via the
Sheets API v4 with an API key (`GOOGLE_SHEETS_ID`, `GOOGLE_SHEETS_API_KEY`
in `.env`) — **the Sheet must be shared "Anyone with the link — Viewer"**,
since an API key alone can't read a private sheet. Row 1 = header, column
order doesn't matter; required columns are **Phone** and **Status** (a few
common header spellings are auto-detected — see `orders.py` for the full
alias list). Optional columns: Order ID, Item, Expected Date, Last Updated
(used to show the most recent order when a phone number has more than
one row). Phone numbers are matched on their last 10 digits, so `+91
98116 90123`, `9811690123`, and `09811690123` are all treated as the same
customer.

If `GOOGLE_SHEETS_ID`/`GOOGLE_SHEETS_API_KEY` are left blank, Order Status
requests politely escalate to the team instead of failing silently.

## Fresh inventory

When a new ERP export arrives:
`python inventory_loader.py` (regenerates `data/rkj_inventory.db`), then
restart. All packing-list stock is assumed available (`in_stock=1`) — no
sold-stock flag exists in the ERP export yet.

## Testing

- `python -m tests.test_core` — 17 checks against the real DB (pricing
  formula, lakh/range formatting, soft occasion filter, variety rule,
  set-component context, show-similar, shape filter, no-internals-leak).
- Internal go-live check: team numbers on the test line first, per the
  build order. Log review: every conversation is stored with
  `persona_version` in `data/sessions.db`.

## Design deviations from the brief (intentional)

- Added `similar_to_sku` tool param: style codes turned out to be unique
  per piece in this export, so "same style_code" alone returns nothing —
  the server falls back to same sub_category + adjacent price band.
- Added `present_pieces` tool so product cards (image + caption) are sent
  by the server with captions written by Claude, keeping the persona file
  verbatim as the system prompt.
- Category matching is word-boundary ("RING" no longer matches "EARRING").
