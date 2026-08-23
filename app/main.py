"""FastAPI application: six endpoints plus the static UI.

The result buffer is the important design point. `/search` fills it and returns
tagged results *without persisting*; `/businesses/save` is what commits them.
That is what makes "find dental clinics in Chromepet" then "add these to Excel"
work as two steps, with a human review in between.
"""

import base64
import binascii
import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import excel
from app import workbooks as wb
from app.providers import PLACES_MAX_RESULTS
from app.models import Business, Command
from app.enrichment import fetch_page_text
from app.parser import (
    DEFAULT_MODEL,
    MissingAPIKey,
    ParserError,
    RateLimited,
    parse_command,
    summarize,
)
from app.research import TaggedBusiness, research
from app.store import DEFAULT_WORKBOOK, DuplicateId, InvalidField, Store

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app")

# Owned by app.workbooks so there is exactly one definition. A second copy here
# drifted the moment tests pointed DATA_DIR elsewhere: main wrote to the temp
# directory while workbooks resolved paths against the original one.
DATA_DIR = wb.DATA_DIR

app = FastAPI(title="Business Research Agent")

# Opt-in HTTP Basic auth. Unset means no auth, because this is a local
# single-user tool by default and a password would be friction on localhost.
# Set APP_PASSWORD before exposing it to a network: every route spends billable
# Google and Gemini quota and can read or delete the lead list.
APP_USERNAME = os.environ.get("APP_USERNAME") or "admin"
APP_PASSWORD = os.environ.get("APP_PASSWORD") or ""


def _authorized(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, _, password = decoded.partition(":")
    # compare_digest on both halves so neither is a timing oracle.
    return secrets.compare_digest(user, APP_USERNAME) and secrets.compare_digest(
        password, APP_PASSWORD
    )


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Guard every route, including the static UI.

    Middleware rather than a per-route dependency: StaticFiles is mounted, not
    routed, so a dependency would leave the page itself open and only its API
    calls failing - which reads as a broken app rather than a locked one.
    """
    if not APP_PASSWORD:
        return await call_next(request)
    if _authorized(request.headers.get("authorization")):
        return await call_next(request)
    return Response(
        status_code=401,
        content="Authentication required.",
        headers={"WWW-Authenticate": 'Basic realm="Business Research Agent"'},
    )

_store = Store(DATA_DIR / "leads.db")
# Single-user local tool: one buffer, no session keying. Cleared on restart.
_buffer: list[TaggedBusiness] = []


class CommandRequest(BaseModel):
    command: str
    workbook: str = DEFAULT_WORKBOOK


class SearchRequest(BaseModel):
    business_type: str
    location: str
    quantity: int | None = None
    radius_km: int | None = None
    workbook: str = DEFAULT_WORKBOOK


class SaveRequest(BaseModel):
    workbook: str = DEFAULT_WORKBOOK


class EditRequest(BaseModel):
    changes: dict
    workbook: str = DEFAULT_WORKBOOK


class RowRequest(BaseModel):
    workbook: str = DEFAULT_WORKBOOK


class WorkbookRequest(BaseModel):
    path: str
    kind: str = "workbook"  # "workbook" | "folder"


class MoveRequest(BaseModel):
    src: str
    dst: str


def _serialize(tagged: list[TaggedBusiness]) -> list[dict]:
    return [{**t.business.model_dump(), "tag": t.tag} for t in tagged]


def _counts(tagged: list[TaggedBusiness]) -> dict[str, int]:
    counts = {"new": 0, "updated": 0, "existing": 0, "review": 0}
    for t in tagged:
        counts[t.tag] = counts.get(t.tag, 0) + 1
    return counts


async def _run_search(
    business_type: str,
    location: str,
    quantity: int | None,
    radius_km: int | None = None,
    workbook: str = DEFAULT_WORKBOOK,
) -> dict:
    global _buffer
    command = Command(
        action="search",
        business_type=business_type,
        location=location,
        quantity=quantity,
        radius_km=radius_km,
    )
    try:
        _buffer = await research(command, _store, workbook)
    except RuntimeError as exc:
        # Provider misconfiguration or an unreachable upstream. The message is
        # written to be actionable, so pass it through rather than masking it.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "action": "search",
        "count": len(_buffer),
        "summary": _counts(_buffer),
        "notice": _shortfall_notice(len(_buffer), quantity),
        "workbook": workbook,
        "businesses": _serialize(_buffer),
    }


def _shortfall_notice(found: int, requested: int | None) -> str | None:
    """Explain a short result set rather than letting it look like a failure."""
    if not requested or found >= requested:
        return None

    provider = os.environ.get("SEARCH_PROVIDER", "osm")
    if provider == "google_places":
        return (
            f"You asked for {requested} but Google returns at most "
            f"{PLACES_MAX_RESULTS} per query. Run narrower searches by "
            f"neighbourhood and save each one - duplicates are collapsed "
            f"automatically, so the list keeps growing."
        )
    return (
        f"You asked for {requested}; OpenStreetMap had {found} in this area. "
        f"Try a wider radius, or switch to SEARCH_PROVIDER=google_places for "
        f"much better coverage."
    )


def _save_buffer(workbook: str = DEFAULT_WORKBOOK) -> dict[str, int]:
    global _buffer
    if not _buffer:
        return {"new": 0, "updated": 0, "existing": 0, "review": 0}

    target = wb.resolve_path(workbook, require_xlsx=True)
    tagged = _store.upsert_many([t.business for t in _buffer], workbook)
    excel.sync(_store.all(workbook), target)
    _buffer = []

    counts = {"new": 0, "updated": 0, "existing": 0, "review": 0}
    for _, tag in tagged:
        counts[tag] = counts.get(tag, 0) + 1
    log.info("saved: %s", counts)
    return counts


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "provider": os.environ.get("SEARCH_PROVIDER", "osm"),
        "gemini_key_configured": bool(
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        ),
        # Single source of truth: a second copy of the default drifted the moment
        # the real default changed, and /health then reported the wrong model.
        "model": os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL,
        "stored": len(_store.all()),
    }


@app.post("/command")
async def command_endpoint(body: CommandRequest) -> dict:
    try:
        command = parse_command(body.command)
    except MissingAPIKey as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RateLimited as exc:
        # Routine on a free key, not a server fault. 500 would tell the user the
        # app is broken when all they need to do is wait a minute.
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ParserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log.info("command %r -> %s", body.command, command.model_dump(exclude_none=True))

    if command.action == "search":
        return await _run_search(
            command.business_type or "",
            command.location or "",
            command.quantity,
            command.radius_km,
            body.workbook,
        )

    if command.action == "store":
        return {"action": "store", **_save_buffer(body.workbook)}

    if command.action == "deduplicate":
        removed = _store.dedupe_existing(body.workbook)
        excel.sync(_store.all(body.workbook), wb.resolve_path(body.workbook, require_xlsx=True))
        return {"action": "deduplicate", "removed": removed}

    if command.action == "filter":
        if not command.filter_kind:
            raise HTTPException(400, "A filter command needs a filter kind.")
        rows = _store.filter(command.filter_kind, body.workbook)
        return {
            "action": "filter",
            "filter": command.filter_kind,
            "count": len(rows),
            "businesses": [b.model_dump() for b in rows],
        }

    if command.action == "export":
        target = wb.resolve_path(body.workbook, require_xlsx=True)
        result = excel.sync(_store.all(body.workbook), target)
        return {"action": "export", "path": body.workbook, **result}

    raise HTTPException(
        400,
        "I could not map that to an action. Try: 'Find dental clinics in Chromepet', "
        "'add these to Excel', 'remove duplicates', or 'show clinics without websites'.",
    )


@app.post("/search")
async def search_endpoint(body: SearchRequest) -> dict:
    return await _run_search(
        body.business_type, body.location, body.quantity, body.radius_km, body.workbook
    )


@app.post("/businesses/save")
def save_endpoint(body: SaveRequest) -> dict[str, int]:
    try:
        return _save_buffer(body.workbook)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/businesses/deduplicate")
def deduplicate_endpoint(body: SaveRequest) -> dict:
    removed = _store.dedupe_existing(body.workbook)
    excel.sync(_store.all(body.workbook), wb.resolve_path(body.workbook, require_xlsx=True))
    return {"removed": removed, "remaining": len(_store.all(body.workbook))}


@app.get("/businesses")
def list_endpoint(filter: str | None = None, workbook: str | None = None) -> dict:
    try:
        rows = _store.filter(filter, workbook) if filter else _store.all(workbook)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"count": len(rows), "businesses": [b.model_dump() for b in rows]}


# --- workbook editor -------------------------------------------------------


def _resync(workbook: str) -> None:
    """Re-render the sheet from the store after any mutation.

    The store is the source of truth; skipping this leaves the .xlsx silently
    lagging until the next save.
    """
    excel.sync(_store.all(workbook), wb.resolve_path(workbook, require_xlsx=True))


@app.post("/businesses")
def add_row(body: RowRequest) -> dict:
    try:
        row = _store.create_blank(body.workbook)
        _resync(body.workbook)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"business": row.model_dump()}


@app.patch("/businesses/{row_id}")
def edit_row(row_id: str, body: EditRequest) -> dict:
    try:
        row = _store.update_fields(row_id, body.changes)
        _resync(body.workbook)
    except (DuplicateId, InvalidField, wb.InvalidPath) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"business": row.model_dump()}


@app.delete("/businesses/{row_id}")
def remove_row(row_id: str, body: RowRequest) -> dict:
    removed = _store.delete_row(row_id)
    if removed:
        try:
            _resync(body.workbook)
        except wb.InvalidPath as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"deleted": removed}


@app.post("/businesses/{row_id}/summarize")
async def summarize_row(row_id: str, body: RowRequest) -> dict:
    """Summarise one business from its own website. One Gemini call per click."""
    business = {r.id: r for r in _store.all()}.get(row_id)
    if business is None:
        raise HTTPException(400, f"no such row: {row_id!r}")
    if not business.website:
        raise HTTPException(
            400,
            f"{business.business_name or 'This row'} has no website to read, "
            "so there is nothing to summarise.",
        )

    fetched = await fetch_page_text(business)
    if fetched is None:
        raise HTTPException(
            400,
            f"Could not read {business.website} - it may be unreachable or "
            "disallowed by robots.txt.",
        )
    text, url = fetched

    try:
        result = summarize(text, business.business_name)
    except MissingAPIKey as exc:
        raise HTTPException(400, str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(429, str(exc)) from exc
    except ParserError as exc:
        raise HTTPException(502, str(exc)) from exc

    if result is None:
        # Correct outcome, not a failure: the pages did not say enough.
        return {"business": business.model_dump(), "summarized": False}

    updated = _store.update_fields(
        row_id, {"short_info": result, "short_info_source": "AI summary"}
    )
    updated = _store.update_fields(
        row_id, {"sources": {**updated.sources, "short_info": url}}
    )
    _resync(body.workbook)
    return {"business": updated.model_dump(), "summarized": True}


@app.post("/workbooks/open")
def open_workbook(body: RowRequest) -> dict:
    """Adopt any hand-typed rows, then return the workbook's saved rows."""
    try:
        target = wb.resolve_path(body.workbook, require_xlsx=True)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc

    adopted = _store.adopt(body.workbook, target) if target.exists() else 0
    rows = _store.all(body.workbook)
    return {
        "workbook": body.workbook,
        "adopted": adopted,
        "count": len(rows),
        "businesses": [b.model_dump() for b in rows],
    }


# --- workbook management ---------------------------------------------------


@app.get("/workbooks")
def workbooks_tree() -> dict:
    return wb.tree()


@app.post("/workbooks")
def workbooks_create(body: WorkbookRequest) -> dict:
    try:
        path = (
            wb.create_folder(body.path)
            if body.kind == "folder"
            else wb.create_workbook(body.path)
        )
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": path, "kind": body.kind}


@app.patch("/workbooks")
def workbooks_move(body: MoveRequest) -> dict:
    try:
        path = wb.rename_or_move(body.src, body.dst)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    # Rows follow the file, or the renamed workbook renders empty.
    moved = _store.move_rows(body.src, path)
    if path.endswith(".xlsx"):
        excel.sync(_store.all(path), wb.resolve_path(path, require_xlsx=True))
    return {"path": path, "rows_moved": moved}


@app.delete("/workbooks")
def workbooks_delete(body: WorkbookRequest) -> dict:
    try:
        if body.kind == "folder":
            wb.delete_folder(body.path)
            return {"deleted": body.path, "kind": "folder"}
        trashed = wb.delete_workbook(body.path)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "deleted": body.path,
        "kind": "workbook",
        "trashed_as": trashed,
        "rows_removed": _store.delete_rows(body.path),
    }


@app.get("/workbooks/download")
def workbooks_download(path: str):
    try:
        target = wb.resolve_path(path, must_exist=True, require_xlsx=True)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        target,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=target.name,
    )


# Mounted last so it cannot shadow the API routes above.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
