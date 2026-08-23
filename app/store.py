"""SQLite master store - the application's source of truth.

Excel is a rendered view of this, not the other way round. Keeping state here
means a filter is a query rather than a full workbook re-parse, and a crash
mid-write cannot corrupt the user's lead sheet.
"""

import json
import sqlite3
from datetime import date
from pathlib import Path
from uuid import uuid4

from app.dedupe import find_match, merge
from app.models import Business, MatchTag
from app.normalize import normalize_phone

_FIELDS = list(Business.model_fields)

DEFAULT_WORKBOOK = "businesses.xlsx"

# Stored as floats on the model; a non-numeric value here fails validation on the
# next read and makes the whole workbook unreadable, not just the one cell.
_NUMERIC_FIELDS = {"latitude", "longitude"}

# Deliberately NOT reusing `status`, which holds new/review/manual - how the
# record entered the system. Overloading it would break the review workflow.
CALL_STATUSES = ("Not called", "Picked up", "No answer", "Wrong number")
INTEREST_VALUES = ("Yes", "No")

_CHOICES = {
    "call_status": CALL_STATUSES,
    "will_speak_further": INTEREST_VALUES,
}
_BOOL_FIELDS = {"follow_up"}


class DuplicateId(ValueError):
    """An edit would give two rows the same id."""


class InvalidField(ValueError):
    """An edit named an unknown field, or a value that cannot be stored."""

_FILTERS = {
    "without_website": "website IS NULL OR website = ''",
    "with_phone": "phone IS NOT NULL AND phone != ''",
    "without_doctor": "doctor_name IS NULL OR doctor_name = ''",
}


class Store:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cols = ", ".join(
            f"{f} TEXT PRIMARY KEY" if f == "id" else f"{f} TEXT" for f in _FIELDS
        )
        with self._conn() as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS businesses ({cols})")
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the workbook column to a pre-multi-workbook database.

        Idempotent. Existing rows are filed under the default workbook so they
        keep rendering instead of orphaning into a workbook nobody selects.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(businesses)")}
        for field in _FIELDS:
            if field not in existing:
                conn.execute(f"ALTER TABLE businesses ADD COLUMN {field} TEXT")
        # Only `workbook` needs a backfill: without one its rows would orphan
        # into a workbook nobody selects.
        conn.execute(
            "UPDATE businesses SET workbook = ? WHERE workbook IS NULL",
            (DEFAULT_WORKBOOK,),
        )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- reading ---------------------------------------------------------

    def all(self, workbook: str | None = None) -> list[Business]:
        sql, params = "SELECT * FROM businesses", ()
        if workbook:
            sql += " WHERE workbook = ?"
            params = (workbook,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_from_row(r) for r in rows]

    def filter(self, kind: str, workbook: str | None = None) -> list[Business]:
        where = _FILTERS.get(kind)
        if where is None:
            raise ValueError(f"unknown filter: {kind!r}. Expected one of {sorted(_FILTERS)}")
        sql, params = f"SELECT * FROM businesses WHERE ({where})", ()
        if workbook:
            sql += " AND workbook = ?"
            params = (workbook,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_from_row(r) for r in rows]

    # -- writing ---------------------------------------------------------

    def upsert_many(
        self, items: list[Business], workbook: str = DEFAULT_WORKBOOK
    ) -> list[tuple[Business, MatchTag]]:
        """Insert, update, or recognise each incoming business in one workbook.

        `known` holds only that workbook's rows, so `find_match` can only ever
        match within it. That is what makes dedupe per-workbook without
        `dedupe.py` knowing workbooks exist at all.
        """
        known = self.all(workbook)
        results: list[tuple[Business, MatchTag]] = []
        today = date.today().isoformat()

        with self._conn() as conn:
            for item in items:
                match, confidence, _tier = find_match(item, known)

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
                        # MEDIUM matches are kept as their own row and flagged,
                        # never merged and never dropped.
                        "status": "review" if confidence == "medium" else (item.status or "new"),
                    }
                )
                _insert(conn, record)
                known.append(record)
                results.append((record, "review" if confidence == "medium" else "new"))

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
                # A cleared cell is missing data, never an empty string. A bool
                # has no "missing" - an unticked box is False.
                updates[field] = False if field in _BOOL_FIELDS else None
                continue

            if field in _CHOICES:
                # The API is reachable without the UI, so the allowed values are
                # enforced here or the column degrades into free text that never
                # groups or filters together.
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
                # Dedupe matches the normalized form, so an un-normalized edit
                # would silently stop matching. Keep the raw text when it cannot
                # be normalized rather than discarding what the user typed.
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
                conn.execute("DELETE FROM businesses WHERE id = ?", (row_id,))
                _insert(conn, updated)
            else:
                _update(conn, updated)
        return updated

    def delete_row(self, row_id: str) -> bool:
        with self._conn() as conn:
            return (
                conn.execute(
                    "DELETE FROM businesses WHERE id = ?", (row_id,)
                ).rowcount
                > 0
            )

    def create_blank(self, workbook: str) -> Business:
        """An empty row the user fills in by hand, for a lead found offline."""
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
        return row

    def adopt(self, workbook: str, path) -> int:
        """Import rows typed straight into the .xlsx. Returns how many were new.

        Routed through upsert_many, so a hand-typed row duplicating an existing
        lead merges instead of doubling - the same rule as every other path.
        Idempotent: once adopted a row has an ID and is never seen again here.
        """
        from app import excel

        orphans = [r for r in excel.read_rows(path) if not r.get("id")]
        if not orphans:
            return 0

        candidates = [Business(**{**r, "id": None}) for r in orphans]
        tagged = self.upsert_many(candidates, workbook)

        # Consume the source rows and re-render. Without this the ID-less
        # originals stay in the sheet and are adopted again on every open.
        excel.delete_orphan_rows(path)
        excel.sync(self.all(workbook), path)

        return sum(1 for _, tag in tagged if tag in ("new", "review"))

    def move_rows(self, src: str, dst: str) -> int:
        """Re-file rows when their workbook is renamed or moved."""
        with self._conn() as conn:
            return conn.execute(
                "UPDATE businesses SET workbook = ? WHERE workbook = ?", (dst, src)
            ).rowcount

    def delete_rows(self, workbook: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "DELETE FROM businesses WHERE workbook = ?", (workbook,)
            ).rowcount

    def dedupe_existing(self, workbook: str | None = None) -> int:
        """Collapse HIGH-confidence duplicates within a workbook.

        Returns the number of rows removed. MEDIUM and LOW pairs are untouched -
        this method never deletes a row the tier hierarchy is unsure about.
        Rows in different workbooks are never duplicates of each other.
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
                    conn.execute("DELETE FROM businesses WHERE id = ?", (row.id,))
                    removed += 1
                else:
                    kept.append(row)
        return removed


# -- row <-> model ------------------------------------------------------


def _from_row(row: sqlite3.Row) -> Business:
    data = dict(row)
    data["sources"] = json.loads(data.get("sources") or "{}")
    # SQLite stores every column as TEXT here; coerce back to the model's types.
    for numeric in ("latitude", "longitude", "rating"):
        if data.get(numeric) not in (None, ""):
            data[numeric] = float(data[numeric])
    if data.get("review_count") not in (None, ""):
        data["review_count"] = int(float(data["review_count"]))
    raw = data.get("follow_up")
    data["follow_up"] = (
        str(raw).strip().lower() in ("true", "1", "yes") if raw is not None else False
    )
    return Business(**data)


def _to_params(b: Business) -> dict:
    data = b.model_dump()
    data["sources"] = json.dumps(data["sources"])
    return data


def _insert(conn: sqlite3.Connection, b: Business) -> None:
    cols = ", ".join(_FIELDS)
    placeholders = ", ".join(f":{f}" for f in _FIELDS)
    conn.execute(f"INSERT INTO businesses ({cols}) VALUES ({placeholders})", _to_params(b))


def _update(conn: sqlite3.Connection, b: Business) -> None:
    assignments = ", ".join(f"{f} = :{f}" for f in _FIELDS if f != "id")
    conn.execute(f"UPDATE businesses SET {assignments} WHERE id = :id", _to_params(b))
