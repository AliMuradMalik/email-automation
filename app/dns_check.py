"""DNS records a sending domain needs, and checks that they are in place."""
import json
from dataclasses import asdict, dataclass

import dns.exception
import dns.resolver

LOOKUP_TIMEOUT = 6

PROVIDERS = {
    "google": {
        "label": "Google Workspace", "spf_include": "_spf.google.com", "spf_match": "_spf.google.com",
        "dkim_selector": "google", "dkim_type": "TXT", "mx": "smtp.google.com (priority 1)",
        "dkim_where": "Google Admin > Apps > Google Workspace > Gmail > Authenticate email",
    },
    "microsoft": {
        "label": "Microsoft 365", "spf_include": "spf.protection.outlook.com", "spf_match": "spf.protection.outlook.com",
        "dkim_selector": "selector1", "dkim_type": "CNAME", "mx": "Copy the MX record Microsoft 365 admin shows you",
        "dkim_where": "Microsoft Defender portal > Email authentication settings > DKIM",
    },
    "zoho": {
        "label": "Zoho Mail", "spf_include": "zohomail.com", "spf_match": "zoho",
        "dkim_selector": "zmail", "dkim_type": "TXT", "mx": "Copy the MX records Zoho Mail admin shows you",
        "dkim_where": "Zoho Mail Admin Console > Domains > Email configuration > DKIM",
    },
    "other": {
        "label": "Other provider", "spf_include": "", "spf_match": "",
        "dkim_selector": "default", "dkim_type": "TXT", "mx": "Copy the MX records your email provider gives you",
        "dkim_where": "your email provider's admin console",
    },
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    tip: str = ""


@dataclass
class Record:
    type: str
    host: str
    value: str
    note: str = ""


def provider_info(provider: str) -> dict:
    return PROVIDERS.get(provider, PROVIDERS["other"])


def records_to_add(domain: str, provider: str, dkim_selector: str) -> list[Record]:
    """The DNS records to create where the domain is managed."""
    info = provider_info(provider)
    spf = (f"v=spf1 include:{info['spf_include']} ~all" if info["spf_include"]
           else "v=spf1 include:(your provider's SPF domain) ~all")
    return [
        Record("MX", "@", info["mx"], "Lets this domain receive replies."),
        Record("TXT", "@", spf, "SPF. A domain can only have one SPF record; if one exists, add the include to it."),
        Record(info["dkim_type"], f"{dkim_selector}._domainkey",
               f"Generate it in {info['dkim_where']}, then turn DKIM signing on.",
               "DKIM. The value is a long key your provider creates for you."),
        Record("TXT", "_dmarc", "v=DMARC1; p=none",
               "DMARC. Start with p=none; switch to p=quarantine after a few weeks of clean sending."),
    ]


def _txt(name: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=LOOKUP_TIMEOUT)
    except dns.exception.DNSException:
        return []
    return [b"".join(r.strings).decode(errors="replace") for r in answers]


def check_domain(domain: str, dkim_selector: str, provider: str = "google") -> list[Check]:
    domain = domain.strip().lower()
    info = provider_info(provider)
    checks: list[Check] = []

    try:
        mx = sorted(str(r.exchange).rstrip(".") for r in dns.resolver.resolve(domain, "MX", lifetime=LOOKUP_TIMEOUT))
    except dns.exception.DNSException:
        mx = []
    checks.append(Check("MX (receives replies)", bool(mx), ", ".join(mx) or "No MX records",
                        "" if mx else "Add the MX record from the table above."))

    spf = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
    if not spf:
        checks.append(Check("SPF", False, "No SPF record", "Add the SPF record from the table above."))
    elif len(spf) > 1:
        checks.append(Check("SPF", False, f"{len(spf)} SPF records", "Merge them into one v=spf1 record."))
    elif spf[0].rstrip().endswith("+all"):
        checks.append(Check("SPF", False, spf[0], "Remove '+all'. It lets anyone send as your domain."))
    elif info["spf_match"] and info["spf_match"] not in spf[0].lower():
        checks.append(Check("SPF", False, spf[0],
                            f"Add include:{info['spf_include']} so {info['label']} is allowed to send for this domain."))
    else:
        checks.append(Check("SPF", True, spf[0]))

    dkim_name = f"{dkim_selector}._domainkey.{domain}"
    dkim = [t for t in _txt(dkim_name) if "p=" in t]
    checks.append(Check(f"DKIM (selector '{dkim_selector}')", bool(dkim),
                        "Found" if dkim else f"No record at {dkim_name}",
                        "" if dkim else f"Generate the key in {info['dkim_where']}, add it, and turn signing on. "
                                        "If your provider uses a different selector, change it on this page."))

    dmarc = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    if dmarc:
        tags = dict(part.strip().split("=", 1) for part in dmarc[0].split(";") if "=" in part)
        policy = tags.get("p", "").strip().lower()
        checks.append(Check("DMARC", True, dmarc[0],
                            "" if policy in ("quarantine", "reject") else
                            "Policy is 'none'. Fine to start; switch to p=quarantine after a few weeks of clean sending."))
    else:
        checks.append(Check("DMARC", False, "No DMARC record", "Add the DMARC record from the table above."))
    return checks


def checks_to_json(checks: list[Check]) -> str:
    return json.dumps([asdict(check) for check in checks])
