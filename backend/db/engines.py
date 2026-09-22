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
from sqlalchemy.engine import URL, Engine, make_url

from app.config import Settings


def _build(dsn: str | URL, *, read_only: bool, timeout_ms: int | None,
           label: str) -> Engine:
    # Postgres is the production database. SQLite appears only in
    # tests/test_api.py, which drives the real app without a container -- and
    # it supports neither connection pooling options nor the two server
    # settings below, so both are applied conditionally rather than guarded at
    # every call site.
    url = make_url(dsn)
    is_postgres = url.get_backend_name() == "postgresql"

    options: dict = {"future": True}
    if is_postgres:
        options.update(
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            # connect_timeout: an external host that never answers must not
            # hold a worker for the operating system's TCP timeout.
            connect_args={"application_name": f"speakql:{label}",
                          "connect_timeout": 10},
        )

    engine = create_engine(url, **options)

    if is_postgres and (read_only or timeout_ms):
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
    """The application's own engines, and the only door to a warehouse.

    Deliberately absent: a `read`, `write` or `edits` engine bound to one
    warehouse. The first version had exactly those, and every question ran on
    whichever warehouse their DSN named -- whatever database the question was
    about. See db/tenant_engine.py. A warehouse engine is reached only through
    `self.tenants`, built from a connection row the caller was authorised to
    hold; a route that tries `engines.read` now fails loudly instead of
    reading the wrong company's data quietly.
    """

    def __init__(self, settings: Settings) -> None:
        from db.tenant_engine import TenantEngines  # noqa: PLC0415 - cycle

        self._settings = settings

        # The application's own data. Never a customer's warehouse.
        self.meta = _build(
            settings.meta_dsn, read_only=False, timeout_ms=None, label="meta"
        )
        # File ingestion: CREATE and INSERT inside one organisation's schema.
        self.uploads = _build(
            settings.uploads_dsn, read_only=False, timeout_ms=None, label="uploads",
        )
        # Every read, write and overlay edit on a warehouse.
        self.tenants = TenantEngines(settings, _build)

        # Health probes only: they prove the three runtime roles can log in.
        # Nothing may run a question through them.
        self._probes = {
            "read": _build(settings.ro_dsn, read_only=True, timeout_ms=2000,
                           label="probe-ro"),
            "write": _build(settings.write_dsn, read_only=True, timeout_ms=2000,
                            label="probe-write"),
            "edits": _build(settings.edits_dsn, read_only=True, timeout_ms=2000,
                            label="probe-edits"),
        }

    def dispose(self) -> None:
        self.tenants.dispose()
        for engine in (self.meta, self.uploads, *self._probes.values()):
            engine.dispose()

    def check(self) -> dict[str, str]:
        """Ping each role. Used by /health -- see main.py."""
        out: dict[str, str] = {}
        for name, engine in (
            ("meta", self.meta),
            *self._probes.items(),
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
