"""Database tables and suppression helpers."""
import json
from datetime import date, datetime, timezone

from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Table, Text,
                        UniqueConstraint, select)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp; all datetimes in the database are naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


ACTIVE_STATUSES = ("queued", "in_progress")

DEFAULT_FOOTER = "--\nNot relevant? Unsubscribe here: {{unsubscribe_url}}\n{{sender_address}}"

campaign_mailboxes = Table(
    "campaign_mailboxes",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
    Column("mailbox_id", ForeignKey("mailboxes.id", ondelete="CASCADE"), primary_key=True),
)


class Domain(Base):
    """A domain that mailboxes send from, with the result of its last DNS check."""

    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(253), unique=True)
    provider: Mapped[str] = mapped_column(String(20), default="google")  # key in dns_check.PROVIDERS
    dkim_selector: Mapped[str] = mapped_column(String(64), default="google")
    dns_results: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of checks
    dns_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    mailboxes: Mapped[list["Mailbox"]] = relationship(back_populates="sending_domain", order_by="Mailbox.email")

    @property
    def checks(self) -> list[dict]:
        return json.loads(self.dns_results or "[]")

    @property
    def dns_ok(self) -> bool:
        checks = self.checks
        return bool(checks) and all(check["ok"] for check in checks)


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    domain_id: Mapped[int | None] = mapped_column(ForeignKey("domains.id", ondelete="SET NULL"), nullable=True)
    sending_domain: Mapped[Domain | None] = relationship(back_populates="mailboxes")
    from_name: Mapped[str] = mapped_column(String(200), default="")
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_security: Mapped[str] = mapped_column(String(10), default="ssl")  # ssl | starttls
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    username: Mapped[str] = mapped_column(String(320))
    password_enc: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text, default="")

    # Pace: the daily cap starts at warmup_start and grows by warmup_step per day up to daily_limit.
    daily_limit: Mapped[int] = mapped_column(Integer, default=30)
    warmup_start: Mapped[int] = mapped_column(Integer, default=5)
    warmup_step: Mapped[int] = mapped_column(Integer, default=2)
    first_send_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    min_delay_seconds: Mapped[int] = mapped_column(Integer, default=120)
    max_delay_seconds: Mapped[int] = mapped_column(Integer, default=360)
    time_zone: Mapped[str] = mapped_column(String(64), default="UTC")
    window_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    window_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    send_days: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4")  # Monday = 0

    status: Mapped[str] = mapped_column(String(20), default="active")  # active | paused | error
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    imap_uidvalidity: Mapped[int] = mapped_column(Integer, default=0)
    imap_last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_inbox_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()

    @property
    def send_day_set(self) -> set[int]:
        return {int(d) for d in self.send_days.split(",") if d.strip().isdigit()}


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(200), default="")
    last_name: Mapped[str] = mapped_column(String(200), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    website: Mapped[str] = mapped_column(String(500), default="")
    custom_json: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()

    @property
    def fields(self) -> dict[str, str]:
        values = json.loads(self.custom_json or "{}")
        values.update({"email": self.email, "first_name": self.first_name, "last_name": self.last_name,
                       "company": self.company, "title": self.title, "website": self.website})
        return values


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | active | paused
    sender_address: Mapped[str] = mapped_column(Text, default="")  # postal address, required by CAN-SPAM
    footer: Mapped[str] = mapped_column(Text, default=DEFAULT_FOOTER)
    stop_on_company_reply: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    steps: Mapped[list["Step"]] = relationship(order_by="Step.position", cascade="all, delete-orphan")
    mailboxes: Mapped[list[Mailbox]] = relationship(secondary=campaign_mailboxes)
    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="campaign",
                                                           cascade="all, delete-orphan")


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    delay_days: Mapped[int] = mapped_column(Integer, default=0)  # wait after the previous step
    subject: Mapped[str] = mapped_column(String(300), default="")  # empty follow-up = reply in same thread
    body: Mapped[str] = mapped_column(Text, default="")


class Enrollment(Base):
    """One lead's progress through one campaign's sequence."""

    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("campaign_id", "lead_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id", ondelete="SET NULL"), nullable=True)
    # queued | in_progress | replied | bounced | unsubscribed | completed | stopped | failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    next_step: Mapped[int] = mapped_column(Integer, default=1)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=utcnow)
    thread_subject: Mapped[str] = mapped_column(String(300), default="")
    thread_references: Mapped[str] = mapped_column(Text, default="")  # space-separated Message-IDs
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="enrollments")
    lead: Mapped[Lead] = relationship()
    mailbox: Mapped[Mailbox | None] = relationship()


class SentEmail(Base):
    __tablename__ = "sent_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id", ondelete="SET NULL"),
                                                      nullable=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id", ondelete="SET NULL"), nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    step_position: Mapped[int] = mapped_column(Integer, default=1)
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300), default="")
    message_id: Mapped[str] = mapped_column(String(300), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="sent")  # sent | failed
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Suppression(Base):
    """Addresses or whole domains that must never be emailed again."""

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(320), unique=True)
    kind: Mapped[str] = mapped_column(String(10), default="email")  # email | domain
    reason: Mapped[str] = mapped_column(String(50), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InboxEvent(Base):
    __tablename__ = "inbox_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id", ondelete="SET NULL"), nullable=True)
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id", ondelete="SET NULL"),
                                                      nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # reply | bounce | auto_reply | unsubscribe
    from_email: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    mailbox: Mapped[Mailbox | None] = relationship()
    enrollment: Mapped[Enrollment | None] = relationship()


_STATUS_FOR_REASON = {"unsubscribed": "unsubscribed", "bounced": "bounced"}


def find_suppression(session: Session, email: str) -> Suppression | None:
    email = email.strip().lower()
    domain = email.rsplit("@", 1)[-1]
    return session.scalar(select(Suppression).where(Suppression.value.in_([email, domain])))


def suppress(session: Session, value: str, reason: str) -> Suppression:
    """Add an email or domain to the suppression list and stop its active enrollments."""
    value = value.strip().lower()
    row = session.scalar(select(Suppression).where(Suppression.value == value))
    if row is None:
        row = Suppression(value=value, kind="email" if "@" in value else "domain", reason=reason)
        session.add(row)

    query = select(Enrollment).join(Lead).where(Enrollment.status.in_(ACTIVE_STATUSES))
    query = query.where(Lead.email == value) if "@" in value else query.where(Lead.email.like(f"%@{value}"))
    for enrollment in session.scalars(query):
        enrollment.status = _STATUS_FOR_REASON.get(reason, "stopped")
        enrollment.next_send_at = None
        enrollment.note = f"Suppressed: {reason}"
    return row
