"""HTTP API. Every task/contact route is scoped to the authenticated user."""
import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DB
from sqlalchemy.orm.exc import StaleDataError

from . import claude_llm, config, engine, evon, gnani
from .db import (Approval, Contact, Notification, PushSubscription, Session, SessionLocal, Task, User,
                 init_db, now)
from .gnani import LANGUAGES, ProviderError

log = logging.getLogger("api")
app = FastAPI(title="Multilingual AI Calling Agent")
init_db()
if config.EVON_MODE == "mock":
    log.warning("EVON_MODE=mock: Evon output is SIMULATED; no model is called")
if config.EVON_MODE == "anthropic":
    log.warning("EVON_MODE=anthropic: reasoning uses Claude (%s) as a stand-in, not Evon", config.CLAUDE_MODEL)
for w in ([] if config.EVON_MODE != "remote" or not config.EVON_BASE_URL else evon.client().warnings()):
    log.warning("Evon config: %s", w)

COOKIE = "session"


def get_db():
    with SessionLocal() as db:
        yield db


@app.exception_handler(engine.EngineError)
def _engine_error(_, e: engine.EngineError):
    return JSONResponse({"detail": e.message}, status_code=e.code)


@app.exception_handler(ProviderError)
def _provider_error(_, e: ProviderError):
    log.warning("provider error: %s", e)
    return JSONResponse({"detail": f"{e.provider} unavailable: {e.message}", "retryable": e.retryable},
                        status_code=503)


@app.exception_handler(StaleDataError)
def _stale(_, e):
    return JSONResponse({"detail": "The task changed at the same time; refresh and try again."}, status_code=409)


# ---------------------------------------------------------------- auth

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def check_password(pw: str, stored: str) -> bool:
    salt, digest = stored.split(":")
    calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()
    return hmac.compare_digest(calc, digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, db: DB = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            token = auth[7:].strip()
    s = db.get(Session, _token_hash(token)) if token else None
    if not s or s.expires_at < now():
        raise HTTPException(401, "Not signed in")
    return db.get(User, s.user_id)


def _start_session(db: DB, user: User, response: Response) -> str:
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=_token_hash(token), user_id=user.id,
                   expires_at=now() + timedelta(days=config.SESSION_DAYS)))
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                        max_age=config.SESSION_DAYS * 86400, path="/")
    return token


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=100)


@app.post("/api/auth/register")
def register(body: Credentials, response: Response, db: DB = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    user = User(email=email, password_hash=hash_password(body.password), display_name=body.display_name.strip())
    db.add(user)
    db.flush()
    token = _start_session(db, user, response)
    out = me_out(user)
    out["token"] = token
    return out


@app.post("/api/auth/login")
def login(body: Credentials, response: Response, db: DB = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not check_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong email or password")
    token = _start_session(db, user, response)
    out = me_out(user)
    out["token"] = token
    return out


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: DB = Depends(get_db)):
    token = request.cookies.get(COOKIE)
    if not token:
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            token = auth[7:].strip()
    if token:
        db.execute(delete(Session).where(Session.token_hash == _token_hash(token)))
        db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- preferences

def me_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "language": u.language,
            "approve_every_call": u.approve_every_call, "allow_auto_redial": u.allow_auto_redial,
            "timezone": u.timezone, "quiet_start_hour": u.quiet_start_hour, "quiet_end_hour": u.quiet_end_hour}


class Prefs(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    language: str | None = None
    approve_every_call: bool | None = None
    allow_auto_redial: bool | None = None
    timezone: str | None = None
    quiet_start_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_end_hour: int | None = Field(default=None, ge=0, le=23)


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return me_out(user)


@app.patch("/api/me")
def update_me(body: Prefs, user: User = Depends(current_user), db: DB = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if data.get("language") and data["language"] not in LANGUAGES:
        raise HTTPException(422, "Unsupported language")
    if data.get("timezone"):
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Unknown timezone")
    for k, v in data.items():
        setattr(user, k, v)
    db.commit()
    return me_out(user)


@app.get("/api/meta")
def meta():
    return {"languages": LANGUAGES, "telephony_mode": config.TELEPHONY_MODE,
            "vapid_public_key": config.VAPID_PUBLIC_KEY or None,
            "integrations": {"prisma_stt": bool(config.GNANI_API_KEY), "timbre_tts": bool(config.GNANI_API_KEY),
                             "evon_llm": {"mock": True, "anthropic": bool(config.ANTHROPIC_API_KEY)}.get(
                                 config.EVON_MODE, bool(config.EVON_BASE_URL))},
            "evon_mode": config.EVON_MODE, "llm_model": config.CLAUDE_MODEL if config.EVON_MODE == "anthropic" else None}


@app.get("/api/evon/health")
def evon_health(user: User = Depends(current_user)):
    """Live check of the remote Evon server (never exposes the URL's key or full address)."""
    return evon.health()


# ---------------------------------------------------------------- contacts

class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str
    language: str | None = None


def contact_out(c: Contact) -> dict:
    return {"id": c.id, "name": c.name, "phone": c.phone, "language": c.language, "opted_out": c.opted_out}


def own_contact(db: DB, user: User, cid: str) -> Contact:
    c = db.get(Contact, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Contact not found")
    return c


@app.get("/api/contacts")
def list_contacts(user: User = Depends(current_user), db: DB = Depends(get_db)):
    return [contact_out(c) for c in db.scalars(select(Contact).where(Contact.user_id == user.id).order_by(Contact.name))]


@app.post("/api/contacts")
def add_contact(body: ContactIn, user: User = Depends(current_user), db: DB = Depends(get_db)):
    phone = engine.normalize_phone(body.phone)
    if not phone:
        raise HTTPException(422, "Phone number must be exactly 10 digits")
    if body.language and body.language not in LANGUAGES:
        raise HTTPException(422, "Unsupported language")
    c = Contact(user_id=user.id, name=body.name.strip(), phone=phone, language=body.language)
    db.add(c)
    db.commit()
    return contact_out(c)


@app.delete("/api/contacts/{cid}")
def delete_contact(cid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    db.delete(own_contact(db, user, cid))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- tasks

def own_task(db: DB, user: User, tid: str) -> Task:
    t = db.get(Task, tid)
    if not t or t.user_id != user.id:
        raise HTTPException(404, "Task not found")
    return t


def task_out(t: Task, full: bool = True) -> dict:
    out = {"id": t.id, "state": t.state, "goal": t.goal, "original_request": t.original_request,
           "goal_for_requester": t.checkpoint.get("goal_for_requester"),
           "contact": {"name": t.contact_name, "phone": t.contact_phone, "status": t.contact_status,
                       "id": t.contact_id},
           "requester_language": t.requester_language, "recipient_language": t.recipient_language,
           "next_action": t.next_action, "status_detail": t.status_detail, "result_summary": t.result_summary,
           "follow_up_at": t.follow_up_at, "follow_up_reason": t.follow_up_reason,
           "follow_up_authorized": t.follow_up_authorized, "follow_up_due": bool(t.checkpoint.get("follow_up_due")),
           "manual_takeover": t.manual_takeover, "created_at": t.created_at, "updated_at": t.updated_at}
    if not full:
        return out
    active = engine.active_call(t)
    return {**out,
            "required_info": [{k: i.get(k) for k in ("key", "description", "status", "value", "evidence")}
                              for i in t.required_info],
            "missing_inputs": t.missing_inputs, "risk_flags": t.risk_flags,
            "pending_turn": bool(t.checkpoint.get("pending_turn")),
            "active_call": {"id": active.id, "mode": active.mode} if active else None,
            "calls": [{"id": c.id, "mode": c.mode, "status": c.status, "created_at": c.created_at,
                       "ended_at": c.ended_at, "failure_reason": c.failure_reason} for c in t.calls],
            "turns": [{"id": x.id, "call_id": x.call_id, "speaker": x.speaker, "text": x.text,
                       "language": x.language, "intent": x.intent, "created_at": x.created_at} for x in t.turns],
            "approvals": [{"id": a.id, "status": a.status, "payload": a.payload, "reasons": a.reasons,
                           "decided_at": a.decided_at, "created_at": a.created_at} for a in t.approvals],
            "events": [{"actor": e.actor, "kind": e.kind, "detail": e.detail, "at": e.created_at}
                       for e in t.events[-40:]]}


class TaskIn(BaseModel):
    request: str = Field(min_length=5, max_length=3000)
    contact_id: str | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = None
    recipient_language: str | None = None
    requester_language: str | None = None  # defaults to the account language



def _trigger_planning(db: DB, task: Task, user: User, bg: BackgroundTasks | None = None):
    if config.SYNC_PLANNING:
        try:
            engine.run_planning(db, task, user)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("planning failed for %s", task.id)
    elif bg:
        _trigger_planning(db, task, user, bg)

def _plan_in_background(task_id: str):
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        try:
            engine.run_planning(db, task, db.get(User, task.user_id))
            db.commit()
        except Exception:
            db.rollback()
            log.exception("planning failed for %s", task_id)  # worker retries stuck PLANNING tasks


@app.get("/api/tasks")
def list_tasks(user: User = Depends(current_user), db: DB = Depends(get_db)):
    tasks = db.scalars(select(Task).where(Task.user_id == user.id).order_by(Task.updated_at.desc()).limit(100))
    return [task_out(t, full=False) for t in tasks]


@app.post("/api/tasks")
def create_task(body: TaskIn, bg: BackgroundTasks, user: User = Depends(current_user), db: DB = Depends(get_db)):
    requester_language = body.requester_language or user.language
    if not requester_language:
        raise HTTPException(422, "Choose your preferred language first")
    if requester_language not in LANGUAGES:
        raise HTTPException(422, "Unsupported language")
    if body.recipient_language and body.recipient_language not in LANGUAGES:
        raise HTTPException(422, "Unsupported recipient language")
    if body.contact_phone and not engine.normalize_phone(body.contact_phone):
        raise HTTPException(422, "Phone number must be exactly 10 digits")
    task = Task(user_id=user.id, original_request=body.request.strip(), requester_language=requester_language,
                recipient_language=body.recipient_language,
                recipient_language_confirmed=bool(body.recipient_language), context_notes=[],
                required_info=[], missing_inputs=[], risk_flags=[], approved_commitments=[], checkpoint={})
    db.add(task)
    db.flush()
    engine.log(db, task, user.id, "created")
    if body.contact_id or body.contact_phone or body.contact_name:
        engine.resolve_contact(db, task, body.contact_name, body.contact_phone, body.contact_id)
    engine.begin_planning(db, task)
    db.commit()
    _trigger_planning(db, task, user, bg)
    return task_out(task)


@app.get("/api/tasks/{tid}")
def get_task(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    return task_out(own_task(db, user, tid))


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    if engine.active_call(task):
        raise HTTPException(409, "Stop the call before deleting the task")
    db.delete(task)  # cascades turns, calls, approvals, events, jobs
    db.commit()
    return {"ok": True}


class InputsIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    contact_id: str | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = None
    recipient_language: str | None = None


@app.post("/api/tasks/{tid}/inputs")
def task_inputs(tid: str, body: InputsIn, bg: BackgroundTasks, user: User = Depends(current_user),
                db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    if body.contact_id:
        own_contact(db, user, body.contact_id)
    engine.provide_inputs(db, task, user, body.note, body.contact_id, body.contact_name,
                          body.contact_phone, body.recipient_language)
    db.commit()
    _trigger_planning(db, task, user, bg)
    return task_out(task)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject", "edit"]
    instruction: str | None = Field(default=None, max_length=1000)
    contact_phone: str | None = None
    contact_id: str | None = None


@app.post("/api/tasks/{tid}/approvals/{aid}")
def decide(tid: str, aid: str, body: DecisionIn, bg: BackgroundTasks, user: User = Depends(current_user),
           db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    approval = db.get(Approval, aid)
    if not approval or approval.task_id != task.id:
        raise HTTPException(404, "Approval not found")
    if body.contact_id:
        own_contact(db, user, body.contact_id)
    engine.decide_approval(db, task, user, approval, body.decision, body.instruction,
                           body.contact_phone, body.contact_id)
    db.commit()
    if task.state == "PLANNING":
        _trigger_planning(db, task, user, bg)
    return task_out(task)


@app.post("/api/tasks/{tid}/retry")
def retry(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.retry(db, task, user)
    db.commit()
    return task_out(task)


class FollowUpIn(BaseModel):
    decision: Literal["call", "dismiss"]


@app.post("/api/tasks/{tid}/follow-up")
def follow_up(tid: str, body: FollowUpIn, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.follow_up_decision(db, task, user, body.decision)
    db.commit()
    return task_out(task)


class AuthorizeIn(BaseModel):
    authorized: bool


@app.post("/api/tasks/{tid}/follow-up-authorization")
def authorize_follow_up(tid: str, body: AuthorizeIn, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    if task.state != "WAITING_FOLLOW_UP":
        raise HTTPException(409, "No follow-up is scheduled")
    task.follow_up_authorized = body.authorized
    engine.log(db, task, user.id, "follow_up_authorization", authorized=body.authorized)
    db.commit()
    return task_out(task)


@app.post("/api/tasks/{tid}/cancel")
def cancel(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.cancel(db, task, user)
    db.commit()
    return task_out(task)


@app.post("/api/tasks/{tid}/takeover")
def takeover(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.take_over(db, task, user)
    db.commit()
    return task_out(task)


@app.post("/api/tasks/{tid}/resolve")
def resolve(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.mark_resolved(db, task, user)
    db.commit()
    return task_out(task)


# ---------------------------------------------------------------- simulated call console
# The recipient's side is typed/spoken by a person at the console. Clearly labelled; never a real call.

class SimReplyIn(BaseModel):
    call_id: str
    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=100)


@app.post("/api/tasks/{tid}/sim/reply")
def sim_reply(tid: str, body: SimReplyIn, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.recipient_says(db, task, user, body.call_id, body.text, body.idempotency_key)
    db.commit()
    return task_out(task)


class SimOutcomeIn(BaseModel):
    call_id: str
    outcome: Literal["no_answer", "busy", "rejected", "disconnected", "failed"]


@app.post("/api/tasks/{tid}/sim/outcome")
def sim_outcome(tid: str, body: SimOutcomeIn, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    engine.call_outcome(db, task, user, body.call_id, body.outcome)
    db.commit()
    return task_out(task)


# ---------------------------------------------------------------- demo page (screen-recording view)
# Claude plays the person being called so the demo runs hands-free. The agent side is the real engine.

CONTACT_SYSTEM = """You are role-playing the person who answers a phone call, for a product demo. The
caller is an AI assistant. Speak ONLY %s, naturally, 1-2 short spoken sentences, no markdown.
Never switch to another language or mix in English, even if the assistant does.
Be cooperative and polite. Infer who you are from the goal (e.g. a bus conductor, a shop owner).
The FIRST time the assistant asks for the key information, answer vaguely (e.g. "it's nearby",
"soon", "almost there") without any number or specific detail. When it asks again more specifically,
give a specific, plausible answer (e.g. "about 15 minutes"). If it reads facts back to you, confirm
them. If it says goodbye, say goodbye briefly.
Return ONLY JSON: {"text": "<your reply in %s>", "english": "<English translation>"}"""


class ContactLine(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    english: str = Field(min_length=1, max_length=600)


@app.post("/api/tasks/{tid}/demo/contact-reply")
def demo_contact_reply(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    """The simulated contact's next line (not stored; the client sends it through /sim/reply)."""
    task = own_task(db, user, tid)
    call = engine.active_call(task)
    if not call:
        raise HTTPException(409, "No active call")
    lang = engine.lang_name(task.recipient_language)
    voice = gnani.CONTACT_VOICES.get(task.recipient_language or "en-IN")
    line = claude_llm.ask_json(CONTACT_SYSTEM % (lang, lang) + gnani.gender_rule(task.recipient_language, voice), json.dumps({
        "goal": task.goal, "context": task.checkpoint.get("context_for_call", ""),
        "conversation": engine._transcript(task, call.id)}, ensure_ascii=False), ContactLine)
    try:
        lang_code = task.recipient_language or "en-IN"
        audio = base64.b64encode(gnani.synthesize(line.text, lang_code, voice)).decode()
    except ProviderError as e:
        log.warning("demo contact audio failed: %s", e)
        audio = None  # the demo continues with text only
    return {"text": line.text, "english": line.english, "audio_mp3_b64": audio}


class TranslateIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=10)


class Translations(BaseModel):
    english: list[str]


@app.post("/api/demo/translate")
def demo_translate(body: TranslateIn, user: User = Depends(current_user)):
    """English subtitles for the demo screen. Text only, never spoken."""
    texts = [t[:2000] for t in body.texts]
    out = claude_llm.ask_json("Translate each item into natural English. Keep order and count. "
                              'Return ONLY JSON: {"english": ["..."]}', json.dumps(texts, ensure_ascii=False),
                              Translations).english
    if len(out) != len(texts):
        raise HTTPException(502, "Translation returned the wrong number of items")
    return {"english": out}


# ---------------------------------------------------------------- speech (Prisma / Timbre)

@app.post("/api/stt")
async def stt(audio: UploadFile = File(...), language: str = Form(...), user: User = Depends(current_user)):
    data = await audio.read()
    if not data:
        raise HTTPException(422, "No audio received")
    if len(data) > 5_000_000:
        raise HTTPException(413, "Recording too long (30 s max)")
    text = gnani.transcribe(data, audio.filename or "speech.wav", language)
    return {"transcript": text, "empty": not text}


@app.get("/api/tasks/{tid}/turns/{turn_id}/audio")
def turn_audio(tid: str, turn_id: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    turn = next((x for x in task.turns if x.id == turn_id), None)
    if not turn:
        raise HTTPException(404, "Turn not found")
    return Response(gnani.synthesize(turn.text, turn.language or task.recipient_language), media_type="audio/mpeg")


@app.get("/api/tasks/{tid}/summary/audio")
def summary_audio(tid: str, user: User = Depends(current_user), db: DB = Depends(get_db)):
    task = own_task(db, user, tid)
    if not task.result_summary:
        raise HTTPException(404, "No summary yet")
    return Response(gnani.synthesize(task.result_summary, task.requester_language), media_type="audio/mpeg")


# ---------------------------------------------------------------- notifications & push

@app.get("/api/notifications")
def notifications(user: User = Depends(current_user), db: DB = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id)
                      .order_by(Notification.created_at.desc()).limit(50))
    return [{"id": n.id, "task_id": n.task_id, "kind": n.kind, "title": n.title, "body": n.body,
             "read": n.read, "created_at": n.created_at} for n in rows]


@app.post("/api/notifications/read")
def mark_read(user: User = Depends(current_user), db: DB = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read.is_(False))):
        n.read = True
    db.commit()
    return {"ok": True}


class PushIn(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: dict[str, str]


@app.post("/api/push/subscribe")
def subscribe(body: PushIn, user: User = Depends(current_user), db: DB = Depends(get_db)):
    if not body.endpoint.startswith("https://") or not {"p256dh", "auth"} <= body.keys.keys():
        raise HTTPException(422, "Invalid subscription")
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub:
        sub.user_id, sub.p256dh, sub.auth = user.id, body.keys["p256dh"], body.keys["auth"]
    else:
        db.add(PushSubscription(user_id=user.id, endpoint=body.endpoint, p256dh=body.keys["p256dh"],
                                auth=body.keys["auth"]))
    db.commit()
    return {"ok": True}
