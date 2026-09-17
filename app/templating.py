"""Personalisation placeholders and pre-send content checks."""
import re
from dataclasses import dataclass, field

# {{first_name}} or {{first_name|there}} (value after | is the fallback when the field is empty)
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*(?:\|\s*(.*?)\s*)?\}\}")

SPAM_PHRASES = (
    "100% free", "100% satisfied", "act now", "best price", "buy now", "call now", "cash bonus",
    "click here", "congratulations", "double your", "earn money", "extra income", "free money",
    "guaranteed", "increase sales", "limited time", "lowest price", "make money", "no cost",
    "no obligation", "not spam", "once in a lifetime", "order now", "risk-free", "risk free",
    "special promotion", "this isn't spam", "urgent", "winner", "you have been selected", "$$$",
)
LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
HTML_TAG = re.compile(r"<(a|br|div|img|p|span|table)\b", re.IGNORECASE)


@dataclass
class Rendered:
    text: str
    missing: list[str] = field(default_factory=list)


def render(template: str, values: dict[str, str]) -> Rendered:
    """Fill placeholders. Fields that are empty and have no fallback are reported in `missing`."""
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        name, fallback = match.group(1).lower(), match.group(2)
        value = str(values.get(name) or "").strip()
        if value:
            return value
        if fallback is not None:
            return fallback
        missing.append(name)
        return ""

    return Rendered(PLACEHOLDER.sub(replace, template), missing)


def check_content(subject: str, body: str) -> list[str]:
    """Warnings about things that commonly push cold emails into spam or hurt replies."""
    warnings: list[str] = []
    text = f"{subject}\n{body}".lower()

    found = sorted(p for p in SPAM_PHRASES if p in text)
    if found:
        warnings.append("Spammy phrases: " + ", ".join(found))
    if subject:
        if len(subject) > 60:
            warnings.append("Subject is long. Keep it under about 60 characters.")
        letters = [c for c in subject if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.5:
            warnings.append("Subject is mostly capital letters.")
    if re.search(r"[!?]{2,}", text):
        warnings.append("Repeated !! or ?? looks spammy.")
    links = len(LINK.findall(body))
    if links > 1:
        warnings.append(f"{links} links in the body. Cold emails land better with 0 or 1 link "
                        "(the unsubscribe link is added by the footer).")
    words = len(PLACEHOLDER.sub("x", body).split())
    if words > 150:
        warnings.append(f"Body is {words} words. Short emails (50-125 words) get more replies.")
    if HTML_TAG.search(body):
        warnings.append("HTML tags found. Emails are sent as plain text, so tags would show literally.")
    if "{{" in PLACEHOLDER.sub("", body):
        warnings.append("A placeholder looks broken. Use {{first_name}} or {{first_name|there}}.")
    if has_example_text(f"{subject}\n{body}"):
        warnings.append("Replace the [bracketed] example text with your own words.")
    return warnings


EXAMPLE_TEXT = re.compile(r"\[[^\]\n]{3,}\]")


def has_example_text(text: str) -> bool:
    return bool(EXAMPLE_TEXT.search(text))
