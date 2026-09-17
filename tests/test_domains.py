import json
from datetime import datetime
from types import SimpleNamespace

import dns.exception
import pytest

from app import dns_check
from app.models import Campaign, Domain, Mailbox, Step
from app.scheduler import validate_campaign


@pytest.fixture()
def fake_dns(monkeypatch):
    txt: dict[str, list[str]] = {}
    mx: dict[str, list[str]] = {}

    def fake_resolve(name, rdtype, lifetime=None):
        if rdtype == "MX" and name in mx:
            return [SimpleNamespace(exchange=f"{host}.") for host in mx[name]]
        raise dns.exception.DNSException("no answer")

    monkeypatch.setattr(dns_check.dns.resolver, "resolve", fake_resolve)
    monkeypatch.setattr(dns_check, "_txt", lambda name: txt.get(name, []))
    return txt, mx


def test_records_for_google_workspace():
    records = dns_check.records_to_add("trybrand.com", "google", "google")
    assert [(r.type, r.host) for r in records] == [
        ("MX", "@"), ("TXT", "@"), ("TXT", "google._domainkey"), ("TXT", "_dmarc")]
    assert records[0].value == "smtp.google.com (priority 1)"
    assert records[1].value == "v=spf1 include:_spf.google.com ~all"
    assert records[3].value == "v=DMARC1; p=none"


def test_all_records_in_place_pass(fake_dns):
    txt, mx = fake_dns
    mx["trybrand.com"] = ["smtp.google.com"]
    txt["trybrand.com"] = ["google-site-verification=abc", "v=spf1 include:_spf.google.com ~all"]
    txt["google._domainkey.trybrand.com"] = ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"]
    txt["_dmarc.trybrand.com"] = ["v=DMARC1; p=none"]
    checks = dns_check.check_domain("trybrand.com", "google", "google")
    assert all(check.ok for check in checks), checks


def test_spf_without_the_providers_include_fails(fake_dns):
    txt, mx = fake_dns
    mx["trybrand.com"] = ["smtp.google.com"]
    txt["trybrand.com"] = ["v=spf1 include:spf.protection.outlook.com ~all"]
    spf = next(c for c in dns_check.check_domain("trybrand.com", "google", "google") if c.name == "SPF")
    assert not spf.ok
    assert "_spf.google.com" in spf.tip


def test_missing_records_all_fail(fake_dns):
    assert not any(check.ok for check in dns_check.check_domain("trybrand.com", "google", "google"))


def test_campaign_cannot_start_until_domain_dns_passes():
    domain = Domain(name="sender.com", provider="google", dkim_selector="google")
    mailbox = Mailbox(email="sam@sender.com", sending_domain=domain, smtp_host="smtp.test", imap_host="imap.test",
                      username="sam@sender.com", password_enc="x", status="active")
    campaign = Campaign(name="Test", sender_address="1 Main St", footer="Unsubscribe: {{unsubscribe_url}}")
    campaign.steps = [Step(position=1, subject="Hi", body="Hello")]
    campaign.mailboxes = [mailbox]

    assert any("Run the DNS check for sender.com" in e for e in validate_campaign(campaign)[0])

    domain.dns_checked_at = datetime(2026, 9, 14, 9, 0)
    domain.dns_results = json.dumps([{"name": "SPF", "ok": False, "detail": "", "tip": ""}])
    assert any("sender.com is missing DNS records" in e for e in validate_campaign(campaign)[0])

    domain.dns_results = json.dumps([{"name": "SPF", "ok": True, "detail": "", "tip": ""}])
    assert not any("sender.com" in e for e in validate_campaign(campaign)[0])
