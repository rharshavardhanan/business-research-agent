"""The pipeline: search -> normalize -> tag against the store.

This function deliberately WRITES NOTHING. It returns results tagged against
what is already stored so the user can review them before committing. Persisting
is `Store.upsert_many`, called only when the user asks to save. That review gap
is what keeps bad rows out of the lead sheet.
"""

import logging
from dataclasses import dataclass

from app.dedupe import find_match, merge
from app.models import Business, Command, MatchTag
from app.normalize import normalize_phone, normalize_url
from app.providers import get_provider

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 25


@dataclass
class TaggedBusiness:
    business: Business
    tag: MatchTag
    matched_id: str | None = None


async def research(
    command: Command, store, workbook: str | None = None
) -> list[TaggedBusiness]:
    """Run a search command end to end, tagging results against one workbook.

    Tags answer "is this already in THIS list", so a clinic saved to another
    workbook still shows as NEW here.
    """
    provider = get_provider()
    limit = command.quantity or DEFAULT_LIMIT

    raw = provider.search(
        query=command.business_type or "",
        location=command.location or "",
        limit=limit,
        radius_m=command.radius_km * 1000 if command.radius_km else None,
    )
    log.info(
        "search: %r / %r (radius=%s) via %s -> %d found",
        command.business_type,
        command.location,
        f"{command.radius_km}km" if command.radius_km else "default",
        provider.name,
        len(raw),
    )

    # Enrichment is no longer done here. The browser calls /enrich once per
    # business, so results appear immediately and no single request has to fit
    # a 5-page crawl inside a serverless function timeout.
    enriched = [_normalize(b) for b in raw]

    known = store.all(workbook)
    batch: list[Business] = []
    results: list[TaggedBusiness] = []

    for business in enriched:
        # Check the batch first so one search returning the same clinic twice
        # does not produce two rows.
        match, confidence, _ = find_match(business, batch)
        if confidence == "high":
            continue

        match, confidence, _ = find_match(business, known)

        if confidence == "high" and match is not None:
            _, changed = merge(match, business)
            tag: MatchTag = "updated" if changed else "existing"
            results.append(TaggedBusiness(business, tag, match.id))
        elif confidence == "medium" and match is not None:
            results.append(TaggedBusiness(business, "review", match.id))
        else:
            results.append(TaggedBusiness(business, "new", None))

        batch.append(business)

    counts: dict[str, int] = {}
    for tagged in results:
        counts[tagged.tag] = counts.get(tagged.tag, 0) + 1
    log.info("tagged: %s", counts or "none")

    return results


def _normalize(business: Business) -> Business:
    """Canonicalise the fields dedupe compares. Display values are untouched."""
    updates: dict[str, object] = {}

    phone = normalize_phone(business.phone)
    if phone != business.phone:
        updates["phone"] = phone

    alternate = normalize_phone(business.alternate_phone)
    if alternate != business.alternate_phone:
        updates["alternate_phone"] = alternate

    # Keep the website human-clickable; normalize_url is for comparison only.
    if business.website and not business.website.startswith(("http://", "https://")):
        updates["website"] = "https://" + business.website
    if business.website and not normalize_url(business.website):
        updates["website"] = None

    return business.model_copy(update=updates) if updates else business
