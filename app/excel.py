"""Renders the SQLite store into `data/businesses.xlsx`.

The workbook is the user's working surface - they sort it, filter it, widen
columns, and add notes. So this updates in place and never recreates a workbook
that already exists: regenerating it every run would silently destroy that work.
"""

from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.models import Business

SHEET = "Businesses"

# Header text -> Business field. Order here is the column order in the sheet.
_LAYOUT: list[tuple[str, str]] = [
    ("ID", "id"),
    ("Business Name", "business_name"),
    ("Category", "category"),
    ("Rating", "rating"),
    ("Reviews", "review_count"),
    ("Business Status", "business_status"),
    ("Doctor Name", "doctor_name"),
    ("Phone", "phone"),
    ("Alternate Phone", "alternate_phone"),
    ("Email", "email"),
    ("Address", "address"),
    ("Area", "area"),
    ("City", "city"),
    ("State", "state"),
    ("Postal Code", "postal_code"),
    ("Website", "website"),
    ("Google Maps URL", "google_maps_url"),
    ("Latitude", "latitude"),
    ("Longitude", "longitude"),
    ("Source", "source"),
    ("Source ID", "source_id"),
    ("Date Found", "date_found"),
    ("Last Updated", "last_updated"),
    ("Status", "status"),
    ("Notes", "notes"),
    ("Short Info", "short_info"),
    ("Info Source", "short_info_source"),
    ("Call Status", "call_status"),
    ("Will Speak Further", "will_speak_further"),
    ("Meeting Date", "meeting_date"),
    ("Meeting Place", "meeting_place"),
    ("Follow Up", "follow_up"),
    ("Follow Up Date", "follow_up_date"),
    ("Sources", "sources"),
]

COLUMNS = [header for header, _ in _LAYOUT]
_FIELDS = [field for _, field in _LAYOUT]

_WIDTHS = {
    "Business Name": 32,
    "Doctor Name": 24,
    "Phone": 16,
    "Alternate Phone": 16,
    "Email": 28,
    "Address": 44,
    "Area": 18,
    "City": 16,
    "Website": 32,
    "Google Maps URL": 34,
    "Notes": 40,
    "Sources": 34,
    "ID": 12,
    "Rating": 9,
    "Reviews": 9,
    "Business Status": 17,
    "Short Info": 52,
    "Info Source": 13,
    "Call Status": 14,
    "Will Speak Further": 17,
    "Meeting Date": 13,
    "Meeting Place": 26,
    "Follow Up": 11,
    "Follow Up Date": 15,
    "Source ID": 18,
}

_HYPERLINK_COLUMNS = {"Website", "Google Maps URL"}


def sync(businesses: list[Business], path: Path | str) -> dict[str, int]:
    """Write `businesses` into the workbook at `path`, updating rows in place.

    Returns {"written": n, "created": 0|1}.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = 0

    if path.exists():
        wb = load_workbook(path)
        ws = wb[SHEET] if SHEET in wb.sheetnames else wb.create_sheet(SHEET)
        if ws.max_row < 1 or ws.cell(row=1, column=1).value != COLUMNS[0]:
            _write_header(ws)
            _apply_widths(ws)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET
        _write_header(ws)
        # Widths are set only at creation. On the update path the user may have
        # resized columns by hand, and overwriting that is the whole reason this
        # function updates in place instead of regenerating.
        _apply_widths(ws)
        created = 1

    row_for_id = {
        ws.cell(row=r, column=1).value: r
        for r in range(2, ws.max_row + 1)
        if ws.cell(row=r, column=1).value
    }

    for business in businesses:
        row = row_for_id.get(business.id)
        if row is None:
            row = ws.max_row + 1
            if business.id:
                row_for_id[business.id] = row
        _write_row(ws, row, business)

    _prune(ws, row_for_id, {b.id for b in businesses if b.id})

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return {"written": len(businesses), "created": created}


def _prune(ws: Worksheet, row_for_id: dict, live_ids: set) -> None:
    """Delete rows this app wrote that the store no longer has.

    Without this, `Remove duplicates` drops rows from the database and leaves
    them visible in the sheet forever.

    Only rows carrying an ID we wrote are eligible. A row the user typed by hand
    has an empty ID column and is never touched - they edit this file, and
    deleting their work would be far worse than showing a stale row.
    """
    stale = sorted(
        (row for wid, row in row_for_id.items() if wid not in live_ids),
        reverse=True,  # bottom-up, so earlier deletions do not shift later rows
    )
    for row in stale:
        ws.delete_rows(row)


def _write_header(ws: Worksheet) -> None:
    for col, header in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)


def _apply_widths(ws: Worksheet) -> None:
    for col, header in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = _WIDTHS.get(header, 14)


def _write_row(ws: Worksheet, row: int, business: Business) -> None:
    for col, (header, field) in enumerate(_LAYOUT, start=1):
        value = getattr(business, field)

        if field == "follow_up":
            # Blank, not "False" - an absence should read as one.
            value = "Yes" if value else None

        if field == "sources":
            # dict -> readable "field: url" lines, blank when empty. Never "{}".
            value = "\n".join(f"{k}: {v}" for k, v in sorted(value.items())) or None

        cell = ws.cell(row=row, column=col)
        # None writes as a genuinely empty cell - never the string "None".
        cell.value = value

        if value and header in _HYPERLINK_COLUMNS:
            cell.hyperlink = value
            cell.style = "Hyperlink"

        if header in ("Address", "Notes", "Sources", "Short Info"):
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def read_rows(path: Path | str) -> list[dict]:
    """Parse a workbook back into field dicts keyed by Business field name.

    Used to find rows the user typed straight into Excel: those have an empty
    ID cell, because only this app writes IDs. Blank cells become None, and a
    row with no business name is skipped as spreadsheet noise.
    """
    path = Path(path)
    if not path.exists():
        return []
    wb = load_workbook(path)
    if SHEET not in wb.sheetnames:
        return []
    ws = wb[SHEET]

    header = [c.value for c in ws[1]]
    index = {name: i for i, name in enumerate(header) if name in COLUMNS}

    out: list[dict] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        record: dict = {}
        for heading, field in _LAYOUT:
            i = index.get(heading)
            value = row[i] if i is not None and i < len(row) else None
            if isinstance(value, str):
                value = value.strip() or None
            record[field] = value
        if not record.get("business_name"):
            continue
        # Reverse what _write_row rendered: "field: url" lines and "Yes"/blank.
        record["sources"] = _parse_sources(record.get("sources"))
        record["follow_up"] = str(record.get("follow_up") or "").strip().lower() in (
            "yes", "true", "1"
        )
        out.append(record)
    return out


def _parse_sources(raw) -> dict[str, str]:
    """Reverse of the "field: url" rendering in _write_row."""
    if not raw or not isinstance(raw, str):
        return {}
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        field, _, url = line.partition(":")
        if field.strip() and url.strip():
            parsed[field.strip()] = url.strip()
    return parsed


def delete_orphan_rows(path: Path | str) -> int:
    """Remove data rows with an empty ID cell. Returns how many were removed.

    Called after adoption: the hand-typed row has become an app-managed record
    with an ID, so leaving the original in place would make the next open adopt
    it all over again.
    """
    path = Path(path)
    if not path.exists():
        return 0
    wb = load_workbook(path)
    if SHEET not in wb.sheetnames:
        return 0
    ws = wb[SHEET]

    stale = [
        r
        for r in range(2, ws.max_row + 1)
        if not ws.cell(row=r, column=1).value
        and ws.cell(row=r, column=COLUMNS.index("Business Name") + 1).value
    ]
    for r in sorted(stale, reverse=True):
        ws.delete_rows(r)
    if stale:
        wb.save(path)
    return len(stale)
