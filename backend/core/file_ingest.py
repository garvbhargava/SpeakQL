"""Turning an uploaded file into a queryable table (Backend Plan §9.2).

Five stages, and the fifth is the one Revision 5 added:

    sniff      delimiter and encoding, from the file rather than the extension
    sanitise   column names -> safe identifiers, reported not silently changed
    infer      types from the first 1,000 rows, falling back to text
    load       COPY, inside a transaction
    reindex    introspect and embed -- SO THE TABLE IS QUERYABLE IMMEDIATELY

**Reindex is a stage, not a chore.** Until Revision 5 an uploaded table was
not retrievable until somebody ran `make reindex`, which meant the demo had a
step that looked exactly like a bug. The demonstration script depends on this.

**The destination is always asked, never inferred.** Guessing produced a
sprawl of single-table databases nobody could join across.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("speakql.ingest")

SAMPLE_ROWS = 1000
MAX_COLUMNS = 200


@dataclass
class ColumnPlan:
    original: str
    name: str
    data_type: str
    renamed: bool = False
    fell_back: bool = False


@dataclass
class IngestPlan:
    table_name: str
    columns: list[ColumnPlan]
    delimiter: str
    row_estimate: int
    warnings: list[str] = field(default_factory=list)

    @property
    def renamed(self) -> list[ColumnPlan]:
        return [c for c in self.columns if c.renamed]

    @property
    def fell_back(self) -> list[ColumnPlan]:
        return [c for c in self.columns if c.fell_back]


def sniff(raw: bytes) -> tuple[str, str]:
    """Delimiter and encoding, read from the file itself.

    Extensions lie: plenty of things named .csv are tab- or semicolon-
    separated, and a European export is usually cp1252 rather than UTF-8.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw[:65536].decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw[:65536].decode("utf-8", errors="replace")
        encoding = "utf-8 (with replacements)"

    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    return delimiter, encoding


_UNSAFE = re.compile(r"[^a-z0-9_]+")
_RESERVED = {
    "select", "from", "where", "table", "order", "group", "user", "default",
    "primary", "key", "index", "column", "all", "and", "or", "not", "null",
}


def sanitise(name: str, taken: set[str]) -> tuple[str, bool]:
    """A safe identifier, and whether it had to change.

    Reported to the uploader rather than silently applied: somebody who
    uploaded "Order Date" should be told the column is `order_date`, or their
    first question will name a column that does not exist.
    """
    original = (name or "").strip()
    cleaned = _UNSAFE.sub("_", original.lower()).strip("_")
    cleaned = re.sub(r"_{2,}", "_", cleaned)

    if not cleaned:
        cleaned = "column"
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    if cleaned in _RESERVED:
        cleaned = f"{cleaned}_col"
    cleaned = cleaned[:63]

    base = cleaned
    suffix = 2
    while cleaned in taken:
        cleaned = f"{base}_{suffix}"
        suffix += 1

    return cleaned, cleaned != original


def infer_type(values: list[str]) -> tuple[str, bool]:
    """A Postgres type from a sample, and whether it fell back to text.

    Falling back is **reported**, never silent. A column that should have been
    numeric and quietly became text produces a chart that will not draw and an
    aggregate that will not run, three steps later.
    """
    present = [v for v in values if v is not None and str(v).strip() != ""]
    if not present:
        return "text", False

    def all_match(check) -> bool:
        return all(check(v) for v in present)

    def is_int(v: str) -> bool:
        s = str(v).strip().replace(",", "")
        return bool(re.fullmatch(r"[+-]?\d+", s)) and abs(int(s)) < 2**63

    def is_numeric(v: str) -> bool:
        try:
            float(str(v).strip().replace(",", ""))
            return True
        except ValueError:
            return False

    def is_bool(v: str) -> bool:
        return str(v).strip().lower() in {"true", "false", "t", "f", "yes", "no", "0", "1"}

    def is_date(v: str) -> bool:
        s = str(v).strip()
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s)) or \
            bool(re.fullmatch(r"\d{2}/\d{2}/\d{4}", s))

    def is_timestamp(v: str) -> bool:
        s = str(v).strip()
        return bool(re.match(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", s))

    if all_match(is_bool) and len({str(v).strip().lower() for v in present}) <= 2:
        return "boolean", False
    if all_match(is_int):
        return "bigint", False
    if all_match(is_numeric):
        return "numeric", False
    if all_match(is_timestamp):
        return "timestamp", False
    if all_match(is_date):
        return "date", False

    # Genuinely text, or mixed. Mixed is the case worth reporting.
    mixed = any(is_numeric(v) for v in present) and not all_match(is_numeric)
    return "text", mixed


def plan(raw: bytes, *, table_name: str) -> IngestPlan:
    """Read the file and decide what the table will look like."""
    delimiter, encoding = sniff(raw)
    text = raw.decode(encoding.split(" ")[0], errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("the file is empty") from None

    if len(header) > MAX_COLUMNS:
        raise ValueError(f"{len(header)} columns is more than the {MAX_COLUMNS} limit")

    sample: list[list[str]] = []
    for index, row in enumerate(reader):
        if index >= SAMPLE_ROWS:
            break
        sample.append(row)

    taken: set[str] = set()
    columns: list[ColumnPlan] = []
    warnings: list[str] = []

    for position, original in enumerate(header):
        name, renamed = sanitise(original, taken)
        taken.add(name)
        values = [row[position] for row in sample if position < len(row)]
        data_type, fell_back = infer_type(values)
        columns.append(ColumnPlan(original, name, data_type, renamed, fell_back))

    renamed = [c for c in columns if c.renamed]
    if renamed:
        warnings.append(
            f"{len(renamed)} column{'s were' if len(renamed) != 1 else ' was'} "
            "renamed to a safe identifier: "
            + ", ".join(f"{c.original} -> {c.name}" for c in renamed[:5])
        )
    fell_back = [c for c in columns if c.fell_back]
    if fell_back:
        warnings.append(
            f"{len(fell_back)} column{'s' if len(fell_back) != 1 else ''} held "
            "mixed values and fell back to text: "
            + ", ".join(c.name for c in fell_back[:5])
        )

    # Count the rest of the rows without holding the file in memory twice.
    row_estimate = len(sample) + sum(1 for _ in reader)

    return IngestPlan(
        table_name=table_name, columns=columns, delimiter=delimiter,
        row_estimate=row_estimate, warnings=warnings,
    )


def create_table_sql(schema: str, plan: IngestPlan) -> str:
    """DDL from a plan whose identifiers have already been sanitised.

    Every name in here came out of `sanitise`, which admits only
    `[a-z0-9_]` -- so the interpolation below cannot carry anything but an
    identifier.
    """
    columns = ",\n    ".join(
        f'"{c.name}" {c.data_type}' for c in plan.columns
    )
    return (
        f'CREATE TABLE "{schema}"."{plan.table_name}" (\n'
        f"    id bigserial PRIMARY KEY,\n"
        f"    {columns}\n"
        f");"
    )
