"""ORM mapping for speakql_meta (Backend Plan §5.2).

These mirror sql/10_meta.sql exactly. The SQL file is the source of truth --
bootstrap.sh runs it, not Alembic against these classes -- so if the two ever
disagree, the SQL is right and this file is the bug.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    Integer, LargeBinary, Numeric, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# JSONB in Postgres, plain JSON on SQLite. The production database is
# Postgres; the variant exists so tests/test_api.py can drive the real
# application against an in-memory database without a container.
JSONColumn = JSONB().with_variant(JSON(), "sqlite")

# BIGSERIAL in Postgres. SQLite only auto-increments a column declared exactly
# INTEGER PRIMARY KEY, so the variant is what lets the same models create a
# working schema in both.
PkType = BigInteger().with_variant(Integer(), "sqlite")


def _pk() -> Mapped[int]:
    return mapped_column(PkType, primary_key=True, autoincrement=True)


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organisation(Base):
    """A company, or one person's personal workspace.

    `domain` is NULL for a personal workspace, and that null is the entire
    tenant-isolation guarantee -- see auth/domain_resolver.py.
    """

    __tablename__ = "organisations"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # company | personal
    domain: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _now()

    people: Mapped[list["Person"]] = relationship(back_populates="org")
    connections: Mapped[list["Connection"]] = relationship(back_populates="org")

    __table_args__ = (
        CheckConstraint("kind IN ('company','personal')", name="org_kind"),
    )

    @property
    def is_personal(self) -> bool:
        return self.kind == "personal"


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = _pk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"))
    product_role: Mapped[str] = mapped_column(Text, nullable=False)  # owner | member
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[dt.datetime] = _now()

    org: Mapped[Organisation] = relationship(back_populates="people")
    grants: Mapped[list["AccessGrant"]] = relationship(
        back_populates="person", foreign_keys="AccessGrant.person_id"
    )

    @property
    def is_owner(self) -> bool:
        return self.product_role == "owner"

    @property
    def is_active(self) -> bool:
        return self.state == "active"


class Connection(Base):
    """A warehouse on this server, a registered external database, or the home
    of an organisation's uploaded datasets."""

    __tablename__ = "connections"

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # internal | external | uploaded
    host: Mapped[Optional[str]] = mapped_column(Text)
    port: Mapped[Optional[int]] = mapped_column(Integer)
    database_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Encrypted at rest and never returned by any endpoint, including the one
    # that lists connections (§9.3).
    secret_cipher: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = _now()

    org: Mapped[Organisation] = relationship(back_populates="connections")

    __table_args__ = (UniqueConstraint("org_id", "name"),)


class AccessGrant(Base):
    """person x connection, carrying the DATABASE role.

    The second axis of permission. It lives on the grant and never on the
    person, which is what lets one person be an analyst on Sales and a viewer
    on Support at the same time.
    """

    __tablename__ = "access_grants"

    id: Mapped[int] = _pk()
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    db_role: Mapped[str] = mapped_column(Text, nullable=False)  # analyst | viewer
    granted_by: Mapped[Optional[int]] = mapped_column(ForeignKey("people.id"))
    granted_at: Mapped[dt.datetime] = _now()
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person] = relationship(back_populates="grants", foreign_keys=[person_id])

    __table_args__ = (UniqueConstraint("person_id", "connection_id"),)

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None

    @property
    def is_analyst(self) -> bool:
        return self.db_role == "analyst" and self.is_live


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = _pk()
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    product_role: Mapped[str] = mapped_column(Text, nullable=False)
    grants_json: Mapped[list] = mapped_column(JSONColumn, default=list)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()


class SchemaColumn(Base):
    """One row per column. is_public decides what a viewer may read."""

    __tablename__ = "schema_registry"

    id: Mapped[int] = _pk()
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "schema.table.column" for a single-column foreign key: the join path,
    # recorded so the generator does not have to guess it.
    references_to: Mapped[Optional[str]] = mapped_column(Text)
    # Nullable columns are where "not recorded" lives. Without this the
    # generator writes units = 0 for "shipments with no units".
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("connection_id", "schema_name", "table_name", "column_name"),
    )

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.table_name}.{self.column_name}"


class EditableTable(Base):
    """Default: every table with a primary key. A change that cannot be pinned
    to one row cannot be checked, so a table without one is never editable."""

    __tablename__ = "editable_tables"

    id: Mapped[int] = _pk()
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    pk_column: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("connection_id", "schema_name", "table_name"),)


class MemberEdit(Base):
    """A member's pending correction. Lives in their overlay until merged."""

    __tablename__ = "member_edits"

    id: Mapped[int] = _pk()
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    pk_column: Mapped[str] = mapped_column(Text, nullable=False)
    pk_value: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    before_value: Mapped[Optional[str]] = mapped_column(Text)
    after_value: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()


class MergeRequest(Base):
    """`stale` is a state, not an error: the row moved after the request was
    raised, so applying it would destroy a newer verified value (§10.5)."""

    __tablename__ = "merge_requests"

    id: Mapped[int] = _pk()
    member_edit_id: Mapped[int] = mapped_column(
        ForeignKey("member_edits.id", ondelete="CASCADE")
    )
    decided_by: Mapped[Optional[int]] = mapped_column(ForeignKey("people.id"))
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[dt.datetime] = _now()
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    edit: Mapped[MemberEdit] = relationship()


class UploadedDataset(Base):
    __tablename__ = "uploaded_datasets"

    id: Mapped[int] = _pk()
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(PkType, nullable=False)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("people.id"))
    uploaded_at: Mapped[dt.datetime] = _now()


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = _pk()
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = _pk()
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class Clarification(Base):
    __tablename__ = "clarifications"

    id: Mapped[int] = _pk()
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    options: Mapped[list] = mapped_column(JSONColumn, nullable=False)
    chosen: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = _pk()
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # -1 | 1
    created_at: Mapped[dt.datetime] = _now()

    __table_args__ = (UniqueConstraint("message_id", "person_id"),)


class OtpAttempt(Base):
    __tablename__ = "otp_attempts"

    id: Mapped[int] = _pk()
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = _now()


# ------------------------------------------------------------- the 3 logs ----
# Each has exactly one writer -- see logs/. query_log gets a row on EVERY
# path, which is what makes the measurement honest.

class QueryLog(Base):
    __tablename__ = "query_log"

    id: Mapped[int] = _pk()
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"))
    connection_id: Mapped[Optional[int]] = mapped_column(ForeignKey("connections.id"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[Optional[str]] = mapped_column(Text)
    generated_sql: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    row_count: Mapped[Optional[int]] = mapped_column(Integer)
    refused_by: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()


class EditLog(Base):
    __tablename__ = "edit_log"

    id: Mapped[int] = _pk()
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    pk_column: Mapped[str] = mapped_column(Text, nullable=False)
    pk_value: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    before_value: Mapped[Optional[str]] = mapped_column(Text)
    after_value: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    via_merge_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merge_requests.id"))
    created_at: Mapped[dt.datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = _pk()
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("people.id"))
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organisations.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[Optional[str]] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    created_at: Mapped[dt.datetime] = _now()
