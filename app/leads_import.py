"""Import leads from CSV with validation, de-duplication and suppression checks."""
import csv
import io
import json
import re
from dataclasses import dataclass, field

from email_validator import EmailNotValidError, caching_resolver, validate_email
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Lead, Suppression

COLUMN_ALIASES = {
    "email": {"email", "e-mail", "email address", "emailaddress", "work email", "mail"},
    "first_name": {"first_name", "first name", "firstname", "first", "given name"},
    "last_name": {"last_name", "last name", "lastname", "last", "surname", "family name"},
    "company": {"company", "company name", "company_name", "organization", "organisation"},
    "title": {"title", "job title", "job_title", "position", "role"},
    "website": {"website", "company website", "url", "domain", "site"},
}

# Shared inboxes rarely belong to the decision maker and complain more often.
ROLE_ACCOUNTS = {
    "abuse", "accounts", "admin", "billing", "careers", "contact", "enquiries", "help", "hello", "hr",
    "info", "inquiries", "jobs", "legal", "marketing", "media", "no-reply", "noreply", "office",
    "postmaster", "press", "privacy", "sales", "support", "team", "webmaster",
}


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (email or row, reason)


def _map_headers(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for header in headers:
        key = (header or "").strip().lower()
        target = next((name for name, aliases in COLUMN_ALIASES.items() if key in aliases), None)
        if target and target not in mapping.values():
            mapping[header] = target
        elif key:
            mapping[header] = "custom:" + re.sub(r"\W+", "_", key).strip("_")
    return mapping


def import_csv(session: Session, data: bytes, source: str, check_mx: bool = True,
               skip_role_accounts: bool = True) -> ImportResult:
    """Add or update leads from a CSV file. The caller commits."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    if not reader.fieldnames:
        raise ValueError("The CSV file is empty.")
    mapping = _map_headers(reader.fieldnames)
    if "email" not in mapping.values():
        raise ValueError("No email column found. Name one of the columns 'email'.")

    suppressed = {row.value: row.reason for row in session.scalars(select(Suppression))}
    resolver = caching_resolver(timeout=5) if check_mx else None
    result = ImportResult()
    seen: set[str] = set()

    for row_number, row in enumerate(reader, start=2):
        values: dict[str, str] = {}
        custom: dict[str, str] = {}
        for header, target in mapping.items():
            cell = (row.get(header) or "").strip()
            if target.startswith("custom:"):
                if cell:
                    custom[target.removeprefix("custom:")] = cell
            else:
                values[target] = cell

        raw_email = values.get("email", "")
        if not raw_email:
            result.skipped.append((f"row {row_number}", "no email"))
            continue
        try:
            email = validate_email(raw_email, check_deliverability=check_mx, dns_resolver=resolver).normalized.lower()
        except EmailNotValidError as exc:
            result.skipped.append((raw_email, str(exc)))
            continue

        domain = email.rsplit("@", 1)[1]
        if email in seen:
            result.skipped.append((email, "duplicate in this file"))
            continue
        seen.add(email)
        if skip_role_accounts and email.split("@")[0] in ROLE_ACCOUNTS:
            result.skipped.append((email, "role address like info@ or sales@"))
            continue
        reason = suppressed.get(email) or suppressed.get(domain)
        if reason:
            result.skipped.append((email, f"on suppression list ({reason})"))
            continue

        lead = session.scalar(select(Lead).where(Lead.email == email))
        if lead is None:
            lead = Lead(email=email, source=source)
            session.add(lead)
            result.added += 1
        else:
            result.updated += 1
        for name in ("first_name", "last_name", "company", "title", "website"):
            if values.get(name):
                setattr(lead, name, values[name])
        if custom:
            merged = json.loads(lead.custom_json or "{}")
            merged.update(custom)
            lead.custom_json = json.dumps(merged)
    return result
