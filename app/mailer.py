"""Builds outgoing messages and talks to SMTP/IMAP servers."""
import imaplib
import smtplib
import ssl
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import urlsplit

from .models import Mailbox
from .security import decrypt

TIMEOUT = 30

# Server settings per provider; keys match dns_check.PROVIDERS.
PRESETS = {
    "google": {"label": "Google Workspace / Gmail", "smtp_host": "smtp.gmail.com", "smtp_port": 465,
               "smtp_security": "ssl", "imap_host": "imap.gmail.com", "imap_port": 993},
    "microsoft": {"label": "Microsoft 365 / Outlook", "smtp_host": "smtp.office365.com", "smtp_port": 587,
                  "smtp_security": "starttls", "imap_host": "outlook.office365.com", "imap_port": 993},
    "zoho": {"label": "Zoho Mail", "smtp_host": "smtp.zoho.com", "smtp_port": 465, "smtp_security": "ssl",
             "imap_host": "imap.zoho.com", "imap_port": 993},
}

LIMIT_MARKERS = ("5.4.5", "limit exceeded", "quota exceeded", "sending limit", "too many messages", "rate limit")


class PermanentRecipientError(Exception):
    """The server rejected the recipient address; treat it as a hard bounce."""


class MailboxAuthError(Exception):
    """Login failed; the mailbox needs a new app password or different settings."""


class MailboxLimitError(Exception):
    """The provider says this mailbox hit its sending limit."""


def is_public_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and host not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} and not host.endswith(".local")


def error_text(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {detail}"
    return str(exc) or exc.__class__.__name__


def build_message(mailbox: Mailbox, to_email: str, subject: str, body: str, unsubscribe_url: str,
                  references: list[str] | None = None) -> EmailMessage:
    """Plain-text email with threading and unsubscribe headers."""
    msg = EmailMessage()
    msg["From"] = formataddr((mailbox.from_name, mailbox.email)) if mailbox.from_name else mailbox.email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=mailbox.domain)
    if references:
        msg["In-Reply-To"] = references[-1]
        msg["References"] = " ".join(references)

    # One-click unsubscribe (RFC 8058) needs a link the recipient's provider can reach,
    # so a localhost link is left out and only the mailto option is offered.
    targets = [f"<mailto:{mailbox.email}?subject=unsubscribe>"]
    if is_public_url(unsubscribe_url):
        targets.insert(0, f"<{unsubscribe_url}>")
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["List-Unsubscribe"] = ", ".join(targets)

    msg.set_content(body)
    return msg


@contextmanager
def smtp_session(mailbox: Mailbox):
    context = ssl.create_default_context()
    if mailbox.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port, timeout=TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=TIMEOUT)
    try:
        if mailbox.smtp_security != "ssl":
            server.starttls(context=context)
        try:
            server.login(mailbox.username, decrypt(mailbox.password_enc))
        except smtplib.SMTPAuthenticationError as exc:
            raise MailboxAuthError(error_text(exc)) from exc
        yield server
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            server.close()


def send(mailbox: Mailbox, msg: EmailMessage) -> None:
    try:
        with smtp_session(mailbox) as server:
            server.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        codes = [code for code, _ in exc.recipients.values()]
        detail = "; ".join(f"{code} {text.decode(errors='replace') if isinstance(text, bytes) else text}"
                           for code, text in exc.recipients.values())
        if codes and all(500 <= code < 600 for code in codes):
            raise PermanentRecipientError(detail) from exc
        raise RuntimeError(detail) from exc
    except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        text = error_text(exc)
        if any(marker in text.lower() for marker in LIMIT_MARKERS):
            raise MailboxLimitError(text) from exc
        raise RuntimeError(text) from exc


def test_connection(mailbox: Mailbox) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    try:
        with smtp_session(mailbox):
            pass
        results.append(("SMTP (sending)", True, "Logged in"))
    except Exception as exc:  # show any failure to the user
        results.append(("SMTP (sending)", False, error_text(exc)))
    try:
        with imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, ssl_context=ssl.create_default_context(),
                               timeout=TIMEOUT) as imap:
            imap.login(mailbox.username, decrypt(mailbox.password_enc))
            imap.select("INBOX", readonly=True)
        results.append(("IMAP (replies and bounces)", True, "Logged in and opened INBOX"))
    except Exception as exc:
        results.append(("IMAP (replies and bounces)", False, str(exc)))
    return results
