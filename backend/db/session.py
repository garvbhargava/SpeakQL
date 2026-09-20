"""Sessions against speakql_meta (Backend Plan §5).

The application's own data. Customer warehouses are never reached through
this — they go through `db/engines.py`, under a role that can only read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class SessionFactory:
    def __init__(self, engine: Engine) -> None:
        self._maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def __call__(self) -> Session:
        return self._maker()

    @contextmanager
    def begin(self) -> Iterator[Session]:
        """A unit of work that commits, or rolls back entirely.

        Used by the write path, where a change and its edit_log row must
        either both land or neither does.
        """
        session = self._maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
