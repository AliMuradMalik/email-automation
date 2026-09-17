from datetime import date, datetime, timedelta

from app import mailer
from app.models import Campaign, Enrollment, Lead, Mailbox, SentEmail, Step, Suppression
from app.scheduler import daily_cap, enroll_leads, in_send_window, run_send_tick, validate_campaign

MONDAY_10AM = datetime(2026, 9, 7, 10, 0)  # naive UTC


def make_mailbox(**overrides) -> Mailbox:
    values = dict(email="sam@sender.com", from_name="Sam", smtp_host="smtp.test", imap_host="imap.test",
                  username="sam@sender.com", password_enc="x", signature="Sam", time_zone="UTC",
                  window_start_hour=9, window_end_hour=17, send_days="0,1,2,3,4", daily_limit=30,
                  warmup_start=5, warmup_step=2, min_delay_seconds=60, max_delay_seconds=60, status="active")
    values.update(overrides)
    return Mailbox(**values)


def make_campaign(session, mailbox: Mailbox, leads: list[Lead]) -> Campaign:
    campaign = Campaign(name="Test", status="active", sender_address="1 Main St, Springfield")
    campaign.steps = [
        Step(position=1, delay_days=0, subject="Question about {{company}}", body="Hi {{first_name|there}},\n\nQuick question."),
        Step(position=2, delay_days=3, subject="", body="Just bumping this."),
    ]
    campaign.mailboxes = [mailbox]
    session.add_all([mailbox, campaign, *leads])
    session.commit()
    return campaign


def enroll_all_due(session, campaign: Campaign) -> tuple[int, int]:
    counts = enroll_leads(session, campaign)
    for enrollment in session.query(Enrollment):
        enrollment.next_send_at = MONDAY_10AM - timedelta(hours=1)
    session.commit()
    return counts


def test_monday_fixture_is_a_monday():
    assert MONDAY_10AM.weekday() == 0


def test_daily_cap_ramps_up():
    mailbox = make_mailbox()
    assert daily_cap(mailbox, date(2026, 9, 7)) == 5
    mailbox.first_send_date = date(2026, 9, 7)
    assert daily_cap(mailbox, date(2026, 9, 10)) == 11
    assert daily_cap(mailbox, date(2026, 12, 1)) == 30


def test_send_window_respects_hours_days_and_time_zone():
    mailbox = make_mailbox()
    assert in_send_window(mailbox, MONDAY_10AM)
    assert not in_send_window(mailbox, MONDAY_10AM.replace(hour=20))
    assert not in_send_window(mailbox, MONDAY_10AM + timedelta(days=5))  # Saturday
    mailbox.time_zone = "Asia/Karachi"  # UTC+5
    assert in_send_window(mailbox, MONDAY_10AM)  # 15:00 in Karachi
    assert not in_send_window(mailbox, MONDAY_10AM.replace(hour=13))  # 18:00 in Karachi


def test_first_email_then_follow_up_in_same_thread(session):
    mailbox = make_mailbox()
    campaign = make_campaign(session, mailbox, [Lead(email="jane@acme.com", first_name="Jane", company="Acme")])
    assert enroll_all_due(session, campaign) == (1, 0)
    enrollment = session.query(Enrollment).one()
    outbox = []

    assert run_send_tick(session, now=MONDAY_10AM, send=lambda mb, msg: outbox.append(msg)) == 1
    first = outbox[0]
    assert first["To"] == "jane@acme.com"
    assert first["Subject"] == "Question about Acme"
    assert first["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "https://outreach.example.com/u/" in first["List-Unsubscribe"]
    body = first.get_content()
    assert "Hi Jane," in body and "1 Main St" in body and "https://outreach.example.com/u/" in body
    assert (enrollment.status, enrollment.next_step) == ("in_progress", 2)
    assert enrollment.next_send_at == MONDAY_10AM + timedelta(days=3)

    # The mailbox waits between emails.
    assert run_send_tick(session, now=MONDAY_10AM + timedelta(seconds=30), send=lambda mb, msg: outbox.append(msg)) == 0

    thursday = MONDAY_10AM + timedelta(days=3, hours=1)
    assert run_send_tick(session, now=thursday, send=lambda mb, msg: outbox.append(msg)) == 1
    follow_up = outbox[1]
    assert follow_up["Subject"] == "Re: Question about Acme"
    assert follow_up["In-Reply-To"] == first["Message-ID"]
    assert enrollment.status == "completed"


def test_daily_cap_and_domain_suppression(session):
    mailbox = make_mailbox(warmup_start=2, min_delay_seconds=30, max_delay_seconds=30)
    leads = [Lead(email=f"p{i}@company{i}.com", first_name=f"P{i}", company=f"C{i}") for i in range(4)]
    campaign = make_campaign(session, mailbox, leads)
    session.add(Suppression(value="company3.com", kind="domain", reason="manual"))
    session.commit()
    assert enroll_all_due(session, campaign) == (3, 1)

    outbox, now = [], MONDAY_10AM
    for _ in range(5):
        run_send_tick(session, now=now, send=lambda mb, msg: outbox.append(msg))
        now += timedelta(minutes=1)
    assert len(outbox) == 2  # day-one warm-up cap


def test_lead_suppressed_after_enrolling_is_not_emailed(session):
    campaign = make_campaign(session, make_mailbox(), [Lead(email="jane@acme.com", first_name="Jane", company="Acme")])
    enroll_all_due(session, campaign)
    session.add(Suppression(value="jane@acme.com", kind="email", reason="unsubscribed"))
    session.commit()
    outbox = []
    assert run_send_tick(session, now=MONDAY_10AM, send=lambda mb, msg: outbox.append(msg)) == 0
    assert outbox == []
    assert session.query(Enrollment).one().status == "unsubscribed"


def test_rejected_recipient_is_treated_as_bounce(session):
    campaign = make_campaign(session, make_mailbox(), [Lead(email="jane@acme.com", first_name="Jane", company="Acme")])
    enroll_all_due(session, campaign)

    def reject(_mailbox, _msg):
        raise mailer.PermanentRecipientError("550 5.1.1 no such user")

    assert run_send_tick(session, now=MONDAY_10AM, send=reject) == 0
    assert session.query(Enrollment).one().status == "bounced"
    assert session.query(Suppression).filter_by(value="jane@acme.com").one().reason == "bounced"


def test_missing_personalisation_skips_lead(session):
    campaign = make_campaign(session, make_mailbox(), [Lead(email="jane@acme.com", company="Acme")])
    campaign.steps[0].body = "Hi {{first_name}}, quick question."
    session.commit()
    enroll_all_due(session, campaign)
    outbox = []
    assert run_send_tick(session, now=MONDAY_10AM, send=lambda mb, msg: outbox.append(msg)) == 0
    enrollment = session.query(Enrollment).one()
    assert enrollment.status == "failed" and "first_name" in enrollment.note


def test_mailbox_pauses_when_too_many_bounces(session):
    mailbox = make_mailbox()
    campaign = make_campaign(session, mailbox, [])
    for i in range(25):
        lead = Lead(email=f"x{i}@d{i}.com")
        session.add(lead)
        session.flush()
        enrollment = Enrollment(campaign_id=campaign.id, lead_id=lead.id, mailbox_id=mailbox.id,
                                status="bounced" if i < 3 else "completed", next_send_at=None)
        session.add(enrollment)
        session.flush()
        session.add(SentEmail(enrollment_id=enrollment.id, mailbox_id=mailbox.id, campaign_id=campaign.id,
                              step_position=1, to_email=lead.email, sent_at=MONDAY_10AM - timedelta(days=1)))
    session.commit()
    run_send_tick(session, now=MONDAY_10AM, send=lambda mb, msg: None)
    assert mailbox.status == "paused"
    assert "bounced" in mailbox.last_error


def test_validate_campaign_lists_blocking_problems():
    campaign = Campaign(name="Draft", sender_address="", footer="Bye")
    campaign.steps = [Step(position=1, subject="", body="")]
    errors, _ = validate_campaign(campaign)
    joined = " ".join(errors)
    for expected in ("subject", "body", "mailbox", "unsubscribe_url", "postal address"):
        assert expected in joined


def test_two_ticks_at_the_same_time_send_only_once(session):
    """An outside timer can fire twice at once; the mailbox claim must prevent a double send."""
    mailbox = make_mailbox()
    campaign = make_campaign(session, mailbox, [Lead(email="jane@acme.com", first_name="Jane", company="Acme")])
    enroll_all_due(session, campaign)
    outbox = []

    def send(_mailbox, msg):
        outbox.append(msg)

    assert run_send_tick(session, now=MONDAY_10AM, send=send) == 1
    assert run_send_tick(session, now=MONDAY_10AM, send=send) == 0
    assert len(outbox) == 1
