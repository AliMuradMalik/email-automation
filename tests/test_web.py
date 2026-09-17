import pytest
from fastapi.testclient import TestClient

from app import dns_check
from app.db import Base, SessionLocal, engine
from app.main import _blank_mailbox, app, templates
from app.models import Mailbox, Suppression
from app.security import unsubscribe_url

ALL_GOOD = [dns_check.Check("SPF", True, "v=spf1 include:_spf.google.com ~all")]

MAILBOX_FORM = {
    "from_name": "Sam", "smtp_host": "smtp.gmail.com", "smtp_port": "465", "smtp_security": "ssl",
    "imap_host": "imap.gmail.com", "imap_port": "993", "password": "abcd efgh ijkl mnop", "time_zone": "UTC",
    "window_start_hour": "9", "window_end_hour": "17", "send_days": ["0", "1", "2", "3", "4"],
    "daily_limit": "30", "warmup_start": "5", "warmup_step": "2",
    "min_delay_seconds": "120", "max_delay_seconds": "360",
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(dns_check, "check_domain", lambda *args: ALL_GOOD)  # no real DNS lookups in tests
    # https base URL because the session cookie is Secure when PUBLIC_BASE_URL is https.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    Base.metadata.drop_all(engine)


def login(client: TestClient) -> None:
    response = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
    assert response.status_code == 303


def add_domain(client: TestClient, name: str = "sender.com") -> int:
    response = client.post("/domains", data={"name": name, "provider": "google"}, follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_pages_require_login(client):
    response = client.get("/campaigns", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_is_rejected(client):
    assert client.post("/login", data={"password": "nope"}).status_code == 401


def test_every_page_renders_after_login(client):
    login(client)
    domain_id = add_domain(client)
    for path in ("/", "/domains", f"/domains/{domain_id}", "/mailboxes", "/mailboxes/new", "/leads",
                 "/campaigns", "/inbox", "/suppression"):
        assert client.get(path).status_code == 200, path


def test_add_domain_cleans_input_and_lists_records(client):
    login(client)
    domain_id = add_domain(client, "https://www.TryBrand.com/")
    html = client.get(f"/domains/{domain_id}").text
    assert "trybrand.com" in html
    assert "v=spf1 include:_spf.google.com ~all" in html
    assert "google._domainkey" in html

    again = client.post("/domains", data={"name": "trybrand.com"}, follow_redirects=False)
    assert again.headers["location"] == f"/domains/{domain_id}"
    assert "Enter a domain like" in client.post("/domains", data={"name": "not a domain"}).text


def test_new_mailbox_needs_a_domain_first(client):
    login(client)
    response = client.get("/mailboxes/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/domains"


def test_domain_mailbox_leads_and_campaign_flow(client):
    login(client)
    domain_id = add_domain(client)
    response = client.post("/mailboxes", follow_redirects=False,
                           data={**MAILBOX_FORM, "domain_id": str(domain_id), "local_part": "Sam"})
    assert response.status_code == 303
    with SessionLocal() as db:
        mailbox = db.query(Mailbox).one()
        assert (mailbox.email, mailbox.domain_id, mailbox.username) == ("sam@sender.com", domain_id, "sam@sender.com")
        mailbox_id = mailbox.id

    csv_data = "Email,First Name,Company,City\njane@acme.com,Jane,Acme,Austin\ninfo@acme.com,,Acme,\nnot-an-email,,,\n"
    response = client.post("/leads/import", files={"file": ("leads.csv", csv_data, "text/csv")},
                           data={"skip_role": "true"})
    assert response.status_code == 200
    assert "Added 1, updated 0, skipped 2" in response.text

    response = client.post("/campaigns", data={"name": "Test"}, follow_redirects=False)
    campaign_url = response.headers["location"]
    assert "replace the [bracketed] example text" in client.get(campaign_url).text

    client.post(f"{campaign_url}/settings", data={
        "name": "Test", "sender_address": "1 Main St", "stop_on_company_reply": "true",
        "footer": "Unsubscribe: {{unsubscribe_url}}\n{{sender_address}}", "mailbox_ids": [str(mailbox_id)],
    })
    client.post(f"{campaign_url}/steps", data={
        "action": "save", "subject": ["Hi {{first_name}} from {{city}}", ""],
        "body": ["Hi {{first_name|there}}, quick question.", "Bumping this."], "delay_days": ["0", "3"],
    })
    assert "Added 1 leads" in client.post(f"{campaign_url}/enroll", data={"source": ""}).text
    assert "Campaign started" in client.post(f"{campaign_url}/start").text
    assert "Hi Jane from Austin" in client.get(campaign_url).text


def test_mailbox_without_domain_is_rejected(client):
    login(client)
    add_domain(client)
    response = client.post("/mailboxes", data={**MAILBOX_FORM, "domain_id": "", "local_part": "sam"})
    assert response.status_code == 400
    assert "Choose the domain this mailbox sends from" in response.text


def test_unsubscribe_page_and_one_click_post(client):
    path = unsubscribe_url("jane@acme.com").replace("https://outreach.example.com", "")
    response = client.get(path)
    assert response.status_code == 200 and "Stop all emails" in response.text

    response = client.post(path, data={"List-Unsubscribe": "One-Click"})
    assert response.status_code == 200 and "unsubscribed" in response.text
    with SessionLocal() as db:
        assert db.query(Suppression).filter_by(value="jane@acme.com").one().reason == "unsubscribed"

    assert client.get("/u/forged-token").status_code == 400


def test_mailbox_form_explains_google_app_passwords():
    html = templates.get_template("mailbox_form.html").render(
        request=None, flashes=[], logged_in=False, mailbox=_blank_mailbox(), errors=[], presets={}, timezones=[],
        domains=[], domain_providers={},
        test_results=[("SMTP (sending)", False, "535 bad credentials"), ("IMAP (replies and bounces)", True, "OK")])
    assert "Google needs an" in html


def test_every_start_error_is_flashed(client):
    login(client)
    campaign_url = client.post("/campaigns", data={"name": "Draft"}, follow_redirects=False).headers["location"]
    html = client.post(f"{campaign_url}/start").text
    # Each error shows twice: as a flash message and in the page's "Fix before starting" list.
    assert html.count("Assign at least one active mailbox") == 2
    assert html.count("Add your postal address") == 2
