"""Settings loaded from environment / .env. Secrets live only here, server-side."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATABASE_URL = _env("DATABASE_URL", "sqlite:///./agent.db")
SESSION_DAYS = int(_env("SESSION_DAYS", "14"))
COOKIE_SECURE = _env("COOKIE_SECURE", "false").lower() == "true"
RETENTION_DAYS = int(_env("RETENTION_DAYS", "30"))
MAX_CALL_ATTEMPTS = int(_env("MAX_CALL_ATTEMPTS", "3"))
REDIAL_BACKOFF_SECONDS = [int(s) for s in _env("REDIAL_BACKOFF_SECONDS", "120,600").split(",")]
MAX_CLARIFICATIONS_PER_FACT = 2

# Gnani hosted speech APIs (Prisma v2.5 STT, Timbre v2.5 TTS) — https://docs.gnani.ai
GNANI_API_KEY = _env("GNANI_API_KEY")
GNANI_BASE_URL = _env("GNANI_BASE_URL", "https://api.vachana.ai")

# Evon v3.3 is open weights; point this at any OpenAI-compatible server hosting it
# (vLLM / SGLang / llama.cpp), e.g. a Colab GPU behind an HTTPS tunnel. See deploy/EVON_REMOTE_GPU.md.
# EVON_MODE: remote (an Evon server) | anthropic (Claude stand-in, below) | mock (labelled simulated output).
EVON_MODE = _env("EVON_MODE", "remote").lower()
EVON_BASE_URL = _env("EVON_BASE_URL")  # e.g. https://<name>.trycloudflare.com/v1
EVON_MODEL = _env("EVON_MODEL_NAME") or _env("EVON_MODEL", "model")
EVON_API_KEY = _env("EVON_API_KEY")
EVON_DISABLE_THINKING = _env("EVON_DISABLE_THINKING", "true").lower() == "true"
EVON_TIMEOUT = float(_env("EVON_TIMEOUT", "90"))

# EVON_MODE=anthropic: Claude stands in for Evon until an Evon GPU server is available (labelled in the UI).
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_TIMEOUT = float(_env("CLAUDE_TIMEOUT", "60"))

# Web Push (VAPID). Generate with: python -m app.worker --gen-vapid
VAPID_PUBLIC_KEY = _env("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = _env("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = _env("VAPID_SUBJECT", "mailto:admin@example.com")

TELEPHONY_MODE = "simulated"  # only mode implemented; see README "Real telephony"
