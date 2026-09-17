"""Background thread that sends due emails and checks inboxes."""
import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import or_, select

from .config import settings
from .db import SessionLocal
from .inbox import poll_mailbox
from .models import Mailbox, utcnow
from .scheduler import run_send_tick

log = logging.getLogger(__name__)


_inbox_lock = threading.Lock()


def check_inboxes(max_age_seconds: int | None = None) -> bool:
    """Poll mailboxes for replies and bounces, skipping any checked within max_age_seconds.

    Returns False if another check is already running.
    """
    if not _inbox_lock.acquire(blocking=False):
        return False
    try:
        with SessionLocal() as session:
            query = select(Mailbox.id)
            if max_age_seconds is not None:
                cutoff = utcnow() - timedelta(seconds=max_age_seconds)
                query = query.where(or_(Mailbox.last_inbox_check.is_(None),
                                        Mailbox.last_inbox_check <= cutoff))
            for mailbox_id in session.scalars(query).all():
                mailbox = session.get(Mailbox, mailbox_id)
                try:
                    poll_mailbox(session, mailbox)
                except Exception as exc:  # keep checking the other mailboxes
                    session.rollback()
                    log.warning("Inbox check failed for %s: %s", mailbox.email, exc)
                    mailbox = session.get(Mailbox, mailbox_id)
                    mailbox.last_error = f"Inbox check failed: {exc}"[:500]
                    mailbox.last_inbox_check = utcnow()
                    session.commit()
        return True
    finally:
        _inbox_lock.release()


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_inbox_check: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="outreach-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(settings.send_tick_seconds)

    def tick(self) -> None:
        try:
            with SessionLocal() as session:
                run_send_tick(session)
        except Exception:
            log.exception("Send tick failed")

        due = (self._last_inbox_check is None
               or time.monotonic() - self._last_inbox_check >= settings.inbox_tick_seconds)
        if due:
            self._last_inbox_check = time.monotonic()
            try:
                check_inboxes()
            except Exception:
                log.exception("Inbox check failed")


worker = Worker()
