"""Encrypted mailbox passwords, signed unsubscribe links and the admin login check."""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeSerializer

from .config import settings


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Saved mailbox password can't be decrypted. Was SECRET_KEY changed?") from exc


def _unsubscribe_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="unsubscribe")


def unsubscribe_url(email: str) -> str:
    """Signed link, so nobody can unsubscribe an address they don't control."""
    token = _unsubscribe_serializer().dumps(email.strip().lower())
    return f"{settings.public_base_url}/u/{token}"


def read_unsubscribe_token(token: str) -> str | None:
    try:
        value = _unsubscribe_serializer().loads(token)
    except BadSignature:
        return None
    return value if isinstance(value, str) and "@" in value else None


def check_admin_password(candidate: str) -> bool:
    expected = settings.admin_password
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())
