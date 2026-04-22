"""Environment configuration for PulseCall."""
import os
from dotenv import load_dotenv

load_dotenv()

# SignalWire
SIGNALWIRE_PROJECT_ID = os.getenv("SIGNALWIRE_PROJECT_ID", "")
SIGNALWIRE_API_TOKEN = os.getenv("SIGNALWIRE_API_TOKEN", "")
SIGNALWIRE_SPACE = os.getenv("SIGNALWIRE_SPACE", "")
SIGNALWIRE_PHONE_NUMBER = os.getenv("SIGNALWIRE_PHONE_NUMBER", "")
DISPLAY_PHONE_NUMBER = os.getenv("DISPLAY_PHONE_NUMBER", SIGNALWIRE_PHONE_NUMBER)

# SWML callback auth
SWML_BASIC_AUTH_USER = os.getenv("SWML_BASIC_AUTH_USER", "")
SWML_BASIC_AUTH_PASSWORD = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")
SWML_PROXY_URL_BASE = os.getenv("SWML_PROXY_URL_BASE", "")

# AI
AI_MODEL = os.getenv("AI_MODEL", "gpt-oss-120b")
AI_TOP_P = float(os.getenv("AI_TOP_P", "0.5"))
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.3"))

# Dialer
MAX_OUTBOUND_CONCURRENT = int(os.getenv("MAX_OUTBOUND_CONCURRENT", "2"))
OUTBOUND_SCHEDULE = os.getenv("OUTBOUND_SCHEDULE", "").strip()

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "pulsecall.db")
