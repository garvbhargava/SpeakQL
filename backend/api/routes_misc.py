"""Threads, schema and feedback — the smaller surfaces (§12).

Nothing here is complicated. It is collected in one file because splitting
four short routers across four files makes the tree harder to read, not
easier, and §15.1's rules are about where *safety* lives rather than about
file count.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from sqlalchemy import select

from api import schemas
from app.deps import PrincipalDep, SessionDep
from app.object_access import own_threads, resolve_message, resolve_thread
from app.rbac import resolve_connection
from db.entities import Feedback, Message, SchemaColumn

log = logging.getLogger("speakql.misc")

router_api = APIRouter(prefix="/api", tags=["threads and schema"])


# =============================================================== threads ====

@router_api.get("/threads", response_model=list[schemas.ThreadOut])
def list_threads(principal: PrincipalDep,
                 session: SessionDep) -> list[schemas.ThreadOut]:
    """Own threads only. Scoped in the query, never filtered afterwards."""
    return [
        schemas.ThreadOut(id=t.id, title=t.title, created_at=t.created_at)
        for t in own_threads(session, principal)
    ]


@router_api.get("/threads/{thread_id}", response_model=list[schemas.MessageOut])
def thread_messages(thread_id: int, principal: PrincipalDep,
                    session: SessionDep) -> list[schemas.MessageOut]:
    thread = resolve_thread(session, principal, thread_id)
    rows = session.scalars(
        select(Message).where(Message.thread_id == thread.id)
        .order_by(Message.created_at.asc())
    ).all()
    return [
        schemas.MessageOut(id=m.id, question=m.question, created_at=m.created_at)
        for m in rows
    ]


# ================================================================ schema ====

@router_api.get("/schema/{connection_id}", response_model=list[schemas.ColumnOut])
def schema(connection_id: int, principal: PrincipalDep,
           session: SessionDep) -> list[schemas.ColumnOut]:
    """What this person may see of the schema.

    A viewer is shown only the public columns. Not greyed out -- absent, so
    the shape of what they cannot see is not itself visible.
    """
    granted = resolve_connection(session, principal, connection_id)

    query = select(SchemaColumn).where(SchemaColumn.connection_id == connection_id)
    if granted.is_viewer:
        query = query.where(SchemaColumn.is_public.is_(True))

    rows = session.scalars(
        query.order_by(SchemaColumn.table_name, SchemaColumn.column_name)
    ).all()

    return [
        schemas.ColumnOut(
            schema_name=r.schema_name, table_name=r.table_name,
            column_name=r.column_name, data_type=r.data_type,
            description=r.description, is_public=r.is_public,
        )
        for r in rows
    ]


# ============================================================== feedback ====

@router_api.post("/feedback/{message_id}", status_code=status.HTTP_204_NO_CONTENT,
                 response_model=None)
def rate(message_id: int, rating: int, principal: PrincipalDep,
         session: SessionDep) -> None:
    """Thumbs up or down on one answer.

    Negatives are exported for a human review step, never used as a
    retraining trigger. A rating tells you where to look; it does not tell you
    what the right answer was, and a model trained on "somebody was annoyed"
    learns the wrong lesson.
    """
    message = resolve_message(session, principal, message_id)
    value = 1 if rating > 0 else -1

    existing = session.scalar(select(Feedback).where(
        Feedback.message_id == message.id,
        Feedback.person_id == principal.person_id,
    ))
    if existing is not None:
        existing.rating = value
    else:
        session.add(Feedback(message_id=message.id,
                             person_id=principal.person_id, rating=value))
    session.flush()
    return None
