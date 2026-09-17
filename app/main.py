"""Web dashboard."""
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import dns_check, mailer
from .config import settings
from .db import get_session, init_db
from .leads_import import import_csv
from .models import (DEFAULT_FOOTER, Campaign, Domain, Enrollment, InboxEvent, Lead, Mailbox, SentEmail, Step,
                     Suppression, suppress, utcnow)
from .scheduler import compose, daily_cap, enroll_leads, in_send_window, local_now, sent_today, validate_campaign
from .security import check_admin_password, encrypt, read_unsubscribe_token
from .templating import check_content, has_example_text
from .worker import check_inboxes, worker

APP_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 50
MAX_CSV_BYTES = 20 * 1024 * 1024
TIMEZONES = sorted(available_timezones())
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
LOCAL_PART_RE = re.compile(r"^[a-z0-9._%+-]+$")

STARTER_STEPS = [
    (0, "Question about {{company|your team}}",
     "Hi {{first_name|there}},\n\n[One sentence on why you are emailing {{company|them}} specifically.]\n\n"
     "[One sentence on the problem you solve and a concrete result you got for someone similar.]\n\n"
     "Worth a quick chat next week?"),
    (3, "", "Hi {{first_name|there}},\n\nJust bumping this in case it got buried. "
            "Is this something {{company|your team}} is looking at?"),
    (5, "", "Hi {{first_name|there}},\n\nI won't keep following up. If [what you offer] becomes a priority "
            "for {{company|your team}}, just reply to this email."),
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.validate()
    init_db()
    if settings.worker_enabled:
        worker.start()
    yield
    worker.stop()


app = FastAPI(title="Outreach", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key or "not-configured", same_site="lax",
                   https_only=settings.public_base_url.startswith("https://"), max_age=7 * 24 * 3600)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else "-"
templates.env.filters["pct"] = lambda v: f"{v:.1%}"


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _redirect_to_login(_request: Request, _exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


def require_login(request: Request) -> None:
    if not request.session.get("admin"):
        raise LoginRequired()


router = APIRouter(dependencies=[Depends(require_login)])


# ---------- helpers ----------

def flash(request: Request, message: str, kind: str = "ok") -> None:
    # Assign a new list: the session cookie is only rewritten when a key is set, not when a list is mutated.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message[:400]]]


def page(request: Request, name: str, status_code: int = 200, **context):
    context["flashes"] = request.session.pop("flash", [])
    context["logged_in"] = bool(request.session.get("admin"))
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def get_or_404(session: Session, model, obj_id: int):
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return obj


def _text(value) -> str:
    return str(value or "").replace("\r\n", "\n")


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def all_domains(session: Session) -> list[Domain]:
    return list(session.scalars(select(Domain).order_by(Domain.name)))


def mailbox_rows(session: Session) -> list[dict]:
    now = utcnow()
    return [{"mailbox": mb, "sent_today": sent_today(session, mb, now),
             "cap": daily_cap(mb, local_now(mb, now).date()), "in_window": in_send_window(mb, now)}
            for mb in session.scalars(select(Mailbox).order_by(Mailbox.email))]


def campaign_stats(session: Session, campaign_id: int) -> dict:
    counts = dict(session.execute(select(Enrollment.status, func.count(Enrollment.id)).where(
        Enrollment.campaign_id == campaign_id).group_by(Enrollment.status)).all())
    sent = select(func.count(SentEmail.id)).where(SentEmail.campaign_id == campaign_id, SentEmail.status == "sent")
    stats = {k: counts.get(k, 0) for k in ("queued", "in_progress", "replied", "bounced", "unsubscribed",
                                           "completed", "stopped", "failed")}
    stats["total"] = sum(counts.values())
    stats["sent"] = session.scalar(sent) or 0
    stats["contacted"] = session.scalar(sent.where(SentEmail.step_position == 1)) or 0
    stats["reply_rate"] = stats["replied"] / stats["contacted"] if stats["contacted"] else 0.0
    stats["bounce_rate"] = stats["bounced"] / stats["contacted"] if stats["contacted"] else 0.0
    return stats


def start_errors(campaign: Campaign) -> tuple[list[str], list[str]]:
    errors, warnings = validate_campaign(campaign)
    for step in campaign.steps:
        if has_example_text(f"{step.subject}\n{step.body}"):
            errors.append(f"Email {step.position}: replace the [bracketed] example text with your own words.")
    return errors, warnings


# ---------- public pages ----------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form("")):
    if check_admin_password(password):
        request.session.clear()
        request.session["admin"] = True
        return back("/")
    time.sleep(1)  # slows down password guessing
    return page(request, "login.html", status_code=401, error="Wrong password.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


@app.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str, session: Session = Depends(get_session)):
    address = read_unsubscribe_token(token)
    if not address:
        return page(request, "unsubscribe.html", status_code=400, invalid=True)
    done = session.scalar(select(Suppression.id).where(Suppression.value == address)) is not None
    return page(request, "unsubscribe.html", address=address, token=token, done=done)


@app.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str, session: Session = Depends(get_session)):
    """The confirm button, and one-click unsubscribe from Gmail/Yahoo (RFC 8058)."""
    address = read_unsubscribe_token(token)
    if not address:
        return page(request, "unsubscribe.html", status_code=400, invalid=True)
    suppress(session, address, "unsubscribed")
    session.commit()
    return page(request, "unsubscribe.html", address=address, token=token, done=True)


# ---------- dashboard ----------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    week_ago = utcnow() - timedelta(days=7)
    rows = mailbox_rows(session)
    domains = all_domains(session)
    lead_count = session.scalar(select(func.count(Lead.id))) or 0
    setup = []
    if not domains:
        setup.append("Add the domain you will send from (Domains page).")
    broken = [d.name for d in domains if not d.dns_ok]
    if broken:
        setup.append(f"Fix the DNS records for {', '.join(broken)} (Domains page).")
    if domains and not rows:
        setup.append("Add a sending mailbox.")
    if not lead_count:
        setup.append("Import leads from a CSV file.")
    if not mailer.is_public_url(settings.public_base_url):
        setup.append("PUBLIC_BASE_URL is localhost. Test with your own addresses; host the app before emailing real leads.")
    return page(
        request, "dashboard.html", mailbox_rows=rows, lead_count=lead_count, setup=setup,
        sent_week=session.scalar(select(func.count(SentEmail.id)).where(
            SentEmail.status == "sent", SentEmail.sent_at >= week_ago)) or 0,
        events_week=dict(session.execute(select(InboxEvent.kind, func.count(InboxEvent.id)).where(
            InboxEvent.received_at >= week_ago).group_by(InboxEvent.kind)).all()),
        campaign_rows=[(c, campaign_stats(session, c.id))
                       for c in session.scalars(select(Campaign).order_by(Campaign.created_at.desc()))],
        recent_events=session.scalars(select(InboxEvent).order_by(InboxEvent.received_at.desc()).limit(10)).all(),
    )


# ---------- sending domains ----------

def _clean_domain(value: str) -> str:
    """Accept 'brand.com', 'https://www.brand.com/' or even 'sam@brand.com'."""
    value = value.strip().lower().split("@")[-1]
    value = re.sub(r"^https?://", "", value).split("/")[0]
    return value.removeprefix("www.")


def _run_dns_check(domain: Domain) -> None:
    checks = dns_check.check_domain(domain.name, domain.dkim_selector, domain.provider)
    domain.dns_results = dns_check.checks_to_json(checks)
    domain.dns_checked_at = utcnow()


@router.get("/domains", response_class=HTMLResponse)
def domains_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "domains.html", domains=all_domains(session), providers=dns_check.PROVIDERS)


@router.post("/domains")
async def add_domain(request: Request, name: str = Form(""), provider: str = Form("google"),
                     session: Session = Depends(get_session)):
    domain_name = _clean_domain(name)
    if not DOMAIN_RE.match(domain_name):
        flash(request, "Enter a domain like getyourbrand.com.", "error")
        return back("/domains")
    existing = session.scalar(select(Domain).where(Domain.name == domain_name))
    if existing:
        flash(request, f"{domain_name} is already added.", "error")
        return back(f"/domains/{existing.id}")
    provider = provider if provider in dns_check.PROVIDERS else "other"
    domain = Domain(name=domain_name, provider=provider,
                    dkim_selector=dns_check.PROVIDERS[provider]["dkim_selector"])
    await run_in_threadpool(_run_dns_check, domain)
    session.add(domain)
    session.commit()
    flash(request, f"{domain_name} added. Create the DNS records below, then click Check DNS.")
    return back(f"/domains/{domain.id}")


@router.get("/domains/{domain_id}", response_class=HTMLResponse)
def domain_detail(request: Request, domain_id: int, session: Session = Depends(get_session)):
    domain = get_or_404(session, Domain, domain_id)
    return page(request, "domain_detail.html", domain=domain, providers=dns_check.PROVIDERS,
                records=dns_check.records_to_add(domain.name, domain.provider, domain.dkim_selector))


@router.post("/domains/{domain_id}")
async def update_domain(request: Request, domain_id: int, provider: str = Form("google"),
                        dkim_selector: str = Form(""), session: Session = Depends(get_session)):
    domain = get_or_404(session, Domain, domain_id)
    domain.provider = provider if provider in dns_check.PROVIDERS else "other"
    domain.dkim_selector = (re.sub(r"[^a-z0-9._-]", "", dkim_selector.strip().lower())
                            or dns_check.PROVIDERS[domain.provider]["dkim_selector"])
    await run_in_threadpool(_run_dns_check, domain)
    session.commit()
    flash(request, "Saved and checked DNS again.")
    return back(f"/domains/{domain_id}")


@router.post("/domains/{domain_id}/check")
async def check_domain_dns(request: Request, domain_id: int, session: Session = Depends(get_session)):
    domain = get_or_404(session, Domain, domain_id)
    await run_in_threadpool(_run_dns_check, domain)
    session.commit()
    if domain.dns_ok:
        flash(request, f"All DNS records for {domain.name} look good.")
    else:
        flash(request, f"Some records for {domain.name} still need fixing. New DNS records can take a few hours "
                       "to show up.", "error")
    return back(f"/domains/{domain_id}")


@router.post("/domains/{domain_id}/delete")
def delete_domain(request: Request, domain_id: int, session: Session = Depends(get_session)):
    domain = get_or_404(session, Domain, domain_id)
    if domain.mailboxes:
        flash(request, "Delete this domain's mailboxes first.", "error")
        return back(f"/domains/{domain_id}")
    session.delete(domain)
    session.commit()
    flash(request, f"{domain.name} deleted.")
    return back("/domains")


# ---------- mailboxes ----------

def _blank_mailbox(domain: Domain | None = None) -> Mailbox:
    preset = mailer.PRESETS.get(domain.provider if domain else "google", mailer.PRESETS["google"])
    return Mailbox(email=f"@{domain.name}" if domain else "", domain_id=domain.id if domain else None,
                   from_name="", username="", password_enc="", signature="",
                   smtp_host=preset["smtp_host"], smtp_port=preset["smtp_port"],
                   smtp_security=preset["smtp_security"], imap_host=preset["imap_host"],
                   imap_port=preset["imap_port"],
                   daily_limit=30, warmup_start=5, warmup_step=2, min_delay_seconds=120, max_delay_seconds=360,
                   time_zone="UTC", window_start_hour=9, window_end_hour=17, send_days="0,1,2,3,4",
                   status="active", last_error="")


def _apply_mailbox_form(session: Session, mailbox: Mailbox, form) -> list[str]:
    errors: list[str] = []
    with session.no_autoflush:
        domain = session.get(Domain, _int(form.get("domain_id"), 0, 0, 2_147_483_647))
        local_part = str(form.get("local_part", "")).strip().lower().split("@")[0]
        if domain is None:
            errors.append("Choose the domain this mailbox sends from.")
        if not LOCAL_PART_RE.match(local_part):
            errors.append("Enter the part before the @, like sam or sam.lee.")
        address = f"{local_part}@{domain.name}" if domain else f"{local_part}@"
        if domain and session.scalar(select(Mailbox.id).where(Mailbox.email == address,
                                                              Mailbox.id != (mailbox.id or 0))):
            errors.append(f"{address} is already added.")
        mailbox.email = address
        mailbox.domain_id = domain.id if domain else None

        mailbox.from_name = str(form.get("from_name", "")).strip()
        mailbox.smtp_host = str(form.get("smtp_host", "")).strip()
        mailbox.imap_host = str(form.get("imap_host", "")).strip()
        if not mailbox.smtp_host or not mailbox.imap_host:
            errors.append("Enter the SMTP and IMAP servers.")
        mailbox.smtp_port = _int(form.get("smtp_port"), 465, 1, 65535)
        mailbox.imap_port = _int(form.get("imap_port"), 993, 1, 65535)
        mailbox.smtp_security = "starttls" if form.get("smtp_security") == "starttls" else "ssl"
        mailbox.username = str(form.get("username", "")).strip() or address

        password = str(form.get("password", ""))
        if mailbox.smtp_host == "smtp.gmail.com":
            password = password.replace(" ", "")  # Google shows app passwords in groups of four
        if password:
            mailbox.password_enc = encrypt(password)
        elif not mailbox.password_enc:
            errors.append("Enter the app password.")

        mailbox.signature = _text(form.get("signature")).strip()
        mailbox.daily_limit = _int(form.get("daily_limit"), 30, 1, 500)
        mailbox.warmup_start = _int(form.get("warmup_start"), 5, 1, 500)
        mailbox.warmup_step = _int(form.get("warmup_step"), 2, 0, 100)
        mailbox.min_delay_seconds = _int(form.get("min_delay_seconds"), 120, 30, 3600)
        mailbox.max_delay_seconds = _int(form.get("max_delay_seconds"), 360, 30, 7200)

        zone = str(form.get("time_zone", "")).strip() or "UTC"
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"Unknown time zone '{zone}'. Use a name like Asia/Karachi or America/New_York.")
        mailbox.time_zone = zone
        mailbox.window_start_hour = _int(form.get("window_start_hour"), 9, 0, 23)
        mailbox.window_end_hour = _int(form.get("window_end_hour"), 17, 1, 24)
        if mailbox.window_end_hour <= mailbox.window_start_hour:
            errors.append("The sending window must end after it starts.")
        days = sorted({d for d in form.getlist("send_days") if d in {"0", "1", "2", "3", "4", "5", "6"}})
        if not days:
            errors.append("Pick at least one sending day.")
        mailbox.send_days = ",".join(days)
    return errors


def _mailbox_page(request: Request, session: Session, mailbox: Mailbox, errors=None, test_results=None,
                  status_code: int = 200):
    domains = all_domains(session)
    return page(request, "mailbox_form.html", status_code=status_code, mailbox=mailbox, errors=errors or [],
                test_results=test_results, domains=domains, presets=mailer.PRESETS,
                domain_providers={str(d.id): d.provider for d in domains}, timezones=TIMEZONES)


@router.get("/mailboxes", response_class=HTMLResponse)
def mailboxes(request: Request, session: Session = Depends(get_session)):
    return page(request, "mailboxes.html", rows=mailbox_rows(session), has_domains=bool(all_domains(session)))


@router.get("/mailboxes/new", response_class=HTMLResponse)
def new_mailbox(request: Request, domain_id: int = 0, session: Session = Depends(get_session)):
    domains = all_domains(session)
    if not domains:
        flash(request, "First add the domain your mailboxes send from.")
        return back("/domains")
    domain = session.get(Domain, domain_id) or (domains[0] if len(domains) == 1 else None)
    return _mailbox_page(request, session, _blank_mailbox(domain))


@router.post("/mailboxes", response_class=HTMLResponse)
async def create_mailbox(request: Request, session: Session = Depends(get_session)):
    mailbox = _blank_mailbox()
    errors = _apply_mailbox_form(session, mailbox, await request.form())
    if errors:
        return _mailbox_page(request, session, mailbox, errors, status_code=400)
    session.add(mailbox)
    session.commit()
    flash(request, f"Mailbox {mailbox.email} added. Click Test connection next.")
    return back(f"/mailboxes/{mailbox.id}")


@router.get("/mailboxes/{mailbox_id}", response_class=HTMLResponse)
def edit_mailbox(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    return _mailbox_page(request, session, get_or_404(session, Mailbox, mailbox_id))


@router.post("/mailboxes/{mailbox_id}", response_class=HTMLResponse)
async def update_mailbox(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    mailbox = get_or_404(session, Mailbox, mailbox_id)
    errors = _apply_mailbox_form(session, mailbox, await request.form())
    if errors:  # nothing is committed, so the saved mailbox stays unchanged
        return _mailbox_page(request, session, mailbox, errors, status_code=400)
    session.commit()
    flash(request, "Mailbox saved.")
    return back(f"/mailboxes/{mailbox_id}")


@router.post("/mailboxes/{mailbox_id}/test", response_class=HTMLResponse)
async def test_mailbox(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    mailbox = get_or_404(session, Mailbox, mailbox_id)
    results = await run_in_threadpool(mailer.test_connection, mailbox)
    if all(ok for _, ok, _ in results) and mailbox.status == "error":
        mailbox.status, mailbox.last_error = "active", ""
        session.commit()
    return _mailbox_page(request, session, mailbox, test_results=results)


@router.post("/mailboxes/{mailbox_id}/status")
def toggle_mailbox(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    mailbox = get_or_404(session, Mailbox, mailbox_id)
    if mailbox.status == "active":
        mailbox.status = "paused"
        flash(request, f"{mailbox.email} paused.")
    else:
        mailbox.status, mailbox.last_error = "active", ""
        flash(request, f"{mailbox.email} resumed.")
    session.commit()
    return back("/mailboxes")


@router.post("/mailboxes/{mailbox_id}/delete")
def delete_mailbox(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    mailbox = get_or_404(session, Mailbox, mailbox_id)
    session.delete(mailbox)
    session.commit()
    flash(request, f"{mailbox.email} deleted.")
    return back("/mailboxes")


# ---------- leads ----------

def _leads_page(request: Request, session: Session, q: str = "", source: str = "", page_no: int = 1,
                result=None, error: str = "", status_code: int = 200):
    query = select(Lead)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Lead.email.ilike(like), Lead.first_name.ilike(like),
                                Lead.last_name.ilike(like), Lead.company.ilike(like)))
    if source:
        query = query.where(Lead.source == source)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page_no = max(1, min(page_no, pages))
    rows = session.scalars(query.order_by(Lead.created_at.desc(), Lead.id.desc())
                           .offset((page_no - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    sources = [s for s in session.scalars(select(Lead.source).distinct().order_by(Lead.source)) if s]
    return page(request, "leads.html", status_code=status_code, leads=rows, total=total, page_no=page_no,
                pages=pages, q=q, source=source, sources=sources, result=result, error=error)


@router.get("/leads", response_class=HTMLResponse)
def leads(request: Request, q: str = "", source: str = "", page_no: int = Query(1, alias="page"),
          session: Session = Depends(get_session)):
    return _leads_page(request, session, q, source, page_no)


@router.post("/leads/import", response_class=HTMLResponse)
async def import_leads(request: Request, file: UploadFile = File(...), check_mx: bool = Form(False),
                       skip_role: bool = Form(False), session: Session = Depends(get_session)):
    data = await file.read()
    if len(data) > MAX_CSV_BYTES:
        return _leads_page(request, session, error="File is larger than 20 MB. Split it up.", status_code=400)
    source = f"{Path(file.filename or 'upload.csv').name} ({utcnow():%Y-%m-%d %H:%M})"
    try:
        result = await run_in_threadpool(import_csv, session, data, source, check_mx, skip_role)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _leads_page(request, session, error=str(exc), status_code=400)
    return _leads_page(request, session, source=source, result=result)


@router.post("/leads/{lead_id}/delete")
def delete_lead(request: Request, lead_id: int, session: Session = Depends(get_session)):
    lead = get_or_404(session, Lead, lead_id)
    session.delete(lead)
    session.commit()
    flash(request, f"{lead.email} deleted.")
    return back("/leads")


# ---------- campaigns ----------

def _sample_fields(session: Session, campaign: Campaign) -> dict[str, str]:
    enrollment = session.scalar(select(Enrollment).where(Enrollment.campaign_id == campaign.id)
                                .order_by(Enrollment.id).limit(1))
    lead = enrollment.lead if enrollment else session.scalar(select(Lead).order_by(Lead.id).limit(1))
    if lead:
        return lead.fields
    return {"email": "jane@example.com", "first_name": "Jane", "last_name": "Doe", "company": "Acme Inc",
            "title": "Founder", "website": "acme.com"}


def _campaign_mailbox(campaign: Campaign) -> Mailbox | None:
    return next((m for m in campaign.mailboxes if m.status == "active"), None) or \
        (campaign.mailboxes[0] if campaign.mailboxes else None)


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns(request: Request, session: Session = Depends(get_session)):
    rows = [(c, campaign_stats(session, c.id))
            for c in session.scalars(select(Campaign).order_by(Campaign.created_at.desc()))]
    return page(request, "campaigns.html", rows=rows)


@router.post("/campaigns")
def create_campaign(request: Request, name: str = Form(""), session: Session = Depends(get_session)):
    campaign = Campaign(name=name.strip() or "Untitled campaign", status="draft", sender_address="",
                        footer=DEFAULT_FOOTER, stop_on_company_reply=True)
    campaign.steps = [Step(position=i, delay_days=delay, subject=subject, body=body)
                      for i, (delay, subject, body) in enumerate(STARTER_STEPS, start=1)]
    session.add(campaign)
    session.commit()
    flash(request, "Campaign created with a 3-email starter sequence. Rewrite it in your own words.")
    return back(f"/campaigns/{campaign.id}")


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    errors, warnings = start_errors(campaign)
    fields = _sample_fields(session, campaign)
    mailbox = _campaign_mailbox(campaign) or Mailbox(email="you@yourdomain.com", from_name="Your Name", signature="")
    previews, thread_subject = [], ""
    for step in campaign.steps:
        draft = compose(campaign, step, fields, mailbox, thread_subject)
        if not thread_subject or step.subject.strip():
            thread_subject = draft.subject
        previews.append({"step": step, "draft": draft, "warnings": check_content(step.subject, step.body)})
    return page(
        request, "campaign_detail.html", campaign=campaign, stats=campaign_stats(session, campaign.id),
        errors=errors, warnings=warnings, previews=previews, sample_email=fields.get("email", ""),
        enrollments=session.scalars(select(Enrollment).where(Enrollment.campaign_id == campaign.id)
                                    .order_by(Enrollment.updated_at.desc()).limit(100)).all(),
        sources=[s for s in session.scalars(select(Lead.source).distinct().order_by(Lead.source)) if s],
        all_mailboxes=session.scalars(select(Mailbox).order_by(Mailbox.email)).all(),
        selected_ids={m.id for m in campaign.mailboxes},
    )


@router.post("/campaigns/{campaign_id}/settings")
async def save_campaign_settings(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    form = await request.form()
    campaign.name = str(form.get("name", "")).strip() or campaign.name
    campaign.sender_address = _text(form.get("sender_address")).strip()
    campaign.footer = _text(form.get("footer")).strip() or DEFAULT_FOOTER
    campaign.stop_on_company_reply = form.get("stop_on_company_reply") == "true"
    ids = {int(v) for v in form.getlist("mailbox_ids") if str(v).isdigit()}
    campaign.mailboxes = list(session.scalars(select(Mailbox).where(Mailbox.id.in_(ids)))) if ids else []
    session.commit()
    flash(request, "Settings saved.")
    return back(f"/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/steps")
async def save_steps(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    form = await request.form()
    subjects, bodies, delays = form.getlist("subject"), form.getlist("body"), form.getlist("delay_days")
    action = str(form.get("action", "save"))
    rows = []
    for i, body in enumerate(bodies):
        if action == f"remove:{i}":
            continue
        subject = str(subjects[i]) if i < len(subjects) else ""
        rows.append((subject.strip(), _text(body).strip(), _int(delays[i] if i < len(delays) else 3, 3, 0, 60)))
    if action == "add":
        rows.append(("", "Hi {{first_name|there}},\n\n", 3))

    campaign.steps.clear()
    session.flush()
    for position, (subject, body, delay) in enumerate(rows, start=1):
        campaign.steps.append(Step(position=position, subject=subject, body=body,
                                   delay_days=0 if position == 1 else delay))
    session.commit()
    flash(request, "Emails saved.")
    return back(f"/campaigns/{campaign_id}#emails")


@router.post("/campaigns/{campaign_id}/enroll")
def enroll(request: Request, campaign_id: int, source: str = Form(""), session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    added, skipped = enroll_leads(session, campaign, source or None)
    flash(request, f"Added {added} leads. Skipped {skipped} (already in this campaign, suppressed, "
                   "replied before, or in another active sequence).")
    return back(f"/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/start")
def start_campaign(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    errors, _ = start_errors(campaign)
    if errors:
        for error in errors:
            flash(request, error, "error")
        return back(f"/campaigns/{campaign_id}")
    campaign.status = "active"
    session.commit()
    flash(request, "Campaign started. Emails go out slowly during each mailbox's sending hours.")
    return back(f"/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    campaign.status = "paused"
    session.commit()
    flash(request, "Campaign paused. Nothing more will be sent until you start it again.")
    return back(f"/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/test")
async def send_test(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    form = await request.form()
    to_email = str(form.get("to_email", "")).strip()
    position = _int(form.get("position"), 1, 1, 100)
    mailbox = _campaign_mailbox(campaign)
    step = next((s for s in campaign.steps if s.position == position), None)
    if "@" not in to_email or mailbox is None or step is None:
        flash(request, "Assign a mailbox to this campaign and enter a valid test address.", "error")
        return back(f"/campaigns/{campaign_id}")

    # Use the test address for the unsubscribe link so clicking it never unsubscribes a real lead.
    fields = dict(_sample_fields(session, campaign), email=to_email)
    first = compose(campaign, campaign.steps[0], fields, mailbox)
    draft = compose(campaign, step, fields, mailbox, "" if step is campaign.steps[0] else first.subject)
    msg = mailer.build_message(mailbox, to_email, draft.subject, draft.body, draft.unsubscribe_url)
    try:
        await run_in_threadpool(mailer.send, mailbox, msg)
    except Exception as exc:  # show the provider's error to the user
        flash(request, f"Test failed: {mailer.error_text(exc)}", "error")
        return back(f"/campaigns/{campaign_id}")
    note = f" Missing data was left blank: {', '.join(sorted(set(draft.missing)))}." if draft.missing else ""
    flash(request, f"Test email {position} sent to {to_email} from {mailbox.email}. "
                   f"Check whether it landed in Inbox or Spam.{note}")
    return back(f"/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/delete")
def delete_campaign(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = get_or_404(session, Campaign, campaign_id)
    session.delete(campaign)
    session.commit()
    flash(request, f"Campaign '{campaign.name}' deleted.")
    return back("/campaigns")


# ---------- suppression list and inbox ----------

@router.get("/suppression", response_class=HTMLResponse)
def suppression_list(request: Request, q: str = "", session: Session = Depends(get_session)):
    query = select(Suppression).order_by(Suppression.created_at.desc())
    if q.strip():
        query = query.where(Suppression.value.ilike(f"%{q.strip()}%"))
    return page(request, "suppression.html", q=q, rows=session.scalars(query.limit(500)).all(),
                total=session.scalar(select(func.count(Suppression.id))) or 0)


@router.post("/suppression")
def add_suppression(request: Request, values: str = Form(""), session: Session = Depends(get_session)):
    added = 0
    for raw in values.replace(",", "\n").splitlines():
        value = raw.strip().lower()
        if value and "." in value:
            suppress(session, value, "manual")
            added += 1
    session.commit()
    flash(request, f"Added {added} entries. They will never be emailed.")
    return back("/suppression")


@router.post("/suppression/{row_id}/delete")
def delete_suppression(request: Request, row_id: int, session: Session = Depends(get_session)):
    row = get_or_404(session, Suppression, row_id)
    session.delete(row)
    session.commit()
    flash(request, f"{row.value} removed from the suppression list.")
    return back("/suppression")


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, kind: str = "", session: Session = Depends(get_session)):
    query = select(InboxEvent).order_by(InboxEvent.received_at.desc())
    if kind:
        query = query.where(InboxEvent.kind == kind)
    return page(request, "inbox.html", kind=kind, events=session.scalars(query.limit(200)).all())


@router.post("/inbox/check")
async def inbox_check(request: Request):
    ran = await run_in_threadpool(check_inboxes)
    flash(request, "Inboxes checked." if ran else "An inbox check is already running. Try again in a minute.")
    return back("/inbox")


app.include_router(router)
