"""Reads inboxes over IMAP to catch replies, bounces, auto-replies and unsubscribe requests."""
import email
import email.policy
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ACTIVE_STATUSES, Enrollment, InboxEvent, Lead, Mailbox, SentEmail, suppress, utcnow
from .security import decrypt

log = logging.getLogger(__name__)

MAX_PER_POLL = 200
TIMEOUT = 30
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

FREE_MAIL_DOMAINS = {
    "aol.com", "gmail.com", "gmx.com", "googlemail.com", "hotmail.com", "icloud.com", "live.com", "mail.com",
    "me.com", "msn.com", "outlook.com", "proton.me", "protonmail.com", "yahoo.com", "yandex.com", "zoho.com",
}
UNSUBSCRIBE_PHRASES = ("unsubscribe", "remove me", "take me off", "stop emailing", "stop contacting",
                       "do not contact", "don't contact", "dont contact", "opt out", "opt-out")
AUTO_REPLY_SUBJECTS = ("auto:", "automatic reply", "autoreply", "auto-reply", "out of office",
                       "out of the office", "away from the office", "on vacation", "on leave")
BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon")
BOUNCE_SUBJECTS = ("undeliverable", "delivery status notification", "mail delivery failed", "returned mail",
                   "delivery failure", "undelivered mail", "failure notice", "address not found")
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


@dataclass
class Classified:
    kind: str  # reply | auto_reply | bounce
    from_email: str
    subject: str
    text: str
    references: list[str] = field(default_factory=list)
    failed_recipients: list[str] = field(default_factory=list)
    hard_bounce: bool = True


def _body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = re.sub(r"<[^>]+>", " ", content)
    return content


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply."""
    text = re.split(r"\n\s*On\b[^\n]{0,200}(?:\n[^\n]{0,200})?wrote:\s*(?:\n|$)", "\n" + text, maxsplit=1)[0]
    text = re.split(r"\n\s*(?:-{2,}\s*Original Message\s*-{2,}|From:\s.+\nSent:\s)", text, maxsplit=1,
                    flags=re.IGNORECASE)[0]
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()


def wants_unsubscribe(subject: str, reply_text: str) -> bool:
    haystack = f"{subject}\n{reply_text[:600]}".lower()
    return any(phrase in haystack for phrase in UNSUBSCRIBE_PHRASES)


def _is_bounce(msg: EmailMessage, from_email: str, subject: str) -> bool:
    if msg.get("X-Failed-Recipients"):
        return True
    if (msg.get_content_type() == "multipart/report"
            and str(msg.get_param("report-type") or "").lower() == "delivery-status"):
        return True
    return from_email.split("@")[0] in BOUNCE_SENDERS and any(s in subject.lower() for s in BOUNCE_SUBJECTS)


def _bounce_details(msg: EmailMessage) -> tuple[list[str], bool, list[str]]:
    """Failed recipients, whether the failure is permanent, and Message-IDs of the original email."""
    recipients: list[str] = []
    statuses: list[str] = []
    original_ids: list[str] = []
    header = msg.get("X-Failed-Recipients")
    if header:
        recipients += [a.strip().lower() for a in str(header).split(",") if "@" in a]
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("message/delivery-status", "message/rfc822", "text/rfc822-headers"):
            continue
        try:
            raw = part.as_string()
        except Exception:  # malformed part; skip it
            continue
        if ctype == "message/delivery-status":
            recipients += [m.strip("<>").lower()
                           for m in re.findall(r"^Final-Recipient:\s*rfc822;\s*(\S+)", raw, re.I | re.M)]
            statuses += re.findall(r"^Status:\s*(\d)\.", raw, re.I | re.M)
        else:
            original_ids += re.findall(r"^Message-ID:\s*(<[^<>\s]+>)", raw, re.I | re.M)
    hard = not statuses or "5" in statuses
    return sorted(set(recipients)), hard, original_ids


def classify(msg: EmailMessage) -> Classified:
    from_email = parseaddr(str(msg.get("From", "")))[1].lower()
    subject = str(msg.get("Subject", "") or "")
    text = _body_text(msg)
    refs = MESSAGE_ID_RE.findall(f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")

    if _is_bounce(msg, from_email, subject):
        recipients, hard, original_ids = _bounce_details(msg)
        if "delay" in subject.lower():
            hard = False
        return Classified("bounce", from_email, subject, text, refs + original_ids, recipients, hard)

    auto_submitted = str(msg.get("Auto-Submitted", "")).strip().lower()
    precedence = str(msg.get("Precedence", "")).strip().lower()
    is_auto = ((auto_submitted and auto_submitted != "no") or bool(msg.get("X-Autoreply"))
               or bool(msg.get("X-Autorespond")) or precedence in ("auto_reply", "bulk", "junk")
               or subject.strip().lower().startswith(AUTO_REPLY_SUBJECTS))
    return Classified("auto_reply" if is_auto else "reply", from_email, subject, text, refs)


def _enrollment_for_ids(session: Session, message_ids: list[str]) -> Enrollment | None:
    if not message_ids:
        return None
    sent = session.scalar(select(SentEmail).where(
        SentEmail.message_id.in_(message_ids), SentEmail.enrollment_id.is_not(None),
    ).order_by(SentEmail.sent_at.desc()).limit(1))
    return session.get(Enrollment, sent.enrollment_id) if sent else None


def _enrollment_for_email(session: Session, mailbox: Mailbox, address: str) -> Enrollment | None:
    return session.scalar(select(Enrollment).join(Lead).where(Lead.email == address).order_by(
        (Enrollment.mailbox_id == mailbox.id).desc(), Enrollment.updated_at.desc()).limit(1))


def _stop_after_reply(session: Session, enrollment: Enrollment) -> None:
    lead, campaign = enrollment.lead, enrollment.campaign
    if enrollment.status in ACTIVE_STATUSES + ("completed", "stopped"):
        enrollment.status, enrollment.next_send_at, enrollment.note = "replied", None, "Lead replied"

    others = session.scalars(select(Enrollment).where(
        Enrollment.lead_id == lead.id, Enrollment.id != enrollment.id,
        Enrollment.status.in_(ACTIVE_STATUSES))).all()
    for other in others:
        other.status, other.next_send_at = "stopped", None
        other.note = f"Lead replied to campaign #{enrollment.campaign_id}"

    if campaign.stop_on_company_reply and lead.domain not in FREE_MAIL_DOMAINS:
        colleagues = session.scalars(select(Enrollment).join(Lead).where(
            Enrollment.campaign_id == campaign.id, Enrollment.id != enrollment.id,
            Enrollment.status.in_(ACTIVE_STATUSES), Lead.email.like(f"%@{lead.domain}"))).all()
        for other in colleagues:
            other.status, other.next_send_at = "stopped", None
            other.note = f"Colleague {lead.email} replied"


def _handle_bounce(session: Session, mailbox: Mailbox, c: Classified) -> bool:
    enrollment = _enrollment_for_ids(session, c.references)
    recipients = set(c.failed_recipients)
    if not recipients and enrollment:
        recipients.add(enrollment.lead.email)
    if not recipients:
        # Last resort: addresses in the bounce text that this mailbox actually emailed.
        for address in {a.lower() for a in EMAIL_RE.findall(c.text)}:
            if session.scalar(select(SentEmail.id).where(
                    SentEmail.to_email == address, SentEmail.mailbox_id == mailbox.id).limit(1)):
                recipients.add(address)
    if not recipients:
        return False

    label = "Hard bounce" if c.hard_bounce else "Temporary delivery problem"
    for address in sorted(recipients):
        target = enrollment if enrollment and enrollment.lead.email == address else \
            _enrollment_for_email(session, mailbox, address)
        session.add(InboxEvent(mailbox_id=mailbox.id, enrollment_id=target.id if target else None, kind="bounce",
                               from_email=address, subject=c.subject[:300],
                               snippet=f"{label}. {strip_quoted(c.text)[:600]}"))
        if c.hard_bounce:
            suppress(session, address, "bounced")
            if target and target.status == "completed":
                target.status, target.note = "bounced", "Bounced"
    return True


def handle_message(session: Session, mailbox: Mailbox, msg: EmailMessage) -> bool:
    """Apply one incoming email. Returns True if it concerned one of our leads."""
    c = classify(msg)
    if not c.from_email or c.from_email == mailbox.email.lower():
        return False
    if c.kind == "bounce":
        return _handle_bounce(session, mailbox, c)

    enrollment = _enrollment_for_ids(session, c.references) or _enrollment_for_email(session, mailbox, c.from_email)
    if enrollment is None:
        return False
    snippet = strip_quoted(c.text)[:1000]
    event = InboxEvent(mailbox_id=mailbox.id, enrollment_id=enrollment.id, kind=c.kind,
                       from_email=c.from_email, subject=c.subject[:300], snippet=snippet)
    session.add(event)
    if c.kind == "auto_reply":
        return True
    if wants_unsubscribe(c.subject, snippet):
        event.kind = "unsubscribe"
        suppress(session, enrollment.lead.email, "unsubscribed")
        if c.from_email != enrollment.lead.email:
            suppress(session, c.from_email, "unsubscribed")
        return True
    _stop_after_reply(session, enrollment)
    return True


def _imap_date(value: datetime) -> str:
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year}"  # IMAP needs English month names


def poll_mailbox(session: Session, mailbox: Mailbox) -> int:
    handled = 0
    with imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, ssl_context=ssl.create_default_context(),
                           timeout=TIMEOUT) as imap:
        imap.login(mailbox.username, decrypt(mailbox.password_enc))
        status, _ = imap.select("INBOX", readonly=True)  # read-only: never marks mail as read
        if status != "OK":
            raise RuntimeError("Could not open INBOX")
        _, values = imap.response("UIDVALIDITY")
        uidvalidity = int(values[0]) if values and values[0] else 0
        if uidvalidity != mailbox.imap_uidvalidity:
            mailbox.imap_uidvalidity, mailbox.imap_last_uid = uidvalidity, 0
            session.commit()

        if mailbox.imap_last_uid:
            _, data = imap.uid("SEARCH", None, "UID", f"{mailbox.imap_last_uid + 1}:*")
        else:  # first check: only mail since the mailbox was added
            _, data = imap.uid("SEARCH", None, "SINCE", _imap_date(mailbox.created_at - timedelta(days=1)))
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > mailbox.imap_last_uid)

        for uid in uids[:MAX_PER_POLL]:
            _, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if raw:
                try:
                    if handle_message(session, mailbox, email.message_from_bytes(raw, policy=email.policy.default)):
                        handled += 1
                except Exception:
                    log.exception("Could not process message %s in %s", uid, mailbox.email)
                    session.rollback()
            mailbox.imap_last_uid = uid
            session.commit()

    mailbox.last_inbox_check = utcnow()
    session.commit()
    return handled
