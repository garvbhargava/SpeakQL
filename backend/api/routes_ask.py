"""POST /api/ask — the twelve steps, end to end (Backend Plan §14).

This is the route the whole product exists for, and it is deliberately thin:
every decision it makes is made by a module that can be tested without it.

    1  deps + rbac            who, and may they
    2  question_handler       layer 1, before anything is generated
    3  context_resolver       make a follow-up self-contained
    4  schema_retriever       which tables
    5  question_handler       ambiguous? ask back, once
    6  sql_generator          candidate SQL
    7  validator              layer 2 -- the AST decides
    8  router                 keep it, or escalate
    9  executor               run it, read-only, under a timeout
    10 visualiser             a chart from the shape
    11 explainer              the sentence, with columns from the registry
    12 query_log              one row, on every path

Five outcomes leave this route, and each is a different shape so the interface
can render them differently: answer, clarify, blocked, refusal, failure.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from api import schemas
from app.deps import (
    EnginesDep, LLMDep, LimiterDep, PrincipalDep, SessionDep, SettingsDep,
)
from app.ratelimit import Limited
from app.rbac import Action, may_see_sql, permitted_tables, require, resolve_connection
from core import executor, question_handler, visualiser
from core.context_resolver import resolve as resolve_context
from core.explainer import explain
from core.llm_client import LLMClient
from core.router import Route, route
from core.schema_retriever import (
    Column, LexicalRetriever, complete_join_paths, to_schema_text,
)
from core.sql_generator import GemmaGenerator
from core.validator import Permitted
from core.executor import OverlaySpec
from db.edits_engine import overlay_table_for
from db.tenant_engine import ConnectionUnavailable
from db.entities import (
    EditableTable, MemberEdit, MergeRequest, Message, SchemaColumn, Thread,
)
from logs import audit_log, query_log
from logs.query_log import Outcome

log = logging.getLogger("speakql.ask")

router_api = APIRouter(prefix="/api", tags=["ask"])

# Step names, so the interface and the log agree on what happened.
STEPS = [
    "Checking what you can see", "Opening the answer", "Reading your last question",
    "Finding the right tables", "Checking it is not ambiguous", "Writing the query",
    "Checking it is safe to run", "Choosing a model", "Running it",
    "Drawing the chart", "Explaining it", "Recording it",
]


@router_api.post("/ask", response_model=None)
def ask(
    body: schemas.Ask,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    engines: EnginesDep,
    limiter: LimiterDep,
    llm: LLMDep,
) -> Any:
    """One question in, one of five outcomes out."""

    # -- rate limit ---------------------------------------------------------
    # Checked before any model runs, and it still writes a query_log row:
    # being rate-limited is an outcome, not an absence of one.
    decision = limiter.check(principal.person_id, Limited.ASK)
    if not decision.allowed:
        query_log.write(session, query_log.Entry(
            person_id=principal.person_id, org_id=principal.org_id,
            question=body.question, outcome=Outcome.RATE_LIMITED,
            connection_id=body.connection_id,
        ))
        audit_log.write(session, action=audit_log.Action.RATE_LIMITED,
                        person_id=principal.person_id, org_id=principal.org_id)
        return schemas.FailureOut(
            reason=decision.message, retryable=True,
            retry_after_seconds=decision.retry_after_seconds,
        )

    # -- 1. who, and may they -----------------------------------------------
    granted = resolve_connection(session, principal, body.connection_id)
    require(Action.READ_ANY_TABLE if granted.is_analyst else Action.READ_PUBLIC_TABLE,
            principal, granted)

    with query_log.record(
        session, person_id=principal.person_id, org_id=principal.org_id,
        question=body.question, connection_id=granted.connection.id,
    ) as entry:

        # -- 2. layer 1, before generation ----------------------------------
        screening = question_handler.screen(body.question)
        if not screening.allowed:
            entry.outcome = Outcome.BLOCKED
            entry.refused_by = f"layer 1: {screening.intent.value}"
            message = _record_message(session, principal, body)
            audit_log.write(
                session, action=audit_log.Action.OBJECT_ACCESS_DENIED,
                person_id=principal.person_id, org_id=principal.org_id,
                target=screening.intent.value,
            )
            return schemas.BlockedOut(
                message_id=message.id,
                refused_by="Layer 1 — intent gatekeeper",
                reason=screening.reason,
                layer=1,
                statement=None,   # nothing was generated; there is nothing to show
            )

        # -- 3. make a follow-up self-contained -----------------------------
        history = _recent_questions(session, principal, body.thread_id)
        resolved = resolve_context(body.question, history)
        question = resolved.question

        # -- 4. which tables ------------------------------------------------
        columns = _registry_columns(session, granted.connection.id)
        if not columns:
            entry.outcome = Outcome.REFUSED
            message = _record_message(session, principal, body)
            return schemas.RefusalOut(
                message_id=message.id,
                reason="this database has not been introspected yet",
                what_would_help="run schema introspection on it first",
            )

        retrieved = LexicalRetriever().search(question, columns)
        # The tables a join has to pass through are added here rather than
        # left to the generator to invent -- see complete_join_paths.
        retrieved = complete_join_paths(retrieved, columns)
        schema_text = to_schema_text(retrieved, public_only=granted.is_viewer)
        if not schema_text:
            entry.outcome = Outcome.REFUSED
            message = _record_message(session, principal, body)
            return schemas.RefusalOut(
                message_id=message.id,
                reason="nothing in this database looks related to that question",
                what_would_help=(
                    "a table carrying the thing you asked about, or a column "
                    "description that names it"
                ),
            )

        # -- 5. ambiguous? ask back, once -----------------------------------
        if body.clarification_choice is None:
            ambiguity = question_handler.check_ambiguity(question)
            if ambiguity.ambiguous:
                entry.outcome = Outcome.CLARIFIED
                message = _record_message(session, principal, body)
                return schemas.ClarifyOut(
                    message_id=message.id, term=ambiguity.term,
                    note=ambiguity.note, options=ambiguity.options,
                )
        else:
            # One round only. The chosen reading is folded in and it commits.
            question = f"{question} (interpreting as: {body.clarification_choice})"

        # -- 6/7/8. generate, validate, route -------------------------------
        tables, public_columns, public_only = permitted_tables(session, granted)
        permitted = Permitted(tables, public_columns, public_only)

        generator = GemmaGenerator(llm) if isinstance(llm, LLMClient) else None
        if generator is None:
            entry.outcome = Outcome.FAILED
            return schemas.FailureOut(
                reason=(
                    "The generator is not reachable. Start it with "
                    "`make up-local && make pull-model`."
                ),
                retryable=True,
            )

        routing = route(
            question, schema_text, permitted,
            primary=generator, fallback=None,
            threshold=settings.confidence_threshold,
        )

        entry.confidence = routing.confidence
        entry.route = routing.route.value
        entry.generated_sql = routing.sql or None

        message = _record_message(session, principal, body)

        if routing.route is Route.CANNOT_ANSWER:
            entry.outcome = Outcome.REFUSED
            return schemas.RefusalOut(
                message_id=message.id,
                reason=(
                    "The data in this database cannot answer that. This is not "
                    "an error -- the question is fine, the data simply does not "
                    "carry it."
                ),
                what_would_help="a table holding the thing you asked about",
            )

        if routing.route is Route.REFUSED:
            entry.outcome = Outcome.BLOCKED
            entry.refused_by = f"layer 2: {routing.verdict.refusal.value if routing.verdict and routing.verdict.refusal else 'validator'}"
            return schemas.BlockedOut(
                message_id=message.id,
                refused_by="Layer 2 — AST validator",
                reason=routing.note or "the generated statement was refused",
                layer=2,
                # Shown struck through: generation happened, execution did not.
                statement=routing.sql or None,
            )

        # -- 9. run it ------------------------------------------------------
        # On THIS connection's warehouse, reached through an engine built from
        # the connection row -- never a shared one. The executor then asks the
        # server which database it is on and refuses a mismatch.
        executor.assert_same_organisation(principal.org_id, granted.connection.org_id)
        final_sql = routing.sql

        overlays = _member_overlays(session, principal, granted.connection)

        try:
            result = executor.execute(
                engines.tenants.read(granted.connection), final_sql, permitted,
                max_rows=settings.max_rows,
                expected_database=engines.tenants.expected_database(granted.connection),
                overlays=overlays,
            )
        except executor.StatementTimeout as exc:
            entry.outcome = Outcome.FAILED
            entry.refused_by = "timeout"
            return schemas.FailureOut(reason=str(exc), retryable=True)
        except executor.ExecutionError as exc:
            entry.outcome = Outcome.FAILED
            return schemas.FailureOut(reason=str(exc), retryable=True)
        except ConnectionUnavailable as exc:
            # An external host that stopped passing the address check, or
            # credentials that no longer decrypt. Retrying will not help.
            entry.outcome = Outcome.FAILED
            entry.refused_by = "connection"
            return schemas.FailureOut(reason=str(exc), retryable=False)

        entry.row_count = result.row_count
        entry.latency_ms = result.elapsed_ms

        # -- 10. chart ------------------------------------------------------
        chart = visualiser.choose(result.columns, result.rows)
        notes = list(result.notes)
        if (warning := visualiser.incomplete_warning(result.columns, result.rows)):
            notes.append(warning)

        # -- 11. explain ----------------------------------------------------
        source_columns = _source_columns(routing.verdict.tables if routing.verdict else ())
        explanation = explain(
            question, final_sql, result.columns, result.rows,
            source_columns=source_columns,
            client=llm if isinstance(llm, LLMClient) else None,
        )

        # -- 12. the outcome ------------------------------------------------
        entry.outcome = Outcome.ANSWERED

        return schemas.AnswerOut(
            message_id=message.id,
            columns=result.columns,
            rows=[list(r) for r in result.rows],
            row_count=result.row_count,
            truncated=result.truncated,
            chart=schemas.ChartOut(**chart.as_dict()),
            explanation=explanation.text,
            source_columns=list(explanation.source_columns),
            # Removed by the server for a viewer or in Readout. Not hidden by
            # the interface -- there is nothing in the payload to hide.
            sql=final_sql if may_see_sql(principal, granted) else None,
            route=routing.route.value,
            generator=routing.generator,
            confidence=routing.confidence,
            latency_ms=result.elapsed_ms,
            notes=notes,
            includes_pending_edit=bool(result.overlay_tables),
        )


# ------------------------------------------------------------- internals ----

def _member_overlays(session, principal, connection) -> dict[str, OverlaySpec]:
    """The caller's own pending corrections on this connection, per table.

    Their reads of exactly those tables get their pending cells laid over the
    real ones, so their own answers include their own values. Owners have
    nothing pending -- their corrections land in the real table immediately --
    and nobody's answers ever include somebody else's overlay, because the
    overlay table is named from the caller's own person id.
    """
    if principal.is_owner or connection.kind != "internal":
        return {}

    pending = session.execute(
        select(MemberEdit.table_name, MemberEdit.column_name)
        .join(MergeRequest, MergeRequest.member_edit_id == MemberEdit.id)
        .where(
            MemberEdit.person_id == principal.person_id,
            MemberEdit.connection_id == connection.id,
            MemberEdit.schema_name == "public",
            MergeRequest.state == "open",
        )
    ).all()
    if not pending:
        return {}

    corrected: dict[str, set[str]] = {}
    for table_name, column_name in pending:
        corrected.setdefault(table_name.lower(), set()).add(column_name)

    overlays: dict[str, OverlaySpec] = {}
    for table_name, columns in corrected.items():
        editable = session.scalar(select(EditableTable).where(
            EditableTable.connection_id == connection.id,
            EditableTable.schema_name == "public",
            EditableTable.table_name == table_name,
        ))
        registry = session.execute(
            select(SchemaColumn.column_name, SchemaColumn.data_type)
            .where(
                SchemaColumn.connection_id == connection.id,
                SchemaColumn.schema_name == "public",
                SchemaColumn.table_name == table_name,
            )
            .order_by(SchemaColumn.id)
        ).all()
        if editable is None or not registry:
            continue   # the registry moved underneath; answer from real data
        overlays[table_name] = OverlaySpec(
            overlay_table=overlay_table_for(principal.person_id, table_name),
            pk_column=editable.pk_column,
            columns=tuple((c, t) for c, t in registry),
            corrected=frozenset(columns),
        )
    return overlays


def _registry_columns(session, connection_id: int) -> list[Column]:
    rows = session.scalars(
        select(SchemaColumn).where(SchemaColumn.connection_id == connection_id)
    ).all()
    return [
        Column(
            schema_name=r.schema_name, table_name=r.table_name,
            column_name=r.column_name, data_type=r.data_type,
            description=r.description, is_public=r.is_public,
            references_to=r.references_to, is_nullable=r.is_nullable,
        )
        for r in rows
    ]


def _recent_questions(session, principal, thread_id: int | None) -> list[str]:
    if thread_id is None:
        return []
    thread = session.get(Thread, thread_id)
    # Threads are scoped by person. Somebody else's history is not context.
    if thread is None or thread.person_id != principal.person_id:
        return []
    rows = session.scalars(
        select(Message.question)
        .where(Message.thread_id == thread_id)
        .order_by(Message.created_at.asc())
    ).all()
    return list(rows)


def _record_message(session, principal, body: schemas.Ask) -> Message:
    thread_id = body.thread_id
    if thread_id is None:
        thread = Thread(person_id=principal.person_id,
                        title=body.question[:120])
        session.add(thread)
        session.flush()
        thread_id = thread.id
    message = Message(thread_id=thread_id, person_id=principal.person_id,
                      question=body.question)
    session.add(message)
    session.flush()
    return message


def _source_columns(tables: tuple[str, ...]) -> tuple[str, ...]:
    """Names for the explanation, taken from what the validator resolved --
    which came from the registry, not from the model."""
    return tables[:4]
