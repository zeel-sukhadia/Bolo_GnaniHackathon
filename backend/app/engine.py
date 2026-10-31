"""Goal-driven task engine.

The model (Evon) proposes: plans, judgements of answers, and wording. Everything that matters
— state transitions, approvals, call placement, retries, follow-ups — is decided here by
deterministic rules. No domain-specific logic: tasks are goals + required_information.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from . import config, evon
from .db import Approval, CallAttempt, Contact, Event, Job, Notification, Task, Turn, User, new_id, now
from .gnani import LANGUAGES, ProviderError, gender_rule

# ---------------------------------------------------------------- state machine

ACTIVE_CALL = {"CALLING", "CLARIFYING", "VERIFYING"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_AFTER_CALL = {"CALLING", "CLARIFYING", "VERIFYING", "COMPLETED", "WAITING_RETRY", "WAITING_FOLLOW_UP",
               "AWAITING_APPROVAL", "NEEDS_HUMAN_HELP", "FAILED", "CANCELLED"}
TRANSITIONS = {
    "NEW": {"PLANNING", "CANCELLED"},
    "PLANNING": {"AWAITING_INPUT", "AWAITING_APPROVAL", "CALLING", "NEEDS_HUMAN_HELP", "FAILED", "CANCELLED"},
    "AWAITING_INPUT": {"PLANNING", "NEEDS_HUMAN_HELP", "CANCELLED"},
    "AWAITING_APPROVAL": {"CALLING", "PLANNING", "NEEDS_HUMAN_HELP", "CANCELLED"},
    "CALLING": _AFTER_CALL, "CLARIFYING": _AFTER_CALL, "VERIFYING": _AFTER_CALL,
    "WAITING_RETRY": {"CALLING", "AWAITING_APPROVAL", "NEEDS_HUMAN_HELP", "FAILED", "CANCELLED"},
    "WAITING_FOLLOW_UP": {"CALLING", "AWAITING_APPROVAL", "COMPLETED", "NEEDS_HUMAN_HELP", "CANCELLED"},
    "NEEDS_HUMAN_HELP": {"PLANNING", "COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(), "FAILED": set(), "CANCELLED": set(),
}
RESOLVED = {"confirmed", "estimate", "refused", "unanswered"}
RISK_FLAGS = {"financial", "booking_change", "contractual", "sensitive_personal_info"}


class EngineError(Exception):
    """A request the rules refuse. `code` maps to an HTTP status in the API."""
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.message, self.code = message, code


def log(db: DB, task: Task, actor: str, kind: str, **detail):
    db.add(Event(task_id=task.id, actor=actor, kind=kind, detail=detail))


def transition(db: DB, task: Task, new: str, actor: str, detail: str | None = None):
    if new != task.state and new not in TRANSITIONS[task.state]:
        raise EngineError(f"Cannot move task from {task.state} to {new}")
    log(db, task, actor, "state", frm=task.state, to=new, detail=detail)
    task.state = new
    task.status_detail = detail


def notify(db: DB, task: Task, kind: str, title: str, body: str):
    """Durable in-app notification; app.worker delivers Web Push for it."""
    db.add(Notification(user_id=task.user_id, task_id=task.id, kind=kind, title=title, body=body))


# ---------------------------------------------------------------- model I/O schemas

class InfoItem(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    description: str = Field(min_length=3, max_length=300)


class Plan(BaseModel):
    feasible: bool = True
    infeasible_reason: str | None = None
    goal: str = Field(min_length=5, max_length=500)
    goal_for_requester: str = Field(min_length=2, max_length=600)
    required_information: list[InfoItem] = Field(min_length=1, max_length=6)
    context_for_call: str = ""
    missing_inputs: list[str] = Field(default_factory=list, max_length=3)
    recipient_language: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("recipient_language")
    @classmethod
    def _lang(cls, v):
        return v if v in LANGUAGES else None

    @field_validator("risk_flags")
    @classmethod
    def _risk(cls, v):
        return [f for f in v if f in RISK_FLAGS]


class Update(BaseModel):
    key: str
    status: str = Field(pattern=r"^(confirmed|estimate|uncertain|refused)$")
    value: str | None = None
    evidence: str | None = None
    contradicts_previous: bool = False


class Signals(BaseModel):
    stop_requested: bool = False      # asked to end the call / stop
    do_not_call: bool = False         # asked never to be called again
    not_convenient: bool = False
    callback_at: str | None = None    # ISO time they asked to be called back
    wrong_person: bool = False
    cannot_help: bool = False
    unsupported_language: bool = False
    readback_confirmed: bool | None = None  # only meaningful while verifying
    commitment_requested: str | None = None  # they ask us to agree/pay/book/disclose something


class Analysis(BaseModel):
    updates: list[Update] = Field(default_factory=list)
    signals: Signals = Field(default_factory=Signals)
    recipient_language: str | None = None
    recipient_question: str | None = None
    promise: str | None = None        # something they will do later, e.g. "deliver by 5 PM"
    promised_at: str | None = None    # ISO time of the promise


class Utterance(BaseModel):
    text: str = Field(min_length=1, max_length=800)


class Summary(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------- helpers

def lang_name(code: str | None) -> str:
    return LANGUAGES.get(code or "", "the recipient's language")


def normalize_phone(raw: str | None) -> str | None:
    """Indian numbers only: exactly 10 digits, optionally written with +91, 91 or a leading 0. Stored as +91XXXXXXXXXX."""
    if not raw:
        return None
    digits = re.sub(r"[\s\-().]", "", raw)
    m = re.fullmatch(r"(?:\+91|91|0)?(\d{10})", digits)
    return "+91" + m.group(1) if m else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None  # refuse naive times: ambiguous


def _local_now(user: User) -> str:
    return now().astimezone(ZoneInfo(user.timezone)).isoformat(timespec="minutes")


def in_quiet_hours(user: User, at: datetime | None = None) -> bool:
    if user.quiet_start_hour is None or user.quiet_end_hour is None:
        return False
    h = (at or now()).astimezone(ZoneInfo(user.timezone)).hour
    s, e = user.quiet_start_hour, user.quiet_end_hour
    return s <= h < e if s < e else (h >= s or h < e)


def _facts(task: Task) -> list[dict]:
    return [{k: i.get(k) for k in ("key", "description", "status", "value")} for i in task.required_info]


def _transcript(task: Task, call_id: str | None = None, limit: int = 16) -> str:
    turns = [t for t in task.turns if call_id is None or t.call_id == call_id][-limit:]
    return "\n".join(f"{t.speaker.upper()}: {t.text}" for t in turns) or "(no conversation yet)"


def _schedule(db: DB, task: Task, kind: str, run_at: datetime, dedupe_key: str):
    if db.scalar(select(Job).where(Job.dedupe_key == dedupe_key)):
        return  # duplicate-job prevention
    db.add(Job(task_id=task.id, kind=kind, run_at=run_at, dedupe_key=dedupe_key))


def cancel_jobs(db: DB, task: Task):
    for j in db.scalars(select(Job).where(Job.task_id == task.id, Job.status == "pending")):
        j.status = "cancelled"


def _supersede_pending(task: Task):
    for a in task.approvals:
        if a.status == "pending":
            a.status = "superseded"


def active_call(task: Task) -> CallAttempt | None:
    return next((c for c in task.calls if c.status == "in_progress"), None)


# ---------------------------------------------------------------- planning

PLAN_SYSTEM = """You plan phone calls for an AI calling assistant. A requester describes something they need
from one person; the assistant will phone that person. Return ONLY a JSON object with keys:
feasible (bool), infeasible_reason (string|null, in the requester language), goal (one English sentence:
the intended outcome), goal_for_requester (same, in the requester language), required_information (1-6
items {"key": snake_case, "description": exactly what must be learned, specific enough to judge whether
an answer suffices, e.g. "approximate minutes until arrival"}), context_for_call (English: only the facts
from the request the recipient needs, e.g. references, locations), missing_inputs (questions in the
requester language, ONLY for information essential to make the call that is absent; never ask for the
phone number; usually []), recipient_language (one of %s, only if stated or clearly implied, else null),
contact_name (string|null, only if stated), contact_phone (string|null, only if stated), risk_flags
(subset of ["financial","booking_change","contractual","sensitive_personal_info"] if the call may involve
paying, booking changes, agreements, or sharing/requesting sensitive personal data).
Understand the intended outcome; do not translate the request literally. feasible=false if a short
phone conversation cannot achieve it, or it is deceptive, harassing or illegal."""


def begin_planning(db: DB, task: Task, kind: str = "first"):
    """Mark the task for (re)planning; run_planning does the model work (API background / worker)."""
    transition(db, task, "PLANNING", "agent")
    task.next_action = "Understanding your request"
    task.checkpoint = {**task.checkpoint, "plan_kind": kind}


def run_planning(db: DB, task: Task, user: User):
    """(Re)plan from the request + requester notes, then decide the next safe action."""
    if task.state != "PLANNING":
        return  # someone else already planned or the task was cancelled
    kind = task.checkpoint.get("plan_kind", "first")
    prompt = json.dumps({
        "requester_language": task.requester_language,
        "requester_name": user.display_name or None,
        "request": task.original_request,
        "requester_notes": task.context_notes,
        "existing_items_reuse_their_keys": _facts(task),
        "known_contact": {"name": task.contact_name, "phone_known": bool(task.contact_phone)},
        "now": _local_now(user),
    }, ensure_ascii=False)
    try:
        plan = evon.ask_json(PLAN_SYSTEM % list(LANGUAGES), prompt, Plan)
    except ProviderError as e:
        transition(db, task, "NEEDS_HUMAN_HELP", "agent", f"Planning model unavailable: {e.message}. Retry later.")
        notify(db, task, "needs_help", "Couldn't plan the task", task.status_detail)
        return
    if not plan.feasible:
        transition(db, task, "NEEDS_HUMAN_HELP", "agent", plan.infeasible_reason or "This task can't be done by phone.")
        notify(db, task, "needs_help", "Task needs your help", task.status_detail)
        return

    task.goal = plan.goal
    task.checkpoint = {**task.checkpoint, "goal_for_requester": plan.goal_for_requester,
                       "context_for_call": plan.context_for_call}
    old = {i["key"]: i for i in task.required_info}
    task.required_info = [  # keep facts already gathered for unchanged keys (no re-asking)
        old[i.key] if i.key in old and old[i.key]["status"] in RESOLVED else
        {"key": i.key, "description": i.description, "status": "unknown", "value": None,
         "evidence": None, "clarifications": 0}
        for i in plan.required_information]
    task.risk_flags = plan.risk_flags
    if plan.recipient_language and not task.recipient_language_confirmed:
        task.recipient_language = plan.recipient_language
    if not task.contact_phone:
        resolve_contact(db, task, plan.contact_name or task.contact_name, plan.contact_phone)
    log(db, task, "agent", "planned", goal=plan.goal, required=[i.key for i in plan.required_information])

    missing = list(plan.missing_inputs)
    if task.contact_status == "missing":
        missing.insert(0, "contact_phone")
    if task.contact_status == "ambiguous":
        missing.insert(0, "contact_choice")
    if not task.recipient_language:
        missing.append("recipient_language")
    task.missing_inputs = missing
    if missing:
        task.next_action = "Answer the missing details"
        transition(db, task, "AWAITING_INPUT", "agent")
        notify(db, task, "input_needed", "A few details needed", plan.goal_for_requester)
        return
    request_call(db, task, user, kind=kind)


def resolve_contact(db: DB, task: Task, name: str | None, phone: str | None, contact_id: str | None = None):
    """Deterministic contact matching; uncertainty is recorded, never guessed away."""
    saved = list(db.scalars(select(Contact).where(Contact.user_id == task.user_id)))
    if contact_id:
        c = next((c for c in saved if c.id == contact_id), None)
        if not c:
            raise EngineError("Contact not found", 404)
        return _use_contact(task, c)
    phone = normalize_phone(phone)
    if phone:
        c = next((c for c in saved if normalize_phone(c.phone) == phone), None)
        if c:
            return _use_contact(task, c)
        task.contact_id, task.contact_name, task.contact_phone = None, name, phone
        task.contact_status = "unconfirmed"  # new number: approval will be required
        return
    if name:
        matches = [c for c in saved if c.name.strip().lower() == name.strip().lower()]
        if len(matches) == 1:
            return _use_contact(task, matches[0])
        task.contact_name = name
        task.contact_status = "ambiguous" if matches else "missing"
        return
    task.contact_status = "missing"


def _use_contact(task: Task, c: Contact):
    task.contact_id, task.contact_name, task.contact_phone = c.id, c.name, c.phone
    task.contact_status = "confirmed"
    if c.language and not task.recipient_language_confirmed:
        task.recipient_language, task.recipient_language_confirmed = c.language, True


def provide_inputs(db: DB, task: Task, user: User, note: str | None, contact_id: str | None,
                   contact_name: str | None, contact_phone: str | None, recipient_language: str | None):
    if task.state not in {"AWAITING_INPUT", "NEEDS_HUMAN_HELP"}:
        raise EngineError(f"Task is {task.state}; it isn't waiting for details")
    if contact_id or contact_phone:
        if contact_phone and not normalize_phone(contact_phone):
            raise EngineError("Phone number must be exactly 10 digits", 422)
        resolve_contact(db, task, contact_name or task.contact_name, contact_phone, contact_id)
    if recipient_language:
        if recipient_language not in LANGUAGES:
            raise EngineError("Unsupported language", 422)
        task.recipient_language, task.recipient_language_confirmed = recipient_language, True
    if note:
        task.context_notes = [*task.context_notes, note.strip()[:1000]]
    task.manual_takeover = False
    log(db, task, user.id, "inputs_provided", note=bool(note), contact=bool(contact_id or contact_phone))
    begin_planning(db, task)


# ---------------------------------------------------------------- approval gate

def call_payload(task: Task) -> dict:
    """The exact action an approval authorizes. Any material change => new hash => new approval."""
    return {
        "action": "place_call", "phone": task.contact_phone, "contact_name": task.contact_name,
        "goal": task.goal, "recipient_language": task.recipient_language,
        "ask_for": [i["description"] for i in task.required_info if i["status"] not in RESOLVED],
        "commitments": task.approved_commitments, "pending_commitment": task.pending_commitment,
    }


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approval_reasons(task: Task, user: User, kind: str) -> list[str]:
    reasons = []
    if user.approve_every_call:
        reasons.append("Your settings require approval before every call.")
    if task.contact_status == "unconfirmed":
        reasons.append("This phone number isn't in your saved contacts — confirm it's the right person.")
    if task.contact_status == "ambiguous":
        reasons.append("More than one saved contact matches.")
    for f in task.risk_flags:
        reasons.append(f"The task may involve {f.replace('_', ' ')}.")
    if task.pending_commitment:
        reasons.append(f"The recipient asked for a commitment: “{task.pending_commitment}”.")
    if kind == "follow_up" and not task.follow_up_authorized:
        reasons.append("This is a follow-up call you haven't pre-authorized.")
    if kind == "redial" and not user.allow_auto_redial:
        reasons.append("Automatic redial is off in your settings.")
    if kind == "edited":
        reasons.append("You edited the plan — review it before calling.")
    return reasons


def request_call(db: DB, task: Task, user: User, kind: str):
    """Next action = call. Either place it now or stop at AWAITING_APPROVAL."""
    reasons = approval_reasons(task, user, kind)
    if not reasons:
        return place_call(db, task, user, kind, approval_id=None,
                          idempotency_key=f"{task.id}:{kind}:{len(task.calls)}")
    _supersede_pending(task)
    payload = call_payload(task)
    task.approvals.append(Approval(id=new_id(), task_id=task.id, action="place_call", payload=payload,
                                   action_hash=payload_hash(payload), reasons=reasons, status="pending",
                                   created_at=now()))
    task.next_action = "Approve, edit or reject the proposed call"
    transition(db, task, "AWAITING_APPROVAL", "agent", " ".join(reasons))
    notify(db, task, "approval_needed", "Approval needed before calling",
           f"Call {task.contact_name or task.contact_phone}? " + " ".join(reasons))


def decide_approval(db: DB, task: Task, user: User, approval: Approval, decision: str,
                    instruction: str | None = None, contact_phone: str | None = None,
                    contact_id: str | None = None):
    if approval.status != "pending":
        raise EngineError(f"This approval is already {approval.status}")
    if task.state != "AWAITING_APPROVAL":
        raise EngineError(f"Task is {task.state}, not awaiting approval")
    approval.decided_by, approval.decided_at = user.id, now()
    log(db, task, user.id, "approval_decision", approval_id=approval.id, decision=decision)
    if decision == "approve":
        if approval.action_hash != payload_hash(call_payload(task)):
            approval.status = "superseded"
            raise EngineError("The task changed since this approval was requested; review the new proposal")
        approval.status = "approved"
        if task.pending_commitment:  # the human authorized exactly this commitment
            task.approved_commitments = [*task.approved_commitments, task.pending_commitment]
            task.pending_commitment = None
        if task.contact_status == "unconfirmed":
            task.contact_status = "confirmed"
        return place_call(db, task, user, "approved", approval_id=approval.id,
                          idempotency_key=f"approval:{approval.id}", approved_hash=approval.action_hash)
    if decision == "reject":
        approval.status = "rejected"
        task.pending_commitment = None
        task.next_action = "Edit the request, take over, or cancel"
        transition(db, task, "NEEDS_HUMAN_HELP", user.id, "You rejected the proposed call. Nothing was sent.")
        return
    if decision == "edit":
        if not (instruction or contact_phone or contact_id):
            raise EngineError("Provide an edited instruction or contact", 422)
        approval.status = "superseded"
        if contact_phone or contact_id:
            if contact_phone and not normalize_phone(contact_phone):
                raise EngineError("Phone number must be exactly 10 digits", 422)
            task.contact_phone = None
            resolve_contact(db, task, task.contact_name, contact_phone, contact_id)
        if instruction:
            task.context_notes = [*task.context_notes, f"Requester edit: {instruction.strip()[:1000]}"]
        task.pending_commitment = None
        begin_planning(db, task, kind="edited")  # "edited" always yields a fresh approval
        return
    raise EngineError("decision must be approve, reject or edit", 422)


# ---------------------------------------------------------------- calls (simulated adapter)

def _consecutive_failures(task: Task) -> int:
    n = 0
    for c in reversed(task.calls):
        if c.status in {"no_answer", "busy", "rejected", "disconnected", "failed"}:
            n += 1
        elif c.status != "in_progress":
            break
    return n


def place_call(db: DB, task: Task, user: User, kind: str, approval_id: str | None, idempotency_key: str,
               approved_hash: str | None = None):
    """Backend-enforced gate: every rule re-checked here regardless of who asks."""
    existing = db.scalar(select(CallAttempt).where(CallAttempt.idempotency_key == idempotency_key))
    if existing:
        return existing  # duplicate request: no second call
    if task.state not in {"PLANNING", "AWAITING_APPROVAL", "WAITING_RETRY", "WAITING_FOLLOW_UP"}:
        raise EngineError(f"Can't place a call while task is {task.state}")
    if active_call(task):
        raise EngineError("A call for this task is already in progress")
    if not normalize_phone(task.contact_phone):
        raise EngineError("No valid phone number for this task", 422)
    if task.contact_id and db.get(Contact, task.contact_id).opted_out:
        raise EngineError("This contact asked not to be called", 403)
    if _consecutive_failures(task) >= config.MAX_CALL_ATTEMPTS:
        raise EngineError("Maximum call attempts reached")
    if approval_id is None and approval_reasons(task, user, kind):
        raise EngineError("Approval required before this call", 403)
    if approval_id is not None:
        a = db.get(Approval, approval_id)
        if not a or a.task_id != task.id or a.status != "approved" or a.action_hash != approved_hash:
            raise EngineError("Approval is not valid for this call", 403)
        a.status = "used"  # single use

    resuming = bool(task.calls)
    call = CallAttempt(id=new_id(), task_id=task.id, idempotency_key=idempotency_key, status="in_progress",
                       created_at=now(), mode=config.TELEPHONY_MODE, approval_id=approval_id)
    task.calls.append(call)
    task.next_action = "Call in progress"
    transition(db, task, "CALLING", "agent", "SIMULATED call — no real phone call is placed.")
    log(db, task, "agent", "call_started", call_id=call.id, mode=call.mode, approval_id=approval_id)
    task.checkpoint = {**task.checkpoint, "call_id": call.id, "last_asked": None, "verify_rounds": 0}
    move = {"type": "resume" if resuming else "open"}
    _agent_says(db, task, user, call, move)
    return call


def _agent_says(db: DB, task: Task, user: User, call: CallAttempt, move: dict) -> Turn:
    text = compose(task, user, call, move)
    turn = Turn(task_id=task.id, call_id=call.id, seq=len(task.turns), speaker="agent", text=text,
                language=task.recipient_language, intent=move["type"])
    task.turns.append(turn)
    return turn


COMPOSE_SYSTEM = """You are an AI phone assistant calling on behalf of a requester. Write ONLY what you
say next, in %s, as natural spoken language: 1-3 short sentences, polite, no lists or markdown.
Never pretend to be human. Never agree to payments, bookings, agreements or share personal data
unless listed in approved_commitments. Share only context needed for the goal.
Return ONLY JSON: {"text": "..."}"""

MOVES = {
    "open": "Greet. Say you are an AI assistant calling on behalf of {requester}. In one sentence give the "
            "reason for the call (the goal). {lang_check}Ask whether now is a convenient time to talk.",
    "resume": "Say you are the AI assistant calling back on behalf of {requester} (the earlier call ended). "
              "Ask whether now is a convenient time to continue. Do not repeat questions already answered.",
    "ask": "{answer_first}Ask for: {target}.",
    "clarify": "{answer_first}The answer about \"{target}\" was not sufficient ({why}). Ask one short, "
               "targeted follow-up question that would get a specific answer.",
    "verify": "Briefly read back these facts and ask them to confirm they are correct: {facts}",
    "verify_fix": "They said something in the read-back is wrong. Ask which detail is incorrect and what it should be.",
    "close": "End the call politely and thank them. Reason to convey (briefly): {reason}",
}


def compose(task: Task, user: User, call: CallAttempt, move: dict) -> str:
    instruction = MOVES[move["type"]].format(
        requester=user.display_name or "the requester",
        lang_check="" if task.recipient_language_confirmed else
        "Ask which language they prefer to continue in. ",
        answer_first=f"First briefly answer their question \"{move['question']}\" using only the context "
                     "(if unknown, say you don't know). " if move.get("question") else "",
        target=move.get("target", ""), why=move.get("why", ""), facts=move.get("facts", ""),
        reason=move.get("reason", ""))
    user_msg = json.dumps({
        "goal": task.goal, "context": task.checkpoint.get("context_for_call", ""),
        "requester_notes": task.context_notes, "approved_commitments": task.approved_commitments,
        "known_facts": [f for f in _facts(task) if f["status"] in RESOLVED],
        "conversation_so_far": _transcript(task, call.id), "instruction": instruction,
    }, ensure_ascii=False)
    return evon.ask_json(COMPOSE_SYSTEM % lang_name(task.recipient_language) + gender_rule(task.recipient_language),
                         user_msg, Utterance).text


ANALYZE_SYSTEM = """You judge the latest reply in a phone call made by an AI assistant. Return ONLY JSON:
{"updates": [{"key", "status", "value", "evidence", "contradicts_previous"}], "signals": {"stop_requested",
"do_not_call", "not_convenient", "callback_at", "wrong_person", "cannot_help", "unsupported_language",
"readback_confirmed", "commitment_requested"}, "recipient_language", "recipient_question", "promise",
"promised_at"}.
updates: one per required item the latest reply addresses. status: "confirmed" = specific and definite and
satisfies the item's description; "estimate" = specific but approximate ("about 20 minutes");
"uncertain" = vague, evasive or insufficient for the description (e.g. "nearby" when a time is needed);
"refused" = they will not or cannot answer this item. value: the normalized answer in English with units.
evidence: their exact words (short). contradicts_previous: true if it conflicts with a known value.
Never invent answers; omit items not addressed.
signals: readback_confirmed true/false only if they responded to a read-back, else null.
commitment_requested: text if they ask the assistant to agree to pay, book, sign, or share personal data.
callback_at / promised_at: absolute ISO-8601 with UTC offset (convert relative times using now).
recipient_language: a code from %s only if they ask to switch or clearly speak another one, else null.
recipient_question: a question they asked us, else null. promise: something they say will happen later."""


def recipient_says(db: DB, task: Task, user: User, call_id: str, text: str, idempotency_key: str):
    """Process one recipient utterance (typed or transcribed). Idempotent per key."""
    if db.scalar(select(Turn).where(Turn.idempotency_key == idempotency_key)) and \
            task.checkpoint.get("pending_turn") != idempotency_key:
        return  # already processed
    call = active_call(task)
    if not call or call.id != call_id or task.state not in ACTIVE_CALL:
        raise EngineError("No active call to respond to")
    if not text.strip():
        raise EngineError("Empty reply", 422)
    if task.checkpoint.get("pending_turn") != idempotency_key:
        task.turns.append(Turn(task_id=task.id, call_id=call.id, seq=len(task.turns), speaker="recipient",
                               text=text.strip()[:2000], language=task.recipient_language,
                               idempotency_key=idempotency_key))
    # Checkpoint the raw reply before calling the model so a model failure loses nothing.
    task.checkpoint = {**task.checkpoint, "pending_turn": idempotency_key}
    db.flush()
    db.commit()

    a = evon.ask_json(ANALYZE_SYSTEM % list(LANGUAGES), json.dumps({
        "now": _local_now(user), "goal": task.goal, "required_items": _facts(task),
        "verifying": task.state == "VERIFYING", "conversation": _transcript(task, call.id),
    }, ensure_ascii=False), Analysis)
    _apply(db, task, user, call, a)
    task.checkpoint = {k: v for k, v in task.checkpoint.items() if k != "pending_turn"}


def _apply(db: DB, task: Task, user: User, call: CallAttempt, a: Analysis):
    items = {i["key"]: dict(i) for i in task.required_info}
    changed = False
    for u in a.updates:
        item = items.get(u.key)
        if not item:
            continue  # ignore keys the plan doesn't have
        if u.contradicts_previous and item.get("value"):
            item["conflict"] = item["value"]
            item["status"] = "uncertain"
        else:
            item["status"] = u.status
        changed |= item.get("value") != u.value and u.value is not None
        item["value"] = u.value if u.value is not None else item.get("value")
        item["evidence"] = u.evidence
    task.required_info = list(items.values())
    if a.recipient_language and a.recipient_language != task.recipient_language:
        task.recipient_language = a.recipient_language
    if a.recipient_language or (len(task.turns) >= 2 and task.turns[-2].intent in {"open", "resume"}):
        task.recipient_language_confirmed = True  # they asked to switch, or answered our language check
    c = db.get(Contact, task.contact_id) if task.contact_id else None
    if c and task.recipient_language_confirmed:
        c.language = task.recipient_language  # remember for next time
    t = _parse_time(a.promised_at)
    if a.promise and t and t > now():
        task.follow_up_at, task.follow_up_reason = t, a.promise
    s = a.signals
    log(db, task, "recipient", "turn_analyzed", updates=[u.key for u in a.updates],
        signals=s.model_dump(exclude_defaults=True))

    # 1. The recipient's wishes come first.
    if s.do_not_call:
        if task.contact_id:
            db.get(Contact, task.contact_id).opted_out = True
        return _end(db, task, user, call, "They asked not to be called again; we will not call them.",
                    "NEEDS_HUMAN_HELP", "Recipient asked not to be called again.", "ended")
    if s.unsupported_language:
        return _end(db, task, user, call, "We can't continue in their language right now.",
                    "NEEDS_HUMAN_HELP", "Recipient's language isn't supported.", "ended")
    if s.stop_requested or s.wrong_person or s.cannot_help:
        why = "wrong person" if s.wrong_person else "they can't help" if s.cannot_help else "they asked to stop"
        return _end(db, task, user, call, "Respect their wish; apologize for the trouble.",
                    "NEEDS_HUMAN_HELP", f"Call ended: {why}.", "ended")
    if s.not_convenient:
        cb = _parse_time(s.callback_at)
        if cb and cb > now():
            task.follow_up_at, task.follow_up_reason = cb, "Recipient asked to be called back at this time."
        return _end(db, task, user, call, "Say we'll try another time.",
                    "WAITING_FOLLOW_UP" if task.follow_up_at else "NEEDS_HUMAN_HELP",
                    "Recipient was busy." + (" Callback time recorded." if task.follow_up_at else ""), "ended")
    if s.commitment_requested and s.commitment_requested not in task.approved_commitments:
        task.pending_commitment = s.commitment_requested[:500]
        _agent_says(db, task, user, call, {"type": "close", "reason":
                    "you must check with the requester before agreeing to that; someone will get back to them"})
        _finish_call(db, task, call, "ended")
        _summarize(db, task, user)
        request_call(db, task, user, kind="commitment")
        return

    # 2. Verification round.
    if task.state == "VERIFYING":
        rounds = task.checkpoint.get("verify_rounds", 0)
        if s.readback_confirmed and not changed:
            return _complete(db, task, user, call)
        if rounds >= 2:
            return _complete(db, task, user, call)  # unconfirmed items stay labelled as such
        if not changed:
            task.checkpoint = {**task.checkpoint, "verify_rounds": rounds + 1}
            _agent_says(db, task, user, call, {"type": "verify_fix"})
            return
        task.checkpoint = {**task.checkpoint, "verify_rounds": rounds + 1}

    # 3. Next information gap: conflicts first, then in plan order.
    last = task.checkpoint.get("last_asked")
    info = [dict(i) for i in task.required_info]  # fresh dicts so the JSON column sees changes
    move = None
    for item in sorted(info, key=lambda i: "conflict" not in i):
        if item["status"] in RESOLVED:
            continue
        if item["status"] == "unknown" and item["key"] != last and "conflict" not in item:
            move = {"type": "ask", "target": item["description"]}
        elif item["clarifications"] >= config.MAX_CLARIFICATIONS_PER_FACT:
            item["status"] = "unanswered"  # stop pressing; report it honestly
            item.pop("conflict", None)
            continue
        else:  # vague, contradictory, or our question went unanswered
            item["clarifications"] += 1
            why = f"conflicts with earlier answer '{item.pop('conflict')}'" if "conflict" in item else \
                f"they said: {item.get('evidence') or 'nothing that answers it'}"
            move = {"type": "clarify", "target": item["description"], "why": why}
        task.checkpoint = {**task.checkpoint, "last_asked": item["key"]}
        break
    task.required_info = info
    if move:
        transition(db, task, "CLARIFYING" if move["type"] == "clarify" else "CALLING", "agent")
        _agent_says(db, task, user, call, {**move, "question": a.recipient_question})
        return

    # 4. Everything resolved: verify what we learned, or give up honestly.
    learned = [i for i in task.required_info if i["status"] in {"confirmed", "estimate"}]
    if not learned:
        return _end(db, task, user, call, "Thank them for their time.", "NEEDS_HUMAN_HELP",
                    "The recipient couldn't provide the information.", "completed")
    transition(db, task, "VERIFYING", "agent")
    facts = "; ".join(f"{i['description']}: {i['value']}" for i in learned)
    _agent_says(db, task, user, call, {"type": "verify", "facts": facts})


def _finish_call(db: DB, task: Task, call: CallAttempt, status: str, reason: str | None = None):
    call.status, call.ended_at, call.failure_reason = status, now(), reason
    log(db, task, "agent", "call_ended", call_id=call.id, status=status, reason=reason)


def _end(db, task, user, call, say_reason, state, detail, call_status):
    _agent_says(db, task, user, call, {"type": "close", "reason": say_reason})
    _finish_call(db, task, call, call_status)
    _summarize(db, task, user)
    task.next_action = "Review the result" if state != "WAITING_FOLLOW_UP" else "Wait for the follow-up time"
    transition(db, task, state, "agent", detail)
    if state == "WAITING_FOLLOW_UP":
        _schedule_follow_up(db, task)
    notify(db, task, "call_ended", "Call ended — " + detail, task.result_summary or detail)


def _complete(db, task, user, call):
    _agent_says(db, task, user, call, {"type": "close", "reason": "we have what we needed"})
    _finish_call(db, task, call, "completed")
    _summarize(db, task, user)
    if task.follow_up_at and task.follow_up_at > now():
        task.next_action = "Wait for the follow-up time"
        transition(db, task, "WAITING_FOLLOW_UP", "agent", f"Follow-up recorded: {task.follow_up_reason}")
        _schedule_follow_up(db, task)
    else:
        task.next_action = None
        transition(db, task, "COMPLETED", "agent")
    notify(db, task, "answer_received", "Result ready", task.result_summary)


def _schedule_follow_up(db, task):
    if task.follow_up_at:
        _schedule(db, task, "follow_up_due", task.follow_up_at,
                  f"{task.id}:follow_up:{task.follow_up_at.isoformat()}")


SUMMARY_SYSTEM = """Summarize a phone task's outcome for the requester in %s. 2-5 short sentences.
Clearly separate confirmed facts, estimates (say they are approximate) and anything unanswered.
Never claim more than the facts show. Mention a follow-up time if given. This was a SIMULATED call —
say so in one short clause. Return ONLY JSON: {"summary": "..."}"""


def _summarize(db: DB, task: Task, user: User):
    try:
        task.result_summary = evon.ask_json(SUMMARY_SYSTEM % lang_name(task.requester_language) + gender_rule(task.requester_language), json.dumps({
            "goal": task.goal, "facts": _facts(task), "follow_up": task.follow_up_reason,
            "follow_up_at_local": task.follow_up_at.astimezone(ZoneInfo(user.timezone)).isoformat(timespec="minutes")
            if task.follow_up_at else None,
        }, ensure_ascii=False), Summary).summary
    except ProviderError:
        task.result_summary = fallback_summary(task)


def fallback_summary(task: Task) -> str:
    """Deterministic English summary used when the model is unavailable."""
    parts = ["[Simulated call] "]
    for i in task.required_info:
        label = {"confirmed": "Confirmed", "estimate": "Estimate", "refused": "Refused",
                 "unanswered": "Not answered", "uncertain": "Unclear"}.get(i["status"], "Not asked yet")
        parts.append(f"{i['description']}: {label}" + (f" — {i['value']}" if i.get("value") else "") + ". ")
    return "".join(parts).strip()


# ---------------------------------------------------------------- call outcomes & recovery

def call_outcome(db: DB, task: Task, user: User, call_id: str, outcome: str):
    """Telephony status event: no_answer | busy | rejected | disconnected | failed."""
    call = active_call(task)
    if not call or call.id != call_id:
        raise EngineError("No active call with that id")
    if outcome not in {"no_answer", "busy", "rejected", "disconnected", "failed"}:
        raise EngineError("Unknown outcome", 422)
    _finish_call(db, task, call, outcome)
    failures = _consecutive_failures(task)
    label = outcome.replace("_", " ")
    if failures >= config.MAX_CALL_ATTEMPTS:
        task.next_action = "Take over or cancel"
        transition(db, task, "NEEDS_HUMAN_HELP", "agent", f"Call {label}. Gave up after {failures} attempts.")
        notify(db, task, "call_failed", f"Couldn't reach {task.contact_name or 'the contact'}", task.status_detail)
        return
    # Progress is kept: answered items stay resolved; the next call resumes from the checkpoint.
    transition(db, task, "WAITING_RETRY", "agent", f"Call {label} (attempt {failures} of {config.MAX_CALL_ATTEMPTS}).")
    if user.allow_auto_redial and not user.approve_every_call:
        delay = config.REDIAL_BACKOFF_SECONDS[min(failures - 1, len(config.REDIAL_BACKOFF_SECONDS) - 1)]
        _schedule(db, task, "redial", now() + timedelta(seconds=delay), f"{task.id}:redial:{len(task.calls)}")
        task.next_action = f"Automatic retry in {delay // 60 or 1} min"
    else:
        task.next_action = "Retry the call when you're ready"
    notify(db, task, "call_failed", f"Call {label}", f"{task.status_detail} {task.next_action}.")


def retry(db: DB, task: Task, user: User, kind: str = "retry"):
    if task.state not in {"WAITING_RETRY"}:
        raise EngineError(f"Task is {task.state}; nothing to retry")
    cancel_jobs(db, task)
    request_call(db, task, user, kind=kind)


def follow_up_decision(db: DB, task: Task, user: User, decision: str):
    if task.state != "WAITING_FOLLOW_UP":
        raise EngineError("No follow-up is pending")
    cancel_jobs(db, task)
    log(db, task, user.id, "follow_up_decision", decision=decision)
    if decision == "dismiss":
        task.next_action = None
        transition(db, task, "COMPLETED", user.id, "Follow-up dismissed.")
        return
    if decision != "call":
        raise EngineError("decision must be call or dismiss", 422)
    _add_follow_up_item(task)
    task.follow_up_authorized = True  # this explicit choice is the authorization
    request_call(db, task, user, kind="follow_up")


def _add_follow_up_item(task: Task):
    key = f"follow_up_{sum(i['key'].startswith('follow_up') for i in task.required_info) + 1}"
    task.required_info = [*task.required_info, {
        "key": key, "description": f"Current status of: {task.follow_up_reason or 'the earlier request'}",
        "status": "unknown", "value": None, "evidence": None, "clarifications": 0}]
    task.follow_up_at = None


def run_due_follow_up(db: DB, task: Task, user: User):
    """Worker: follow-up time arrived. Call only if pre-authorized and policy allows; else ask."""
    if task.state != "WAITING_FOLLOW_UP":
        return
    if task.follow_up_authorized and not approval_reasons(task, user, "follow_up") \
            and not in_quiet_hours(user):
        _add_follow_up_item(task)
        return request_call(db, task, user, kind="follow_up")
    task.next_action = "Decide whether to follow up"
    task.checkpoint = {**task.checkpoint, "follow_up_due": True}
    notify(db, task, "follow_up_due", "Follow-up time",
           f"{task.follow_up_reason or 'A follow-up is due'}. Should the agent call again?")


def cancel(db: DB, task: Task, user: User):
    if task.state in TERMINAL:
        raise EngineError(f"Task is already {task.state}")
    _stop_activity(db, task, user, "Requester cancelled the task")
    task.next_action = None
    transition(db, task, "CANCELLED", user.id)


def take_over(db: DB, task: Task, user: User):
    if task.state in TERMINAL:
        raise EngineError(f"Task is already {task.state}")
    _stop_activity(db, task, user, "The requester will handle this personally")
    task.manual_takeover = True
    _summarize(db, task, user)
    task.next_action = "You're handling this yourself"
    transition(db, task, "NEEDS_HUMAN_HELP", user.id, "You took over this task. The agent won't act until you resume it.")


def _stop_activity(db, task, user, reason):
    call = active_call(task)
    if call:
        try:
            _agent_says(db, task, user, call, {"type": "close", "reason": reason})
        except ProviderError:
            pass  # hanging up matters more than a polite goodbye
        _finish_call(db, task, call, "ended", reason)
    cancel_jobs(db, task)
    _supersede_pending(task)
    task.pending_commitment = None


def mark_resolved(db: DB, task: Task, user: User):
    if task.state != "NEEDS_HUMAN_HELP":
        raise EngineError("Only tasks needing help can be marked resolved")
    transition(db, task, "COMPLETED", user.id, "Marked resolved by you.")
