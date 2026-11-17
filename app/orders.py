"""Order-status lookup against a staff-maintained Google Sheet.

Read-only access via the Sheets API v4 with an API key — no OAuth, no
service account. This means the Sheet MUST be shared as "Anyone with the
link — Viewer" (Share -> General access -> Anyone with the link), since an
API key alone cannot read a private sheet.

--- Sheet layout ---------------------------------------------------------

Row 1 is a header row. EVERY functional field is matched explicitly by
name (case/whitespace/punctuation-insensitive) against the aliases in
_COLUMN_ALIASES below — there is no "any leftover column becomes a
stage" auto-detection anymore. A column whose header doesn't match any
known alias is simply ignored. This is deliberate: auto-detection was
the root cause of an earlier bug where a near-duplicate/mismatched
header (e.g. "Expected Delivery Date" with different spacing) silently
became a stray, wrongly-labelled progress-stage checkpoint instead of
being recognised as the known field it was. With every field named
explicitly, an unrecognised header can now only ever be inert, never
mislabelled.

    Mobile / Phone           required — unique key for the customer.
    Order Number              optional — shown as "Order: <value>".
    Item                      optional — shown as "Item: <value>".
    Order Cancelled           optional — ANY value here wins over
                              everything else; cancelled message only.
    Revised Delivery Date     optional — ANY value here (with Order
                              Cancelled blank) wins over the normal stage
                              messages; "delayed" message.
    Expected Delivery Date    optional — the estimate shown on the final
                              "Ready For Pickup" line while that stage is
                              still pending.
    Order Confirmed           the sole gate for progressing past "Order
                              Placed" — while blank, the customer only
                              ever sees the Order Placed message, no
                              matter what the later stage fields hold.
    Order Received             optional — the date shown on the "Order
                              Placed" checkpoint (whether it's pinned
                              there because Order Confirmed is blank, or
                              shown as an earlier completed step once
                              later stages are current). Falls back to
                              Order Confirmed's own date if this column
                              isn't present in the sheet.
    Under Manufacturing        2nd stage field.
    Quality Check              3rd stage field.
    Ready for Pickup           4th stage field. While pending, the
                              customer sees the Expected Delivery Date
                              estimate; once it becomes the current stage
                              (see below), the checkpoint is announced
                              using Quality Check's own completion date.

If MORE THAN ONE column header matches the same field's aliases, ALL of
them are recognised and whichever one actually HAS a value wins (first
non-blank, left to right) — see _first_value.

--- Current-stage logic ----------------------------------------------------

Current stage = the FIRST BLANK field, checked strictly in this order:

    Order Confirmed -> Under Manufacturing -> Quality Check -> Ready for Pickup

Order Confirmed is a hard gate, not just "first blank": if it's blank,
the customer sees the Order Placed message even if Under Manufacturing,
Quality Check, or Ready for Pickup already have values in the sheet
(out-of-order staff entry is never surfaced early). Order Received can
still flip the Order Placed checkpoint's own tick to ✅ with its date
while pinned on that stage, but does not by itself unlock progress to
Under Manufacturing or beyond — only Order Confirmed does that.

For every stage after Order Placed, this is the FIRST BLANK field,
checked in this order:

This is deliberately NOT "right-most filled column" — an earlier blank
field is always treated as the current stage even if a later field
happens to have a value (out-of-order data is not inferred past). If you
need the sheet to correctly reflect a later stage, fill the earlier
field too, even with a placeholder value.

If ALL FOUR stage fields are filled (and there's no cancellation or
revised delivery date), there is deliberately no defined message for
that case yet — lookup_order_status()/format_status_reply() return the
literal string "No status message required" per the current business
rules, pending a real "already picked up, nothing further to say"
message being specified.

--- Reply precedence (first match wins) ------------------------------------

  1. Order Cancelled has a value       -> cancelled message
  2. Revised Delivery Date has a value -> "delayed" message
  3. Order Confirmed blank             -> "received" message
  4. Under Manufacturing blank         -> "under manufacturing" message
  5. Quality Check blank               -> "quality check" message
  6. Ready for Pickup blank            -> "ready for pickup" message
  7. all four filled                   -> "No status message required"

--- Dates -------------------------------------------------------------------

Most dates shown to the customer are reformatted to a friendly
"<ordinal day> <abbreviated month>" form (e.g. "31st May"), with the year
dropped, regardless of how it's stored in the sheet — see
_friendly_date(). Parsing is defensive: if a cell's value doesn't match
a recognised date format, it's shown exactly as stored rather than
crashing or silently dropping it.

One exception: the Order Cancelled note (the 📝 line on the cancelled
message) is shown exactly as stored in the sheet, unformatted — it is
free-text staff commentary, not necessarily a plain date.

The Ready For Pickup checkpoint uses two different sources depending on
where it is: while pending (shown on every earlier stage as "... 12th
Jun Expected") it's the Expected Delivery Date estimate; once it becomes
the current stage itself ("✅ Ready For Pickup - 5th Aug", announced
before staff formally stamp the Ready for Pickup cell) it switches to
Quality Check's own completion date — the last real, confirmed
milestone at that point.

The "Order Placed" checkpoint's date always comes from the Order
Received column, never Order Confirmed's own date (Order Confirmed only
gates whether that stage counts as done). If the sheet has no Order
Received column, it falls back to Order Confirmed's date.

Configure via .env:
    GOOGLE_SHEETS_ID       the spreadsheet ID (from its URL)
    GOOGLE_SHEETS_API_KEY  a Google Cloud API key with Sheets API enabled
    ORDERS_SHEET_RANGE     e.g. "OrderStatus!A:Z"

Every function here is defensive: on any failure (not configured, network
error, malformed sheet) callers get `None`/empty rather than an exception,
so an order-status hiccup never breaks the rest of the bot.

--- Debugging stale/wrong data ---------------------------------------------

  debug_status()
      Force-refetches the sheet and reports fetch health, which columns
      were recognised for which field, and flags any field matched by
      more than one column header.

  raw_rows_for_order(order_id) / raw_rows_for_phone(phone)
      Dumps the exact, unprocessed sheet row(s) for an order ID or phone
      number — no interpretation applied.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from . import config

log = logging.getLogger("rkj.orders")

_CACHE_TTL = 60  # seconds — avoid re-fetching the whole sheet on every message

_cache: dict[str, Any] = {
    "rows": None,
    "ts": 0.0,
    "last_attempt_ts": None,
    "last_success_ts": None,
    "last_error": None,
}

# Every functional field is named explicitly here — nothing falls through
# to a generic "leftover column = stage" bucket anymore (see module
# docstring for why).
_COLUMN_ALIASES = {
    "phone": {"phone", "phone number", "mobile", "mobile number",
              "whatsapp", "whatsapp number", "customer phone"},
    "order_id": {"order id", "order no", "order number", "order #", "orderid"},
    "item": {"item", "product", "description", "item description"},
    "cancelled": {"order cancelled", "cancelled", "canceled", "cancel",
                  "order cancelled?", "order canceled", "cancellation"},
    "revised_delivery_date": {"revised delivery date", "revised date", "revised eta",
                              "new delivery date", "new expected date", "revised delivery"},
    "expected_date": {"expected date", "expected delivery", "expected delivery date",
                      "eta", "delivery date", "first delivery date"},
    "order_confirmed": {"order confirmed", "confirmed", "confirmation date",
                        "order confirmation"},
    "order_received": {"order received", "received date", "order receive date",
                       "date received", "order received date"},
    "under_manufacturing": {"under manufacturing", "manufacturing", "in manufacturing"},
    "quality_check": {"quality check", "quality checked", "qc"},
    "ready_for_pickup": {"ready for pickup", "ready for dispatch", "picked up",
                         "pickup date", "ready to dispatch"},
}

_GENERIC_FLAG_VALUES = {"yes", "y", "true", "1", "done", "cancelled",
                        "canceled", "✓", "x"}

_INTRO = {
    "order_confirmed": "Your order has been received.",
    "under_manufacturing": "Your order is under manufacturing.",
    "quality_check": "Your order is undergoing final quality check.",
    "ready_for_pickup": "Your order is ready for pickup.",
    "delayed": "Your order is delayed for pickup.",
}

_NO_STATUS_MESSAGE = "No status message required"

_DATE_FORMATS = (
    "%d-%b-%y", "%d-%b-%Y",   # 31-May-26 / 31-May-2026
    "%d/%m/%y", "%d/%m/%Y",
    "%Y-%m-%d",
    "%d %b %y", "%d %b %Y",
    "%d-%m-%y", "%d-%m-%Y",
)


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _friendly_date(raw: str | None) -> str | None:
    """Reformat a sheet date value to '<ordinal day> <abbrev month>'
    (e.g. '31st May'), year dropped, per the DATE RULES spec. If the raw
    value doesn't match a recognised date format, it's returned exactly
    as stored — never crashes, never silently drops the value."""
    if not raw:
        return raw
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return f"{_ordinal(dt.day)} {dt.strftime('%b')}"
        except ValueError:
            continue
    return raw  # unrecognised format — show as-is rather than guessing


def configured() -> bool:
    return bool(config.GOOGLE_SHEETS_ID and config.GOOGLE_SHEETS_API_KEY)


def _normalize_phone(raw: str) -> str | None:
    """Normalise a phone number to a bare 10-digit core, or None if it
    doesn't cleanly fit a recognised format (10 digits; 11 with a leading
    0; 12 with a 91 country code; 13 with a leading 091). Stricter than
    blindly taking the last 10 digits of anything — a malformed or
    accidentally-too-long number is rejected outright rather than matched
    via coincidental digit overlap with a real number."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10:
        return d
    if len(d) == 11 and d.startswith("0"):
        return d[1:]
    if len(d) == 12 and d.startswith("91"):
        return d[2:]
    if len(d) == 13 and d.startswith("091"):
        return d[3:]
    return None


def _normalize_header(h: str) -> str:
    """Lowercased, whitespace-collapsed, trailing-punctuation-stripped
    header text, used for alias matching — tolerant of the small
    real-world variations sheet headers pick up (double spaces, a stray
    trailing colon, etc.)."""
    h = (h or "").strip()
    h = re.sub(r"\s+", " ", h)
    h = h.rstrip(":?.")
    return h.lower()


def _map_headers(header_row: list[str]) -> dict[str, list[int]]:
    """Map every known field to ALL column indices whose header matches
    one of its aliases. Columns matching no known field are ignored —
    there is no fallback "stage" bucket."""
    mapping: dict[str, list[int]] = {}
    for i, raw in enumerate(header_row):
        h = (raw or "").strip()
        if not h:
            continue
        hl = _normalize_header(h)
        for field, aliases in _COLUMN_ALIASES.items():
            if hl in aliases:
                mapping.setdefault(field, []).append(i)
                break
    return mapping


def _first_value(row: list[str], indices: list[int] | None) -> str | None:
    """First non-blank cell value among the candidate indices for a
    known field (left to right), or None."""
    if not indices:
        return None
    for i in indices:
        if i < len(row):
            v = (row[i] or "").strip()
            if v:
                return v
    return None


def _fetch_rows(force: bool = False) -> list[list[str]] | None:
    """Fetch (and cache for _CACHE_TTL seconds) the raw sheet values.
    Returns None if not configured, or a possibly-stale cached copy if a
    fresh fetch fails. Every attempt is recorded for debug_status()."""
    if not configured():
        return None
    now = time.time()
    if not force and _cache["rows"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["rows"]
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/"
           f"{config.GOOGLE_SHEETS_ID}/values/{config.ORDERS_SHEET_RANGE}")
    _cache["last_attempt_ts"] = now
    try:
        r = httpx.get(url, params={"key": config.GOOGLE_SHEETS_API_KEY}, timeout=10)
        r.raise_for_status()
        rows = r.json().get("values", [])
    except Exception as exc:
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        _cache["last_error"] = detail
        log.exception("Orders sheet fetch failed")
        return _cache["rows"]
    _cache["rows"], _cache["ts"] = rows, now
    _cache["last_success_ts"] = now
    _cache["last_error"] = None
    return rows


def debug_status(force_refetch: bool = True) -> dict:
    """Diagnostic snapshot for figuring out why the bot might be serving
    stale or wrong order data. Does not affect normal lookup behaviour."""
    now = time.time()
    if not configured():
        return {
            "configured": False,
            "note": ("GOOGLE_SHEETS_ID and/or GOOGLE_SHEETS_API_KEY are not "
                     "set — Order Status requests are escalating to the "
                     "team instead of reading the sheet at all."),
        }

    rows = _fetch_rows(force=force_refetch)
    last_success_ts = _cache.get("last_success_ts")
    last_attempt_ts = _cache.get("last_attempt_ts")
    last_error = _cache.get("last_error")

    info: dict[str, Any] = {
        "configured": True,
        "spreadsheet_id": config.GOOGLE_SHEETS_ID,
        "range": config.ORDERS_SHEET_RANGE,
        "this_call_forced_refetch": force_refetch,
        "last_fetch_attempt": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_attempt_ts))
            if last_attempt_ts else None),
        "last_successful_fetch": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_success_ts))
            if last_success_ts else None),
        "seconds_since_last_success": (
            round(now - last_success_ts, 1) if last_success_ts else None),
        "last_fetch_error": last_error,
    }

    if rows is None:
        info["status"] = "FAILING — no data available at all"
        info["likely_cause"] = (
            "Never successfully fetched. Check GOOGLE_SHEETS_ID, "
            "GOOGLE_SHEETS_API_KEY, and that the sheet is shared as "
            "'Anyone with the link — Viewer'.")
        return info

    if last_error:
        info["status"] = "STALE — serving cached data, latest fetch failed"
        info["likely_cause"] = (
            "The sheet was reachable before but the most recent fetch "
            "failed (see last_fetch_error above). The bot is silently "
            f"serving the last good copy from {info['last_successful_fetch']} "
            "until this is fixed.")
    else:
        info["status"] = "OK — last fetch succeeded"

    info["data_row_count"] = max(len(rows) - 1, 0) if rows else 0
    if rows:
        cols = _map_headers(rows[0])
        info["header_row"] = rows[0]
        info["recognised_known_columns"] = {
            field: [rows[0][i] for i in idxs if i < len(rows[0])]
            for field, idxs in cols.items()
        }
        info["unrecognised_columns"] = [
            h for i, h in enumerate(rows[0])
            if (h or "").strip() and not any(i in idxs for idxs in cols.values())
        ]

        ambiguous = {f: heads for f, heads in info["recognised_known_columns"].items()
                    if len(heads) > 1}
        if ambiguous:
            info["ambiguous_column_headers"] = ambiguous
            info["ambiguous_column_warning"] = (
                "The following fields are matched by MORE THAN ONE column "
                "header. Handled correctly (first non-blank cell wins), "
                "but worth confirming it's intentional.")

        if "phone" not in cols:
            info["status"] = "MISCONFIGURED — no Mobile/Phone column recognised"
        missing_stages = [f for f in ("order_confirmed", "under_manufacturing",
                                       "quality_check", "ready_for_pickup")
                          if f not in cols]
        if missing_stages:
            info["missing_stage_columns"] = missing_stages
            info["status"] = "MISCONFIGURED — one or more stage columns not found"
            info["likely_cause"] = (
                f"No column header matched: {missing_stages}. Check the "
                "sheet's actual header text against _COLUMN_ALIASES.")
    return info


def raw_rows_for_order(order_id: str) -> list[dict[str, str]]:
    """Exact, unprocessed sheet row(s) for an Order Number — header ->
    raw cell value, no interpretation applied."""
    rows = _fetch_rows(force=True)
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    cols = _map_headers(header)
    order_idx = cols.get("order_id")
    if not order_idx:
        return []
    target = order_id.strip().lower()
    out = []
    for row in rows[1:]:
        val = _first_value(row, order_idx)
        if val and val.strip().lower() == target:
            out.append({header[i] if i < len(header) else f"col_{i}":
                        (row[i] if i < len(row) else "")
                        for i in range(len(header))})
    return out


def raw_rows_for_phone(phone: str) -> list[dict[str, str]]:
    """Exact, unprocessed sheet row(s) for a Mobile number — header ->
    raw cell value, no interpretation applied."""
    rows = _fetch_rows(force=True)
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    cols = _map_headers(header)
    phone_idx = cols.get("phone")
    if not phone_idx:
        return []
    target = _normalize_phone(phone)
    if not target:
        return []
    out = []
    for row in rows[1:]:
        raw_phone = _first_value(row, phone_idx)
        if raw_phone and _normalize_phone(raw_phone) == target:
            out.append({header[i] if i < len(header) else f"col_{i}":
                        (row[i] if i < len(row) else "")
                        for i in range(len(header))})
    return out


def lookup_order_status(phone: str) -> dict | None:
    """Look up ALL order(s) for this WhatsApp phone number — Mobile is
    the unique key. Matching is EXACT on the normalised phone number,
    never a partial/substring match.

    Returns:
      None                — not configured, sheet unreachable, malformed
                             sheet (missing Mobile or a stage column), or
                             no match at all
      { one match dict }
      {"multiple": [...], "total": N}  — every order for this number

    Each match dict has the RAW field values (order_id, item,
    expected_date, order_confirmed, order_received, under_manufacturing,
    quality_check, ready_for_pickup, revised_delivery_date,
    cancelled_note) — dates are formatted at render time in
    _order_block(), not here.
    """
    rows = _fetch_rows()
    if not rows or len(rows) < 2:
        return None
    cols = _map_headers(rows[0])
    required = ("phone", "order_confirmed", "under_manufacturing",
                "quality_check", "ready_for_pickup")
    if any(f not in cols for f in required):
        log.error("Orders sheet is missing a required column: %s",
                  [f for f in required if f not in cols])
        return None
    target = _normalize_phone(phone)
    if not target:
        return None

    matches = []
    for row in rows[1:]:
        if not any((c or "").strip() for c in row):
            continue  # fully blank spacer row
        row_phone = _first_value(row, cols.get("phone"))
        if not row_phone or _normalize_phone(row_phone) != target:
            continue  # strict, exact match only — no partial matching
        matches.append({
            "order_id": _first_value(row, cols.get("order_id")),
            "item": _first_value(row, cols.get("item")),
            "expected_date": _first_value(row, cols.get("expected_date")),
            "order_confirmed": _first_value(row, cols.get("order_confirmed")),
            "order_received": _first_value(row, cols.get("order_received")),
            "under_manufacturing": _first_value(row, cols.get("under_manufacturing")),
            "quality_check": _first_value(row, cols.get("quality_check")),
            "ready_for_pickup": _first_value(row, cols.get("ready_for_pickup")),
            "revised_delivery_date": _first_value(row, cols.get("revised_delivery_date")),
            "cancelled_note": _first_value(row, cols.get("cancelled")),
        })
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return {"multiple": matches, "total": len(matches)}


def _cancelled_block(m: dict) -> str:
    lines = ["Your order has been cancelled.", ""]
    lines.append(f"Order: {m['order_id']}" if m.get("order_id") else "Order: -")
    if m.get("item"):
        lines.append(f"Item: {m['item']}")
    lines.append("")
    lines.append("❌ Order Cancelled")
    note = m["cancelled_note"].strip()
    if note.lower() not in _GENERIC_FLAG_VALUES:
        # Shown exactly as stored in the sheet — not run through
        # _friendly_date() — per the Aug 2026 spec revision.
        lines.append(f"📝 {note}")
    return "\n".join(lines)


def _delayed_block(m: dict, order_received_date: str | None) -> str:
    return "\n |\n".join([
        f"✅ Order Placed - *{order_received_date}*",
        "✅ Manufactured",
        "✅ Quality Checked",
        f"⏳ Delayed For Pickup - *{_friendly_date(m['revised_delivery_date'])}*",
    ])


def _order_block(m: dict) -> str:
    """One order as a customer-facing WhatsApp message, following the
    exact decision tree and wording from the current business spec.
    Precedence: cancelled > delayed > current-stage > "No status message
    required" if all four filled.

    Order Confirmed is the sole gate for progressing past "Order
    Placed": while it's blank, the customer only ever sees the Order
    Placed message, even if Under Manufacturing / Quality Check / Ready
    for Pickup already have values (out-of-order staff entry is never
    surfaced early). Order Received doesn't unlock later stages by
    itself — it only flips the Order Placed checkpoint's own tick to
    done (with its date) while still pinned on that stage.
    """
    # 1. Cancelled always wins.
    if m.get("cancelled_note"):
        return _cancelled_block(m)

    order_confirmed = m.get("order_confirmed")
    order_received = m.get("order_received")
    under_manufacturing = m.get("under_manufacturing")
    quality_check = m.get("quality_check")
    ready_for_pickup = m.get("ready_for_pickup")

    # The DATE shown on the Order Placed checkpoint always prefers
    # Order Received, falling back to Order Confirmed's own date.
    order_received_date = _friendly_date(order_received) or _friendly_date(order_confirmed)
    expected_date = _friendly_date(m.get("expected_date"))
    # Pending line shown on every stage before Ready for Pickup becomes
    # current — sourced from Expected Delivery Date.
    pending_ready_line = (f"⏳ Ready For Pickup - *{expected_date}* Expected"
                          if expected_date else "⏳ Ready For Pickup")

    # 2. Revised Delivery Date -> delayed (checked before stage logic).
    if m.get("revised_delivery_date"):
        intro = _INTRO["delayed"]
        checkpoints = _delayed_block(m, order_received_date)

    # 3. Order Confirmed blank -> pinned on "Order Placed", regardless
    # of what Under Manufacturing / Quality Check / Ready for Pickup
    # contain. Its own tick still reflects Order Received if present.
    elif not order_confirmed:
        intro = _INTRO["order_confirmed"]
        order_placed_line = (f"✅ Order Placed - *{order_received_date}*"
                             if order_received else "⏳ Order Placed")
        checkpoints = "\n |\n".join([
            order_placed_line,
            "⏳ Manufacturing",
            "⏳ Quality Check",
            pending_ready_line,
        ])

    elif not under_manufacturing:
        intro = _INTRO["under_manufacturing"]
        checkpoints = "\n |\n".join([
            f"✅ Order Placed - *{order_received_date}*",
            "🔄 Under Manufacturing",
            "⏳ Quality Check",
            pending_ready_line,
        ])

    elif not quality_check:
        intro = _INTRO["quality_check"]
        checkpoints = "\n |\n".join([
            f"✅ Order Placed - *{order_received_date}*",
            "✅ Manufactured",
            "🔄 Under Quality Check",
            pending_ready_line,
        ])

    elif not ready_for_pickup:
        # Per spec this is an "it's basically ready" announcement sent
        # BEFORE staff formally stamp the Ready for Pickup cell — that's
        # why the condition is "blank" yet the checkpoint still renders
        # as ✅ done. Uses the Quality Check column's own completion
        # date (the last real, confirmed milestone) rather than the
        # Expected Delivery Date estimate, per the Aug 2026 spec revision.
        intro = _INTRO["ready_for_pickup"]
        checkpoints = "\n |\n".join([
            f"✅ Order Placed - *{order_received_date}*",
            "✅ Manufactured",
            "✅ Quality Checked",
            f"✅ Ready For Pickup - *{_friendly_date(quality_check)}*",
        ])

    else:
        # All four stage fields filled (Ready for Pickup itself now has
        # its own real date), no cancellation/delay — no message is
        # currently defined for this case. Per spec, returned verbatim,
        # with no Order/Item wrapper.
        return _NO_STATUS_MESSAGE

    lines = [intro, ""]
    lines.append(f"Order: {m['order_id']}" if m.get("order_id") else "Order: -")
    if m.get("item"):
        lines.append(f"Item: {m['item']}")
    lines.append("")
    lines.append(checkpoints)
    return "\n".join(lines)


def format_status_reply(match: dict) -> str:
    """Turn a lookup_order_status() result into a customer-facing WhatsApp
    message. A single order is just its block; multiple orders for the
    same mobile number are shown together in one reply."""
    if "multiple" in match:
        blocks = [_order_block(m) for m in match["multiple"]]
        parts = ["✨ *Here's an update on your orders!*", "",
                 "\n\n———\n\n".join(blocks)]
        return "\n".join(parts)
    return _order_block(match)