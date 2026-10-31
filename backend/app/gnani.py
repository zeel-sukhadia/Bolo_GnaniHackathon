"""Gnani hosted speech APIs. Verified against https://docs.gnani.ai (Oct 2026):
- Prisma v2.5 STT: POST {base}/stt/v3, multipart, header X-API-Key-ID; omit `model` => v2.5. Max 30 s audio.
- Timbre v2.5 TTS: POST {base}/api/v1/tts/inference, JSON, model "timbre-v2.5"; returns raw audio bytes.
"""
import time

import httpx

from . import config

# Languages supported by Prisma v2.5 and Timbre v2.5 alike.
LANGUAGES = {
    "en-IN": "English", "hi-IN": "हिन्दी (Hindi)", "gu-IN": "ગુજરાતી (Gujarati)", "ta-IN": "தமிழ் (Tamil)",
    "te-IN": "తెలుగు (Telugu)", "kn-IN": "ಕನ್ನಡ (Kannada)", "ml-IN": "മലയാളം (Malayalam)",
    "mr-IN": "मराठी (Marathi)", "bn-IN": "বাংলা (Bengali)", "pa-IN": "ਪੰਜਾਬੀ (Punjabi)",
}
# One Timbre v2.5 voice per language (catalog: docs.gnani.ai/api/TTS/available-voices).
VOICES = {
    "en-IN": "Kaveri", "hi-IN": "Nalini", "gu-IN": "Falak", "ta-IN": "Asmita", "te-IN": "Lavanya",
    "kn-IN": "Saanvi", "ml-IN": "Reshma", "mr-IN": "Zahira", "bn-IN": "Kirra", "pa-IN": "Mehuli",
}
# Second voice so the simulated contact in the demo doesn't sound like the agent (same catalog, v2.5).
CONTACT_VOICES = {"ta-IN": "Brinda", "gu-IN": "Veera"}
# Gender of each voice above, from the same catalog. Text is written to match it (e.g. Hindi female
# "मैं बोल रही हूँ", not "बोल रहा"), so the words agree with the voice speaking them.
VOICE_GENDER = {
    "Kaveri": "female", "Nalini": "female", "Falak": "female", "Asmita": "female", "Lavanya": "female",
    "Saanvi": "female", "Reshma": "female", "Zahira": "female", "Kirra": "female", "Mehuli": "female",
    "Brinda": "female", "Veera": "male",
}


def gender_rule(language: str | None, voice: str | None = None) -> str:
    """Prompt line making self-references grammatically match the voice that will speak the text."""
    g = VOICE_GENDER.get(voice or VOICES.get(language or "", ""))
    if not g:
        return ""
    return (f"\nThis text is spoken in a {g} voice. Whenever you refer to yourself, use {g} grammatical forms "
            "(verb endings, adjectives, participles) in languages that mark gender, e.g. Hindi/Marathi/Gujarati/"
            "Punjabi/Bengali. Never use the other gender's forms for yourself.")


RETRYABLE ={429, 500, 502, 503, 504}


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider, self.message, self.retryable, self.status = provider, message, retryable, status


def _headers() -> dict:
    if not config.GNANI_API_KEY:
        raise ProviderError("gnani", "GNANI_API_KEY is not configured on the server")
    return {"X-API-Key-ID": config.GNANI_API_KEY}


def _error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    err = body.get("error") or body.get("detail") or body
    if isinstance(err, dict):
        return f"HTTP {r.status_code} {err.get('type') or err.get('error_code') or ''} {err.get('message', '')}".strip()
    return f"HTTP {r.status_code} {body.get('message', '')}".strip()


def _post(provider: str, path: str, *, timeout: float, attempts: int = 3, **kw) -> httpx.Response:
    headers = {**_headers(), **kw.pop("headers", {})}
    for i in range(attempts):
        try:
            r = httpx.post(f"{config.GNANI_BASE_URL}{path}", headers=headers, timeout=timeout, **kw)
        except httpx.TimeoutException:
            if i == attempts - 1:
                raise ProviderError(provider, "request timed out", retryable=True)
        except httpx.HTTPError as e:
            if i == attempts - 1:
                raise ProviderError(provider, f"network error: {type(e).__name__}", retryable=True)
        else:
            if r.status_code == 200:
                return r
            if r.status_code not in RETRYABLE or i == attempts - 1:
                raise ProviderError(provider, _error_message(r), r.status_code in RETRYABLE, r.status_code)
        time.sleep(0.5 * 2 ** i)
    raise AssertionError("unreachable")


def transcribe(audio: bytes, filename: str, language: str) -> str:
    """Prisma v2.5 speech-to-text. Returns transcript ('' if nothing was heard)."""
    if language not in LANGUAGES:
        raise ProviderError("prisma", f"unsupported language {language}")
    r = _post("prisma", "/stt/v3", timeout=45,
              files={"audio_file": (filename, audio)}, data={"language_code": language})
    body = r.json()
    if not body.get("success", True) or "transcript" not in body:
        raise ProviderError("prisma", "malformed response")
    return body["transcript"].strip()


def synthesize(text: str, language: str, voice: str | None = None) -> bytes:
    """Timbre v2.5 text-to-speech. Returns MP3 bytes."""
    if language not in VOICES:
        raise ProviderError("timbre", f"unsupported language {language}")
    r = _post("timbre", "/api/v1/tts/inference", timeout=60, json={
        "text": text, "model": "timbre-v2.5", "voice": voice or VOICES[language], "language": language,
        "audio_config": {"sample_rate": 24000, "num_channels": 1, "container": "mp3", "bitrate": "64k"},
    })
    if not r.content:
        raise ProviderError("timbre", "empty audio")
    return r.content
