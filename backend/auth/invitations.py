"""Invitations: issued by an owner, redeemed once (Backend Plan §7.5).

Three properties, and each one closes a specific hole:

    single-use   a token that could be redeemed twice makes two accounts
    expiring     one that never expires is a credential sitting in an inbox
    fixing       the token fixes the organisation and the role, so a redeemer
                 cannot choose to be an owner, and no domain lookup happens --
                 which is what lets a contractor on Gmail join a company
                 without weakening the free-mail rule in domain_resolver.py

Redemption is a second factor, not a replacement for the first. The token
proves somebody holds the link; the six-digit code sent to the invited
address proves they hold the inbox. A link that leaks -- forwarded, pasted
into a ticket, left in a proxy log -- is not enough on its own.

Only the SHA-256 of a token is stored. A database read yields no usable link.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.entities import Invitation, Person

# One sentence for every failure, as §7.5 requires: a revoked, expired, used
# or misaddressed token is indistinguishable from one that never existed.
REFUSAL = "this invitation is not valid. Ask the person who invited you for a new one."


class InvitationError(ValueError):
    def __init__(self) -> None:
        super().__init__(REFUSAL)


def new_token() -> tuple[str, str]:
    """(the raw token to mail, the hash to store). 256 bits from `secrets`."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _utc(moment: dt.datetime) -> dt.datetime:
    # Postgres returns aware datetimes; SQLite (the test database) naive ones.
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def live_invitation(session: Session, raw_token: str, email: str) -> Invitation:
    """The invitation this token names, if it may still be redeemed by `email`.

    Checked at /start, before a code is sent. Nothing is consumed here --
    consuming happens in `redeem`, after the code proves the inbox.
    """
    invitation = session.scalar(
        select(Invitation).where(Invitation.token_hash == hash_token(raw_token))
    )
    now = dt.datetime.now(dt.timezone.utc)
    if (
        invitation is None
        or invitation.redeemed_at is not None
        or _utc(invitation.expires_at) <= now
        # Bound to the address it was sent to. Without this, anyone holding a
        # leaked link could redeem it into their own inbox.
        or invitation.email != email
    ):
        raise InvitationError()
    return invitation


def redeem(session: Session, invitation_id: int, email: str) -> Person:
    """Consume the invitation and activate the account, in one transaction.

    The claim is a conditional UPDATE, not a read followed by a write. Two
    requests racing on one token both pass a read; only one of them can turn
    `redeemed_at` from NULL to a timestamp, and the other sees rowcount 0.
    """
    now = dt.datetime.now(dt.timezone.utc)
    claimed = session.execute(
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.email == email,
            Invitation.redeemed_at.is_(None),
            Invitation.expires_at > now,
        )
        .values(redeemed_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        raise InvitationError()

    invitation = session.get(Invitation, invitation_id, populate_existing=True)
    person = session.scalar(
        select(Person).where(Person.email == email,
                             Person.org_id == invitation.org_id)
    )
    if person is None or person.state == "suspended":
        # Removed or suspended by the owner after the invitation was sent.
        # The invitation dies with the decision; it does not resurrect them.
        raise InvitationError()

    # The invitation IS the owner's approval, so a redeemed invitation is an
    # active account. Asking the owner to approve somebody they invited is a
    # second click that protects nothing.
    person.state = "active"
    session.flush()
    return person
