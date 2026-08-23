"""Business search providers.

Two real implementations behind one interface, chosen by SEARCH_PROVIDER:

  osm            OpenStreetMap via Nominatim + Overpass. Free, no key, no
                 billing. Coverage is patchy - many small clinics are absent,
                 and of those present most lack phone and website tags.
  google_places  Google Places API (New). Far richer, needs a key and a
                 billing-enabled GCP project.

Neither ever scrapes Google Maps HTML.
"""

import os
import re
import time
from abc import ABC, abstractmethod

import httpx

from app.models import Business
from app.normalize import normalize_phone

USER_AGENT = "business-research-agent/0.1 (local lead research)"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Public Overpass instances are shared and frequently return 429/504 under load.
# Mirrors are tried in order; each gets the full retry budget.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_ATTEMPTS = 3

# Google Places Text Search returns at most 20 per page and 3 pages per query,
# so 60 is a hard per-query ceiling regardless of the limit asked for.
PLACES_PAGE_SIZE = 20
PLACES_MAX_RESULTS = 60
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

DEFAULT_RADIUS_M = 5000

# Matches a distance anywhere in the string, with or without a leading
# preposition, followed by an optional trailing connector. Deliberately broad:
# an unrecognised phrasing used to fall through to the default radius AND leak
# into the geocode query, where Nominatim returns None - a silent empty search.
_RADIUS_RE = re.compile(
    r"(?:within|near|around|inside)?\s*"
    r"(\d+(?:\.\d+)?)\s*(km|kms|kilometers?|kilometres?|m|meters?|metres?)\b"
    r"\s*(?:radius|surrounding|around|of|from|near)?",
    re.I,
)
_FILLER_RE = re.compile(r"^\s*(?:of|from|near|around|in|within|the)\b\s*", re.I)

# Nominatim's usage policy caps clients at one request per second.
_NOMINATIM_MIN_INTERVAL = 1.0
_last_nominatim_call = 0.0

# Business type -> OSM tag filters. Anything unmatched falls back to a
# case-insensitive name regex, which is weak but better than returning nothing.
_TAG_MAP: list[tuple[re.Pattern, list[tuple[str, str]]]] = [
    (re.compile(r"dent", re.I), [("amenity", "dentist"), ("healthcare", "dentist")]),
    (re.compile(r"pharmac|chemist|medical shop", re.I), [("amenity", "pharmacy")]),
    (re.compile(r"hospital", re.I), [("amenity", "hospital")]),
    (re.compile(r"restaurant|dining", re.I), [("amenity", "restaurant")]),
    (re.compile(r"cafe|coffee", re.I), [("amenity", "cafe")]),
    (re.compile(r"gym|fitness", re.I), [("leisure", "fitness_centre")]),
    (re.compile(r"salon|parlour|parlor|spa", re.I), [("shop", "beauty"), ("shop", "hairdresser")]),
    (re.compile(r"school", re.I), [("amenity", "school")]),
    (re.compile(r"vet", re.I), [("amenity", "veterinary")]),
    (
        re.compile(r"doctor|clinic|physician", re.I),
        [("amenity", "doctors"), ("amenity", "clinic"), ("healthcare", "centre")],
    ),
]


def geocode(place: str) -> tuple[float, float] | None:
    """Place name -> (lat, lon) via Nominatim, or None.

    Module-level because both providers need it: OSM to centre its Overpass
    query, Google to build a structured locationBias circle.
    """
    global _last_nominatim_call
    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < _NOMINATIM_MIN_INTERVAL:
        time.sleep(_NOMINATIM_MIN_INTERVAL - elapsed)
    _last_nominatim_call = time.monotonic()

    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(NOMINATIM_URL, params={"format": "json", "limit": 1, "q": place})
        resp.raise_for_status()
        results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


class BusinessSearchProvider(ABC):
    """The seam that lets the data source be swapped without touching anything else."""

    name: str

    @abstractmethod
    def search(
        self, query: str, location: str, limit: int, radius_m: int | None = None
    ) -> list[Business]:
        """Return up to `limit` businesses matching `query` near `location`."""


class OSMProvider(BusinessSearchProvider):
    name = "osm"

    def search(
        self, query: str, location: str, limit: int, radius_m: int | None = None
    ) -> list[Business]:
        radius, place = self._resolve_radius(location, radius_m)
        coords = self._geocode(place)
        if coords is None:
            return []
        lat, lon = coords

        overpass = self._build_query(query, lat, lon, radius)
        elements = self._run_overpass(overpass)

        out: list[Business] = []
        for el in elements:
            business = self._to_business(el)
            if business is not None:
                out.append(business)
            if len(out) >= limit:
                break
        return out

    # -- helpers -------------------------------------------------------

    def _run_overpass(self, query: str) -> list[dict]:
        """POST an Overpass query, retrying transient failures across mirrors.

        Free Overpass instances routinely answer 429 or 504 when busy. Without
        this the user sees a raw gateway error for what is a retryable hiccup.
        """
        last_error: Exception | None = None
        with httpx.Client(timeout=90.0, headers={"User-Agent": USER_AGENT}) as client:
            for url in OVERPASS_URLS:
                for attempt in range(OVERPASS_ATTEMPTS):
                    try:
                        resp = client.post(url, data={"data": query})
                        if resp.status_code in (429, 502, 503, 504):
                            raise httpx.HTTPStatusError(
                                f"{resp.status_code} from {url}",
                                request=resp.request,
                                response=resp,
                            )
                        resp.raise_for_status()
                        return resp.json().get("elements", [])
                    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                        last_error = exc
                        if attempt < OVERPASS_ATTEMPTS - 1:
                            time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(
            "Overpass is unavailable (tried "
            f"{len(OVERPASS_URLS)} mirrors x {OVERPASS_ATTEMPTS} attempts). "
            "This is usually transient load on the free public API - retry shortly, "
            f"or narrow the search area. Last error: {last_error}"
        )

    def _parse_radius(self, location: str) -> tuple[int | None, str]:
        """Return (radius in metres or None, place with the distance removed).

        Returns None rather than a default so callers can tell "no radius stated"
        apart from "5 km stated" - the previous version conflated them, which is
        how a requested 20 km silently became 5 km.
        """
        m = _RADIUS_RE.search(location)
        if not m:
            return None, location.strip()

        value, unit = float(m.group(1)), m.group(2).lower()
        radius = int(value * 1000) if unit.startswith("k") else int(value)

        place = (location[: m.start()] + " " + location[m.end():]).strip()
        # Strip filler the excision leaves behind ("of kodambakkam" -> "kodambakkam").
        while (stripped := _FILLER_RE.sub("", place)) != place:
            place = stripped
        return radius, place.strip(" ,")

    def _resolve_radius(self, location: str, radius_m: int | None) -> tuple[int, str]:
        """An explicit argument wins, but the string is cleaned either way."""
        parsed, place = self._parse_radius(location)
        return (radius_m or parsed or DEFAULT_RADIUS_M), place

    def _geocode(self, place: str) -> tuple[float, float] | None:
        return geocode(place)

    def _build_query(self, query: str, lat: float, lon: float, radius: int) -> str:
        around = f"(around:{radius},{lat},{lon})"
        clauses: list[str] = []
        for pattern, tags in _TAG_MAP:
            if pattern.search(query):
                for key, value in tags:
                    clauses.append(f'node["{key}"="{value}"]{around};')
                    clauses.append(f'way["{key}"="{value}"]{around};')
                break
        if not clauses:
            escaped = re.escape(query)
            clauses = [
                f'node["name"~"{escaped}",i]{around};',
                f'way["name"~"{escaped}",i]{around};',
            ]
        return "[out:json][timeout:60];(" + "".join(clauses) + ");out center tags;"

    def _to_business(self, el: dict, _unused: str | None = None) -> Business | None:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            # No name means no lead. Never fabricate one from the tags.
            return None

        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")

        address = ", ".join(
            part
            for part in (
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:suburb"),
                tags.get("addr:city"),
                tags.get("addr:postcode"),
            )
            if part
        )

        maps_url = (
            f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            if lat is not None and lon is not None
            else None
        )

        return Business(
            business_name=name,
            category=tags.get("amenity") or tags.get("healthcare") or tags.get("shop"),
            phone=normalize_phone(tags.get("phone") or tags.get("contact:phone")),
            email=tags.get("email") or tags.get("contact:email"),
            website=tags.get("website") or tags.get("contact:website"),
            address=address or None,
            # From the business's own tags, never the search string: a Mylapore
            # clinic must not be labelled with wherever the user happened to search.
            area=(
                tags.get("addr:suburb")
                or tags.get("addr:neighbourhood")
                or tags.get("addr:city")
            ),
            city=tags.get("addr:city"),
            postal_code=tags.get("addr:postcode"),
            google_maps_url=maps_url,
            latitude=float(lat) if lat is not None else None,
            longitude=float(lon) if lon is not None else None,
            source="osm",
            source_id=f"{el.get('type')}/{el.get('id')}",
        )


class GooglePlacesProvider(BusinessSearchProvider):
    name = "google_places"

    FIELD_MASK = ",".join(
        [
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.nationalPhoneNumber",
            "places.internationalPhoneNumber",
            "places.websiteUri",
            "places.location",
            "places.googleMapsUri",
            "places.primaryType",
            "places.rating",
            "places.userRatingCount",
            "places.businessStatus",
            # Typed components, not the display string: an Indian formatted
            # address ends ", India", so slicing from the end yields the state.
            "places.addressComponents",
            "nextPageToken",
        ]
    )

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(
        self, query: str, location: str, limit: int, radius_m: int | None = None
    ) -> list[Business]:
        # NEVER put the radius in textQuery. Google treats it as literal search
        # terms: "dental clinic in perungalathur" returns 60 results,
        # "dental clinic in perungalathur (within 25 km)" returns 17.
        location_bias = None
        if radius_m:
            coords = geocode(location)
            if coords:
                location_bias = {
                    "circle": {
                        "center": {"latitude": coords[0], "longitude": coords[1]},
                        "radius": float(radius_m),
                    }
                }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": self.FIELD_MASK,
        }
        out: list[Business] = []
        page_token: str | None = None

        with httpx.Client(timeout=30.0, headers=headers) as client:
            while len(out) < limit:
                body: dict = {
                    "textQuery": f"{query} in {location}",
                    "maxResultCount": min(limit - len(out), PLACES_PAGE_SIZE),
                }
                if location_bias:
                    body["locationBias"] = location_bias
                if page_token:
                    body["pageToken"] = page_token
                resp = client.post(PLACES_URL, json=body)
                if resp.status_code >= 400:
                    raise RuntimeError(_google_error(resp))
                data = resp.json()

                for place in data.get("places", []):
                    business = self._to_business(place, location)
                    if business is not None:
                        out.append(business)

                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return out[:limit]

    def _to_business(self, place: dict, _unused: str | None = None) -> Business | None:
        name = (place.get("displayName") or {}).get("text")
        if not name:
            return None

        address = place.get("formattedAddress")
        parts = _address_parts(place.get("addressComponents") or [])

        postcode = parts.get("postal_code")
        if not postcode and address:
            m = re.search(r"\b(\d{6})\b", address)
            postcode = m.group(1) if m else None

        loc = place.get("location") or {}
        raw_phone = place.get("nationalPhoneNumber") or place.get(
            "internationalPhoneNumber"
        )

        return Business(
            business_name=name,
            category=place.get("primaryType"),
            phone=normalize_phone(raw_phone),
            website=place.get("websiteUri"),
            address=address,
            area=parts.get("area"),
            city=parts.get("city"),
            state=parts.get("state"),
            postal_code=postcode,
            google_maps_url=place.get("googleMapsUri"),
            latitude=loc.get("latitude"),
            longitude=loc.get("longitude"),
            rating=place.get("rating"),
            review_count=place.get("userRatingCount"),
            business_status=place.get("businessStatus"),
            source="google_places",
            source_id=place.get("id"),
        )


def get_provider(name: str | None = None) -> BusinessSearchProvider:
    choice = (name or os.environ.get("SEARCH_PROVIDER") or "osm").strip().lower()

    if choice == "osm":
        return OSMProvider()

    if choice == "google_places":
        key = os.environ.get("GOOGLE_PLACES_API_KEY")
        if not key:
            raise RuntimeError(
                "SEARCH_PROVIDER=google_places but GOOGLE_PLACES_API_KEY is not set. "
                "Add it to .env, or set SEARCH_PROVIDER=osm to use the free provider."
            )
        return GooglePlacesProvider(api_key=key)

    raise RuntimeError(f"Unknown SEARCH_PROVIDER {choice!r}. Expected 'osm' or 'google_places'.")


# Google component type -> the Business field it fills. First match wins, so
# sublocality (Kodambakkam) beats locality (Chennai) for `area`.
_COMPONENT_MAP: list[tuple[str, str]] = [
    ("sublocality_level_1", "area"),
    ("sublocality", "area"),
    ("neighborhood", "area"),
    ("locality", "city"),
    ("administrative_area_level_2", "city"),
    ("administrative_area_level_1", "state"),
    ("postal_code", "postal_code"),
]


def _address_parts(components: list[dict]) -> dict[str, str]:
    """Pull typed locality fields out of Places addressComponents.

    Returns only what Google actually typed - a missing field stays missing
    rather than being guessed from the display string.
    """
    out: dict[str, str] = {}
    for type_name, field in _COMPONENT_MAP:
        if field in out:
            continue
        for component in components:
            if type_name in (component.get("types") or []):
                value = component.get("longText") or component.get("shortText")
                if value:
                    out[field] = value
                break
    return out


def _google_error(resp) -> str:
    """Turn a Places API error response into a message that names the fix.

    `raise_for_status()` discards the body, but the body is where Google puts
    the actionable part - most often a SERVICE_DISABLED notice with the exact
    activation URL. A bare "403 Forbidden" sends the user hunting.
    """
    try:
        err = resp.json().get("error", {})
        status = err.get("status") or resp.status_code
        message = err.get("message") or "no detail returned"
    except Exception:  # noqa: BLE001 - a non-JSON body is still worth showing
        return f"Google Places returned HTTP {resp.status_code}: {resp.text[:300]}"
    return f"Google Places error ({status}): {message}"
