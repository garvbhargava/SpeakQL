"""Merging a member's correction, and refusing a stale one (§10.5).

**This was a real bug, not an omission**, and it is worth knowing the shape of
it because a panel will ask.

Revision 4 applied an approved merge to "the row the request names". Consider:

    a member proposes  units: empty -> 42
    somebody else sets units: empty -> 50   (verified, from the manifest)
    the owner approves the merge

and 42 overwrites 50. A verified value is destroyed by an older proposal, and
**both writes look correct in the log** — which is what makes it nasty. There
is no error, no conflict, nothing to notice.

The fix: a merge re-reads the live row and compares it with the *before* value
the request was raised against. If it moved, nothing is written and the
request goes **stale** — a state, not an error. The owner is shown all three
values and decides.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.edit_validator import _safe_identifier, assert_single_row
from db.entities import MemberEdit, MergeRequest
from logs import audit_log, edit_log

log = logging.getLogger("speakql.merge")


class MergeOutcome(str, Enum):
    MERGED = "merged"
    REJECTED = "rejected"
    STALE = "stale"
    NOT_FOUND = "not_found"


@dataclass
class MergeResult:
    outcome: MergeOutcome
    message: str
    current_value: str | None = None
    edit_log_id: int | None = None


def read_current_value(engine: Engine, edit: MemberEdit) -> str | None:
    """What the row says *now*, not what it said when the request was raised."""
    schema = _safe_identifier(edit.schema_name)
    table = _safe_identifier(edit.table_name)
    column = _safe_identifier(edit.column_name)
    key = _safe_identifier(edit.pk_column)

    sql = text(
        f'SELECT "{column}" FROM "{schema}"."{table}" WHERE "{key}" = :pk'
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"pk": edit.pk_value}).first()
    if row is None:
        return None
    return None if row[0] is None else str(row[0])


def _same(a: str | None, b: str | None) -> bool:
    """Compare as the database would, not as Python would.

    None and '' are both "empty" to a person filling a gap, and 203 and
    '203' are the same number arriving through different paths.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return (a or "") == (b or "")
    a_s, b_s = str(a).strip(), str(b).strip()
    if a_s == b_s:
        return True
    try:
        return float(a_s) == float(b_s)
    except ValueError:
        return False


def approve(
    meta: Session,
    write_engine: Engine,
    read_engine: Engine,
    merge: MergeRequest,
    edit: MemberEdit,
    *,
    decided_by: int,
    org_id: int,
) -> MergeResult:
    """Apply a member's correction to the real table — unless the row moved."""

    if merge.state != "open":
        return MergeResult(MergeOutcome.NOT_FOUND,
                           f"this request is already {merge.state}")

    # --- the staleness check ------------------------------------------------
    current = read_current_value(read_engine, edit)

    if not _same(current, edit.before_value):
        merge.state = "stale"
        merge.decided_at = dt.datetime.now(dt.timezone.utc)
        meta.flush()
        audit_log.write(
            meta, action=audit_log.Action.MERGE_STALE,
            person_id=decided_by, org_id=org_id, target=f"merge:{merge.id}",
            detail={
                "raised_against": edit.before_value,
                "found": current,
                "proposed": edit.after_value,
            },
        )
        return MergeResult(
            MergeOutcome.STALE,
            (
                "This row changed after the correction was proposed, so it was "
                "not applied. Approving it would have destroyed a newer value, "
                "and both writes would have looked correct in the log."
            ),
            current_value=current,
        )

    # --- apply, with the log row in the same transaction --------------------
    schema = _safe_identifier(edit.schema_name)
    table = _safe_identifier(edit.table_name)
    column = _safe_identifier(edit.column_name)
    key = _safe_identifier(edit.pk_column)

    statement = text(
        f'UPDATE "{schema}"."{table}" SET "{column}" = :value WHERE "{key}" = :pk'
    )

    with write_engine.begin() as conn:
        result = conn.execute(statement, {"value": edit.after_value, "pk": edit.pk_value})
        single = assert_single_row(result.rowcount)
        if not single.allowed:
            # The transaction rolls back on the way out of this block.
            raise RuntimeError(
                f"refusing to merge: {single.reason}. Nothing was written."
            )

    row = edit_log.write(meta, edit_log.Change(
        person_id=edit.person_id,
        connection_id=edit.connection_id,
        schema_name=edit.schema_name,
        table_name=edit.table_name,
        pk_column=edit.pk_column,
        pk_value=edit.pk_value,
        column_name=edit.column_name,
        before_value=edit.before_value,
        after_value=edit.after_value,
        note=edit.note,
        via_merge_id=merge.id,
    ))

    merge.state = "merged"
    merge.decided_by = decided_by
    merge.decided_at = dt.datetime.now(dt.timezone.utc)
    meta.flush()

    audit_log.write(meta, action=audit_log.Action.MERGE_APPROVED,
                    person_id=decided_by, org_id=org_id,
                    target=f"merge:{merge.id}")

    return MergeResult(
        MergeOutcome.MERGED,
        "Merged. The value is in the real database and everybody sees it now.",
        current_value=edit.after_value,
        edit_log_id=row.id,
    )


def reject(meta: Session, merge: MergeRequest, *, decided_by: int, org_id: int,
           reason: str | None = None) -> MergeResult:
    """Rejected, and the member is told why.

    It is removed from their copy, and the real table never changed. A
    rejection with no explanation is how people stop reporting problems.
    """
    if merge.state != "open":
        return MergeResult(MergeOutcome.NOT_FOUND,
                           f"this request is already {merge.state}")

    merge.state = "rejected"
    merge.decided_by = decided_by
    merge.decided_at = dt.datetime.now(dt.timezone.utc)
    meta.flush()

    audit_log.write(meta, action=audit_log.Action.MERGE_REJECTED,
                    person_id=decided_by, org_id=org_id,
                    target=f"merge:{merge.id}",
                    detail={"reason": reason} if reason else {})

    return MergeResult(MergeOutcome.REJECTED,
                       "Rejected. It is gone from their copy, and the real "
                       "table never changed.")


def reraise_against_current(
    meta: Session, read_engine: Engine, merge: MergeRequest, edit: MemberEdit
) -> MergeRequest:
    """Raise a fresh request against what the row says now.

    Offered instead of "apply anyway", so the decision is recorded against the
    value that is really on the row rather than the one it had a day ago.
    """
    current = read_current_value(read_engine, edit)

    fresh_edit = MemberEdit(
        person_id=edit.person_id,
        connection_id=edit.connection_id,
        schema_name=edit.schema_name,
        table_name=edit.table_name,
        pk_column=edit.pk_column,
        pk_value=edit.pk_value,
        column_name=edit.column_name,
        before_value=current,          # the new baseline
        after_value=edit.after_value,
        note=(edit.note or "") + " (re-raised against the current value)",
    )
    meta.add(fresh_edit)
    meta.flush()

    fresh = MergeRequest(member_edit_id=fresh_edit.id, state="open")
    meta.add(fresh)
    meta.flush()
    return fresh
