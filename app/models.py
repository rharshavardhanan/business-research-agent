"""Pydantic models shared across every module.

`Business` is the single currency of this application: providers produce it,
enrichment decorates it, dedupe compares it, the store persists it, and Excel
renders it. Only `business_name` is required - every other field is `None` when
it could not be sourced. Nothing here is ever invented.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MatchTag = Literal["new", "existing", "updated", "review"]

Action = Literal["search", "store", "deduplicate", "filter", "export", "unknown"]

FilterKind = Literal["without_website", "with_phone", "without_doctor"]


class Business(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str | None = None
    business_name: str
    category: str | None = None
    doctor_name: str | None = None
    phone: str | None = None
    alternate_phone: str | None = None
    email: str | None = None
    address: str | None = None
    area: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    website: str | None = None
    google_maps_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Quality signals. Google supplies all three; OSM supplies none, so they
    # stay None rather than being invented as zero.
    rating: float | None = None
    review_count: int | None = None
    business_status: str | None = None
    source: str | None = None
    source_id: str | None = None
    # ISO date strings, not datetimes: these round-trip through SQLite TEXT and
    # Excel cells, and a str avoids two lossy conversions on every read.
    date_found: str | None = None
    last_updated: str | None = None
    status: str | None = None
    notes: str | None = None
    # A short description of the business. `short_info_source` records how it was
    # produced, so a marketing blurb is never mistaken for a sourced summary.
    short_info: str | None = None
    short_info_source: str | None = None  # Website | AI summary | Manual
    # Outreach state - the user's own observations, never touched by a search.
    # Blank is meaningful: "not called yet" is not "called, no answer".
    call_status: str | None = None
    will_speak_further: str | None = None
    meeting_date: str | None = None
    meeting_place: str | None = None
    follow_up: bool = False
    follow_up_date: str | None = None
    # Relative path of the workbook this row belongs to. Dedupe is scoped by it,
    # so the same business may legitimately exist in two different lead lists.
    workbook: str | None = None
    # Maps an enriched field name -> the URL it was read from. Replaces six
    # per-field `*_source` columns that would be null on almost every row.
    sources: dict[str, str] = Field(default_factory=dict)


class Command(BaseModel):
    """The structured form of a natural-language instruction."""

    action: Action
    business_type: str | None = None
    location: str | None = None
    quantity: int | None = None
    # Kilometres. Extracted by the LLM so the provider never does NLP on a
    # location string - a regex there silently defaulted a stated radius to 5 km.
    radius_km: int | None = None
    filter_kind: FilterKind | None = None
