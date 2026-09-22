"""Corrections and the merge queue (Backend Plan §10).

The asymmetry this file implements, stated plainly because it is the whole
design: **an owner's correction writes the real table immediately; a member's
goes to their overlay and waits.**

That is not a permission gradient for its own sake. A member's answers use
their own pending value from the moment they enter it, and nobody else's
answers change at all — which is what protects the numbers other people quote
while still letting the person who spotted the gap fix it where they stand.

Every warehouse engine here comes from `engines.tenants`, built from the
connection the correction is about. An owner's write therefore lands in their
own warehouse and cannot land in anybody else's — which the first version of
this file, holding one write engine bound to one DSN, could not promise.
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
from db.edits_engine import sync_rows
from db.entities import (
    Connection, EditableTable, MemberEdit, MergeRequest, Person, SchemaColumn,
)
from db.tenant_engine import WriteNotSupported
from logs import edit_log

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
    connection = granted.connection

    # A registered external database is read-only by construction; refuse
    # before validating anything, with the reason stated.
    try:
        write_engine = engines.tenants.write(connection)
        edits_engine = engines.tenants.edits(connection)
    except WriteNotSupported as exc:
        return schemas.EditResult(accepted=False, landed="refused", reason=str(exc))

    context = _edit_context(session, granted, principal)
    request = EditRequest(
        connection_id=connection.id,
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

    read_engine = engines.tenants.read(connection)
    before = merge_core.read_current_value(
        read_engine,
        MemberEdit(
            schema_name=request.schema_name, table_name=request.table_name,
            pk_column=request.pk_column, pk_value=request.pk_value,
            column_name=request.column_name,
        ),
    )

    # --- the owner path: straight to the real table ------------------------
    if principal.is_owner:
        statement, params = compose_update(request)
        with write_engine.begin() as conn:
            result = conn.execute(text(statement), params)
            single = assert_single_row(result.rowcount)
            if not single.allowed:
                # rolls back on the way out
                raise HTTPException(status.HTTP_409_CONFLICT, single.reason)

        row = edit_log.write(session, edit_log.Change(
            person_id=principal.person_id,
            connection_id=connection.id,
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
    # A newer proposal for the same cell replaces the older one rather than
    # stacking behind it: the member changed their mind, and the owner should
    # decide on what they now propose.
    _supersede_open_edit(session, principal.person_id, request)

    edit = MemberEdit(
        person_id=principal.person_id,
        connection_id=connection.id,
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

    # Their own answers use it from this moment on.
    _sync_overlay(session, edits_engine, principal.person_id, connection.id,
                  body.table_name)

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

    An owner has nothing pending, ever, because their writes already landed.
    """
    query = (
        select(MergeRequest, MemberEdit, Person)
        .join(MemberEdit, MemberEdit.id == MergeRequest.member_edit_id)
        .join(Person, Person.id == MemberEdit.person_id)
        .order_by(MergeRequest.created_at.desc())
    )
    if principal.is_owner:
        connection_ids = [
            c.id for c in session.scalars(
                select(Connection).where(Connection.org_id == principal.org_id)
            )
        ]
        query = query.where(MemberEdit.connection_id.in_(connection_ids or [-1]))
    else:
        query = query.where(MemberEdit.person_id == principal.person_id)

    out: list[schemas.MergeOut] = []
    for request_row, edit, person in session.execute(query).all():
        current = None
        if request_row.state in ("open", "stale"):
            connection = session.get(Connection, edit.connection_id)
            try:
                current = merge_core.read_current_value(
                    engines.tenants.read(connection), edit
                )
            except Exception:  # noqa: BLE001 - a listing must not fail on one row
                current = None
        out.append(_merge_out(request_row, edit, person, current))
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

    connection = session.get(Connection, edit.connection_id)
    if connection is None or connection.org_id != principal.org_id:
        raise Denied("no such item, or it is not yours")

    try:
        write_engine = engines.tenants.write(connection)
        edits_engine = engines.tenants.edits(connection)
    except WriteNotSupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    read_engine = engines.tenants.read(connection)

    if body.decision == "approve":
        result = merge_core.approve(
            session, write_engine, read_engine, request_row, edit,
            decided_by=principal.person_id, org_id=principal.org_id,
        )
    elif body.decision == "reject":
        result = merge_core.reject(
            session, request_row, decided_by=principal.person_id,
            org_id=principal.org_id, reason=body.reason,
        )
    else:
        fresh = merge_core.reraise_against_current(
            session, read_engine, request_row, edit
        )
        request_row.state = "rejected"
        session.flush()
        _sync_overlay(session, edits_engine, edit.person_id, connection.id,
                      edit.table_name)
        fresh_edit = session.get(MemberEdit, fresh.member_edit_id)
        return _merge_out(fresh, fresh_edit, session.get(Person, edit.person_id))

    # Whatever the decision, the request is no longer open, so it leaves the
    # member's overlay: merged values now come from the real table, rejected
    # and stale ones are gone from their answers.
    _sync_overlay(session, edits_engine, edit.person_id, connection.id,
                  edit.table_name)

    member = session.get(Person, edit.person_id)
    if member is not None and body.decision in ("approve", "reject"):
        mailer.send_merge_decision(
            member.email,
            approved=(result.outcome is merge_core.MergeOutcome.MERGED),
            what=f"{edit.table_name}.{edit.column_name}",
            reason=body.reason or (result.message if result.outcome
                                   is merge_core.MergeOutcome.STALE else None),
        )

    return _merge_out(request_row, edit, member, result.current_value)


# ------------------------------------------------------------- internals ----

def _sync_overlay(session, edits_engine, person_id: int, connection_id: int,
                  table_name: str) -> None:
    """Make the member's overlay hold exactly their open requests.

    Rebuilt from the merge queue, the source of truth, rather than patched --
    so the overlay can never disagree with what the queue says is pending.
    """
    open_cells = session.execute(
        select(MemberEdit.pk_value, MemberEdit.column_name, MemberEdit.after_value)
        .join(MergeRequest, MergeRequest.member_edit_id == MemberEdit.id)
        .where(
            MemberEdit.person_id == person_id,
            MemberEdit.connection_id == connection_id,
            MemberEdit.table_name == table_name,
            MergeRequest.state == "open",
        )
    ).all()
    sync_rows(edits_engine, person_id, table_name,
              [(pk, col, val) for pk, col, val in open_cells])


def _supersede_open_edit(session, person_id: int, request: EditRequest) -> None:
    previous = session.execute(
        select(MergeRequest)
        .join(MemberEdit, MemberEdit.id == MergeRequest.member_edit_id)
        .where(
            MemberEdit.person_id == person_id,
            MemberEdit.connection_id == request.connection_id,
            MemberEdit.table_name == request.table_name,
            MemberEdit.pk_value == str(request.pk_value),
            MemberEdit.column_name == request.column_name,
            MergeRequest.state == "open",
        )
    ).scalars().all()
    for row in previous:
        row.state = "rejected"
    if previous:
        session.flush()


def _merge_out(request_row: MergeRequest, edit: MemberEdit | None,
               person: Person | None, current: str | None = None) -> schemas.MergeOut:
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
