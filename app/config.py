"""Environment configuration. Copy .env.example -> .env (or set on host)."""

import os
from pathlib import Path

# Load .env if present (no dependency on python-dotenv)
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# Meta WhatsApp Cloud API
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "rkj-verify")
APP_SECRET = os.getenv("APP_SECRET", "")
GRAPH_URL = os.getenv("GRAPH_URL", "https://graph.facebook.com/v20.0")

# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))

# Escalation: Cloud API cannot message WhatsApp groups, so escalations
# go to each team member's number individually (comma-separated).
TEAM_NOTIFY_NUMBERS = [n.strip() for n in
                       os.getenv("TEAM_NOTIFY_NUMBERS", "").split(",") if n.strip()]

# Admin token for POST /admin/rates
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Daily leads email backup (leave SMTP_* empty to disable)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
LEADS_EMAIL_TO = [e.strip() for e in os.getenv("LEADS_EMAIL_TO", "").split(",") if e.strip()]
LEADS_REPORT_HOUR = int(os.getenv("LEADS_REPORT_HOUR", "21"))  # IST, 24h clock

PERSONA_FILE = Path(__file__).resolve().parent.parent / "persona" / "rkj-whatsapp-persona.md"
PERSONA_VERSION = os.getenv("PERSONA_VERSION", "v1.0")

# Design-protection: hard cap on product images per customer. Beyond this,
# the bot stops sending pieces and hands over to a human — protects the
# catalogue from rival jewellers harvesting designs.
MAX_CARDS_PER_CUSTOMER = int(os.getenv("MAX_CARDS_PER_CUSTOMER", "20"))

# Bumped with every code change; shown at /health to verify what's deployed
BUILD = "2026-08-19.5-crm-flow-v1.4"
