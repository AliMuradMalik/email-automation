import email
import email.policy
from email.message import EmailMessage

from app.inbox import classify, handle_message, strip_quoted
from app.models import Campaign, Enrollment, InboxEvent, Lead, Mailbox, SentEmail, Suppression

GMAIL_BOUNCE = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: sam@sender.com
Subject: Delivery Status Notification (Failure)
MIME-Version: 1.0
Content-Type: multipart/report; boundary="b1"; report-type=delivery-status
X-Failed-Recipients: ghost@nowhere.com

--b1
Content-Type: text/plain; charset="UTF-8"

Address not found. Your message wasn't delivered to ghost@nowhere.com.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; ghost@nowhere.com
Action: failed
Status: 5.1.1

--b1
Content-Type: message/rfc822

From: sam@sender.com
To: ghost@nowhere.com
Subject: Question about Nowhere
Message-ID: <abc123@sender.com>

Hi there
--b1--
"""


def parse(raw: str):
    return email.message_from_string(raw, policy=email.policy.default)


def reply(sender: str, body: str, subject: str = "Re: Question about Acme", **headers) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "sam@sender.com"
    msg["Subject"] = subject
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return msg


def setup_campaign(session):
    mailbox = Mailbox(email="sam@sender.com", smtp_host="smtp.test", imap_host="imap.test",
                      username="sam@sender.com", password_enc="x")
    jane = Lead(email="jane@acme.com", first_name="Jane", company="Acme")
    bob = Lead(email="bob@acme.com", first_name="Bob", company="Acme")
    campaign = Campaign(name="Test", status="active", sender_address="1 Main St")
    campaign.mailboxes = [mailbox]
    session.add_all([mailbox, jane, bob, campaign])
    session.flush()
    jane_enrollment = Enrollment(campaign_id=campaign.id, lead_id=jane.id, mailbox_id=mailbox.id, status="in_progress")
    bob_enrollment = Enrollment(campaign_id=campaign.id, lead_id=bob.id, status="queued")
    session.add_all([jane_enrollment, bob_enrollment])
    session.flush()
    session.add(SentEmail(enrollment_id=jane_enrollment.id, mailbox_id=mailbox.id, campaign_id=campaign.id,
                          to_email=jane.email, subject="Question about Acme", message_id="<m1@sender.com>"))
    session.commit()
    return mailbox, jane_enrollment, bob_enrollment


def test_classify_bounce_finds_recipient_and_original_message():
    c = classify(parse(GMAIL_BOUNCE))
    assert c.kind == "bounce"
    assert c.failed_recipients == ["ghost@nowhere.com"]
    assert c.hard_bounce is True
    assert "<abc123@sender.com>" in c.references


def test_classify_auto_reply():
    msg = reply("Jane <jane@acme.com>", "I'm out until Monday.", subject="Automatic reply: Question about Acme",
                Auto_Submitted="auto-replied")
    assert classify(msg).kind == "auto_reply"


def test_strip_quoted_keeps_only_new_text():
    text = ("Sounds good, call me.\n\nOn Mon, Sep 7, 2026 at 10:00 AM Sam <sam@sender.com> wrote:\n"
            "> Hi Jane\n> Unsubscribe here: https://x")
    assert strip_quoted(text) == "Sounds good, call me."


def test_reply_stops_lead_and_colleagues(session):
    mailbox, jane, bob = setup_campaign(session)
    msg = reply("Jane <jane@acme.com>", "Yes, let's talk Tuesday.", In_Reply_To="<m1@sender.com>")
    assert handle_message(session, mailbox, msg)
    session.commit()
    assert jane.status == "replied"
    assert bob.status == "stopped"
    assert session.query(InboxEvent).one().kind == "reply"


def test_auto_reply_does_not_stop_sequence(session):
    mailbox, jane, _ = setup_campaign(session)
    msg = reply("jane@acme.com", "Out of office", subject="Out of Office: Question", Auto_Submitted="auto-replied")
    assert handle_message(session, mailbox, msg)
    session.commit()
    assert jane.status == "in_progress"


def test_unsubscribe_reply_suppresses_address(session):
    mailbox, jane, _ = setup_campaign(session)
    assert handle_message(session, mailbox, reply("jane@acme.com", "Please remove me from your list."))
    session.commit()
    assert jane.status == "unsubscribed"
    assert session.query(Suppression).filter_by(value="jane@acme.com").one().reason == "unsubscribed"


def test_bounce_suppresses_address_and_marks_enrollment(session):
    mailbox, jane, _ = setup_campaign(session)
    raw = GMAIL_BOUNCE.replace("ghost@nowhere.com", "jane@acme.com").replace("<abc123@sender.com>", "<m1@sender.com>")
    assert handle_message(session, mailbox, parse(raw))
    session.commit()
    assert jane.status == "bounced"
    assert session.query(Suppression).filter_by(value="jane@acme.com").one().reason == "bounced"


def test_mail_from_strangers_is_ignored(session):
    mailbox, jane, _ = setup_campaign(session)
    assert handle_message(session, mailbox, reply("deals@shop.com", "50% off today")) is False
    assert jane.status == "in_progress"
