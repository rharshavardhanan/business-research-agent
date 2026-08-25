"""Copy leads from the local SQLite database into Postgres.

The SQLite version stored every column as TEXT; Postgres has real types. That
coercion happens here, once, rather than on every read forever.

    uv run python scripts/migrate_sqlite_to_postgres.py \
        --source data/leads.db --dsn "$DATABASE_URL"

Refuses to run against a non-empty target unless --force is given, so it cannot
silently double a list that was already migrated.
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Business  # noqa: E402
from app.store import Store, _insert  # noqa: E402

_FLOATS = {"latitude", "longitude", "rating"}
_INTS = {"review_count"}
_BOOLS = {"follow_up"}


def _coerce(row: dict) -> dict:
    """SQLite gave us strings. Turn them into what the model declares."""
    data = {k: v for k, v in row.items() if k in Business.model_fields}

    for field in _FLOATS:
        if data.get(field) not in (None, ""):
            data[field] = float(data[field])
        else:
            data[field] = None

    for field in _INTS:
        if data.get(field) not in (None, ""):
            data[field] = int(float(data[field]))
        else:
            data[field] = None

    for field in _BOOLS:
        data[field] = str(data.get(field) or "").strip().lower() in ("1", "true", "yes")

    raw_sources = data.get("sources")
    if isinstance(raw_sources, str):
        try:
            data["sources"] = json.loads(raw_sources or "{}")
        except json.JSONDecodeError:
            data["sources"] = {}
    data["sources"] = data.get("sources") or {}

    data.setdefault("workbook", "businesses.xlsx")
    data["workbook"] = data["workbook"] or "businesses.xlsx"
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/leads.db")
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--force", action="store_true",
                        help="write even if the target already holds rows")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"No SQLite database at {source}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(source)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM businesses")]
    conn.close()

    expected = Counter(r.get("workbook") or "businesses.xlsx" for r in rows)
    print(f"source: {len(rows)} rows across {len(expected)} workbooks")
    for wb, n in sorted(expected.items()):
        print(f"   {wb:<26} {n}")

    store = Store(args.dsn)
    store.init_schema()

    existing = len(store.all())
    if existing and not args.force:
        print(f"\nTarget already holds {existing} rows. Re-run with --force to add anyway.",
              file=sys.stderr)
        return 1

    for workbook in expected:
        store.ensure_workbook(workbook)

    written = 0
    with store._conn() as pg:
        for row in rows:
            business = Business(**_coerce(row))
            # Insert directly: upsert_many would re-run dedupe, and these rows
            # were already deduplicated when they were first saved.
            _insert(pg, business)
            written += 1
        pg.commit()

    actual = Counter(b.workbook for b in store.all())
    print(f"\nwrote {written} rows")
    ok = True
    for workbook, n in sorted(expected.items()):
        got = actual.get(workbook, 0)
        mark = "ok" if got == n else "MISMATCH"
        if got != n:
            ok = False
        print(f"   {workbook:<26} {n} -> {got}  {mark}")
    total = sum(actual.values())
    print(f"   {'total':<26} {len(rows)} -> {total}  {'ok' if total == len(rows) else 'MISMATCH'}")

    return 0 if ok and total == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
