"""Evon v3.3 (open weights, gnani/gnani-evon-v3.3-30B-A3B) via an OpenAI-compatible server.

Gnani does not host Evon. Serve it yourself on a GPU (vLLM / SGLang / llama.cpp, see
deploy/EVON_REMOTE_GPU.md) and set EVON_BASE_URL, or set EVON_MODE=mock for offline development.
The model only proposes; app.engine validates every output.

Check the configured server from the command line:  python -m app.evon
"""
import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from . import claude_llm, config
from .gnani import RETRYABLE, ProviderError

log = logging.getLogger("evon")
MOCK_TAG = "[Mock Evon: simulated, not a real model]"
# 530/524: Cloudflare tunnel is up but the origin (the model server) is down or too slow.
_UNAVAILABLE = RETRYABLE | {524, 530}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class EvonClient:
    """OpenAI-compatible chat client. Swapping the inference provider means pointing base_url elsewhere."""

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 90.0,
                 disable_thinking: bool = True, attempts: int = 3):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.timeout, self.disable_thinking, self.attempts = timeout, disable_thinking, attempts
        self.host = urlparse(self.base_url).hostname or "?"

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def warnings(self) -> list[str]:
        """Configuration mistakes that are legal but probably not intended."""
        out = []
        if self.host in _LOCAL_HOSTS:
            out.append("EVON_BASE_URL points at this machine. That's fine for a local server, but a "
                       "Colab/remote GPU needs its public HTTPS tunnel URL.")
        else:
            if self.base_url.startswith("http://"):
                out.append("EVON_BASE_URL uses plain http to a remote host; use the HTTPS tunnel URL.")
            if not self.api_key:
                out.append("No EVON_API_KEY for a remote server; start the server with --api-key.")
        if not self.base_url.endswith("/v1"):
            out.append("EVON_BASE_URL usually ends in /v1.")
        return out

    def _error(self, r: httpx.Response) -> ProviderError:
        s = r.status_code
        if s in (401, 403):
            return ProviderError("evon", "the Evon server rejected the API key (check EVON_API_KEY)", False, s)
        if s == 404:
            return ProviderError("evon", "endpoint or model not found: EVON_BASE_URL must end in /v1 and "
                                         "EVON_MODEL_NAME must match --served-model-name", False, s)
        if s == 429:
            return ProviderError("evon", "rate limited by the Evon server", True, s)
        if s in _UNAVAILABLE:
            return ProviderError("evon", f"Evon server unavailable (HTTP {s}): it may be loading, restarting, "
                                         "or the GPU session ended", True, s)
        return ProviderError("evon", f"HTTP {s}", False, s)

    def _unreachable(self, e: Exception) -> ProviderError:
        return ProviderError("evon", f"cannot reach the Evon server at {self.host} ({type(e).__name__}). "
                                     "The GPU session may have stopped or the tunnel URL changed.", True)

    def complete(self, messages: list[dict], max_tokens: int = 2048) -> str:
        if not self.base_url:
            raise ProviderError("evon", "EVON_BASE_URL is not configured on the server (or set EVON_MODE=mock)")
        body = {"model": self.model, "messages": messages, "temperature": 0.6, "top_p": 0.95,
                "max_tokens": max_tokens}
        if self.disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        for i in range(self.attempts):
            wait = 1.0 * 2 ** i
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers,
                               timeout=self.timeout)
            except httpx.TimeoutException:
                err = ProviderError("evon", "request timed out", retryable=True)
            except httpx.HTTPError as e:
                err = self._unreachable(e)
            else:
                if r.status_code == 200:
                    try:
                        return r.json()["choices"][0]["message"]["content"] or ""
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise ProviderError("evon", "malformed response from the Evon server")
                err = self._error(r)
                if r.status_code == 429:
                    try:
                        wait = min(float(r.headers.get("retry-after", wait)), 10.0)
                    except ValueError:
                        pass
            log.warning("evon attempt %d/%d failed: %s", i + 1, self.attempts, err.message)
            if not err.retryable or i == self.attempts - 1:
                raise err
            time.sleep(wait)
        raise AssertionError("unreachable")

    def health(self) -> dict:
        """One cheap GET /models: is the server up, is the key accepted, is our model served?"""
        out = {"mode": "remote", "configured": bool(self.base_url), "host": self.host if self.base_url else None,
               "model": self.model, "reachable": False, "model_available": False, "warnings": self.warnings(),
               "detail": None}
        if not self.base_url:
            out["detail"] = "EVON_BASE_URL is not set"
            return out
        t = time.monotonic()
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
        except httpx.HTTPError as e:
            out["detail"] = self._unreachable(e).message
            return out
        out["latency_ms"] = round((time.monotonic() - t) * 1000)
        if r.status_code != 200:
            out["detail"] = self._error(r).message
            return out
        out["reachable"] = True
        try:
            served = [m["id"] for m in r.json()["data"]]
        except (ValueError, KeyError, TypeError):
            out["detail"] = "malformed /models response"
            return out
        out["model_available"] = self.model in served
        out["detail"] = None if out["model_available"] else f"server serves {served}, not '{self.model}'"
        return out


class MockEvon:
    """Deterministic stand-in so the app runs with no GPU. Every output is tagged as simulated.

    Heuristics only: a reply with a number counts as a specific answer, words like "nearby" or "soon"
    count as vague, so the clarify / estimate / verify paths can all be exercised.
    """
    VAGUE = ("nearby", "soon", "later", "sometime", "not sure", "maybe", "few", "a while", "shortly")
    APPROX = ("about", "around", "approx", "roughly", "nearly", "almost", "~")
    REFUSE = ("don't know", "dont know", "no idea", "can't say", "cannot say", "won't tell", "not allowed")

    def ask_json[T: BaseModel](self, system: str, user: str, schema: type[T]) -> T:
        p = json.loads(user)
        return schema.model_validate(getattr(self, "_" + schema.__name__.lower())(p))

    def _plan(self, p: dict) -> dict:
        req = p["request"]
        phone = re.search(r"\+?\d[\d\s-]{7,}\d", req)
        items = [{"key": i["key"], "description": i["description"]} for i in p.get("existing_items_reuse_their_keys") or []]
        return {"goal": f"{MOCK_TAG} {req}"[:500], "goal_for_requester": f"{MOCK_TAG} {req}"[:600],
                "required_information": items or [{"key": "answer", "description": f"a specific answer to: {req}"[:300]}],
                "context_for_call": req[:500], "contact_phone": phone.group(0) if phone else None}

    def _utterance(self, p: dict) -> dict:
        return {"text": f"{MOCK_TAG} {p['instruction']}"[:800]}

    def _summary(self, p: dict) -> dict:
        facts = "; ".join(f"{f['description']}: {f.get('value') or '-'} ({f.get('status') or 'not asked'})"
                          for f in p["facts"])
        return {"summary": f"{MOCK_TAG} Simulated call. {facts}"[:2000]}

    def _analysis(self, p: dict) -> dict:
        lines = p["conversation"].splitlines()
        reply_i = max((i for i, l in enumerate(lines) if l.startswith("RECIPIENT:")), default=None)
        if reply_i is None:
            return {}
        reply = lines[reply_i].split(":", 1)[1].strip()
        low = reply.lower()
        asked = next((l for l in reversed(lines[:reply_i]) if l.startswith("AGENT:")), "")
        signals = {"stop_requested": "stop" in low or "bye" in low,
                   "do_not_call": "call" in low and any(w in low for w in ("don't", "do not", "never"))}
        if p.get("verifying"):
            signals["readback_confirmed"] = not (low.startswith("no") or "wrong" in low or "incorrect" in low)
            return {"signals": signals}
        open_items = [i for i in p["required_items"] if i.get("status") not in ("confirmed", "estimate", "refused", "unanswered")]
        if not open_items or "convenient time" in asked or any(signals.values()):
            return {"signals": signals}  # replying to the opener (or stopping), not answering a fact
        if any(w in low for w in self.REFUSE):
            status, value = "refused", None
        elif re.search(r"\d", low):
            status, value = ("estimate" if any(w in low for w in self.APPROX) else "confirmed"), reply
        elif any(w in low for w in self.VAGUE) or len(low.split()) < 3:
            status, value = "uncertain", None
        else:
            status, value = "confirmed", reply
        return {"signals": signals, "updates": [{"key": open_items[0]["key"], "status": status, "value": value,
                                                 "evidence": reply[:200]}]}


def client() -> EvonClient:
    return EvonClient(config.EVON_BASE_URL, config.EVON_MODEL, config.EVON_API_KEY, config.EVON_TIMEOUT,
                      config.EVON_DISABLE_THINKING)


def complete(messages: list[dict]) -> str:
    return client().complete(messages)


def health() -> dict:
    if config.EVON_MODE == "anthropic":
        return claude_llm.health()
    if config.EVON_MODE == "mock":
        return {"mode": "mock", "configured": True, "reachable": True, "model_available": True, "warnings": [],
                "detail": "EVON_MODE=mock: responses are simulated, no model is called"}
    return client().health()


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def ask_json[T: BaseModel](system: str, user: str, schema: type[T]) -> T:
    """Ask for JSON matching `schema`; one repair round, then fail loudly (never guess)."""
    if config.EVON_MODE == "mock":
        return MockEvon().ask_json(system, user, schema)
    if config.EVON_MODE == "anthropic":
        return claude_llm.ask_json(system, user, schema)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    out = complete(messages)
    try:
        return schema.model_validate(_extract_json(out))
    except (ValueError, ValidationError) as e:
        log.warning("evon returned invalid %s, asking for a repair: %s", schema.__name__, str(e)[:200])
        messages += [{"role": "assistant", "content": out},
                     {"role": "user", "content": f"That was not valid JSON for the schema ({str(e)[:300]}). "
                                                 "Reply with only the corrected JSON object."}]
        try:
            return schema.model_validate(_extract_json(complete(messages)))
        except (ValueError, ValidationError):
            raise ProviderError("evon", "model returned invalid structured output", retryable=True)


if __name__ == "__main__":
    # End-to-end check of the configured Evon server: health, then one real structured request.
    logging.basicConfig(level=logging.INFO)
    h = health()
    print(json.dumps(h, indent=2))
    if not h["model_available"]:
        raise SystemExit("Evon is not available; fix the issue above.")
    if config.EVON_MODE == "mock":
        raise SystemExit("Mock mode: nothing remote to test.")  # anthropic mode falls through to a real request

    class Echo(BaseModel):
        text: str
    t = time.monotonic()
    r = ask_json('Reply with ONLY JSON {"text": "..."}.',
                 "In Tamil, ask politely how many minutes until the bus arrives.", Echo)
    print(f"OK in {time.monotonic() - t:.1f}s ({config.EVON_MODE}): {r.text}")
