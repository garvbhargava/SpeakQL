"""The audit log — administrative actions and refused attempts (§20, §6.1).

**Exactly one writer, and it is this module.**

Two kinds of row live here:

    what somebody *did*       invited, approved, granted, revoked, connected
    what somebody *tried*     a cross-organisation reach, a guessed identifier

The second kind is the one worth having. A single 403 is a typo. A run of them
against sequential identifiers is somebody mapping the system, and that
signature only exists if the refusals were recorded.

Never record the thing that was refused *in full* — a rejected query can
contain another tenant's table names, and copying them into a log shared with
an owner would leak exactly what the refusal prevented.
"""

from __future__ import annotations

import logging
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.entities import AuditLog

log = logging.getLogger("speakql.audit")


class Action(str, Enum):
    # things people do
    ORG_CREATED = "org.created"
    PERSON_INVITED = "person.invited"
    PERSON_APPROVED = "person.approved"
    PERSON_SUSPENDED = "person.suspended"
    GRANT_ADDED = "grant.added"
    GRANT_REVOKED = "grant.revoked"
    CONNECTION_ADDED = "connection.added"
    CONNECTION_REFUSED = "connection.refused"
    DATASET_UPLOADED = "dataset.uploaded"
    MERGE_APPROVED = "merge.approved"
    MERGE_REJECTED = "merge.rejected"
    MERGE_STALE = "merge.stale"
    # things people tried
    CROSS_ORG_ATTEMPT = "security.cross_org_attempt"
    OBJECT_ACCESS_DENIED = "security.object_denied"
    RATE_LIMITED = "security.rate_limited"
    OTP_LOCKOUT = "security.otp_lockout"


def write(session: Session, *, action: Action, person_id: int | None = None,
          org_id: int | None = None, target: str | None = None,
          detail: dict | None = None) -> AuditLog:
    row = AuditLog(
        person_id=person_id,
        org_id=org_id,
        action=action.value,
        target=target,
        detail=_redact(detail or {}),
    )
    session.add(row)
    session.flush()
    return row


def record_refusal(session: Session, *, action: Action, person_id: int,
                   org_id: int, identifier: str, note: str = "") -> AuditLog:
    """A refused attempt, with the identifier that was reached for.

    The identifier is recorded because that is the signal — a run of them in
    sequence is the thing worth alerting on. The *content* behind it is not.
    """
    return write(
        session, action=action, person_id=person_id, org_id=org_id,
        target=identifier[:200], detail={"note": note} if note else {},
    )


_SENSITIVE = ("password", "secret", "token", "credential", "dsn", "key", "sql")


def _redact(detail: dict) -> dict:
    """Never let a secret or a rejected statement reach this table."""
    clean: dict = {}
    for key, value in detail.items():
        if any(word in key.lower() for word in _SENSITIVE):
            clean[key] = "[redacted]"
        elif isinstance(value, str) and len(value) > 500:
            clean[key] = value[:500] + "…"
        else:
            clean[key] = value
    return clean


def recent_refusals(session: Session, org_id: int, *, limit: int = 50) -> list[AuditLog]:
    return list(session.scalars(
        select(AuditLog)
        .where(
            AuditLog.org_id == org_id,
            AuditLog.action.like("security.%"),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ))


def refusal_burst(session: Session, person_id: int, *, within_minutes: int = 10,
                  threshold: int = 5) -> bool:
    """Is this person producing a run of refusals?

    One 403 is a typo. Five in ten minutes is the signature worth alerting on.
    """
    cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, within_minutes)
    count = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.person_id == person_id,
            AuditLog.action.like("security.%"),
            AuditLog.created_at >= cutoff,
        )
    )
    return bool(count and count >= threshold)
