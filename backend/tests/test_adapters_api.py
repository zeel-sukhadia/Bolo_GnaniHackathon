import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, evon, gnani
from app.engine import Plan


class Resp:
    def __init__(self, status, body=None, content=b""):
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


# ---- Gnani Prisma / Timbre

def test_missing_gnani_key_is_explicit():
    with pytest.raises(gnani.ProviderError, match="GNANI_API_KEY"):
        gnani.transcribe(b"x", "a.wav", "ta-IN")


def test_unsupported_language_rejected(monkeypatch):
    monkeypatch.setattr(config, "GNANI_API_KEY", "k")
    with pytest.raises(gnani.ProviderError, match="unsupported"):
        gnani.synthesize("hi", "or-IN")


def test_prisma_request_shape_and_parse(monkeypatch):
    monkeypatch.setattr(config, "GNANI_API_KEY", "k")
    seen = {}

    def post(url, headers, timeout, **kw):
        seen.update(url=url, headers=headers, **kw)
        return Resp(200, {"success": True, "transcript": " வணக்கம் "})
    monkeypatch.setattr(httpx, "post", post)
    assert gnani.transcribe(b"RIFF", "a.wav", "ta-IN") == "வணக்கம்"
    assert seen["url"].endswith("/stt/v3") and seen["headers"]["X-API-Key-ID"] == "k"
    assert seen["data"] == {"language_code": "ta-IN"}  # no `model` => Prisma v2.5 default


def test_timbre_request_shape(monkeypatch):
    monkeypatch.setattr(config, "GNANI_API_KEY", "k")
    seen = {}

    def post(url, headers, timeout, json):
        seen.update(url=url, json=json)
        return Resp(200, content=b"ID3audio")
    monkeypatch.setattr(httpx, "post", post)
    assert gnani.synthesize("வணக்கம்", "ta-IN") == b"ID3audio"
    assert seen["json"]["model"] == "timbre-v2.5" and seen["json"]["voice"] == "Asmita"


def test_gnani_auth_error_not_retried(monkeypatch):
    monkeypatch.setattr(config, "GNANI_API_KEY", "bad")
    calls = []

    def post(*a, **k):
        calls.append(1)
        return Resp(401, {"detail": {"error_code": "INVALID_API_KEY", "message": "Key is not valid"}})
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(gnani.ProviderError, match="INVALID_API_KEY") as e:
        gnani.transcribe(b"x", "a.wav", "hi-IN")
    assert len(calls) == 1 and not e.value.retryable


def test_gnani_rate_limit_retried_then_fails(monkeypatch):
    monkeypatch.setattr(config, "GNANI_API_KEY", "k")
    monkeypatch.setattr(gnani.time, "sleep", lambda s: None)
    calls = []

    def post(*a, **k):
        calls.append(1)
        return Resp(429, {"detail": {"error_code": "RATE_LIMITED", "message": "slow down"}})
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(gnani.ProviderError) as e:
        gnani.synthesize("hi", "hi-IN")
    assert len(calls) == 3 and e.value.retryable


# ---- Evon

def test_missing_evon_url_is_explicit():
    with pytest.raises(gnani.ProviderError, match="EVON_BASE_URL"):
        evon.complete([{"role": "user", "content": "x"}])


def test_evon_timeout_retried_then_fails(monkeypatch):
    monkeypatch.setattr(config, "EVON_BASE_URL", "http://evon.local/v1")
    monkeypatch.setattr(evon.time, "sleep", lambda s: None)
    calls = []

    def post(*a, **k):
        calls.append(k["json"])
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(gnani.ProviderError, match="timed out"):
        evon.complete([{"role": "user", "content": "x"}])
    assert len(calls) == 3 and calls[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_evon_json_with_think_tags_and_repair(monkeypatch):
    outputs = iter(['<think>hmm {"no": 1}</think> not json', '```json\n{"feasible": true, "goal": "Ask the time", '
                    '"goal_for_requester": "समय पूछें", "required_information": [{"key": "eta", "description": "minutes"}]}\n```'])
    monkeypatch.setattr(evon, "complete", lambda m: next(outputs))
    p = evon.ask_json("s", "u", Plan)
    assert p.required_information[0].key == "eta"


def test_evon_invalid_twice_fails_loudly(monkeypatch):
    monkeypatch.setattr(evon, "complete", lambda m: '{"goal": 1}')
    with pytest.raises(gnani.ProviderError, match="invalid structured output"):
        evon.ask_json("s", "u", Plan)


def test_plan_drops_unsupported_language_and_unknown_risk():
    p = Plan.model_validate({"goal": "Ask the time", "goal_for_requester": "समय", "recipient_language": "fr-FR",
                             "risk_flags": ["financial", "rocket"],
                             "required_information": [{"key": "eta", "description": "minutes"}]})
    assert p.recipient_language is None and p.risk_flags == ["financial"]


# ---- API: auth & authorization

@pytest.fixture
def client(db):
    from app.main import app
    return TestClient(app)


def _signup(client, email):
    r = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    client.patch("/api/me", json={"language": "gu-IN", "approve_every_call": True})


def test_requires_login(client):
    assert client.get("/api/tasks").status_code == 401


def test_users_cannot_see_each_others_tasks(client, fake):
    from app.main import app
    _signup(client, "a@x.com")
    r = client.post("/api/tasks", json={"request": "Ask the conductor when bus 21G reaches Guindy",
                                        "contact_phone": "+91 98765 43210"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["state"] == "AWAITING_APPROVAL"  # background planning ran; approval enforced
    other = TestClient(app)
    _signup(other, "b@x.com")
    assert other.get(f"/api/tasks/{tid}").status_code == 404
    assert other.post(f"/api/tasks/{tid}/cancel").status_code == 404
    aid = t["approvals"][-1]["id"]
    assert other.post(f"/api/tasks/{tid}/approvals/{aid}", json={"decision": "approve"}).status_code == 404
    r = client.post(f"/api/tasks/{tid}/approvals/{aid}", json={"decision": "approve"})
    assert r.json()["state"] == "CALLING" and r.json()["active_call"]["mode"] == "simulated"


def test_stt_without_key_returns_503(client):
    _signup(client, "c@x.com")
    r = client.post("/api/stt", files={"audio": ("a.wav", b"RIFF")}, data={"language": "hi-IN"})
    assert r.status_code == 503 and "GNANI_API_KEY" in r.json()["detail"]


def test_wrong_password(client):
    _signup(client, "d@x.com")
    assert client.post("/api/auth/login", json={"email": "d@x.com", "password": "wrongpass1"}).status_code == 401
