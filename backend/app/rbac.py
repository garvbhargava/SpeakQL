"""Authorisation: the five questions (Backend Plan §6, Complete Logic §3).

Every request answers these, in order. Failing any one stops the request.

    1. Is the session token valid?                        no -> 401
    2. Is the account active (not pending)?               no -> 403
    3. Is there a grant for THIS database?                no -> 403
    4. Is every id in this request theirs to name?        no -> 403
    5. Does the grant's database role allow the action?   no -> 403

Question 4 is the one most projects forget, and it lives in object_access.py.

The invariant to memorise: **the organisation and connection identity come
from the session, never from the request body** -- and are checked again in
the executor immediately before the query runs. Two checks, because one check
is one point of failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.entities import AccessGrant, Connection, Person, SchemaColumn


class Action(str, Enum):
    """What is being asked. Mapped to requirements in ALLOWS below."""

    READ_PUBLIC_TABLE = "read a table flagged is_public"
    READ_ANY_TABLE = "read any other table"
    SEE_GENERATED_SQL = "see the generated SQL"
    CORRECT_VALUE = "correct a value"
    WRITE_REAL_TABLE = "have that correction land in the real table immediately"
    APPROVE_MERGE = "approve someone else's correction"
    ADMINISTER = "invite, approve a signup, grant access"


@dataclass(frozen=True)
class Principal:
    """Who is asking, resolved from the session and nothing else."""

    person_id: int
    org_id: int
    email: str
    product_role: str          # owner | member
    state: str                 # pending | active | suspended
    persona: str = "workbench"  # workbench | readout

    @property
    def is_owner(self) -> bool:
        return self.product_role == "owner"

    @property
    def is_active(self) -> bool:
        return self.state == "active"


class Denied(HTTPException):
    """403 with a reason the interface can name.

    Deliberately identical for 'you may not' and 'it does not exist' -- see
    object_access.py. A different status for the two would let somebody map
    the system by iterating identifiers.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, detail=reason)


class Unauthenticated(HTTPException):
    def __init__(self, reason: str = "not signed in") -> None:
        super().__init__(status.HTTP_401_UNAUTHORIZED, detail=reason)


# --------------------------------------------------------- question 1-2 ----

def require_active(principal: Principal | None) -> Principal:
    if principal is None:
        raise Unauthenticated()
    if principal.state == "pending":
        raise Denied("this account is waiting for the owner to approve it")
    if principal.state == "suspended":
        raise Denied("this account has been suspended")
    return principal


# ----------------------------------------------------------- question 3 ----

@dataclass(frozen=True)
class GrantedConnection:
    """A connection the caller demonstrably holds a live grant on."""

    connection: Connection
    db_role: str  # analyst | viewer

    @property
    def is_analyst(self) -> bool:
        return self.db_role == "analyst"

    @property
    def is_viewer(self) -> bool:
        return self.db_role == "viewer"


def resolve_connection(
    session: Session, principal: Principal, connection_id: int
) -> GrantedConnection:
    """Question 3, and question 4 for this identifier, in one step.

    The connection must belong to the caller's organisation AND the caller
    must hold a live grant on it. Both are checked here rather than trusting
    that a connection id in a request body is theirs to name.
    """
    connection = session.get(Connection, connection_id)

    # Same refusal for 'not yours' and 'does not exist'.
    if connection is None or connection.org_id != principal.org_id:
        raise Denied("no such database, or it is not yours")

    # An owner implicitly holds analyst on every database in their
    # organisation. This is a single check, here, and not a special case
    # sprinkled through the call sites.
    if principal.is_owner:
        return GrantedConnection(connection, "analyst")

    grant = session.scalar(
        select(AccessGrant).where(
            AccessGrant.person_id == principal.person_id,
            AccessGrant.connection_id == connection_id,
            AccessGrant.revoked_at.is_(None),
        )
    )
    if grant is None:
        raise Denied("no such database, or it is not yours")

    return GrantedConnection(connection, grant.db_role)


# ----------------------------------------------------------- question 5 ----

def require(action: Action, principal: Principal, granted: GrantedConnection | None = None) -> None:
    """The last gate. Raises Denied, or returns quietly."""

    if action is Action.ADMINISTER:
        if not principal.is_owner:
            raise Denied("only an owner can invite people or grant access")
        return

    if action is Action.APPROVE_MERGE:
        if not principal.is_owner:
            raise Denied("only an owner can approve a correction")
        return

    if granted is None:
        raise Denied("no database was named")

    if action is Action.READ_PUBLIC_TABLE:
        return  # any live grant is enough

    if action in (Action.READ_ANY_TABLE, Action.CORRECT_VALUE):
        if not granted.is_analyst:
            raise Denied("a viewer may only read tables marked public")
        return

    if action is Action.SEE_GENERATED_SQL:
        # Two conditions, and both matter. The persona is a display choice;
        # the grant is a permission. A viewer never sees SQL whatever their
        # persona says, and the server strips it rather than the frontend
        # hiding it.
        if not granted.is_analyst:
            raise Denied("a viewer does not receive generated SQL")
        if principal.persona != "workbench":
            raise Denied("SQL is not included in Readout")
        return

    if action is Action.WRITE_REAL_TABLE:
        if not principal.is_owner:
            raise Denied("a member's correction is proposed, not written directly")
        return

    raise Denied(f"unrecognised action: {action}")


def may_see_sql(principal: Principal, granted: GrantedConnection) -> bool:
    """Non-raising form, for shaping a response rather than gating a route."""
    return granted.is_analyst and principal.persona == "workbench"


# ---------------------------------------------- what the validator needs ----

def permitted_tables(
    session: Session, granted: GrantedConnection
) -> tuple[frozenset[str], frozenset[str], bool]:
    """Build the permission set the AST validator checks against.

    Returns (tables, public_columns, public_only). A viewer gets public_only,
    which makes `SELECT *` a refusal and every named column checked.
    """
    rows = session.execute(
        select(
            SchemaColumn.schema_name,
            SchemaColumn.table_name,
            SchemaColumn.column_name,
            SchemaColumn.is_public,
        ).where(SchemaColumn.connection_id == granted.connection.id)
    ).all()

    tables: set[str] = set()
    public_columns: set[str] = set()

    for schema_name, table_name, column_name, is_public in rows:
        qualified = f"{schema_name.lower()}.{table_name.lower()}"
        if granted.is_viewer and not is_public:
            # A viewer's permitted set contains only tables that have at least
            # one public column; the column check does the rest.
            continue
        tables.add(qualified)
        if is_public:
            public_columns.add(column_name.lower())

    return frozenset(tables), frozenset(public_columns), granted.is_viewer
