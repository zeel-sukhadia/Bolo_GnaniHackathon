"""Remote Evon client error handling, health check, and the labelled mock driving a real task."""
import uuid

import httpx
import pytest

from app import config, engine, evon
from app.gnani import ProviderError
from conftest import make_task, plan
from test_adapters_api import Resp


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(config, "EVON_BASE_URL", "https://evon.example.com/v1")
    monkeypatch.setattr(config, "EVON_API_KEY", "k")
    monkeypatch.setattr(evon.time, "sleep", lambda s: None)


@pytest.mark.parametrize("status,match,attempts", [
    (401, "rejected the API key", 1), (404, "must end in /v1", 1),
    (530, "GPU session ended", 3), (429, "rate limited", 3)])
def test_remote_errors_are_explained_and_bounded(remote, monkeypatch, status, match, attempts):
    calls, resp = [], Resp(status, {})
    resp.headers = {"retry-after": "1"}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(k) or resp)
    with pytest.raises(ProviderError, match=match):
        evon.complete([{"role": "user", "content": "x"}])
    assert len(calls) == attempts and calls[0]["headers"] == {"Authorization": "Bearer k"}


def test_tunnel_gone_is_unavailable_not_silent(remote, monkeypatch):
    def post(*a, **k):
        raise httpx.ConnectError("dns")
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ProviderError, match="tunnel URL changed"):
        evon.complete([{"role": "user", "content": "x"}])


def test_malformed_response(remote, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp(200, {"choices": []}))
    with pytest.raises(ProviderError, match="malformed"):
        evon.complete([{"role": "user", "content": "x"}])


def test_health_checks_served_model(remote, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp(200, {"data": [{"id": "other"}]}))
    h = evon.health()
    assert h["reachable"] and not h["model_available"] and "other" in h["detail"]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp(200, {"data": [{"id": config.EVON_MODEL}]}))
    assert evon.health()["model_available"]


def test_config_warnings():
    assert any("this machine" in w for w in evon.EvonClient("http://localhost:8000/v1", "model").warnings())
    remote = evon.EvonClient("http://x.trycloudflare.com/v1", "model").warnings()
    assert any("HTTPS" in w for w in remote) and any("--api-key" in w for w in remote)
    assert evon.EvonClient("https://x.trycloudflare.com/v1", "model", "k").warnings() == []


def test_mock_drives_vague_then_estimate_then_verified(db, user, monkeypatch):
    monkeypatch.setattr(config, "EVON_MODE", "mock")
    task = make_task(db, user, recipient_language="ta-IN")  # the mock can't infer language, so we give it
    plan(db, task, user)
    assert task.state == "CALLING" and task.goal.startswith(evon.MOCK_TAG)

    def say(text):
        engine.recipient_says(db, task, user, engine.active_call(task).id, text, uuid.uuid4().hex)
        db.commit()
    say("yes go ahead")
    say("the bus is nearby")
    assert task.state == "CLARIFYING"
    say("about 15 minutes")
    assert task.state == "VERIFYING" and task.required_info[0]["status"] == "estimate"
    say("yes correct")
    assert task.state == "COMPLETED" and task.result_summary.startswith(evon.MOCK_TAG)
    assert all(t.text.startswith(evon.MOCK_TAG) for t in task.turns if t.speaker == "agent")
