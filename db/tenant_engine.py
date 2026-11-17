"""The warehouse a question is about, and nothing else (Backend Plan §5, §15).

This module exists because of a bug, and the bug is worth knowing by heart.

The first version of the backend opened ONE read engine, bound to one DSN,
and ran every question on it. Authorisation was correct -- the connection was
resolved against the caller's organisation, the validator checked the tables
against that connection's registry -- and then the approved query ran on
whichever warehouse the DSN happened to name. A second company's question
would have read the first company's data. The write engine had the same flaw,
so an owner's correction could have landed in another tenant's table.

The fix is structural rather than a check added somewhere: **every warehouse
engine is built from the connection row**, which only exists in the caller's
hands after `rbac.resolve_connection` has proved it belongs to them. There is
no warehouse engine that is not derived from a connection, so there is no
engine that could point anywhere else.

And the executor confirms it anyway: before a query runs it asks the server
`SELECT current_database()` and refuses if the answer is not the database the
question was about. One check is one point of failure.

Three kinds of connection:

    internal   a warehouse on this server -- the seeded demo warehouses
    uploaded   the organisation's own schema inside speakql_uploads
    external   an owner-registered database, reached with its own credentials,
               which are read-only by construction (registration refuses an
               account that can write)
"""

from __future__ import annotations

import json
import logging
import threading

from sqlalchemy.engine import Engine, make_url

from app.config import Settings
from db.crypto import CredentialError, unseal
from db.host_guard import HostRefused, external_url, pin

log = logging.getLogger("speakql.tenant")


class WriteNotSupported(RuntimeError):
    """This connection cannot be written to, by design."""


class ConnectionUnavailable(RuntimeError):
    """An external connection that cannot be reached safely right now: its
    stored credentials no longer decrypt, or its host no longer passes the
    address check. A failure the owner can fix, not a crash."""


class TenantEngines:
    """Per-connection engines, built on first use and cached.

    The cache key is the connection id, and the URL is derived from the
    connection row every time a new engine is made -- never from anything the
    request supplied.
    """

    def __init__(self, settings: Settings, build) -> None:
        self._settings = settings
        self._build = build  # db.engines._build, passed in to avoid a cycle
        self._read: dict[int, Engine] = {}
        self._write: dict[int, Engine] = {}
        self._edits: dict[int, Engine] = {}
        self._lock = threading.Lock()

        # Credentials for the three runtime roles on THIS server. Only the
        # database name is swapped per connection; the role never is.
        self._ro = make_url(settings.ro_dsn)
        self._write_base = make_url(settings.write_dsn)
        self._edits_base = make_url(settings.edits_dsn)
        self._uploads_db = make_url(settings.uploads_dsn).database

    # -- the three ways a warehouse is reached -------------------------------

    def read(self, connection) -> Engine:
        """Read-only, statement timeout, and the right database. Every read."""
        return self._cached(self._read, connection, "ro", read_only=True,
                            url_for=self._read_url)

    def write(self, connection) -> Engine:
        """The edit path only. Internal warehouses only.

        An external connection was registered with an account that cannot
        write -- registration refuses one that can -- so a correction there is
        impossible by design rather than by omission.
        """
        if connection.kind != "internal":
            raise WriteNotSupported(
                "corrections are available on this server's warehouses only. "
                "This database was registered read-only, and SpeakQL will not "
                "hold credentials that can write to it."
            )
        return self._cached(
            self._write, connection, "write", read_only=False,
            url_for=lambda c: self._write_base.set(database=c.database_name),
        )

    def edits(self, connection) -> Engine:
        """Member overlays live beside the warehouse they overlay."""
        if connection.kind != "internal":
            raise WriteNotSupported(
                "pending corrections are available on this server's "
                "warehouses only"
            )
        return self._cached(
            self._edits, connection, "edits", read_only=False,
            url_for=lambda c: self._edits_base.set(database=c.database_name),
        )

    def expected_database(self, connection) -> str:
        """What `current_database()` must return for this connection."""
        if connection.kind == "uploaded":
            return self._uploads_db
        return connection.database_name

    # -- internals -------------------------------------------------------------

    def _read_url(self, connection):
        if connection.kind == "internal":
            return self._ro.set(database=connection.database_name)
        if connection.kind == "uploaded":
            return self._ro.set(database=self._uploads_db)
        if connection.kind == "external":
            return self._external_url(connection)
        raise ValueError(f"unknown connection kind: {connection.kind!r}")

    def _external_url(self, connection):
        """Decrypted here and nowhere else, at the moment it is needed.

        The URL is rebuilt from the row's columns plus the sealed username and
        password -- never parsed out of a stored string -- and the host is
        judged again and pinned, because the address check at registration
        says nothing about what the name resolves to today.
        """
        try:
            credentials = json.loads(
                unseal(self._settings.secret_key, connection.secret_cipher)
            )
            return pin(external_url(
                username=credentials["username"],
                password=credentials["password"],
                host=connection.host or "",
                port=connection.port or 5432,
                database=connection.database_name,
            ))
        except HostRefused as exc:
            log.warning("connection %s refused at connect time: %s",
                        connection.id, exc)
            raise ConnectionUnavailable(
                f"this database's host no longer passes the address check "
                f"({exc}). Ask the owner to register it again."
            ) from exc
        except (CredentialError, KeyError, ValueError) as exc:
            raise ConnectionUnavailable(
                "this database's stored credentials cannot be read. Ask the "
                "owner to register it again."
            ) from exc

    def _cached(self, cache: dict, connection, label: str, *, read_only: bool,
                url_for) -> Engine:
        with self._lock:
            engine = cache.get(connection.id)
            if engine is None:
                # The URL object itself, not a rendered string: rendering and
                # re-parsing is where an escaped character can turn back into
                # syntax.
                engine = self._build(
                    url_for(connection),
                    read_only=read_only,
                    timeout_ms=self._settings.statement_timeout_ms,
                    label=f"{label}:c{connection.id}",
                )
                cache[connection.id] = engine
            return engine

    def forget(self, connection_id: int) -> None:
        """Drop cached engines for a connection that was removed or
        re-registered, so the next request builds from the new row."""
        with self._lock:
            for cache in (self._read, self._write, self._edits):
                engine = cache.pop(connection_id, None)
                if engine is not None:
                    engine.dispose()

    def dispose(self) -> None:
        with self._lock:
            for cache in (self._read, self._write, self._edits):
                for engine in cache.values():
                    engine.dispose()
                cache.clear()
