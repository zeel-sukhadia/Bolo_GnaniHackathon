"""Database models. Task state lives here, never only in model context."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        TypeDecorator, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Store UTC; SQLite drops tzinfo, so reattach it on read to keep comparisons aware."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, v, dialect):
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v is not None else None

    def process_result_value(self, v, dialect):
        return v.replace(tzinfo=timezone.utc) if v is not None else None


TS = UTCDateTime


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100), default="")
    language: Mapped[str | None] = mapped_column(String(10))  # requester language, e.g. gu-IN
    approve_every_call: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_auto_redial: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    quiet_start_hour: Mapped[int | None] = mapped_column(Integer)  # local hour, calls deferred
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(TS)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20))
    language: Mapped[str | None] = mapped_column(String(10))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)  # recipient asked not to be called
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_request: Mapped[str] = mapped_column(Text)
    context_notes: Mapped[list] = mapped_column(JSON, default=list)  # extra requester answers/edits
    goal: Mapped[str | None] = mapped_column(Text)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    contact_status: Mapped[str] = mapped_column(String(20), default="missing")  # missing|unconfirmed|ambiguous|confirmed
    requester_language: Mapped[str] = mapped_column(String(10))
    recipient_language: Mapped[str | None] = mapped_column(String(10))
    recipient_language_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    required_info: Mapped[list] = mapped_column(JSON, default=list)
    missing_inputs: Mapped[list] = mapped_column(JSON, default=list)  # questions for the requester
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["financial", "sensitive"]
    pending_commitment: Mapped[str | None] = mapped_column(Text)  # requested mid-call, needs approval
    approved_commitments: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(24), default="NEW", index=True)
    next_action: Mapped[str | None] = mapped_column(String(200))
    status_detail: Mapped[str | None] = mapped_column(Text)  # why paused/failed, human-readable
    result_summary: Mapped[str | None] = mapped_column(Text)  # in requester language
    follow_up_at: Mapped[datetime | None] = mapped_column(TS)
    follow_up_reason: Mapped[str | None] = mapped_column(Text)
    follow_up_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    manual_takeover: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=now, onupdate=now)

    turns: Mapped[list["Turn"]] = relationship(order_by="Turn.seq", cascade="all, delete-orphan")
    calls: Mapped[list["CallAttempt"]] = relationship(order_by="CallAttempt.created_at", cascade="all, delete-orphan")
    approvals: Mapped[list["Approval"]] = relationship(order_by="Approval.created_at", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(order_by="Event.created_at", cascade="all, delete-orphan")

    # Optimistic locking: concurrent writers on the same task fail instead of clobbering.
    __mapper_args__ = {"version_id_col": version}


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    mode: Mapped[str] = mapped_column(String(20))  # "simulated" — never presented as a real call
    approval_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    # in_progress | completed | no_answer | busy | rejected | disconnected | failed | ended
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(TS)


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(10))  # agent | recipient
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(10))
    intent: Mapped[str | None] = mapped_column(String(20))  # opening|question|clarify|verify|close
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(30))  # "place_call"
    action_hash: Mapped[str] = mapped_column(String(64))  # binds approval to exact action content
    payload: Mapped[dict] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|superseded|used
    decided_by: Mapped[str | None] = mapped_column(String(32))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Event(Base):
    """Audit trail for state transitions, approvals and calls."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(40))  # user id | "agent" | "worker" | "recipient"
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # Web Push delivered by worker
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Job(Base):
    """Durable scheduled work, run by app.worker. dedupe_key prevents duplicate jobs."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # follow_up_due | redial
    run_at: Mapped[datetime] = mapped_column(TS, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|done|cancelled|failed
    dedupe_key: Mapped[str] = mapped_column(String(120), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))


def make_engine(url: str = config.DATABASE_URL):
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    eng = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
    return eng


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db(eng=engine):
    Base.metadata.create_all(eng)
