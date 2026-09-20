"""One engine per database role (Backend Plan §5.1, §15.1).

The rule this module exists to enforce:

    executor.py receives an engine, never a DSN, and cannot construct one.

Each role gets its own engine, built once here. Nothing downstream may reach
for a connection string, so no module can quietly acquire more privilege than
the caller intended. If you find yourself importing `settings` into something
under core/ to build an engine, that is the bug this file prevents.

The read engine also sets the statement timeout and a read-only transaction on
every connection, so the database refuses a write even if everything above it
somehow agrees to one. Belt, braces, and a third check at the grant.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.config import Settings


def _build(dsn: str, *, read_only: bool, timeout_ms: int | None, label: str) -> Engine:
    engine = create_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        future=True,
        connect_args={"application_name": f"speakql:{label}"},
    )

    if read_only or timeout_ms:
        @event.listens_for(engine, "connect")
        def _configure(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            if timeout_ms:
                cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
            if read_only:
                # Every transaction on this engine is read-only at the server.
                cur.execute("SET default_transaction_read_only = on")
            cur.close()

    return engine


class Engines:
    """The four runtime engines, and nothing that can make a fifth."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        self.meta = _build(
            settings.meta_dsn, read_only=False, timeout_ms=None, label="meta"
        )
        # Every read in the product goes through this one.
        self.read = _build(
            settings.ro_dsn,
            read_only=True,
            timeout_ms=settings.statement_timeout_ms,
            label="ro",
        )
        # The edit path, and nothing else.
        self.write = _build(
            settings.write_dsn, read_only=False, timeout_ms=settings.statement_timeout_ms,
            label="write",
        )
        # Pending member corrections. Holds nothing on public.
        self.edits = _build(
            settings.edits_dsn, read_only=False, timeout_ms=settings.statement_timeout_ms,
            label="edits",
        )
        # File ingestion only.
        self.uploads = _build(
            settings.uploads_dsn, read_only=False, timeout_ms=None, label="uploads",
        )

    def dispose(self) -> None:
        for engine in (self.meta, self.read, self.write, self.edits, self.uploads):
            engine.dispose()

    def check(self) -> dict[str, str]:
        """Ping each engine. Used by /health -- see main.py."""
        out: dict[str, str] = {}
        for name, engine in (
            ("meta", self.meta),
            ("read", self.read),
            ("write", self.write),
            ("edits", self.edits),
            ("uploads", self.uploads),
        ):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                out[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - health must not raise
                out[name] = f"unavailable: {type(exc).__name__}"
        return out


@lru_cache(maxsize=1)
def engines_for(settings: Settings) -> Engines:
    return Engines(settings)
