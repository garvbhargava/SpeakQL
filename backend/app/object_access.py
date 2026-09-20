"""Question 4: is every id in this request theirs to name? (§6.1)

New in Revision 5, and the check most projects forget.

Grants answer *may this person use this database*. They do not answer *is this
particular message, thread or merge request theirs*. Without this module,
somebody who legitimately holds a grant can paste another person's
`message_id` into an export URL and read an answer that was never theirs --
inside the same organisation, so no tenant check catches it.

Two rules, and the second is the one that matters:

    1. Every identifier is resolved to its owning person and organisation
       before it is used.
    2. A guessed identifier is **indistinguishable** from one that does not
       exist. Same status, same message, same timing characteristics.

If a missing row returned 404 and a forbidden one returned 403, the pair of
responses is an oracle: iterate the integers and you have mapped the system.
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.rbac import Denied, Principal
from db.entities import (
    Connection, Feedback, MemberEdit, MergeRequest, Message, Thread,
    UploadedDataset,
)

T = TypeVar("T")

# One sentence for every failure on this path. Never say which of the two it
# was -- that is the whole point.
_SAME_ANSWER = "no such item, or it is not yours"


def _deny() -> Denied:
    return Denied(_SAME_ANSWER)


def resolve_thread(session: Session, principal: Principal, thread_id: int) -> Thread:
    thread = session.get(Thread, thread_id)
    if thread is None or thread.person_id != principal.person_id:
        raise _deny()
    return thread


def resolve_message(session: Session, principal: Principal, message_id: int) -> Message:
    """A message belongs to the person who asked it. An owner can see their
    organisation's aggregate figures, but not somebody else's conversation --
    those are different things and §19 keeps them apart."""
    message = session.get(Message, message_id)
    if message is None or message.person_id != principal.person_id:
        raise _deny()
    return message


def resolve_merge_request(
    session: Session, principal: Principal, merge_id: int
) -> MergeRequest:
    """Visible to the member who raised it and to the owner who must decide.

    The owner check goes through the organisation rather than through the
    merge request, so an owner cannot reach another company's queue by id.
    """
    merge = session.get(MergeRequest, merge_id)
    if merge is None:
        raise _deny()

    edit = session.get(MemberEdit, merge.member_edit_id)
    if edit is None:
        raise _deny()

    if edit.person_id == principal.person_id:
        return merge

    if principal.is_owner:
        connection = session.get(Connection, edit.connection_id)
        if connection is not None and connection.org_id == principal.org_id:
            return merge

    raise _deny()


def resolve_member_edit(
    session: Session, principal: Principal, edit_id: int
) -> MemberEdit:
    edit = session.get(MemberEdit, edit_id)
    if edit is None:
        raise _deny()
    if edit.person_id == principal.person_id:
        return edit
    if principal.is_owner:
        connection = session.get(Connection, edit.connection_id)
        if connection is not None and connection.org_id == principal.org_id:
            return edit
    raise _deny()


def resolve_dataset(
    session: Session, principal: Principal, dataset_id: int
) -> UploadedDataset:
    dataset = session.get(UploadedDataset, dataset_id)
    if dataset is None:
        raise _deny()
    connection = session.get(Connection, dataset.connection_id)
    if connection is None or connection.org_id != principal.org_id:
        raise _deny()
    return dataset


def resolve_feedback(session: Session, principal: Principal, feedback_id: int) -> Feedback:
    row = session.get(Feedback, feedback_id)
    if row is None or row.person_id != principal.person_id:
        raise _deny()
    return row


def own_threads(session: Session, principal: Principal) -> list[Thread]:
    """Listing is scoped, not filtered after the fact. The query never selects
    a row the caller may not see, so there is no chance of one escaping
    through a serialiser."""
    return list(
        session.scalars(
            select(Thread)
            .where(Thread.person_id == principal.person_id)
            .order_by(Thread.created_at.desc())
        )
    )
