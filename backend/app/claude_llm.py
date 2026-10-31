"""Claude (Anthropic API) as a stand-in for Evon v3.3 while no Evon GPU server is available.

Enabled with EVON_MODE=anthropic. This is NOT Evon: /api/meta reports the mode and the UI labels it.
Structured outputs constrain every reply to the engine's Pydantic schema; app.engine still validates it.
"""
import logging

import anthropic
from pydantic import BaseModel, ValidationError

from . import config
from .gnani import ProviderError

log = logging.getLogger("claude")
# Planning sets up the whole task, so it gets more thought; judging, wording and summaries stay quick.
EFFORT = {"Plan": "medium"}
_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if not config.ANTHROPIC_API_KEY:
        raise ProviderError("claude", "ANTHROPIC_API_KEY is not configured on the server")
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=config.CLAUDE_TIMEOUT, max_retries=2)
    return _client


def _error(e: anthropic.APIError) -> ProviderError:
    # Most specific first: APITimeoutError subclasses APIConnectionError.
    if isinstance(e, anthropic.AuthenticationError):
        return ProviderError("claude", "the Anthropic API key was rejected (check ANTHROPIC_API_KEY)", False, 401)
    if isinstance(e, anthropic.PermissionDeniedError):
        return ProviderError("claude", "the Anthropic API key lacks permission for this model", False, 403)
    if isinstance(e, anthropic.NotFoundError):
        return ProviderError("claude", f"model '{config.CLAUDE_MODEL}' not found", False, 404)
    if isinstance(e, anthropic.RateLimitError):
        return ProviderError("claude", "rate limited by the Anthropic API", True, 429)
    if isinstance(e, anthropic.BadRequestError):
        return ProviderError("claude", f"request rejected: {e.message}", False, 400)
    if isinstance(e, anthropic.APIStatusError):
        return ProviderError("claude", f"Anthropic API error (HTTP {e.status_code})", e.status_code >= 500, e.status_code)
    if isinstance(e, anthropic.APITimeoutError):
        return ProviderError("claude", "request timed out", True)
    return ProviderError("claude", "cannot reach the Anthropic API (network error)", True)


def ask_json[T: BaseModel](system: str, user: str, schema: type[T]) -> T:
    """One structured request. The SDK already retries 429/5xx/network errors (max_retries=2)."""
    try:
        r = client().beta.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": EFFORT.get(schema.__name__, "low"),
                           "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
            # On a safety-classifier decline, the API re-runs the request on Anthropic's recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        err = _error(e)
        log.warning("claude %s failed: %s", schema.__name__, err.message)
        raise err
    if r.stop_reason == "refusal":
        raise ProviderError("claude", "the model declined this request")
    if r.stop_reason == "max_tokens":
        raise ProviderError("claude", "the reply was cut off before it finished", retryable=True)
    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        log.warning("claude returned invalid %s (request %s): %s", schema.__name__, r._request_id, str(e)[:200])
        raise ProviderError("claude", "model returned invalid structured output", retryable=True)


def health() -> dict:
    """Checks the key and model with GET /v1/models/{id}, which costs no tokens."""
    out = {"mode": "anthropic", "configured": bool(config.ANTHROPIC_API_KEY), "model": config.CLAUDE_MODEL,
           "reachable": False, "model_available": False, "warnings": [], "detail": None}
    try:
        client().models.retrieve(config.CLAUDE_MODEL)
    except ProviderError as e:
        out["detail"] = e.message
        return out
    except anthropic.APIError as e:
        out["detail"] = _error(e).message
        return out
    out["reachable"] = out["model_available"] = True
    return out
