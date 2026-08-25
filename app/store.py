"""Postgres store — the application's source of truth.

Rewritten from SQLite for serverless hosting, where there is no persistent disk.
Two behavioural differences matter:

* Columns carry real types. The SQLite version stored everything as TEXT and
  coerced on read; that coercion is gone, not relocated.
* Deletes are soft. `deleted_at` replaces the `.trash/` directory, so a deleted
  workbook is recoverable by clearing one column.
"""

import json
from datetime import date
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.dedupe import find_match, merge
from app.models import Business, MatchTag
from app.normalize import normalize_phone

DEFAULT_WORKBOOK = "businesses.xlsx"

# Everything else is TEXT. Getting these right is the point of the migration:
# a rating that sorts as text puts "5" below "4.9".
_TYPES = {
    "latitude": "DOUBLE PRECISION",
    "longitude": "DOUBLE PRECISION",
    "rating": "REAL",
    "review_count": "INTEGER",
    "follow_up": "BOOLEAN",
    "sources": "JSONB",
}

_FIELDS = list(Business.model_fields)
# `id` and `workbook` are declared explicitly in the schema below.
_DATA_FIELDS = [f for f in _FIELDS if f not in ("id", "workbook")]

_FILTERS = {
    "without_website": "(website IS NULL OR website = '')",
    "with_phone": "(phone IS NOT NULL AND phone != '')",
    "without_doctor": "(doctor_name IS NULL OR doctor_name = '')",
}

_NUMERIC_FIELDS = {"latitude", "longitude"}

CALL_STATUSES = ("Not called", "Picked up", "No answer", "Wrong number")
INTEREST_VALUES = ("Yes", "No")

_CHOICES = {"call_status": CALL_STATUSES, "will_speak_further": INTEREST_VALUES}
_BOOL_FIELDS = {"follow_up"}


class DuplicateId(ValueError):
    """An edit would give two rows the same id."""


class InvalidField(ValueError):
    """An edit named an unknown field, or a value that cannot be stored."""


def _column_ddl() -> str:
    return ", ".join(f"{f} {_TYPES.get(f, 'TEXT')}" for f in _DATA_FIELDS)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS workbooks (
    path       TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS businesses (
    id         TEXT PRIMARY KEY,
    workbook   TEXT NOT NULL REFERENCES workbooks(path)
                    ON UPDATE CASCADE ON DELETE CASCADE,
    deleted_at TIMESTAMPTZ,
    {_column_ddl()}
);
CREATE INDEX IF NOT EXISTS businesses_workbook_idx
    ON businesses (workbook) WHERE deleted_at IS NULL;
"""


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self):
        # A new connection per operation: a serverless instance cannot keep a
        # pool alive between invocations, and Neon's pooled endpoint expects it.
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def init_schema(self) -> None:
        """Create the tables. Idempotent, safe to call on every cold start."""
        with self._conn() as conn:
            conn.execute(SCHEMA)
            conn.commit()

    # -- reading ---------------------------------------------------------

    def all(self, workbook: str | None = None) -> list[Business]:
        sql = "SELECT * FROM businesses WHERE deleted_at IS NULL"
        params: tuple = ()
        if workbook:
            sql += " AND workbook = %s"
            params = (workbook,)
        sql += " ORDER BY date_found NULLS LAST, business_name"
        with self._conn() as conn:
            return [_to_business(r) for r in conn.execute(sql, params).fetchall()]

    def filter(self, kind: str, workbook: str | None = None) -> list[Business]:
        where = _FILTERS.get(kind)
        if where is None:
            raise ValueError(f"unknown filter: {kind!r}. Expected one of {sorted(_FILTERS)}")
        sql = f"SELECT * FROM businesses WHERE deleted_at IS NULL AND {where}"
        params: tuple = ()
        if workbook:
            sql += " AND workbook = %s"
            params = (workbook,)
        with self._conn() as conn:
            return [_to_business(r) for r in conn.execute(sql, params).fetchall()]

    def count_deleted(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM businesses WHERE deleted_at IS NOT NULL"
            ).fetchone()["n"]

    # -- writing ---------------------------------------------------------

    def ensure_workbook(self, path: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO workbooks (path) VALUES (%s) ON CONFLICT (path) DO NOTHING",
                (path,),
            )
            conn.commit()

    def upsert_many(
        self, items: list[Business], workbook: str = DEFAULT_WORKBOOK
    ) -> list[tuple[Business, MatchTag]]:
        """Insert, update, or recognise each incoming business in one workbook.

        `known` holds only that workbook's rows, so `find_match` can only ever
        match within it - which is what makes dedupe per-workbook without
        `dedupe.py` knowing workbooks exist.
        """
        self.ensure_workbook(workbook)
        known = self.all(workbook)
        results: list[tuple[Business, MatchTag]] = []
        today = date.today().isoformat()

        with self._conn() as conn:
            for item in items:
                match, confidence, _ = find_match(item, known)

                if confidence == "high" and match is not None:
                    merged, changed = merge(match, item)
                    if changed:
                        _update(conn, merged)
                        known[known.index(match)] = merged
                        results.append((merged, "updated"))
                    else:
                        results.append((match, "existing"))
                    continue

                record = item.model_copy(
                    update={
                        "workbook": workbook,
                        "id": item.id or uuid4().hex,
                        "date_found": item.date_found or today,
                        "last_updated": today,
                        "status": "review" if confidence == "medium" else (item.status or "new"),
                    }
                )
                _insert(conn, record)
                known.append(record)
                results.append((record, "review" if confidence == "medium" else "new"))
            conn.commit()

        return results

    def update_fields(self, row_id: str, changes: dict) -> Business:
        """Apply an edit to one row.

        Every column is editable by explicit decision, including provenance.
        The only refusals are the two that would corrupt the app's own state:
        a colliding or empty id, and a non-numeric coordinate.
        """
        rows = {r.id: r for r in self.all()}
        current = rows.get(row_id)
        if current is None:
            raise InvalidField(f"no such row: {row_id!r}")

        updates: dict = {}
        for field, raw in changes.items():
            if field not in Business.model_fields:
                raise InvalidField(f"unknown field: {field!r}")

            if field == "sources":
                updates[field] = raw or {}
                continue

            value = raw.strip() if isinstance(raw, str) else raw

            if value in (None, ""):
                if field == "id":
                    raise InvalidField("id cannot be empty")
                updates[field] = False if field in _BOOL_FIELDS else None
                continue

            if field in _CHOICES:
                if value not in _CHOICES[field]:
                    raise InvalidField(
                        f"{field} must be one of: {', '.join(_CHOICES[field])}"
                    )
            elif field in _BOOL_FIELDS:
                value = (
                    value
                    if isinstance(value, bool)
                    else str(value).strip().lower() in ("true", "1", "yes", "on")
                )
            elif field == "id":
                if value != row_id and value in rows:
                    raise DuplicateId(
                        f"id {value!r} is already used by another row. Two rows "
                        "sharing an id would collide on the same workbook row."
                    )
            elif field in _NUMERIC_FIELDS:
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise InvalidField(
                        f"{field} must be a number or blank, got {raw!r}"
                    ) from exc
            elif field in ("phone", "alternate_phone"):
                value = normalize_phone(value) or value

            updates[field] = value

        # A hand-written note must never carry a badge claiming it came from a
        # website or a model.
        if "short_info" in updates and "short_info_source" not in changes:
            updates["short_info_source"] = "Manual" if updates["short_info"] else None

        # The follow-up pair cannot disagree: a scheduled follow-up is needed,
        # and not following up means there is no date. Flag-on-without-a-date
        # stays valid - "needs follow-up, not yet scheduled" is a real state.
        if updates.get("follow_up_date"):
            updates["follow_up"] = True
        if updates.get("follow_up") is False and "follow_up" in updates:
            updates["follow_up_date"] = None

        updated = current.model_copy(
            update={**updates, "last_updated": date.today().isoformat()}
        )
        with self._conn() as conn:
            if updated.id != row_id:
                conn.execute("DELETE FROM businesses WHERE id = %s", (row_id,))
                _insert(conn, updated)
            else:
                _update(conn, updated)
            conn.commit()
        return updated

    def delete_row(self, row_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE businesses SET deleted_at = now() "
                "WHERE id = %s AND deleted_at IS NULL",
                (row_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_rows(self, workbook: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE businesses SET deleted_at = now() "
                "WHERE workbook = %s AND deleted_at IS NULL",
                (workbook,),
            )
            conn.commit()
            return cur.rowcount

    def create_blank(self, workbook: str = DEFAULT_WORKBOOK) -> Business:
        """An empty row the user fills in by hand, for a lead found offline."""
        self.ensure_workbook(workbook)
        today = date.today().isoformat()
        row = Business(
            business_name="",
            id=uuid4().hex,
            workbook=workbook,
            date_found=today,
            last_updated=today,
            status="manual",
        )
        with self._conn() as conn:
            _insert(conn, row)
            conn.commit()
        return row

    def move_rows(self, src: str, dst: str) -> int:
        """Re-file rows when a workbook is renamed.

        Normally unnecessary - the foreign key's ON UPDATE CASCADE does it - but
        kept for callers that move rows without renaming the workbook row.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE businesses SET workbook = %s WHERE workbook = %s", (dst, src)
            )
            conn.commit()
            return cur.rowcount

    def dedupe_existing(self, workbook: str | None = None) -> int:
        """Collapse HIGH-confidence duplicates within a workbook.

        MEDIUM and LOW pairs are untouched - this never removes a row the tier
        hierarchy is unsure about. Rows in different workbooks are never
        duplicates of each other.
        """
        rows = self.all(workbook)
        removed = 0
        with self._conn() as conn:
            kept: list[Business] = []
            for row in rows:
                match, confidence, _ = find_match(row, kept)
                if confidence == "high" and match is not None:
                    merged, changed = merge(match, row)
                    if changed:
                        _update(conn, merged)
                        kept[kept.index(match)] = merged
                    conn.execute(
                        "UPDATE businesses SET deleted_at = now() WHERE id = %s",
                        (row.id,),
                    )
                    removed += 1
                else:
                    kept.append(row)
            conn.commit()
        return removed


# -- row <-> model ------------------------------------------------------


def _to_business(row: dict) -> Business:
    """Postgres returns native types, so there is nothing to coerce."""
    data = {k: v for k, v in row.items() if k in Business.model_fields}
    data["sources"] = data.get("sources") or {}
    data["follow_up"] = bool(data.get("follow_up"))
    return Business(**data)


def _params(b: Business) -> dict:
    data = b.model_dump()
    data["sources"] = Jsonb(data.get("sources") or {})
    return data


def _insert(conn, b: Business) -> None:
    cols = ", ".join(_FIELDS)
    placeholders = ", ".join(f"%({f})s" for f in _FIELDS)
    conn.execute(f"INSERT INTO businesses ({cols}) VALUES ({placeholders})", _params(b))


def _update(conn, b: Business) -> None:
    assignments = ", ".join(f"{f} = %({f})s" for f in _FIELDS if f != "id")
    conn.execute(f"UPDATE businesses SET {assignments} WHERE id = %(id)s", _params(b))
