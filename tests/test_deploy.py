import json
from pathlib import Path

from app.db import _normalise_url

ROOT = Path(__file__).resolve().parent.parent


def test_hosted_postgres_urls_use_the_psycopg_driver():
    assert _normalise_url("postgres://u:p@host/db") == "postgresql+psycopg://u:p@host/db"
    assert _normalise_url("postgresql://u:p@host/db?sslmode=require") == \
        "postgresql+psycopg://u:p@host/db?sslmode=require"
    assert _normalise_url("sqlite:///data/outreach.db") == "sqlite:///data/outreach.db"


def test_vercel_uses_the_fastapi_entrypoint():
    """Vercel finds the FastAPI app itself. A rewrite would hand it the wrong path (/api/index)."""
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert "app/main.py" in config["functions"]
    assert "app/**" in config["functions"]["app/main.py"]["includeFiles"]
    assert "rewrites" not in config and "routes" not in config
    assert not (ROOT / "api").exists()


def test_blank_settings_fall_back_to_defaults(monkeypatch):
    """A hosting dashboard makes it easy to create a variable with an empty value."""
    from app.config import Settings

    for name in ("HOST", "PORT", "PUBLIC_BASE_URL", "WORKER_ENABLED", "SEND_TICK_SECONDS", "INBOX_TICK_SECONDS"):
        monkeypatch.setenv(name, "")
    settings = Settings()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.public_base_url == "http://localhost:8000"
    assert settings.worker_enabled is True
    assert (settings.send_tick_seconds, settings.inbox_tick_seconds) == (30, 300)
