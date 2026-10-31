import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import config, engine, worker
from app.db import Approval, Contact, Job, Notification, now
from conftest import make_task, plan


def reply(db, task, user, text, analysis, key=None):
    engine.evon.ask_json.__self__.analyses.append(analysis)  # the FakeEvon bound in the fixture
    engine.recipient_says(db, task, user, engine.active_call(task).id, text, key or uuid.uuid4().hex)
    db.commit()


def upd(key, status, value=None, **kw):
    return {"key": key, "status": status, "value": value, "evidence": value, **kw}


def last_move(fake):
    return fake.composed[-1]["instruction"]


ARRIVAL = "arrival_minutes"


def started(db, user, fake, **kw):
    task = make_task(db, user, **kw)
    plan(db, task, user)
    assert task.state == "CALLING", task.status_detail
    return task


# ---- Feature 1: adaptive conversation

def test_complete_answer_verifies_then_completes(db, user, fake):
    task = started(db, user, fake)
    assert "AI assistant" in last_move(fake) and "Asha" in last_move(fake)
    reply(db, task, user, "ஆமா சொல்லுங்க", {})                              # "yes, go ahead"
    assert task.state == "CALLING" and "Ask for: approximate minutes" in last_move(fake)
    reply(db, task, user, "சுமார் 20 நிமிடம்", {"updates": [upd(ARRIVAL, "estimate", "about 20 minutes")]})
    assert task.state == "VERIFYING" and "about 20 minutes" in last_move(fake)
    reply(db, task, user, "ஆமா", {"signals": {"readback_confirmed": True}})
    assert task.state == "COMPLETED"
    assert task.required_info[0]["status"] == "estimate"
    assert task.calls[-1].status == "completed"
    assert task.result_summary
    assert db.scalar(select(Notification).where(Notification.kind == "answer_received"))


def test_vague_answer_triggers_targeted_follow_up(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "ok", {})
    reply(db, task, user, "பஸ் பக்கத்துல இருக்கு", {"updates": [upd(ARRIVAL, "uncertain", "nearby")]})
    assert task.state == "CLARIFYING"
    assert "not sufficient" in last_move(fake) and "nearby" in last_move(fake)
    assert "Tamil" in fake.composed[-1]["system"]  # spoken in recipient's language


def test_clarifications_are_bounded_and_reported_honestly(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "ok", {})
    for _ in range(config.MAX_CLARIFICATIONS_PER_FACT + 1):
        reply(db, task, user, "theriyala", {"updates": [upd(ARRIVAL, "uncertain", "doesn't know")]})
    assert task.required_info[0]["status"] == "unanswered"
    assert task.state == "NEEDS_HUMAN_HELP"  # nothing learned: no fake success
    assert sum("not sufficient" in c["instruction"] for c in fake.composed) == config.MAX_CLARIFICATIONS_PER_FACT


def test_unanswered_question_is_asked_again_not_skipped(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "ok", {})
    reply(db, task, user, "who is this?", {"recipient_question": "who is calling?"})
    assert task.state == "CLARIFYING"
    assert "who is calling?" in last_move(fake)


def test_contradiction_is_clarified(db, user, fake):
    fake.plan["required_information"].append({"key": "bus_number", "description": "bus registration number"})
    task = started(db, user, fake)
    reply(db, task, user, "ok", {})
    reply(db, task, user, "10 min", {"updates": [upd(ARRIVAL, "confirmed", "10 minutes")]})
    reply(db, task, user, "no wait, 40 minutes",
          {"updates": [upd(ARRIVAL, "confirmed", "40 minutes", contradicts_previous=True)]})
    assert task.state == "CLARIFYING"
    assert "conflicts with earlier answer '10 minutes'" in last_move(fake)


def test_recipient_language_switch(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "Hindi mein boliye", {"recipient_language": "hi-IN"})
    assert task.recipient_language == "hi-IN" and task.recipient_language_confirmed
    assert "Hindi" in fake.composed[-1]["system"]
    assert task.turns[-1].language == "hi-IN"
    reply(db, task, user, "20 minute", {"updates": [upd(ARRIVAL, "estimate", "about 20 minutes")]})
    reply(db, task, user, "haan", {"signals": {"readback_confirmed": True}})
    assert "Gujarati" in fake.summaries[-1]  # result in requester's language
    assert db.get(Contact, task.contact_id).language == "hi-IN"  # remembered


def test_unknown_keys_from_model_are_ignored(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "ok", {"updates": [upd("made_up_key", "confirmed", "x")]})
    assert [i["key"] for i in task.required_info] == [ARRIVAL]


# ---- Feature 2: memory, recovery, duplicates

def test_disconnect_resumes_without_repeating_answered_items(db, user, fake):
    fake.plan["required_information"].append({"key": "bus_number", "description": "bus registration number"})
    task = started(db, user, fake)
    reply(db, task, user, "ok", {})
    reply(db, task, user, "15 min", {"updates": [upd(ARRIVAL, "confirmed", "15 minutes")]})
    engine.call_outcome(db, task, user, engine.active_call(task).id, "disconnected")
    db.commit()
    assert task.state == "WAITING_RETRY" and task.required_info[0]["status"] == "confirmed"
    engine.retry(db, task, user)
    db.commit()
    assert task.state == "CALLING" and len(task.calls) == 2
    assert "calling back" in last_move(fake)
    reply(db, task, user, "ok", {})
    assert "registration number" in last_move(fake)  # skips the answered arrival question


def test_state_survives_new_session(db, user, fake):
    task = started(db, user, fake)
    tid = task.id
    db.close()
    from app.db import SessionLocal, Task
    with SessionLocal() as db2:
        t = db2.get(Task, tid)
        assert t.state == "CALLING" and engine.active_call(t) and t.turns[0].intent == "open"


def test_same_reply_key_processed_once(db, user, fake):
    task = started(db, user, fake)
    fake.analyses.append({})
    engine.recipient_says(db, task, user, engine.active_call(task).id, "ok", "key-12345")
    db.commit()
    n = len(task.turns)
    engine.recipient_says(db, task, user, engine.active_call(task).id, "ok", "key-12345")
    assert len(task.turns) == n


def test_model_failure_mid_turn_keeps_reply_and_retry_recovers(db, user, fake):
    task = started(db, user, fake)
    fake.fail_next = 1
    with pytest.raises(engine.ProviderError):
        engine.recipient_says(db, task, user, engine.active_call(task).id, "ok", "key-abcdef")
    db.rollback()
    assert task.checkpoint.get("pending_turn") == "key-abcdef"
    assert task.turns[-1].speaker == "recipient"
    fake.analyses.append({})
    engine.recipient_says(db, task, user, engine.active_call(task).id, "ok", "key-abcdef")
    db.commit()
    assert task.turns[-1].speaker == "agent" and "pending_turn" not in task.checkpoint
    assert sum(t.speaker == "recipient" for t in task.turns) == 1


def test_no_second_call_while_one_active(db, user, fake):
    task = started(db, user, fake)
    with pytest.raises(engine.EngineError):
        engine.place_call(db, task, user, "retry", None, "other-key")


def test_place_call_idempotent(db, user, fake):
    task = make_task(db, user)
    engine.begin_planning(db, task)
    task.goal = "g"
    c1 = engine.place_call(db, task, user, "first", None, "k1")
    assert engine.place_call(db, task, user, "first", None, "k1") is c1


def test_bounded_redials(db, user, fake):
    task = started(db, user, fake)
    for i in range(config.MAX_CALL_ATTEMPTS):
        engine.call_outcome(db, task, user, engine.active_call(task).id, "no_answer")
        if i < config.MAX_CALL_ATTEMPTS - 1:
            engine.retry(db, task, user)
    assert task.state == "NEEDS_HUMAN_HELP"
    with pytest.raises(engine.EngineError):
        engine.retry(db, task, user)


# ---- Feature 4: approvals

def test_approval_required_and_enforced(db, user, fake):
    user.approve_every_call = True
    task = make_task(db, user)
    plan(db, task, user)
    assert task.state == "AWAITING_APPROVAL" and not task.calls
    with pytest.raises(engine.EngineError) as e:  # backend refuses to dial without approval
        engine.place_call(db, task, user, "first", None, "sneaky")
    assert e.value.code == 403
    a = task.approvals[-1]
    engine.decide_approval(db, task, user, a, "approve")
    assert task.state == "CALLING" and a.status == "used"
    with pytest.raises(engine.EngineError):  # single use
        engine.decide_approval(db, task, user, a, "approve")


def test_unknown_number_needs_approval(db, user, fake):
    task = make_task(db, user, saved=False)
    plan(db, task, user)
    assert task.state == "AWAITING_APPROVAL"
    assert any("saved contacts" in r for r in task.approvals[-1].reasons)


def test_ambiguous_contact_asks_requester(db, user, fake):
    db.add_all([Contact(user_id=user.id, name="Ravi", phone="+919000000001"),
                Contact(user_id=user.id, name="Ravi", phone="+919000000002")])
    task = make_task(db, user, phone=None)
    engine.resolve_contact(db, task, "Ravi", None)
    plan(db, task, user)
    assert task.state == "AWAITING_INPUT" and "contact_choice" in task.missing_inputs


def test_missing_phone_asks_only_for_it(db, user, fake):
    task = make_task(db, user, phone=None)
    plan(db, task, user)
    assert task.state == "AWAITING_INPUT" and task.missing_inputs == ["contact_phone"]
    with pytest.raises(engine.EngineError):
        engine.provide_inputs(db, task, user, None, None, "Conductor", "12", None)
    engine.provide_inputs(db, task, user, None, None, "Conductor", "+91 98765 43210", None)
    engine.run_planning(db, task, user)
    assert task.state == "AWAITING_APPROVAL"  # new number => approval


def test_reject_pauses_safely(db, user, fake):
    user.approve_every_call = True
    task = make_task(db, user)
    plan(db, task, user)
    engine.decide_approval(db, task, user, task.approvals[-1], "reject")
    assert task.state == "NEEDS_HUMAN_HELP" and not task.calls


def test_edit_replans_and_requires_fresh_approval(db, user, fake):
    user.approve_every_call = False
    fake.plan["risk_flags"] = ["financial"]
    task = make_task(db, user)
    plan(db, task, user)
    first = task.approvals[-1]
    engine.decide_approval(db, task, user, first, "edit", instruction="Also ask if seat 12 is free")
    assert task.state == "PLANNING" and first.status == "superseded"
    engine.run_planning(db, task, user)
    assert task.state == "AWAITING_APPROVAL" and task.approvals[-1].id != first.id
    assert any("edited" in r for r in task.approvals[-1].reasons)


def test_approval_bound_to_action(db, user, fake):
    user.approve_every_call = True
    task = make_task(db, user)
    plan(db, task, user)
    a = task.approvals[-1]
    task.contact_phone = "+919111111111"  # materially different action
    with pytest.raises(engine.EngineError):
        engine.decide_approval(db, task, user, a, "approve")
    assert not task.calls


def test_commitment_request_pauses_for_approval(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "Pay 200 rupees extra and I'll hold the bus",
          {"signals": {"commitment_requested": "pay 200 rupees extra"}})
    assert task.state == "AWAITING_APPROVAL" and task.calls[-1].status == "ended"
    assert "check with the requester" in last_move(fake)
    engine.decide_approval(db, task, user, task.approvals[-1], "approve")
    assert task.approved_commitments == ["pay 200 rupees extra"] and task.state == "CALLING"


def test_do_not_call_is_respected(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "Don't call me again", {"signals": {"do_not_call": True}})
    assert task.state == "NEEDS_HUMAN_HELP"
    assert db.get(Contact, task.contact_id).opted_out
    task.state = "WAITING_RETRY"
    with pytest.raises(engine.EngineError) as e:
        engine.place_call(db, task, user, "retry", None, "again")
    assert e.value.code == 403


def test_cancel_stops_call_and_jobs(db, user, fake):
    task = started(db, user, fake)
    engine.cancel(db, task, user)
    assert task.state == "CANCELLED" and task.calls[-1].status == "ended"
    with pytest.raises(engine.EngineError):
        engine.cancel(db, task, user)


def test_take_over(db, user, fake):
    task = started(db, user, fake)
    engine.take_over(db, task, user)
    assert task.state == "NEEDS_HUMAN_HELP" and task.manual_takeover and not engine.active_call(task)


# ---- Feature 3: follow-ups and notifications

def _promise_flow(db, user, fake):
    task = started(db, user, fake)
    reply(db, task, user, "ok", {})
    at = (now() + timedelta(hours=2)).isoformat()
    reply(db, task, user, "20 min; I'll message the driver, he'll reach by then",
          {"updates": [upd(ARRIVAL, "estimate", "about 20 minutes")], "promise": "bus at Guindy", "promised_at": at})
    reply(db, task, user, "yes", {"signals": {"readback_confirmed": True}})
    assert task.state == "WAITING_FOLLOW_UP"
    return task


def test_promise_schedules_one_follow_up_job(db, user, fake):
    task = _promise_flow(db, user, fake)
    jobs = db.scalars(select(Job).where(Job.task_id == task.id)).all()
    assert len(jobs) == 1 and jobs[0].kind == "follow_up_due"
    engine._schedule_follow_up(db, task)  # duplicate attempt
    db.commit()
    assert len(db.scalars(select(Job).where(Job.task_id == task.id)).all()) == 1


def test_due_follow_up_asks_user_when_not_authorized(db, user, fake):
    task = _promise_flow(db, user, fake)
    job = db.scalar(select(Job).where(Job.task_id == task.id))
    job.run_at = now() - timedelta(seconds=1)
    db.commit()
    worker.process_jobs(db)
    assert task.state == "WAITING_FOLLOW_UP" and not engine.active_call(task)
    assert db.scalar(select(Notification).where(Notification.kind == "follow_up_due"))
    engine.follow_up_decision(db, task, user, "call")
    assert task.state == "CALLING"
    assert task.required_info[-1]["key"] == "follow_up_1"


def test_due_follow_up_calls_when_pre_authorized(db, user, fake):
    task = _promise_flow(db, user, fake)
    task.follow_up_authorized = True
    job = db.scalar(select(Job).where(Job.task_id == task.id))
    job.run_at = now() - timedelta(seconds=1)
    db.commit()
    worker.process_jobs(db)
    assert task.state == "CALLING"
    worker.process_jobs(db)  # already done: no second call
    assert len(task.calls) == 2


def test_cancelled_follow_up_never_fires(db, user, fake):
    task = _promise_flow(db, user, fake)
    engine.follow_up_decision(db, task, user, "dismiss")
    db.commit()
    job = db.scalar(select(Job).where(Job.task_id == task.id))
    assert job.status == "cancelled" and task.state == "COMPLETED"


def test_auto_redial_respects_quiet_hours(db, user, fake):
    user.allow_auto_redial = True
    task = started(db, user, fake)
    engine.call_outcome(db, task, user, engine.active_call(task).id, "busy")
    db.commit()
    job = db.scalar(select(Job).where(Job.kind == "redial"))
    job.run_at = now() - timedelta(seconds=1)
    from zoneinfo import ZoneInfo
    hour = now().astimezone(ZoneInfo(user.timezone)).hour
    user.quiet_start_hour, user.quiet_end_hour = hour, (hour + 1) % 24
    db.commit()
    worker.process_jobs(db)
    assert task.state == "WAITING_RETRY" and job.status == "pending"  # deferred, not dropped
    user.quiet_start_hour = user.quiet_end_hour = None
    job.run_at = now() - timedelta(seconds=1)
    db.commit()
    worker.process_jobs(db)
    assert task.state == "CALLING" and len(task.calls) == 2


# ---- failures

def test_planning_model_unavailable_needs_help(db, user, fake):
    fake.fail_next = 1
    task = make_task(db, user)
    plan(db, task, user)
    assert task.state == "NEEDS_HUMAN_HELP" and "unavailable" in task.status_detail


def test_infeasible_task_is_explained(db, user, fake):
    fake.plan.update(feasible=False, infeasible_reason="ફોન દ્વારા શક્ય નથી")
    task = make_task(db, user)
    plan(db, task, user)
    assert task.state == "NEEDS_HUMAN_HELP" and task.status_detail == "ફોન દ્વારા શક્ય નથી"


def test_fallback_summary_labels_estimates(db, user, fake):
    task = make_task(db, user)
    task.required_info = [{"key": "a", "description": "arrival", "status": "estimate", "value": "about 20 min"},
                          {"key": "b", "description": "platform", "status": "unanswered", "value": None}]
    s = engine.fallback_summary(task)
    assert "Simulated" in s and "Estimate — about 20 min" in s and "platform: Not answered" in s


def test_invalid_transition_rejected(db, user, fake):
    task = make_task(db, user)
    with pytest.raises(engine.EngineError):
        engine.transition(db, task, "COMPLETED", "x")


def test_phone_must_be_exactly_10_digits():
    from app.engine import normalize_phone
    for ok in ["9876543210", "98765 43210", "+91 98765 43210", "919876543210", "09876543210"]:
        assert normalize_phone(ok) == "+919876543210", ok
    for bad in ["987654321", "98765432101", "+1 2025550123", "+4498765432101", "98765abcde"]:
        assert normalize_phone(bad) is None, bad
