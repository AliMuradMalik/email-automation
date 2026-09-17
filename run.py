"""Start the dashboard with `python run.py`."""
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def ensure_env_file() -> None:
    """Create .env with a random dashboard password and secret key on first start."""
    if ENV_FILE.exists():
        return
    password = secrets.token_urlsafe(12)
    content = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    content = content.replace("ADMIN_PASSWORD=\n", f"ADMIN_PASSWORD={password}\n", 1)
    content = content.replace("SECRET_KEY=\n", f"SECRET_KEY={secrets.token_urlsafe(48)}\n", 1)
    ENV_FILE.write_text(content, encoding="utf-8")
    print(f"Created .env. Your dashboard password is: {password}")
    print("You can change ADMIN_PASSWORD in .env at any time.")


if __name__ == "__main__":
    ensure_env_file()

    import uvicorn

    from app.config import settings

    settings.validate()
    print(f"Dashboard: http://{settings.host}:{settings.port}")
    # One process only: the background sender must not run twice.
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1)
