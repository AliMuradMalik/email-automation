import json
from pathlib import Path

from app.db import _normalise_url

ROOT = Path(__file__).resolve().parent.parent


def test_hosted_postgres_urls_use_the_psycopg_driver():
    assert _normalise_url("postgres://u:p@host/db") == "postgresql+psycopg://u:p@host/db"
    assert _normalise_url("postgresql://u:p@host/db?sslmode=require") == \
        "postgresql+psycopg://u:p@host/db?sslmode=require"
    assert _normalise_url("sqlite:///data/outreach.db") == "sqlite:///data/outreach.db"


def test_vercel_config_sends_every_path_to_the_app():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["rewrites"] == [{"source": "/(.*)", "destination": "/api/index"}]
    assert "app/**" in config["functions"]["api/index.py"]["includeFiles"]


def test_entry_point_exposes_the_app():
    assert "from app.main import app" in (ROOT / "api" / "index.py").read_text(encoding="utf-8")


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
