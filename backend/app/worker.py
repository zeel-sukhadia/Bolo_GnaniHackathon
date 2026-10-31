"""Durable background worker: scheduled follow-ups, redials, Web Push delivery, retention.

Run as a separate process:  python -m app.worker
All work comes from the database, so nothing is lost if this process or the API restarts.
"""
import json
import logging
import sys
import time
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DB

from . import config, engine
from .db import Job, Notification, PushSubscription, SessionLocal, Task, User, init_db, now
from .gnani import ProviderError

log = logging.getLogger("worker")


def claim_due_jobs(db: DB) -> list[Job]:
    due = db.scalars(select(Job.id).where(Job.status == "pending", Job.run_at <= now())).all()
    claimed = []
    for job_id in due:
        # Atomic claim: only one worker can flip pending -> running.
        if db.execute(update(Job).where(Job.id == job_id, Job.status == "pending")
                      .values(status="running")).rowcount == 1:
            claimed.append(job_id)
    db.commit()
    return [db.get(Job, j) for j in claimed]


def run_job(db: DB, job: Job):
    task = db.get(Task, job.task_id)
    user = db.get(User, task.user_id) if task else None
    if not task or task.manual_takeover:
        job.status = "cancelled"
        return
    if job.kind == "redial":
        if task.state != "WAITING_RETRY":
            job.status = "cancelled"
            return
        if engine.in_quiet_hours(user):
            job.status, job.run_at = "pending", now() + timedelta(minutes=30)  # defer, don't drop
            return
        job.status = "done"  # before acting, so a crash can't redial twice
        engine.request_call(db, task, user, kind="redial")
    elif job.kind == "follow_up_due":
        job.status = "done"
        engine.run_due_follow_up(db, task, user)


def process_jobs(db: DB):
    for job in claim_due_jobs(db):
        try:
            run_job(db, job)
            db.commit()
        except (engine.EngineError, ProviderError) as e:
            db.rollback()
            job = db.get(Job, job.id)
            job.attempts += 1
            job.last_error = str(e)[:500]
            retryable = isinstance(e, ProviderError) and e.retryable and job.attempts < 3
            job.status = "pending" if retryable else "failed"
            job.run_at = now() + timedelta(minutes=2 * job.attempts)
            task = db.get(Task, job.task_id)
            if not retryable and task:
                engine.notify(db, task, "needs_help", "Scheduled action failed", str(e)[:300])
            db.commit()
            log.warning("job %s failed: %s", job.id, e)


def recover_stuck_planning(db: DB):
    """API died mid-plan? Re-run planning. Optimistic locking makes a concurrent run fail safely."""
    stale = now() - timedelta(minutes=5)
    for task in db.scalars(select(Task).where(Task.state == "PLANNING", Task.updated_at < stale)).all():
        try:
            engine.run_planning(db, task, db.get(User, task.user_id))
            db.commit()
        except Exception:
            db.rollback()
            log.exception("re-planning %s failed", task.id)


def push_notifications(db: DB):
    pending = db.scalars(select(Notification).where(Notification.pushed.is_(False)).limit(50)).all()
    if not pending:
        return
    if not (config.VAPID_PUBLIC_KEY and config.VAPID_PRIVATE_KEY):
        for n in pending:
            n.pushed = True  # in-app only; nothing to deliver
        db.commit()
        return
    from pywebpush import WebPushException, webpush
    for n in pending:
        for sub in db.scalars(select(PushSubscription).where(PushSubscription.user_id == n.user_id)):
            try:
                webpush({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                        json.dumps({"title": n.title, "body": n.body[:300], "task_id": n.task_id}),
                        vapid_private_key=config.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": config.VAPID_SUBJECT}, timeout=10)
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    db.delete(sub)  # subscription expired
                else:
                    log.warning("push failed for notification %s", n.id)
        n.pushed = True
    db.commit()


def purge_expired(db: DB):
    cutoff = now() - timedelta(days=config.RETENTION_DAYS)
    db.execute(delete(Task).where(Task.state.in_(engine.TERMINAL), Task.updated_at < cutoff))
    db.execute(delete(Notification).where(Notification.created_at < cutoff))
    db.commit()


def gen_vapid():
    """Print a fresh VAPID key pair for .env (raw base64url, as browsers and pywebpush expect)."""
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = ec.generate_private_key(ec.SECP256R1())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    print("VAPID_PRIVATE_KEY=" + b64(key.private_numbers().private_value.to_bytes(32, "big")))
    print("VAPID_PUBLIC_KEY=" + b64(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)))


def main():
    if "--gen-vapid" in sys.argv:
        return gen_vapid()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    init_db()
    log.info("worker started")
    last_purge = 0.0
    while True:
        with SessionLocal() as db:
            try:
                process_jobs(db)
                recover_stuck_planning(db)
                push_notifications(db)
                if time.time() - last_purge > 3600:
                    purge_expired(db)
                    last_purge = time.time()
            except Exception:
                log.exception("worker loop error")  # keep running; state is in the DB
        if "--once" in sys.argv:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
