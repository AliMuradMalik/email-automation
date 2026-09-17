"""Settings loaded from the .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
        self.secret_key = os.getenv("SECRET_KEY", "")
        self.database_url = os.getenv("DATABASE_URL", "")
        self.cron_secret = os.getenv("CRON_SECRET", "")
        self.public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        self.host = os.getenv("HOST", "127.0.0.1")
        self.port = int(os.getenv("PORT", "8000"))
        self.worker_enabled = _bool("WORKER_ENABLED", True)
        self.send_tick_seconds = int(os.getenv("SEND_TICK_SECONDS", "30"))
        self.inbox_tick_seconds = int(os.getenv("INBOX_TICK_SECONDS", "300"))

    def validate(self) -> None:
        missing = [name for name, value in (("ADMIN_PASSWORD", self.admin_password),
                                            ("SECRET_KEY", self.secret_key)) if not value]
        if missing:
            raise RuntimeError(f"Missing in .env: {', '.join(missing)}. Run `python run.py` to generate them.")
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters long.")


settings = Settings()
