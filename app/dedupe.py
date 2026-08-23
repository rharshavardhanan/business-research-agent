"""Duplicate detection: a strict tier hierarchy with confidence bands.

Policy, in one line: HIGH merges automatically, MEDIUM is flagged for a human,
LOW is left alone, and nothing is ever deleted below HIGH confidence.

The bands exist because "ABC Dental Clinic" and "ABC Dental Care" look nearly
identical to a string matcher but are frequently two different businesses on the
same road. Auto-merging them silently destroys a lead.
"""

from datetime import date
from typing import Literal

from rapidfuzz import fuzz

from app.models import Business
from app.normalize import normalize_address, normalize_name, normalize_phone, normalize_url

Confidence = Literal["high", "medium", "low", "none"]

FUZZY_HIGH = 88  # at/above this, with a matching area -> MEDIUM (review)
FUZZY_LOW = 75  # at/above this -> LOW (kept separate, unflagged)

# Fields never carried over by a merge.
_MERGE_SKIP = {"id", "date_found", "last_updated", "business_name", "sources"}

# Observations of a changing world rather than facts about identity: a clinic
# that gained reviews or closed permanently should show the new value on
# re-search. Every other field keeps the fill-gaps-only rule, so a phone number
# the user corrected is never clobbered.
REFRESHABLE = {"rating", "review_count", "business_status"}


def find_match(
    candidate: Business, existing: list[Business]
) -> tuple[Business | None, Confidence, str]:
    """Return (matched record, confidence, name of the tier that fired).

    Tiers are checked in order and the first hit wins, so a provider ID match
    never gets downgraded by a weaker signal further down the list.
    """
    cand_phone = normalize_phone(candidate.phone)
    cand_url = normalize_url(candidate.website)
    cand_name = normalize_name(candidate.business_name)
    cand_addr = normalize_address(candidate.address)
    cand_area = normalize_address(candidate.area)

    best_fuzzy: tuple[Business, int] | None = None

    for other in existing:
        # Tier 1 - the provider says they are the same record.
        if (
            candidate.source
            and candidate.source_id
            and candidate.source == other.source
            and candidate.source_id == other.source_id
        ):
            return other, "high", "source_id"

        # Tier 2 - same phone. The strongest evidence we can derive ourselves.
        if cand_phone and cand_phone == normalize_phone(other.phone):
            return other, "high", "phone"

        # Tier 3 - same website.
        if cand_url and cand_url == normalize_url(other.website):
            return other, "high", "website"

        # Tier 4 - same name at the same address.
        other_addr = normalize_address(other.address)
        if cand_addr and other_addr and cand_addr == other_addr:
            if cand_name and cand_name == normalize_name(other.business_name):
                return other, "high", "name_address"

        # Tiers 5/6 - remember the closest name, decide after the full scan so a
        # later exact match is never pre-empted by an earlier fuzzy one.
        score = int(fuzz.token_sort_ratio(cand_name, normalize_name(other.business_name)))
        if best_fuzzy is None or score > best_fuzzy[1]:
            best_fuzzy = (other, score)

    if best_fuzzy:
        other, score = best_fuzzy
        if score >= FUZZY_HIGH and cand_area == normalize_address(other.area):
            return other, "medium", "fuzzy_name_area"
        if score >= FUZZY_LOW:
            return other, "low", "fuzzy_name"

    return None, "none", "none"


def merge(existing: Business, new: Business) -> tuple[Business, bool]:
    """Fill gaps in `existing` from `new`. Never overwrite existing data.

    Returns the merged record and whether anything actually changed, so callers
    can distinguish an EXISTING result from an UPDATED one without diffing.
    """
    updates: dict[str, object] = {}

    for field in Business.model_fields:
        if field in _MERGE_SKIP:
            continue
        current = getattr(existing, field)
        incoming = getattr(new, field)

        if field in REFRESHABLE:
            if incoming not in (None, "") and incoming != current:
                updates[field] = incoming
            continue

        if incoming in (None, "") or current not in (None, ""):
            continue
        updates[field] = incoming

    # Provenance is additive; the original attribution wins on a key collision.
    merged_sources = {**new.sources, **existing.sources}
    if merged_sources != existing.sources:
        updates["sources"] = merged_sources

    if not updates:
        return existing, False

    updates["last_updated"] = date.today().isoformat()
    return existing.model_copy(update=updates), True
