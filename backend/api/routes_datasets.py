"""Uploading a file (Backend Plan §9.2).

Two routes, because the destination is **always asked and never inferred**:

    POST /api/datasets/plan     read the file, report what would be created
    POST /api/datasets/load     do it, then reindex

Splitting them is what lets the interface show the uploader the table name,
the column renames and the type fall-backs *before* anything is created.
Guessing a destination produced a sprawl of single-table databases nobody
could join across, which is the problem this two-step exists to prevent.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select, text

from api import schemas
from app.deps import EnginesDep, LimiterDep, OwnerDep, SessionDep
from app.ratelimit import Limited
from core import file_ingest
from db.entities import Connection, UploadedDataset
from db.introspect import introspect
from logs import audit_log

log = logging.getLogger("speakql.datasets")

router_api = APIRouter(prefix="/api/datasets", tags=["databases"])

MAX_UPLOAD_MB = 50


@router_api.post("/plan", response_model=dict)
async def plan_upload(
    principal: OwnerDep,
    limiter: LimiterDep,
    file: UploadFile = File(...),
    table_name: str = Form(...),
) -> dict:
    """Read the file and say what would happen. Creates nothing."""
    decision = limiter.check(principal.person_id, Limited.UPLOAD)
    if not decision.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, decision.message)

    raw = await _read_bounded(file)
    safe_name, _ = file_ingest.sanitise(table_name, set())

    try:
        plan = file_ingest.plan(raw, table_name=safe_name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return {
        "table_name": plan.table_name,
        "delimiter": plan.delimiter,
        "row_estimate": plan.row_estimate,
        "columns": [
            {
                "original": c.original, "name": c.name,
                "data_type": c.data_type, "renamed": c.renamed,
                "fell_back_to_text": c.fell_back,
            }
            for c in plan.columns
        ],
        # Reported, never silently applied.
        "warnings": plan.warnings,
    }


@router_api.post("/load", response_model=dict, status_code=status.HTTP_201_CREATED)
async def load_upload(
    principal: OwnerDep,
    session: SessionDep,
    engines: EnginesDep,
    limiter: LimiterDep,
    file: UploadFile = File(...),
    table_name: str = Form(...),
) -> dict:
    """Create the table, load the rows, then reindex — in that order.

    No connection id is accepted from the form. Uploads always land in the
    caller's own organisation's upload connection, found or created here, so
    there is no field through which an upload could be pointed elsewhere.
    """
    decision = limiter.check(principal.person_id, Limited.UPLOAD)
    if not decision.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, decision.message)

    connection = _uploads_connection(session, principal.org_id)

    raw = await _read_bounded(file)
    safe_name, _ = file_ingest.sanitise(table_name, set())

    try:
        plan = file_ingest.plan(raw, table_name=safe_name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    schema = f"org_{principal.org_id}"
    loaded = 0

    # The upload role can CREATE and INSERT inside its own organisation's
    # schema, and nothing anywhere else.
    with engines.uploads.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{plan.table_name}"'))
        conn.execute(text(file_ingest.create_table_sql(schema, plan)))

        import csv, io  # noqa: PLC0415

        delimiter, encoding = file_ingest.sniff(raw)
        reader = csv.reader(
            io.StringIO(raw.decode(encoding.split(" ")[0], errors="replace")),
            delimiter=delimiter,
        )
        next(reader, None)   # header

        names = [c.name for c in plan.columns]
        types = [c.data_type for c in plan.columns]
        placeholders = ", ".join(f":{n}" for n in names)
        quoted = ", ".join(f'"{n}"' for n in names)
        insert = text(
            f'INSERT INTO "{schema}"."{plan.table_name}" ({quoted}) '
            f"VALUES ({placeholders})"
        )

        batch: list[dict] = []
        for row in reader:
            values = {
                name: file_ingest.coerce(row[i] if i < len(row) else None, types[i])
                for i, name in enumerate(names)
            }
            batch.append(values)
            if len(batch) >= 500:
                conn.execute(insert, batch)
                loaded += len(batch)
                batch = []
        if batch:
            conn.execute(insert, batch)
            loaded += len(batch)

        # The read role can use this organisation's schema and read this
        # table -- and the validator, checking against this organisation's
        # registry, is what stops it reaching any other organisation's.
        conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO speakql_ro'))
        conn.execute(text(
            f'GRANT SELECT ON "{schema}"."{plan.table_name}" TO speakql_ro'
        ))

    dataset = UploadedDataset(
        connection_id=connection.id,
        original_name=file.filename or "upload.csv",
        schema_name=schema,
        table_name=plan.table_name,
        row_count=loaded,
        uploaded_by=principal.person_id,
    )
    session.add(dataset)
    session.flush()

    # Stage five. The table is queryable when this response returns -- there
    # is no manual reindex step, and the demonstration script depends on that.
    # Read with this connection's own read engine, and ONLY this
    # organisation's schema: introspecting the whole shared uploads database
    # would put other organisations' table names into this registry.
    result = introspect(session, engines.tenants.read(connection), connection.id,
                        only_schemas=(schema,))

    audit_log.write(session, action=audit_log.Action.DATASET_UPLOADED,
                    person_id=principal.person_id, org_id=principal.org_id,
                    target=f"{schema}.{plan.table_name}",
                    detail={"rows": loaded})
    session.flush()

    return {
        "created": f"{schema}.{plan.table_name}",
        "rows": loaded,
        "warnings": plan.warnings,
        "reindexed": result.summary,
        "queryable_now": True,
    }


def _uploads_connection(session, org_id: int) -> Connection:
    """The organisation's upload connection, created the first time it is
    needed. One per organisation; its tables live in schema org_<id>."""
    connection = session.scalar(select(Connection).where(
        Connection.org_id == org_id, Connection.kind == "uploaded"
    ))
    if connection is None:
        connection = Connection(
            org_id=org_id, name="Uploads", kind="uploaded",
            database_name="speakql_uploads",
        )
        session.add(connection)
        session.flush()
    return connection


async def _read_bounded(file: UploadFile) -> bytes:
    """Read with a ceiling. An unbounded read is a memory exhaustion bug
    wearing an upload form."""
    limit = MAX_UPLOAD_MB * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"that file is larger than {MAX_UPLOAD_MB} MB",
        )
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that file is empty")
    return raw
