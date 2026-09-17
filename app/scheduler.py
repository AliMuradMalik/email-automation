"""Sending engine: which lead gets which email, from which mailbox, and when."""
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from . import mailer
from .config import settings
from .models import (ACTIVE_STATUSES, Campaign, Enrollment, Lead, Mailbox, SentEmail, Step, Suppression,
                     campaign_mailboxes, find_suppression, suppress, utcnow)
from .security import unsubscribe_url
from .templating import render

log = logging.getLogger(__name__)

RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS = 3
LIMIT_BACKOFF = timedelta(hours=6)
# Pause a mailbox when more than 5% of its recent first emails bounced (after at least 20 sends).
BOUNCE_PAUSE_RATE = 0.05
BOUNCE_SAMPLE = 50
BOUNCE_MIN_SENDS = 20
CLAIM_LEASE = timedelta(seconds=60)

_STATUS_FOR_REASON = {"unsubscribed": "unsubscribed", "bounced": "bounced"}


def _zone(mailbox: Mailbox) -> ZoneInfo:
    try:
        return ZoneInfo(mailbox.time_zone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_now(mailbox: Mailbox, now: datetime) -> datetime:
    return now.replace(tzinfo=timezone.utc).astimezone(_zone(mailbox))


def daily_cap(mailbox: Mailbox, today: date) -> int:
    """Ramp up slowly: warmup_start on the first day, +warmup_step per day, up to daily_limit."""
    if mailbox.first_send_date is None:
        return min(mailbox.warmup_start, mailbox.daily_limit)
    days = max(0, (today - mailbox.first_send_date).days)
    return min(mailbox.daily_limit, mailbox.warmup_start + mailbox.warmup_step * days)


def in_send_window(mailbox: Mailbox, now: datetime) -> bool:
    local = local_now(mailbox, now)
    return (local.weekday() in mailbox.send_day_set
            and mailbox.window_start_hour <= local.hour < mailbox.window_end_hour)


def sent_today(session: Session, mailbox: Mailbox, now: datetime) -> int:
    local_midnight = local_now(mailbox, now).replace(hour=0, minute=0, second=0, microsecond=0)
    since = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return session.scalar(select(func.count(SentEmail.id)).where(
        SentEmail.mailbox_id == mailbox.id, SentEmail.status == "sent", SentEmail.sent_at >= since)) or 0


def bounce_rate(session: Session, mailbox: Mailbox) -> tuple[int, float]:
    recent = session.scalars(select(SentEmail.enrollment_id).where(
        SentEmail.mailbox_id == mailbox.id, SentEmail.status == "sent", SentEmail.step_position == 1,
    ).order_by(SentEmail.sent_at.desc()).limit(BOUNCE_SAMPLE)).all()
    ids = [i for i in recent if i]
    if not ids:
        return len(recent), 0.0
    bounced = session.scalar(select(func.count(Enrollment.id)).where(
        Enrollment.id.in_(ids), Enrollment.status == "bounced")) or 0
    return len(recent), bounced / len(recent)


@dataclass
class Draft:
    subject: str
    body: str
    unsubscribe_url: str
    references: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def compose(campaign: Campaign, step: Step, lead_fields: dict[str, str], mailbox: Mailbox,
            thread_subject: str = "", thread_references: str = "") -> Draft:
    values = dict(lead_fields)
    link = unsubscribe_url(values.get("email", ""))
    values.update(sender_name=mailbox.from_name, sender_email=mailbox.email,
                  sender_address=campaign.sender_address, unsubscribe_url=link)

    body = render(step.body, values)
    signature = render(mailbox.signature, values)
    footer = render(campaign.footer, values)
    missing = body.missing + signature.missing + footer.missing
    text = "\n\n".join(part.text.strip() for part in (body, signature, footer) if part.text.strip())

    if step.subject.strip() or not thread_subject:
        subject = render(step.subject, values)
        missing += subject.missing
        return Draft(subject.text.strip(), text, link, [], missing)
    # Follow-up without its own subject: reply in the same thread.
    reply_subject = thread_subject if thread_subject.lower().startswith("re:") else f"Re: {thread_subject}"
    return Draft(reply_subject, text, link, thread_references.split(), missing)


def validate_campaign(campaign: Campaign) -> tuple[list[str], list[str]]:
    """Problems that block starting the campaign, and warnings that don't."""
    errors: list[str] = []
    warnings: list[str] = []
    if not campaign.steps:
        errors.append("Add at least one email step.")
    elif not campaign.steps[0].subject.strip():
        errors.append("The first email needs a subject.")
    if any(not s.body.strip() for s in campaign.steps):
        errors.append("Every step needs a body.")
    if not any(m.status == "active" for m in campaign.mailboxes):
        errors.append("Assign at least one active mailbox.")
    for domain in sorted({m.sending_domain for m in campaign.mailboxes if m.sending_domain}, key=lambda d: d.name):
        if domain.dns_checked_at is None:
            errors.append(f"Run the DNS check for {domain.name} on the Domains page.")
        elif not domain.dns_ok:
            errors.append(f"{domain.name} is missing DNS records (MX, SPF, DKIM or DMARC). "
                          "Fix them on the Domains page, then check again.")
    if "unsubscribe_url" not in campaign.footer:
        errors.append("The footer must contain {{unsubscribe_url}}.")
    if not campaign.sender_address.strip():
        errors.append("Add your postal address (the law requires it in commercial emails).")
    if not mailer.is_public_url(settings.public_base_url):
        warnings.append("PUBLIC_BASE_URL is localhost, so unsubscribe links only work on this computer. "
                        "Fine for testing with your own addresses; host the app before emailing real leads.")
    return errors, warnings


def enroll_leads(session: Session, campaign: Campaign, source: str | None = None) -> tuple[int, int]:
    """Queue leads for a campaign, skipping anyone suppressed, already contacted or mid-sequence elsewhere."""
    query = select(Lead)
    if source:
        query = query.where(Lead.source == source)
    in_campaign = set(session.scalars(select(Enrollment.lead_id).where(Enrollment.campaign_id == campaign.id)))
    busy_elsewhere = set(session.scalars(select(Enrollment.lead_id).where(
        or_(Enrollment.status.in_(ACTIVE_STATUSES), Enrollment.status == "replied"))))
    suppressed = set(session.scalars(select(Suppression.value)))

    added = skipped = 0
    now = utcnow()
    for lead in session.scalars(query):
        if (lead.id in in_campaign or lead.id in busy_elsewhere
                or lead.email in suppressed or lead.domain in suppressed):
            skipped += 1
            continue
        session.add(Enrollment(campaign_id=campaign.id, lead_id=lead.id, next_send_at=now))
        added += 1
    session.commit()
    return added, skipped


def _due_enrollments(session: Session, mailbox: Mailbox, now: datetime) -> list[Enrollment]:
    campaign_ids = select(campaign_mailboxes.c.campaign_id).where(campaign_mailboxes.c.mailbox_id == mailbox.id)
    return list(session.scalars(select(Enrollment).join(Campaign).where(
        Campaign.status == "active",
        Enrollment.campaign_id.in_(campaign_ids),
        Enrollment.status.in_(ACTIVE_STATUSES),
        Enrollment.next_send_at <= now,
        or_(Enrollment.mailbox_id == mailbox.id, Enrollment.mailbox_id.is_(None)),
    ).order_by(Enrollment.next_step.desc(), Enrollment.next_send_at, Enrollment.id).limit(25)))


def _stop(enrollment: Enrollment, status: str, note: str) -> None:
    enrollment.status = status
    enrollment.next_send_at = None
    enrollment.note = note


def _send_next(session: Session, mailbox: Mailbox, now: datetime, today: date, send) -> bool:
    for enrollment in _due_enrollments(session, mailbox, now):
        lead, campaign = enrollment.lead, enrollment.campaign

        suppression = find_suppression(session, lead.email)
        if suppression:
            _stop(enrollment, _STATUS_FOR_REASON.get(suppression.reason, "stopped"),
                  f"Suppressed: {suppression.reason}")
            continue
        step = next((s for s in campaign.steps if s.position >= enrollment.next_step), None)
        if step is None:
            _stop(enrollment, "completed", "")
            continue

        draft = compose(campaign, step, lead.fields, mailbox, enrollment.thread_subject,
                        enrollment.thread_references)
        if draft.missing:
            _stop(enrollment, "failed", "Missing lead data: " + ", ".join(sorted(set(draft.missing))))
            continue
        if not draft.subject:
            _stop(enrollment, "failed", "The email has no subject.")
            continue

        msg = mailer.build_message(mailbox, lead.email, draft.subject, draft.body, draft.unsubscribe_url,
                                   draft.references)
        log_row = SentEmail(enrollment_id=enrollment.id, mailbox_id=mailbox.id, campaign_id=campaign.id,
                            step_position=step.position, to_email=lead.email, subject=draft.subject,
                            message_id=msg["Message-ID"], sent_at=now)
        try:
            send(mailbox, msg)
        except mailer.PermanentRecipientError as exc:
            log_row.status, log_row.error = "failed", str(exc)
            session.add(log_row)
            suppress(session, lead.email, "bounced")
            session.commit()
            continue
        except mailer.MailboxAuthError as exc:
            mailbox.status, mailbox.last_error = "error", f"Login failed: {exc}"
            session.commit()
            return False
        except mailer.MailboxLimitError as exc:
            mailbox.next_send_at = now + LIMIT_BACKOFF
            mailbox.last_error = f"Provider sending limit reached, waiting: {exc}"
            session.commit()
            return False
        except Exception as exc:  # network errors, temporary server problems
            log.warning("Send to %s failed: %s", lead.email, exc)
            log_row.status, log_row.error = "failed", mailer.error_text(exc)
            session.add(log_row)
            enrollment.attempts += 1
            if enrollment.attempts >= MAX_ATTEMPTS:
                _stop(enrollment, "failed", f"Failed {MAX_ATTEMPTS} times: {log_row.error}")
            else:
                enrollment.next_send_at = now + RETRY_DELAY
                enrollment.note = f"Will retry: {log_row.error}"
            mailbox.last_error = log_row.error[:500]
            mailbox.next_send_at = now + timedelta(minutes=5)
            session.commit()
            return False

        session.add(log_row)
        enrollment.mailbox_id = mailbox.id
        enrollment.status = "in_progress"
        enrollment.attempts = 0
        enrollment.note = ""
        if draft.references:
            enrollment.thread_references = f"{enrollment.thread_references} {msg['Message-ID']}".strip()
        else:
            enrollment.thread_subject = draft.subject
            enrollment.thread_references = msg["Message-ID"]

        following = next((s for s in campaign.steps if s.position > step.position), None)
        if following:
            enrollment.next_step = following.position
            enrollment.next_send_at = now + timedelta(days=following.delay_days)
        else:
            enrollment.next_step = step.position + 1
            _stop(enrollment, "completed", "")

        if mailbox.first_send_date is None:
            mailbox.first_send_date = today
        low, high = sorted((mailbox.min_delay_seconds, mailbox.max_delay_seconds))
        mailbox.next_send_at = now + timedelta(seconds=random.randint(low, high))
        mailbox.last_error = ""
        session.commit()
        return True

    session.commit()
    return False


def claim_mailbox(session: Session, mailbox: Mailbox, now: datetime) -> bool:
    """Take this mailbox for this tick, so two ticks running at once never send twice."""
    claimed = session.execute(update(Mailbox).where(
        Mailbox.id == mailbox.id,
        or_(Mailbox.next_send_at.is_(None), Mailbox.next_send_at <= now),
    ).values(next_send_at=now + CLAIM_LEASE))
    session.commit()
    return claimed.rowcount == 1


def run_send_tick(session: Session, now: datetime | None = None, send=None) -> int:
    """Send at most one email per mailbox. Called every few seconds by the worker."""
    now = now or utcnow()
    send = send or mailer.send
    sent = 0
    mailboxes = list(session.scalars(select(Mailbox).where(Mailbox.status == "active")))
    random.shuffle(mailboxes)
    for mailbox in mailboxes:
        if not in_send_window(mailbox, now):
            continue
        today = local_now(mailbox, now).date()
        if sent_today(session, mailbox, now) >= daily_cap(mailbox, today):
            continue
        total, rate = bounce_rate(session, mailbox)
        if total >= BOUNCE_MIN_SENDS and rate > BOUNCE_PAUSE_RATE:
            mailbox.status = "paused"
            mailbox.last_error = (f"Auto-paused: {rate:.0%} of the last {total} first emails bounced. "
                                  "Clean your lead list before resuming.")
            session.commit()
            continue
        if not claim_mailbox(session, mailbox, now):
            continue  # another tick is already sending from this mailbox
        if _send_next(session, mailbox, now, today, send):
            sent += 1
    return sent
