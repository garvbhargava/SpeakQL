"""Corrections and the merge queue (Backend Plan §10).

The asymmetry this file implements, stated plainly because it is the whole
design: **an owner's correction writes the real table immediately; a member's
goes to their overlay and waits.**

That is not a permission gradient for its own sake. A member's answers use
their own pending value from the moment they enter it, and nobody else's
answers change at all — which is what protects the numbers other people quote
while still letting the person who spotted the gap fix it where they stand.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, text

from api import schemas
from app.deps import EnginesDep, MailerDep, PrincipalDep, SessionDep
from app.object_access import resolve_merge_request
from app.rbac import Action, Denied, require, resolve_connection
from core import merge as merge_core
from core.edit_validator import (
    EditContext, EditRequest, assert_single_row, compose_update, validate_edit,
)
from db.entities import Connection, EditableTable, MemberEdit, MergeRequest, Person, SchemaColumn
from logs import audit_log, edit_log

log = logging.getLogger("speakql.merges")

router_api = APIRouter(prefix="/api", tags=["corrections"])


@router_api.post("/rows/edit", response_model=schemas.EditResult)
def propose_edit(body: schemas.ProposeEdit, principal: PrincipalDep,
                 session: SessionDep, engines: EnginesDep,
                 mailer: MailerDep) -> schemas.EditResult:
    """One entry point for both paths. Where the write lands is the only
    difference, and the validator runs identically for both."""

    granted = resolve_connection(session, principal, body.connection_id)
    require(Action.CORRECT_VALUE, principal, granted)

    context = _edit_context(session, granted, principal)
    request = EditRequest(
        connection_id=body.connection_id,
        schema_name=body.schema_name,
        table_name=body.table_name,
        pk_column=body.pk_column,
        pk_value=body.pk_value,
        column_name=body.column_name,
        after_value=body.after_value,
        note=body.note,
    )

    verdict = validate_edit(request, context)
    if not verdict.allowed:
        return schemas.EditResult(
            accepted=False, landed="refused", reason=verdict.reason
        )

    before = merge_core.read_current_value(
        engines.read,
        MemberEdit(
            schema_name=request.schema_name, table_name=request.table_name,
            pk_column=request.pk_column, pk_value=request.pk_value,
            column_name=request.column_name,
        ),
    )

    # --- the owner path: straight to the real table ------------------------
    if principal.is_owner:
        statement, params = compose_update(request)
        with engines.write.begin() as conn:
            result = conn.execute(text(statement), params)
            single = assert_single_row(result.rowcount)
            if not single.allowed:
                # rolls back on the way out
                raise HTTPException(status.HTTP_409_CONFLICT, single.reason)

        row = edit_log.write(session, edit_log.Change(
            person_id=principal.person_id,
            connection_id=body.connection_id,
            schema_name=body.schema_name,
            table_name=body.table_name,
            pk_column=body.pk_column,
            pk_value=body.pk_value,
            column_name=body.column_name,
            before_value=before,
            after_value=body.after_value,
            note=body.note,
        ))
        session.flush()
        return schemas.EditResult(
            accepted=True, landed="real_table", edit_log_id=row.id,
            before_value=before, after_value=body.after_value,
        )

    # --- the member path: their overlay, and a merge request ---------------
    edit = MemberEdit(
        person_id=principal.person_id,
        connection_id=body.connection_id,
        schema_name=body.schema_name,
        table_name=body.table_name,
        pk_column=body.pk_column,
        pk_value=body.pk_value,
        column_name=body.column_name,
        before_value=before,
        after_value=body.after_value or "",
        note=body.note,
    )
    session.add(edit)
    session.flush()

    request_row = MergeRequest(member_edit_id=edit.id, state="open")
    session.add(request_row)
    session.flush()

    owner = session.scalar(select(Person).where(
        Person.org_id == principal.org_id, Person.product_role == "owner"
    ))
    if owner is not None:
        mailer.queue_merge_notice(
            owner.id, owner.email,
            f"{body.table_name}.{body.column_name} "
            f"({body.pk_column}={body.pk_value}) -> {body.after_value}",
        )

    return schemas.EditResult(
        accepted=True, landed="overlay", merge_request_id=request_row.id,
        before_value=before, after_value=body.after_value,
    )


@router_api.get("/merges", response_model=list[schemas.MergeOut])
def list_merges(principal: PrincipalDep, session: SessionDep,
                engines: EnginesDep) -> list[schemas.MergeOut]:
    """An owner sees their organisation's queue; a member sees their own.

    Their own edits never appear as merge requests to themselves -- an owner
    has nothing pending, ever, because their writes already landed.
    """
    if principal.is_owner:
        connection_ids = [
            c.id for c in session.scalars(
                select(Connection).where(Connection.org_id == principal.org_id)
            )
        ]
        rows = session.execute(
            select(MergeRequest, MemberEdit, Person)
            .join(MemberEdit, MemberEdit.id == MergeRequest.member_edit_id)
            .join(Person, Person.id == MemberEdit.person_id)
            .where(MemberEdit.connection_id.in_(connection_ids or [-1]))
            .order_by(MergeRequest.created_at.desc())
        ).all()
    else:
        rows = session.execute(
            select(MergeRequest, MemberEdit, Person)
            .join(MemberEdit, MemberEdit.id == MergeRequest.member_edit_id)
            .join(Person, Person.id == MemberEdit.person_id)
            .where(MemberEdit.person_id == principal.person_id)
            .order_by(MergeRequest.created_at.desc())
        ).all()

    out: list[schemas.MergeOut] = []
    for request_row, edit, person in rows:
        current = None
        if request_row.state in ("open", "stale"):
            try:
                current = merge_core.read_current_value(engines.read, edit)
            except Exception:  # noqa: BLE001 - a listing must not fail on one row
                current = None
        out.append(schemas.MergeOut(
            id=request_row.id, state=request_row.state,  # type: ignore[arg-type]
            raised_by=person.email,
            table=f"{edit.schema_name}.{edit.table_name}",
            pk_value=edit.pk_value, column_name=edit.column_name,
            before_value=edit.before_value, proposed_value=edit.after_value,
            current_value=current, note=edit.note,
            created_at=request_row.created_at,
        ))
    return out


@router_api.post("/merges/{merge_id}", response_model=schemas.MergeOut)
def decide(merge_id: int, body: schemas.MergeDecision, principal: PrincipalDep,
           session: SessionDep, engines: EnginesDep,
           mailer: MailerDep) -> schemas.MergeOut:
    """Approve, reject, or re-raise against the current value."""
    require(Action.APPROVE_MERGE, principal)

    request_row = resolve_merge_request(session, principal, merge_id)
    edit = session.get(MemberEdit, request_row.member_edit_id)
    if edit is None:
        raise Denied("no such item, or it is not yours")

    if body.decision == "approve":
        result = merge_core.approve(
            session, engines.write, engines.read, request_row, edit,
            decided_by=principal.person_id, org_id=principal.org_id,
        )
    elif body.decision == "reject":
        result = merge_core.reject(
            session, request_row, decided_by=principal.person_id,
            org_id=principal.org_id, reason=body.reason,
        )
    else:
        fresh = merge_core.reraise_against_current(
            session, engines.read, request_row, edit
        )
        request_row.state = "rejected"
        session.flush()
        return _merge_out(session, fresh, engines)

    member = session.get(Person, edit.person_id)
    if member is not None and body.decision in ("approve", "reject"):
        mailer.send_merge_decision(
            member.email,
            approved=(result.outcome is merge_core.MergeOutcome.MERGED),
            what=f"{edit.table_name}.{edit.column_name}",
            reason=body.reason or (result.message if result.outcome
                                   is merge_core.MergeOutcome.STALE else None),
        )

    return _merge_out(session, request_row, engines, current=result.current_value)


def _merge_out(session, request_row: MergeRequest, engines,
               current: str | None = None) -> schemas.MergeOut:
    edit = session.get(MemberEdit, request_row.member_edit_id)
    person = session.get(Person, edit.person_id) if edit else None
    return schemas.MergeOut(
        id=request_row.id, state=request_row.state,  # type: ignore[arg-type]
        raised_by=person.email if person else "?",
        table=f"{edit.schema_name}.{edit.table_name}" if edit else "?",
        pk_value=edit.pk_value if edit else "",
        column_name=edit.column_name if edit else "",
        before_value=edit.before_value if edit else None,
        proposed_value=edit.after_value if edit else "",
        current_value=current,
        note=edit.note if edit else None,
        created_at=request_row.created_at,
    )


def _edit_context(session, granted, principal) -> EditContext:
    editable = {
        f"{row.schema_name.lower()}.{row.table_name.lower()}": row.pk_column
        for row in session.scalars(
            select(EditableTable).where(
                EditableTable.connection_id == granted.connection.id
            )
        )
    }
    columns: dict[str, dict[str, str]] = {}
    for row in session.scalars(
        select(SchemaColumn).where(SchemaColumn.connection_id == granted.connection.id)
    ):
        key = f"{row.schema_name.lower()}.{row.table_name.lower()}"
        columns.setdefault(key, {})[row.column_name] = row.data_type

    return EditContext(
        editable=editable, columns=columns,
        is_owner=principal.is_owner, db_role=granted.db_role,
    )
