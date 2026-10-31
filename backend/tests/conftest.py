import os
import tempfile

# Isolate tests from any real .env: no real credentials, throwaway DB.
_tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{_tmp}/test.db", GNANI_API_KEY="", EVON_BASE_URL="",
                  EVON_MODE="remote", ANTHROPIC_API_KEY="", EVON_API_KEY="",
                  VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")

import pytest  # noqa: E402

from app import engine, evon  # noqa: E402
from app.db import Base, Contact, SessionLocal, Task, User, engine as db_engine  # noqa: E402


class FakeEvon:
    """Scripted stand-in for the Evon model. Plans/analyses are queued by each test."""

    def __init__(self):
        self.plan = {
            "goal": "Find out when bus KA-01 will reach Guindy stop.",
            "goal_for_requester": "બસ ક્યારે આવશે તે જાણો",
            "required_information": [{"key": "arrival_minutes", "description": "approximate minutes until the bus reaches Guindy"}],
            "context_for_call": "Booked seat on bus KA-01, boarding at Guindy.",
            "recipient_language": "ta-IN",
        }
        self.analyses: list[dict] = []
        self.composed: list[dict] = []  # (language, instruction) of each agent utterance
        self.summaries: list[str] = []
        self.fail_next = 0

    def ask_json(self, system, user, schema):
        if self.fail_next:
            self.fail_next -= 1
            raise evon.ProviderError("evon", "request timed out", retryable=True)
        if schema is engine.Plan:
            return engine.Plan.model_validate(self.plan)
        if schema is engine.Analysis:
            return engine.Analysis.model_validate(self.analyses.pop(0))
        if schema is engine.Utterance:
            import json
            instruction = json.loads(user)["instruction"]
            self.composed.append({"system": system, "instruction": instruction})
            return engine.Utterance(text=f"[agent] {instruction[:60]}")
        if schema is engine.Summary:
            self.summaries.append(system)
            return engine.Summary(summary="summary in requester language")
        raise AssertionError(schema)


@pytest.fixture
def fake(monkeypatch):
    f = FakeEvon()
    monkeypatch.setattr(evon, "ask_json", f.ask_json)
    return f


@pytest.fixture
def db():
    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    with SessionLocal() as s:
        yield s


@pytest.fixture
def user(db):
    u = User(email="a@example.com", password_hash="x", display_name="Asha", language="gu-IN",
             approve_every_call=False)
    db.add(u)
    db.commit()
    return u


def make_task(db, user, phone="+919876543210", saved=True, **kw) -> Task:
    if saved and phone:
        db.add(Contact(user_id=user.id, name="Conductor", phone=phone))
    t = Task(user_id=user.id, original_request="When will my bus reach Guindy?", requester_language=user.language,
             context_notes=[], required_info=[], missing_inputs=[], risk_flags=[], approved_commitments=[],
             checkpoint={}, **kw)
    db.add(t)
    db.flush()
    if phone:
        engine.resolve_contact(db, t, "Conductor", phone)
    return t


def plan(db, task, user):
    engine.begin_planning(db, task)
    engine.run_planning(db, task, user)
    db.commit()
