# Outreach: cold email dashboard

A small web app that sends personalised cold emails and follow-ups from your own mailboxes, slowly and politely,
so they land in the inbox instead of spam.

- Sends from real mailboxes (Google Workspace recommended) over SMTP, one email at a time, with random gaps
- Ramps each new mailbox up slowly (5 emails on day one, +2 a day, up to your limit), only during business hours
- Follow-ups go out in the same email thread and **stop automatically when someone replies**
- Reads each inbox over IMAP to catch replies, bounces, out-of-office messages and "remove me" requests
- One-click unsubscribe link and header (what Gmail and Yahoo require), plus a permanent do-not-contact list
- Pauses a mailbox by itself if too many emails bounce
- Checks your domain's SPF, DKIM and DMARC records and warns about spammy wording before you send
- Plain-text emails, no open or click tracking (tracking pixels and redirect links hurt cold email delivery)

## Why not Brevo, Amazon SES or Resend?

Those services only allow emailing people who opted in. Brevo, for example, bans purchased and scraped lists and
suspends accounts that send cold email. Cold email has to go through normal mailboxes you own, at low volume.
Zoho Mail also blocks automated cold email, and Microsoft 365 is turning off password logins for apps by default
at the end of 2026, so **Google Workspace** is the recommended mailbox provider.

## What it costs

| Item | While testing | After testing |
|---|---|---|
| A separate sending domain | about $10-15 per year | same |
| Google Workspace mailbox | 14-day free trial (limited to 500 emails/day) | $7/mailbox/month yearly, or $8.40 month-to-month |
| This app | free, runs on your PC | free |
| Public web address for unsubscribe links | not needed for tests to yourself | free with Cloudflare Tunnel, or a small server for about $5/month |

Example: 2 mailboxes on one new domain is about $15-17 a month and safely sends 40-80 cold emails a day once warmed up.

## Setup

### 1. Buy a separate domain

Use a domain that looks like your brand but is not your main one, for example `trythebrand.com` next to
`thebrand.com`. If a cold campaign goes badly, only this domain is hurt and your normal business email keeps working.
Point the new domain's website to your main site so people who check it see your real business.

### 2. Create Google Workspace mailboxes

Sign up for Google Workspace on the new domain and create 1-3 mailboxes with real names (`sam@trythebrand.com`),
each with a profile photo and signature.

### 3. Add DNS records

In your domain's DNS settings add:

- **SPF**: TXT record on `@` with `v=spf1 include:_spf.google.com ~all`
- **DKIM**: in Google Admin go to Apps > Google Workspace > Gmail > Authenticate email, generate a key, add the TXT
  record it shows, then click Start authentication
- **DMARC**: TXT record on `_dmarc` with `v=DMARC1; p=none`

The app's **Domains** page lists these records for your domain and checks whether they are working.

### 4. Create an app password

Normal passwords no longer work for apps. For each mailbox: turn on 2-Step Verification in the Google account, then
create an app password at <https://myaccount.google.com/apppasswords>. If the page is missing, a Workspace admin has to
allow app passwords.

### 5. Run the app

```
cd "D:\email automation"
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

The first start creates a `.env` file and prints your dashboard password. Open <http://127.0.0.1:8000>.

### 6. Add your domain and mailboxes

1. **Domains** > add your sending domain and pick Google Workspace. The page lists the exact DNS records to create.
   Click **Check DNS** until every row says OK; campaigns can't start until it does.
2. **Mailboxes** > Add mailbox: pick the domain, type the name before the @ (like `sam`), paste the app password and
   set the time zone of the people you are emailing. Then click **Test connection**.

Use a brand-new mailbox normally for a week or two before cold emailing (send and receive real emails). Then let the
app's slow ramp-up do the rest.

### 7. Import leads

Leads > Import CSV. The file needs an `email` column; `first_name`, `company`, `title` and `website` are recognised,
and any other column becomes a placeholder (a `city` column can be used as `{{city}}`). The importer drops invalid
addresses, domains that can't receive email, duplicates, shared inboxes like `info@`, and anyone on the do-not-contact list.

### 8. Create a campaign

1. Campaigns > create one. It starts with a 3-email example sequence; rewrite the `[bracketed]` parts.
2. Pick the mailboxes, fill in your postal address, keep `{{unsubscribe_url}}` in the footer.
3. Send yourself tests at a Gmail and an Outlook address. Check that they land in the inbox.
4. Add leads, then click **Start sending**.

Keep the app running. It sends during each mailbox's hours and checks inboxes every 5 minutes.

## Before emailing real leads: make the unsubscribe link public

Unsubscribe links point at `PUBLIC_BASE_URL` in `.env`. While that is `localhost`, the links only work on your PC,
which is fine for tests to yourself. Before emailing real people, give the app a public https address:

- **Free**: move your domain's DNS to Cloudflare (free plan) and run a Cloudflare Tunnel on this PC, mapping
  for example `https://go.trythebrand.com` to `http://127.0.0.1:8000`. The PC has to stay on.
- **Always on**: rent a small Linux server (about $5/month), run the app there behind Caddy or nginx for https.

Then set `PUBLIC_BASE_URL=https://go.trythebrand.com` and restart. Run only one copy of the app, or emails get sent twice.

## Put it online with Vercel

Vercel only runs code when a request arrives, and it wipes files between deploys. So two things change:
the database moves to free hosted Postgres, and an outside timer triggers the sending.

1. **Database**: sign up at neon.com (free), create a project and copy the pooled connection string. It looks like
   `postgresql://user:password@ep-something-pooler.region.aws.neon.tech/neondb?sslmode=require`.
2. **Import the repo**: at vercel.com choose Add New > Project, import this GitHub repository, and pick "Other"
   as the framework.
3. **Environment variables** (Settings > Environment Variables):

   | Name | Value |
   |---|---|
   | `DATABASE_URL` | the Neon connection string |
   | `SECRET_KEY` | a long random string; never change it after adding mailboxes |
   | `ADMIN_PASSWORD` | your dashboard password |
   | `PUBLIC_BASE_URL` | `https://your-project.vercel.app` (set it after the first deploy, then redeploy) |
   | `WORKER_ENABLED` | `false` |
   | `CRON_SECRET` | another long random string |

4. **Deploy**, open the URL and log in. The hosted app starts with an empty database, so add your domain,
   mailboxes and leads again there.
5. **The timer**: sign up at cron-job.org (free) and create a job that calls
   `https://your-project.vercel.app/tasks/tick?key=YOUR_CRON_SECRET` every 3 minutes. Each call sends whatever is
   due and checks the inboxes. Vercel's own cron can't do this on the free plan, where jobs run only once a day.

To check it works, open `https://your-project.vercel.app/tasks/tick?key=YOUR_CRON_SECRET` in a browser. It should
answer `{"sent": 0, "inboxes_checked": true}`.

Worth knowing:

- One email per mailbox per timer call, so a 3-minute timer allows about 20 per hour per mailbox, far above the
  20-40 per day you should actually send.
- Vercel's free Hobby plan is meant for personal, non-commercial projects; business use needs Pro at $20/month.
  A small Linux server at about $5/month runs this app unchanged, background sender included.
- Keep `WORKER_ENABLED=false` on Vercel, and never commit `.env`.

## Rules that keep you out of spam

- 20-40 cold emails per mailbox per day, at most. Add mailboxes to send more, don't raise the limit.
- Email people you have a real reason to contact, and say that reason in the first line.
- Keep it short (50-125 words), plain text, zero or one link, no attachments or images.
- Personalise with more than the first name. Don't send the same text to thousands of people.
- Keep bounces under 3%: import only verified addresses. The app pauses a mailbox above 5%.
- Keep spam complaints under 0.1%. Google starts blocking at 0.3%. Free Google Postmaster Tools shows your rate.
- Honour every "stop". The app does this automatically; never remove people from the do-not-contact list to email them again.

## Legal basics (not legal advice)

- **USA (CAN-SPAM)**: cold B2B email is allowed if the sender and subject are honest, the email includes your postal
  address, and opt-outs are honoured.
- **UK/EU (GDPR, PECR)**: stricter. Emailing work addresses about something relevant to their job can be allowed, but
  rules differ by country, and emailing private individuals usually needs consent.
- **Canada (CASL)**: generally needs consent. Check before emailing Canadian addresses.

## How it works

| Part | File |
|---|---|
| Web dashboard | `app/main.py`, `app/templates/` |
| Sending engine: pacing, ramp-up, follow-ups, auto-pause | `app/scheduler.py` |
| SMTP sending and email headers | `app/mailer.py` |
| Reply, bounce and unsubscribe detection over IMAP | `app/inbox.py` |
| Background worker thread | `app/worker.py` |
| CSV import and validation | `app/leads_import.py` |
| DNS checks | `app/dns_check.py` |
| Database tables (SQLite in `data/`) | `app/models.py` |

Mailbox passwords are stored encrypted with `SECRET_KEY`. Don't change that key after adding mailboxes.

Run the tests with:

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```
