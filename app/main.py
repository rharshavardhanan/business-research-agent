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
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import asyncio

import httpx

from app import excel
from app import workbooks as wb
from app.enrichment import PER_BUSINESS_BUDGET, USER_AGENT, enrich
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

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get(
    "DATABASE_URL_TEST", "postgresql://postgres:test@localhost:55432/bra"
)

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

_store = Store(DATABASE_URL)
_store.init_schema()


class CommandRequest(BaseModel):
    command: str
    workbook: str = DEFAULT_WORKBOOK
    businesses: list[dict] = []


class SearchRequest(BaseModel):
    business_type: str
    location: str
    quantity: int | None = None
    radius_km: int | None = None
    workbook: str = DEFAULT_WORKBOOK


class SaveRequest(BaseModel):
    workbook: str = DEFAULT_WORKBOOK
    # The browser holds the result set. Serverless has no shared process memory,
    # so consecutive requests land on different instances and a server-side
    # buffer would simply be gone by the time Save is pressed.
    businesses: list[dict] = []


class EnrichRequest(BaseModel):
    business: dict


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
    command = Command(
        action="search",
        business_type=business_type,
        location=location,
        quantity=quantity,
        radius_km=radius_km,
    )
    try:
        results = await research(command, _store, workbook)
    except RuntimeError as exc:
        # Provider misconfiguration or an unreachable upstream. The message is
        # written to be actionable, so pass it through rather than masking it.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "action": "search",
        "count": len(results),
        "summary": _counts(results),
        "notice": _shortfall_notice(len(results), quantity),
        "workbook": workbook,
        "businesses": _serialize(results),
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


def _save(workbook: str, rows: list[dict]) -> dict[str, int]:
    """Persist rows the client sends back. Nothing is held between requests."""
    counts = {"new": 0, "updated": 0, "existing": 0, "review": 0}
    if not rows:
        return counts

    incoming = [Business(**{k: v for k, v in r.items() if k != "tag"}) for r in rows]
    tagged = _store.upsert_many(incoming, workbook)
    for _, tag in tagged:
        counts[tag] = counts.get(tag, 0) + 1
    log.info("saved to %s: %s", workbook, counts)
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
        return {"action": "store", **_save(body.workbook, body.businesses)}

    if command.action == "deduplicate":
        return {"action": "deduplicate", "removed": _store.dedupe_existing(body.workbook)}

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
        # Nothing to write: the workbook is generated at download time.
        rows = _store.all(body.workbook)
        return {"action": "export", "path": body.workbook, "written": len(rows)}

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
        return _save(wb.validate(body.workbook, require_xlsx=True), body.businesses)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/businesses/deduplicate")
def deduplicate_endpoint(body: SaveRequest) -> dict:
    removed = _store.dedupe_existing(body.workbook)
    return {"removed": removed, "remaining": len(_store.all(body.workbook))}


@app.get("/businesses")
def list_endpoint(filter: str | None = None, workbook: str | None = None) -> dict:
    try:
        rows = _store.filter(filter, workbook) if filter else _store.all(workbook)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"count": len(rows), "businesses": [b.model_dump() for b in rows]}


# --- workbook editor -------------------------------------------------------


@app.post("/businesses")
def add_row(body: RowRequest) -> dict:
    try:
        row = _store.create_blank(body.workbook)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"business": row.model_dump()}


@app.patch("/businesses/{row_id}")
def edit_row(row_id: str, body: EditRequest) -> dict:
    try:
        row = _store.update_fields(row_id, body.changes)
    except (DuplicateId, InvalidField, wb.InvalidPath) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"business": row.model_dump()}


@app.delete("/businesses/{row_id}")
def remove_row(row_id: str, body: RowRequest) -> dict:
    return {"deleted": _store.delete_row(row_id)}


@app.post("/enrich")
async def enrich_one(body: EnrichRequest) -> dict:
    """Enrich a single business from its website.

    One business per request, so every call is short enough for a serverless
    function timeout and a slow site delays one row instead of the whole search.
    """
    business = Business(**{k: v for k, v in body.business.items() if k != "tag"})
    if not business.website:
        return {"business": business.model_dump(), "enriched": False}

    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as http:
            enriched = await asyncio.wait_for(
                enrich(business, http), timeout=PER_BUSINESS_BUDGET
            )
    except (TimeoutError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        # Enrichment is a nice-to-have. Never fail a row over someone else's
        # unreachable website.
        log.info("enrichment skipped for %s: %s", business.website, exc)
        return {"business": business.model_dump(), "enriched": False}

    return {"business": enriched.model_dump(), "enriched": enriched != business}


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
    return {"business": updated.model_dump(), "summarized": True}


@app.post("/workbooks/open")
def open_workbook(body: RowRequest) -> dict:
    """Return a workbook's saved rows.

    There is no adoption step any more: the .xlsx is generated at download and
    never stored, so there is no file anyone could have typed rows into.
    """
    try:
        target = wb.validate(body.workbook, require_xlsx=True)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    rows = _store.all(target)
    return {
        "workbook": target,
        "adopted": 0,
        "count": len(rows),
        "businesses": [b.model_dump() for b in rows],
    }

# --- workbook management ---------------------------------------------------


@app.get("/workbooks")
def workbooks_tree() -> dict:
    return wb.tree(_store)


@app.post("/workbooks")
def workbooks_create(body: WorkbookRequest) -> dict:
    try:
        path = (
            wb.create_folder(_store, body.path)
            if body.kind == "folder"
            else wb.create_workbook(_store, body.path)
        )
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": path, "kind": body.kind}


@app.patch("/workbooks")
def workbooks_move(body: MoveRequest) -> dict:
    try:
        path = wb.rename_or_move(_store, body.src, body.dst)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    # ON UPDATE CASCADE already carried the rows across.
    return {"path": path, "rows_moved": len(_store.all(path))}


@app.delete("/workbooks")
def workbooks_delete(body: WorkbookRequest) -> dict:
    try:
        if body.kind == "folder":
            wb.delete_folder(_store, body.path)
            return {"deleted": body.path, "kind": "folder"}
        removed = wb.delete_workbook(_store, body.path)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    # Soft delete: the rows are hidden, not destroyed.
    return {"deleted": body.path, "kind": "workbook", "rows_removed": removed}


@app.get("/workbooks/download")
def workbooks_download(path: str):
    """Build the workbook in memory and stream it. Nothing is stored."""
    try:
        target = wb.validate(path, require_xlsx=True)
    except wb.InvalidPath as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = excel.build(_store.all(target))
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition":
                f'attachment; filename="{target.rsplit("/", 1)[-1]}"'
        },
    )


# Mounted last so it cannot shadow the API routes above.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
