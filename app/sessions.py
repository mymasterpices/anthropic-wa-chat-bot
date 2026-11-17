"""Per-phone-number conversation state + escalation/conversation logging.

SQLite-backed (data/sessions.db). Swap for Redis later if needed —
callers only use get_history / append / log_escalation / seen_message.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sessions.db"
MAX_TURNS = 30  # messages of history sent to Claude

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    phone TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    persona_version TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages(phone, ts);
CREATE TABLE IF NOT EXISTS escalations (
    phone TEXT NOT NULL,
    reason TEXT,
    summary TEXT,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (
    message_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_messages (
    message_id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'card' or 'text'
    ref TEXT NOT NULL,           -- SKU for cards, text snippet for texts
    ts REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def get_history(phone: str, max_turns: int = MAX_TURNS) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE phone=? "
            "ORDER BY ts DESC LIMIT ?", (phone, max_turns)).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def append(phone: str, role: str, content: str, persona_version: str = "") -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (phone, role, content, persona_version, ts) "
            "VALUES (?,?,?,?,?)", (phone, role, content, persona_version, time.time()))


def log_escalation(phone: str, reason: str, summary: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO escalations (phone, reason, summary, ts) "
                     "VALUES (?,?,?,?)", (phone, reason, summary, time.time()))


def record_sent(message_id: str, phone: str, kind: str, ref: str) -> None:
    """Remember an outbound message so quoted replies can be resolved."""
    if not message_id:
        return
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO sent_messages "
                     "(message_id, phone, kind, ref, ts) VALUES (?,?,?,?,?)",
                     (message_id, phone, kind, ref, time.time()))


def lookup_sent(message_id: str) -> tuple[str, str] | None:
    """Return (kind, ref) for a previously sent message id, if known."""
    if not message_id:
        return None
    with _conn() as conn:
        row = conn.execute("SELECT kind, ref FROM sent_messages WHERE message_id=?",
                           (message_id,)).fetchone()
    return (row[0], row[1]) if row else None


def leads_summary() -> list[dict]:
    """One row per customer: contact window, pieces shown, escalations, status.
    Used by export_leads.py and GET /admin/leads."""
    import re
    from datetime import datetime

    shown_re = re.compile(r"\[showed pieces: ([^\]]+)\]")
    with _conn() as conn:
        msgs = conn.execute(
            "SELECT phone, role, content, ts FROM messages ORDER BY ts").fetchall()
        escs = conn.execute(
            "SELECT phone, reason, summary, ts FROM escalations ORDER BY ts").fetchall()

    leads: dict[str, dict] = {}
    for phone, role, content, ts in msgs:
        d = leads.setdefault(phone, {
            "phone": phone, "first_ts": ts, "last_ts": ts, "messages": 0,
            "pieces_shown": [], "last_customer_message": "",
            "escalations": 0, "escalation_reasons": [], "latest_escalation": ""})
        d["first_ts"] = min(d["first_ts"], ts)
        d["last_ts"] = max(d["last_ts"], ts)
        d["messages"] += 1
        if role == "user":
            d["last_customer_message"] = content[:120]
        else:
            for m in shown_re.findall(content):
                for sku in m.split(","):
                    sku = sku.strip()
                    if sku and sku not in d["pieces_shown"]:
                        d["pieces_shown"].append(sku)
    for phone, reason, summary, ts in escs:
        if phone in leads:
            d = leads[phone]
            d["escalations"] += 1
            if reason and reason not in d["escalation_reasons"]:
                d["escalation_reasons"].append(reason)
            d["latest_escalation"] = (summary or "")[:160]

    rows = []
    for d in sorted(leads.values(), key=lambda x: -x["last_ts"]):
        if d["escalations"]:
            status = "Hot — asked for the team"
        elif d["pieces_shown"]:
            status = "Warm — pieces shown"
        else:
            status = "New — enquiry only"
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%d %b %Y %H:%M")
        rows.append({
            "Phone": d["phone"],
            "Status": status,
            "First contact": fmt(d["first_ts"]),
            "Last contact": fmt(d["last_ts"]),
            "Messages": d["messages"],
            "Pieces shown": ", ".join(d["pieces_shown"]),
            "Escalations": d["escalations"],
            "Escalation reasons": ", ".join(d["escalation_reasons"]),
            "Latest escalation note": d["latest_escalation"],
            "Last customer message": d["last_customer_message"],
        })
    return rows


def shown_skus(phone: str) -> list[str]:
    """All SKUs already presented to this customer as product cards."""
    with _conn() as conn:
        rows = conn.execute("SELECT ref FROM sent_messages WHERE phone=? "
                            "AND kind='card' ORDER BY ts", (phone,)).fetchall()
    out: list[str] = []
    for (ref,) in rows:
        if ref not in out:
            out.append(ref)
    return out


def list_conversations() -> list[dict]:
    """All customers with their latest message — for the live inbox."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT m.phone,
                   COUNT(*) AS msgs,
                   MAX(m.ts) AS last_ts,
                   (SELECT content FROM messages x WHERE x.phone=m.phone
                     ORDER BY x.ts DESC LIMIT 1) AS last_msg,
                   (SELECT role FROM messages x WHERE x.phone=m.phone
                     ORDER BY x.ts DESC LIMIT 1) AS last_role,
                   (SELECT COUNT(*) FROM escalations e WHERE e.phone=m.phone) AS escalations
            FROM messages m GROUP BY m.phone ORDER BY last_ts DESC""").fetchall()
    import re
    clean = re.compile(r"\s*\[showed pieces:[^\]]*\]")
    return [{"phone": r[0], "messages": r[1], "last_ts": r[2],
             "last_msg": clean.sub("", r[3] or "")[:90],
             "last_role": r[4], "escalations": r[5]} for r in rows]


def conversation(phone: str, limit: int = 200) -> list[dict]:
    """Full transcript for one customer, oldest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content, ts, persona_version FROM messages "
            "WHERE phone=? ORDER BY ts DESC LIMIT ?", (phone, limit)).fetchall()
        escs = conn.execute(
            "SELECT reason, summary, ts FROM escalations WHERE phone=? ORDER BY ts",
            (phone,)).fetchall()
    import re
    shown_re = re.compile(r"\[showed pieces: ([^\]]+)\]")
    out = []
    for role, content, ts, ver in reversed(rows):
        skus = shown_re.findall(content or "")
        out.append({"role": role, "ts": ts, "persona_version": ver,
                    "text": shown_re.sub("", content or "").strip(),
                    "cards": skus[0].split(", ") if skus else []})
    for reason, summary, ts in escs:
        out.append({"role": "escalation", "ts": ts,
                    "text": f"{reason}: {summary}", "cards": []})
    out.sort(key=lambda m: m["ts"])
    return out


def has_escalation(phone: str, reason: str) -> bool:
    """True if this customer was already escalated for this reason."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM escalations WHERE phone=? AND reason=? "
                           "LIMIT 1", (phone, reason)).fetchone()
    return row is not None


def seen_message(message_id: str) -> bool:
    """True if this webhook message id was already processed (Meta retries)."""
    with _conn() as conn:
        try:
            conn.execute("INSERT INTO processed (message_id, ts) VALUES (?,?)",
                         (message_id, time.time()))
            return False
        except sqlite3.IntegrityError:
            return True
