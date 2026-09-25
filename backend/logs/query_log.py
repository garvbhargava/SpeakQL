"""The query log — one row per request, on every path (Backend Plan §20).

**Exactly one writer, and it is this module.** Anything else appending rows
would make the accuracy figures unreliable in a way nobody would notice until
they were quoted in a report.

The rule that makes the measurement honest: a row is written for **every**
outcome — answered, clarified, blocked, refused, failed and rate-limited
alike. A log that only records successes produces an accuracy number that
means nothing, and it is the easiest thing in the world to do by accident by
putting the write on the happy path.

Confidence is recorded even now, when there is one generator and nothing to
escalate to, because the week-9 threshold sweep runs over this column.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from time import monotonic

from sqlalchemy.orm import Session

from db.entities import QueryLog

log = logging.getLogger("speakql.query_log")


class Outcome(str, Enum):
    ANSWERED = "answered"
    CLARIFIED = "clarified"
    BLOCKED = "blocked"        # a rule refused it
    REFUSED = "refused"        # the data cannot answer it
    FAILED = "failed"          # timeout, offline, server error
    RATE_LIMITED = "rate_limited"


@dataclass
class Entry:
    person_id: int
    org_id: int
    question: str
    outcome: Outcome
    connection_id: int | None = None
    route: str | None = None
    # Which model wrote the statement. With two generators, "confidence 0.61"
    # means different things depending on which one produced it, and the
    # week-9 threshold sweep has to be able to tell them apart.
    generator: str | None = None
    generated_sql: str | None = None
    confidence: float | None = None
    latency_ms: int | None = None
    row_count: int | None = None
    refused_by: str | None = None


def write(session: Session, entry: Entry) -> QueryLog:
    row = QueryLog(
        person_id=entry.person_id,
        org_id=entry.org_id,
        connection_id=entry.connection_id,
        question=entry.question[:4000],
        outcome=entry.outcome.value,
        route=entry.route,
        generator=entry.generator,
        generated_sql=entry.generated_sql,
        confidence=entry.confidence,
        latency_ms=entry.latency_ms,
        row_count=entry.row_count,
        refused_by=entry.refused_by,
    )
    session.add(row)
    session.flush()
    return row


@contextmanager
def record(session: Session, *, person_id: int, org_id: int, question: str,
           connection_id: int | None = None):
    """Guarantee a row, whatever happens inside.

        with query_log.record(...) as entry:
            ...
            entry.outcome = Outcome.ANSWERED

    An exception inside the block still writes a row, marked FAILED. That is
    the whole point: the failure path is exactly where a hand-written log call
    gets forgotten, and it is the path whose count matters most.
    """
    started = monotonic()
    entry = Entry(
        person_id=person_id, org_id=org_id, question=question,
        outcome=Outcome.FAILED, connection_id=connection_id,
    )
    try:
        yield entry
    except Exception:
        entry.outcome = Outcome.FAILED
        raise
    finally:
        if entry.latency_ms is None:
            entry.latency_ms = int((monotonic() - started) * 1000)
        try:
            write(session, entry)
        except Exception:  # noqa: BLE001
            # Never let logging break a request that otherwise succeeded, but
            # do make the gap loud -- a silently missing row corrupts the
            # measurement quietly.
            log.exception("failed to write query_log row for person %s", person_id)
