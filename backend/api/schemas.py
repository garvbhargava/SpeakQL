"""The API contract (Backend Plan §12).

**This file is the single definition of every request and response shape.**
If §12 and this file disagree, this file is wrong — the document is what the
frontend was built against, and the frontend already exists.

One rule shows up repeatedly below and is worth stating once: a response
carries `sql` only when the caller is entitled to it. The field is removed by
the server, never hidden by the interface — so a viewer inspecting the network
tab finds nothing, because nothing was sent.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# ================================================================= auth ====

class SignupStart(Model):
    email: EmailStr


class SignupStarted(Model):
    """What happens next, stated before the person commits to it.

    `path` drives the interface's one question: a brand-new company domain
    asks whether they are setting it up; every other path never sees it.
    """

    path: Literal[
        "join_existing", "create_company", "personal_workspace",
        "invite_required", "refused",
    ]
    reason: str
    organisation_name: str | None = None
    domain: str | None = None
    resend_after_seconds: int = 60


class VerifyCode(Model):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    # Only consulted on the create_company path; ignored everywhere else.
    as_owner: bool = False


class Session(Model):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class Refresh(Model):
    refresh_token: str


class Me(Model):
    person_id: int
    email: str
    product_role: Literal["owner", "member"]
    state: Literal["pending", "active", "suspended"]
    organisation: "OrganisationOut"


# ======================================================== organisation ====

class OrganisationOut(Model):
    id: int
    name: str
    kind: Literal["company", "personal"]
    domain: str | None
    people_count: int = 0
    database_count: int = 0


class PersonOut(Model):
    id: int
    email: str
    product_role: Literal["owner", "member"]
    state: str
    grants: list["GrantOut"] = Field(default_factory=list)


class GrantOut(Model):
    connection_id: int
    connection_name: str
    db_role: Literal["analyst", "viewer"]


class InvitePerson(Model):
    email: EmailStr
    product_role: Literal["member"] = "member"
    grants: list["GrantIn"] = Field(default_factory=list)


class GrantIn(Model):
    connection_id: int
    db_role: Literal["analyst", "viewer"]


# =========================================================== databases ====

class ConnectionOut(Model):
    """Note what is absent: credentials.

    Not masked, not partially shown — absent. This response model has no field
    they could occupy, so an accidental `.model_dump()` of a row cannot leak
    one (§9.3).
    """

    id: int
    name: str
    kind: Literal["external", "uploaded"]
    database_name: str
    host: str | None = None
    table_count: int = 0
    row_estimate: int | None = None
    created_at: dt.datetime


class RegisterConnection(Model):
    name: str = Field(min_length=1, max_length=120)
    host: str
    port: int = 5432
    database_name: str
    username: str
    password: str


class ConnectionCheck(Model):
    """The four checks, reported one at a time rather than as one boolean.

    "Connection failed" is the least useful message a form can give.
    """

    check: Literal["host", "tls", "query", "privileges"]
    passed: bool
    detail: str


class ConnectionResult(Model):
    accepted: bool
    checks: list[ConnectionCheck]
    connection: ConnectionOut | None = None
    refused_because: str | None = None


# =============================================================== asking ====

class Ask(Model):
    question: str = Field(min_length=1, max_length=2000)
    connection_id: int
    thread_id: int | None = None
    # Set when answering a clarification, so the pipeline commits rather than
    # asking again. One round only.
    clarification_choice: str | None = None


class RetrievedTable(Model):
    table: str
    score: float
    used: bool


class PipelineStep(Model):
    """One event per step, so the interface can name what is happening as it
    happens rather than showing an undifferentiated spinner."""

    step: int
    name: str
    state: Literal["working", "done", "skipped", "failed"]
    detail: str | None = None
    elapsed_ms: int | None = None


class ChartOut(Model):
    chart: Literal["number", "bar", "line", "table"]
    label_column: str | None = None
    value_column: str | None = None
    reason: str = ""


class AnswerOut(Model):
    """The answered mode. One of five (§11.2)."""

    mode: Literal["answer"] = "answer"
    message_id: int
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    chart: ChartOut
    explanation: str
    source_columns: list[str]
    # Present only for an analyst grant in Workbench. Removed server-side.
    sql: str | None = None
    route: str
    generator: str
    confidence: float | None = None
    latency_ms: int
    notes: list[str] = Field(default_factory=list)
    includes_pending_edit: bool = False


class ClarifyOut(Model):
    mode: Literal["clarify"] = "clarify"
    message_id: int
    term: str
    note: str
    options: list[dict]


class BlockedOut(Model):
    """A rule refused it. The rule that fired is named."""

    mode: Literal["blocked"] = "blocked"
    message_id: int
    refused_by: str
    reason: str
    layer: int
    # Shown struck through when generation happened before the refusal, and
    # absent when layer 1 caught it -- which is how the interface shows the
    # difference between the two without a word of explanation.
    statement: str | None = None


class RefusalOut(Model):
    """The data cannot answer it. Not an error."""

    mode: Literal["refusal"] = "refusal"
    message_id: int
    reason: str
    what_would_help: str | None = None


class FailureOut(Model):
    """A failure, not a refusal: retry strip, no seal band (§13)."""

    mode: Literal["failure"] = "failure"
    reason: str
    retryable: bool = True
    retry_after_seconds: int | None = None


# ========================================================= corrections ====

class ProposeEdit(Model):
    connection_id: int
    schema_name: str = "public"
    table_name: str
    pk_column: str
    pk_value: str
    column_name: str
    after_value: str | None
    note: str | None = Field(default=None, max_length=500)


class EditResult(Model):
    accepted: bool
    # An owner writes the real table; a member raises a merge request. The
    # interface says which happened, because they are genuinely different.
    landed: Literal["real_table", "overlay", "refused"]
    edit_log_id: int | None = None
    merge_request_id: int | None = None
    before_value: str | None = None
    after_value: str | None = None
    reason: str | None = None


class MergeOut(Model):
    id: int
    state: Literal["open", "merged", "rejected", "stale"]
    raised_by: str
    table: str
    pk_value: str
    column_name: str
    before_value: str | None
    proposed_value: str
    # Populated only when the row moved underneath the request.
    current_value: str | None = None
    note: str | None
    created_at: dt.datetime


class MergeDecision(Model):
    decision: Literal["approve", "reject", "reraise"]
    reason: str | None = Field(default=None, max_length=500)


# ============================================================== threads ====

class ThreadOut(Model):
    id: int
    title: str
    created_at: dt.datetime


class MessageOut(Model):
    id: int
    question: str
    created_at: dt.datetime


# ============================================================== schema ====

class ColumnOut(Model):
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    description: str | None = None
    is_public: bool


class HealthOut(Model):
    status: Literal["ok", "degraded"]
    version: str
    env: str
    databases: dict[str, str]
    llm: dict[str, str]


class ErrorOut(Model):
    """Every 4xx and 5xx uses this. One shape, so the interface has one
    place to render a problem."""

    detail: str
    code: str | None = None


# resolve the forward references declared above
Me.model_rebuild()
PersonOut.model_rebuild()
InvitePerson.model_rebuild()
