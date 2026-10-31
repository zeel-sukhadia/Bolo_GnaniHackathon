"""Claude stand-in: request shape, refusal / truncation / invalid-output handling, error mapping. No network."""
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app import claude_llm, config, evon
from app.engine import Analysis, Plan
from app.gnani import ProviderError


class FakeMessages:
    def __init__(self, reply=None, stop="end_turn", exc=None):
        self.reply, self.stop, self.exc, self.calls = reply, stop, exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(stop_reason=self.stop, _request_id="req_test",
                               content=[SimpleNamespace(type="text", text=self.reply)])


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(config, "EVON_MODE", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    m = FakeMessages('{"goal": "Ask the bus ETA", "goal_for_requester": "ETA", '
                     '"required_information": [{"key": "eta", "description": "minutes until arrival"}]}')
    monkeypatch.setattr(claude_llm, "_client", SimpleNamespace(beta=SimpleNamespace(messages=m)))
    return m


def test_routes_through_evon_ask_json_with_schema_effort_and_fallback(fake):
    p = evon.ask_json("plan it", "{}", Plan)
    assert p.required_information[0].key == "eta"
    kw = fake.calls[0]
    assert kw["model"] == config.CLAUDE_MODEL and kw["system"] == "plan it"
    assert kw["output_config"]["effort"] == "medium" and kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]


def test_judging_uses_low_effort(fake):
    fake.reply = '{"updates": []}'
    evon.ask_json("judge", "{}", Analysis)
    assert fake.calls[0]["output_config"]["effort"] == "low"


@pytest.mark.parametrize("stop,reply,match", [("refusal", "", "declined"), ("max_tokens", '{"goal"', "cut off"),
                                              ("end_turn", '{"goal": 1}', "invalid structured output")])
def test_never_returns_a_guess(fake, stop, reply, match):
    fake.stop, fake.reply = stop, reply
    with pytest.raises(ProviderError, match=match):
        evon.ask_json("s", "{}", Plan)


def _status_error(cls, status):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("x", response=httpx2.Response(status, request=req), body=None)


@pytest.mark.parametrize("exc,match,retryable", [
    (lambda: _status_error(anthropic.AuthenticationError, 401), "key was rejected", False),
    (lambda: _status_error(anthropic.RateLimitError, 429), "rate limited", True),
    (lambda: _status_error(anthropic.InternalServerError, 500), "HTTP 500", True),
    (lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "cannot reach", True)])
def test_api_errors_are_explained(fake, exc, match, retryable):
    fake.exc = exc()
    with pytest.raises(ProviderError, match=match) as e:
        evon.ask_json("s", "{}", Plan)
    assert e.value.retryable is retryable


def test_missing_key_is_explicit(monkeypatch):
    monkeypatch.setattr(config, "EVON_MODE", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        evon.ask_json("s", "{}", Plan)
    assert evon.health()["detail"].startswith("ANTHROPIC_API_KEY")
