"""Step 10: pick a chart from the shape of the result (Backend Plan §14).

A rule, not a model. The synopsis deck's own cut order puts the chart-type
classifier first on the list to drop if the schedule slips, and this is what
it drops to -- so it has to be good enough to keep.

It reads only the result's shape: how many columns, of what types, how many
rows, and whether one of them is temporal. That is genuinely most of what a
chart choice depends on, and it has the advantage of being explainable when a
panel asks why a particular chart appeared.

Unknown shape means a table. A table is never wrong.
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass
from enum import Enum


class ChartType(str, Enum):
    NUMBER = "number"    # one row, one numeric column: the headline figure
    BAR = "bar"          # one category, one measure
    LINE = "line"        # time on the x axis
    TABLE = "table"      # anything else, and anything uncertain


@dataclass
class ChartSpec:
    chart: ChartType
    label_column: str | None = None
    value_column: str | None = None
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "chart": self.chart.value,
            "label_column": self.label_column,
            "value_column": self.value_column,
            "reason": self.reason,
        }


_NUMERIC_TYPES = (int, float, decimal.Decimal)
_TEMPORAL_TYPES = (dt.date, dt.datetime)

MAX_BAR_CATEGORIES = 25


def choose(columns: list[str], rows: list[tuple]) -> ChartSpec:
    """Decide from shape alone. Never raises; falls back to a table."""
    if not columns or not rows:
        return ChartSpec(ChartType.TABLE, reason="no rows to draw")

    kinds = [_kind_of(rows, i) for i in range(len(columns))]
    numeric = [i for i, k in enumerate(kinds) if k == "numeric"]
    temporal = [i for i, k in enumerate(kinds) if k == "temporal"]
    textual = [i for i, k in enumerate(kinds) if k == "text"]

    # one row, one number: a headline figure, not a chart
    if len(rows) == 1 and len(columns) == 1 and numeric:
        return ChartSpec(
            ChartType.NUMBER, value_column=columns[0],
            reason="a single numeric value",
        )

    # time on one axis and a measure on the other
    if temporal and numeric and len(columns) <= 3:
        return ChartSpec(
            ChartType.LINE,
            label_column=columns[temporal[0]],
            value_column=columns[numeric[0]],
            reason="one temporal column and one measure",
        )

    # a category and a measure, few enough categories to read
    if textual and numeric and len(columns) <= 3:
        if len(rows) <= MAX_BAR_CATEGORIES:
            return ChartSpec(
                ChartType.BAR,
                label_column=columns[textual[0]],
                value_column=columns[numeric[0]],
                reason=f"{len(rows)} categories against one measure",
            )
        return ChartSpec(
            ChartType.TABLE,
            reason=f"{len(rows)} categories is too many to read as bars",
        )

    return ChartSpec(
        ChartType.TABLE,
        reason=f"{len(columns)} columns; the shape does not suggest one chart",
    )


def _kind_of(rows: list[tuple], index: int) -> str:
    """Judge a column from its values, not from a declared type.

    The result set is what is being drawn, and a COUNT(*) arrives as an int
    whatever the underlying column was.
    """
    for row in rows[:20]:
        if index >= len(row):
            continue
        value = row[index]
        if value is None:
            continue
        if isinstance(value, bool):
            return "text"                      # booleans are categories here
        if isinstance(value, _TEMPORAL_TYPES):
            return "temporal"
        if isinstance(value, _NUMERIC_TYPES):
            return "numeric"
        return "text"
    return "empty"


def incomplete_warning(columns: list[str], rows: list[tuple]) -> str | None:
    """Say so when a measure has gaps.

    Rows with no value are excluded from a total rather than counted as zero,
    so a total computed over them is short. The interface marks those cells
    and offers to fill them; this is the sentence that explains why the number
    is lower than expected.
    """
    if not rows:
        return None
    missing = 0
    for index in range(len(columns)):
        if _kind_of(rows, index) != "numeric":
            continue
        missing += sum(1 for row in rows if index < len(row) and row[index] is None)
    if missing == 0:
        return None
    return (
        f"{missing} value{'s are' if missing != 1 else ' is'} missing. Rows with "
        "no value are excluded from a total rather than counted as zero, so the "
        "figure above is short until they are filled in."
    )
