import os
import sys
import tempfile
from pathlib import Path

# Configure a throwaway database and settings before the app is imported.
_tmp = Path(tempfile.mkdtemp(prefix="outreach-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-definitely-long-enough"
os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["PUBLIC_BASE_URL"] = "https://outreach.example.com"
os.environ["WORKER_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture()
def session():
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
