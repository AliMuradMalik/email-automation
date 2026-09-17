"""Database engine and session helpers."""
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import BASE_DIR, settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    if settings.database_url:
        url = settings.database_url
    else:
        (BASE_DIR / "data").mkdir(exist_ok=True)
        url = URL.create("sqlite", database=str(BASE_DIR / "data" / "outreach.db"))

    if not str(url).startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)

    # The web app and the background worker share one SQLite file across threads.
    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(eng, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401  registers the tables

    Base.metadata.create_all(engine)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    """FastAPI dependency that yields a session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
