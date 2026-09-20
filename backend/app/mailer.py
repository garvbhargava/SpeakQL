"""The one module that sends email (Backend Plan §10.6).

Single writer, like the three logs, and for the same reason: a second place
that sent mail would eventually send a different-looking code, or send one
twice, or forget the digest.

**In development it writes to the log instead of the network.** The six-digit
code appears in `make logs`, which is how the demo signs in without a mail
server — and is also why `SPEAKQL_ENV` must never say `development` anywhere
real, since that setting prints login codes.

Merge notifications are sent as a **digest**: one email per ten minutes, not
one per row, plus a reminder each morning while anything is still waiting. A
member's correction should not sit for a week because the owner forgot, and a
hundred separate emails is how an owner learns to ignore them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger("speakql.mail")

DIGEST_WINDOW = timedelta(minutes=10)


@dataclass
class Message:
    to: str
    subject: str
    body: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Mailer:
    def __init__(self, *, env: str = "development") -> None:
        self.env = env
        self.outbox: list[Message] = []          # development, and tests
        self._pending_digest: dict[int, list[str]] = {}
        self._last_digest: dict[int, datetime] = {}

    # -- the two things this product sends ---------------------------------

    def send_code(self, email: str, code: str, *, purpose: str) -> None:
        """A sign-in code. Ten minutes, single use."""
        body = (
            f"Your SpeakQL code is {code}\n\n"
            f"It is valid for ten minutes and can be used once.\n"
            f"Purpose: {purpose}\n\n"
            "If you did not ask for this, ignore it — somebody typed your "
            "address by mistake. No account was created or changed."
        )
        self._deliver(Message(email, "Your SpeakQL sign-in code", body))
        if self.env != "production":
            # The demo's front door. Deliberately loud, deliberately guarded.
            log.info("SIGN-IN CODE for %s is %s", email, code)

    def queue_merge_notice(self, owner_id: int, owner_email: str, summary: str) -> None:
        """Hold a merge notice for the digest rather than sending it now."""
        self._pending_digest.setdefault(owner_id, []).append(summary)
        if self._digest_due(owner_id):
            self.flush_digest(owner_id, owner_email)

    def flush_digest(self, owner_id: int, owner_email: str) -> Message | None:
        items = self._pending_digest.pop(owner_id, [])
        if not items:
            return None
        self._last_digest[owner_id] = datetime.now(timezone.utc)

        lines = "\n".join(f"  · {item}" for item in items)
        body = (
            f"{len(items)} correction{'s are' if len(items) != 1 else ' is'} "
            "waiting for you in SpeakQL.\n\n"
            f"{lines}\n\n"
            "Until you decide, the member who raised each one sees their own "
            "value in their own answers. Everybody else sees the real data."
        )
        message = Message(
            owner_email,
            f"{len(items)} correction{'s' if len(items) != 1 else ''} waiting",
            body,
        )
        self._deliver(message)
        return message

    def send_invitation(self, email: str, org_name: str, token: str,
                        *, base_url: str = "http://localhost:5173") -> None:
        body = (
            f"You have been invited to {org_name} on SpeakQL.\n\n"
            f"{base_url}/join?token={token}\n\n"
            "The link works once and expires in a week."
        )
        self._deliver(Message(email, f"You have been invited to {org_name}", body))

    def send_merge_decision(self, email: str, *, approved: bool,
                            what: str, reason: str | None) -> None:
        """Tell the member either way. A rejection with no explanation is how
        people stop reporting problems."""
        verdict = "merged" if approved else "not merged"
        body = f"Your correction to {what} was {verdict}."
        if reason:
            body += f"\n\nReason given: {reason}"
        if not approved:
            body += "\n\nIt has been removed from your copy. The real table never changed."
        self._deliver(Message(email, f"Your correction was {verdict}", body))

    # -- delivery -----------------------------------------------------------

    def _deliver(self, message: Message) -> None:
        if self.env == "production":
            # A real transport goes here. Deliberately not stubbed with a
            # silent no-op: a mailer that pretends to send is worse than one
            # that says it cannot.
            raise NotImplementedError(
                "No mail transport is configured. Set one before running in "
                "production — see app/mailer.py."
            )
        self.outbox.append(message)
        log.info("mail to %s: %s", message.to, message.subject)

    def _digest_due(self, owner_id: int) -> bool:
        last = self._last_digest.get(owner_id)
        return last is None or datetime.now(timezone.utc) - last >= DIGEST_WINDOW
