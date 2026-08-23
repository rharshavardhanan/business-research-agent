import re

import pytest

from app.providers import GooglePlacesProvider, OSMProvider, get_provider


def test_osm_element_to_business_maps_tags():
    el = {
        "type": "node",
        "id": 1,
        "lat": 12.95,
        "lon": 80.14,
        "tags": {
            "name": "ABC Dental",
            "phone": "+91 98765 43210",
            "website": "https://abcdental.com",
            "addr:street": "GST Road",
            "addr:city": "Chennai",
            "addr:postcode": "600044",
        },
    }
    b = OSMProvider()._to_business(el, "Chromepet")
    assert b.business_name == "ABC Dental"
    assert b.phone == "+919876543210"
    assert b.source == "osm" and b.source_id == "node/1"
    assert (
        b.google_maps_url
        == "https://www.google.com/maps/search/?api=1&query=12.95,80.14"
    )
    assert b.area == "Chennai"  # from addr:city, not the search string


def test_osm_element_without_name_is_skipped():
    assert OSMProvider()._to_business({"type": "node", "id": 2, "tags": {}}, "X") is None


def test_osm_way_uses_center_coordinates():
    el = {
        "type": "way",
        "id": 7,
        "center": {"lat": 13.0, "lon": 80.2},
        "tags": {"name": "Way Clinic"},
    }
    b = OSMProvider()._to_business(el, "X")
    assert b.latitude == 13.0 and b.source_id == "way/7"


def test_google_place_to_business_maps_fields():
    place = {
        "id": "PLACE1",
        "displayName": {"text": "Smile Care"},
        "nationalPhoneNumber": "098765 43211",
        "websiteUri": "http://www.smilecare.in/",
        "formattedAddress": "1 GST Rd, Chromepet, Chennai 600044",
        "location": {"latitude": 12.9, "longitude": 80.1},
        "googleMapsUri": "https://maps.google.com/?cid=1",
    }
    b = GooglePlacesProvider(api_key="x")._to_business(place, "Chromepet")
    assert b.business_name == "Smile Care"
    assert b.phone == "+919876543211"
    assert b.source == "google_places" and b.source_id == "PLACE1"
    assert b.postal_code == "600044"


def test_get_provider_defaults_to_osm(monkeypatch):
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    assert isinstance(get_provider(), OSMProvider)


def test_google_provider_without_key_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_PLACES_API_KEY"):
        get_provider("google_places")


@pytest.mark.parametrize(
    "text,radius,place",
    [
        ("within 3 km of Tambaram", 3000, "Tambaram"),
        ("20 km surrounding of kodambakkam", 20000, "kodambakkam"),
        ("20km radius of Kodambakkam", 20000, "Kodambakkam"),
        ("within 20km around Kodambakkam", 20000, "Kodambakkam"),
        ("near Kodambakkam within 20 km", 20000, "Kodambakkam"),
        ("500 m from Guindy", 500, "Guindy"),
        ("Kodambakkam, Chennai", None, "Kodambakkam, Chennai"),
    ],
)
def test_parse_radius_handles_real_phrasings(text, radius, place):
    assert OSMProvider()._parse_radius(text) == (radius, place)


def test_distance_phrase_never_reaches_the_geocoder():
    """A leaked phrase makes Nominatim return None - a silent zero-result search."""
    for text in ["20 km surrounding of kodambakkam", "20km radius of Kodambakkam"]:
        _, place = OSMProvider()._parse_radius(text)
        assert not re.search(
            r"\d\s*k?m\b|within|surrounding|radius|around|near", place, re.I
        ), f"{place!r} still carries a distance phrase"


def test_explicit_radius_argument_wins_over_the_string():
    radius, place = OSMProvider()._resolve_radius("within 3 km of Tambaram", 20000)
    assert radius == 20000 and place == "Tambaram"


def test_resolve_radius_falls_back_to_default_when_nothing_stated():
    assert OSMProvider()._resolve_radius("Chromepet, Chennai", None) == (
        5000,
        "Chromepet, Chennai",
    )


def test_area_comes_from_the_business_not_the_search_string():
    el = {
        "type": "node", "id": 9, "lat": 13.08, "lon": 80.27,
        "tags": {"name": "Faraway Clinic", "addr:suburb": "Mylapore",
                 "addr:city": "Chennai"},
    }
    b = OSMProvider()._to_business(el, "Kodambakkam, Chennai")
    assert b.area == "Mylapore", "must not inherit where the user searched"


def test_area_is_none_when_the_business_has_no_locality_tag():
    el = {"type": "node", "id": 10, "lat": 13.08, "lon": 80.27,
          "tags": {"name": "Bare Clinic"}}
    b = OSMProvider()._to_business(el, "Kodambakkam, Chennai")
    assert b.area is None, "empty is honest; the search string is a lie"


def _component(long, *types):
    return {"longText": long, "shortText": long, "types": list(types)}


def test_google_locality_comes_from_typed_components_not_string_slicing():
    """An Indian address ends ", India", so index-from-the-end picks the state."""
    place = {
        "id": "P1",
        "displayName": {"text": "Kalaa Dental Care"},
        "formattedAddress": "12, Arcot Rd, Kodambakkam, Chennai, Tamil Nadu 600024, India",
        "addressComponents": [
            _component("Kodambakkam", "sublocality_level_1", "sublocality"),
            _component("Chennai", "locality"),
            _component("Tamil Nadu", "administrative_area_level_1"),
            _component("600024", "postal_code"),
            _component("India", "country"),
        ],
    }
    b = GooglePlacesProvider(api_key="k")._to_business(place, "SEARCH STRING")
    assert b.area == "Kodambakkam", "must be the neighbourhood, not the state"
    assert b.city == "Chennai"
    assert b.state == "Tamil Nadu"
    assert b.postal_code == "600024"


def test_google_area_is_none_when_components_are_absent():
    place = {"id": "P2", "displayName": {"text": "X"},
             "formattedAddress": "somewhere unparseable"}
    b = GooglePlacesProvider(api_key="k")._to_business(place, "Kodambakkam")
    assert b.area is None, "empty beats guessing, and never the search string"


def test_google_field_mask_requests_address_components():
    assert "places.addressComponents" in GooglePlacesProvider.FIELD_MASK


def test_overpass_retries_then_raises_actionable_error(monkeypatch):
    """A flaky free endpoint must not surface as a raw gateway error."""
    calls = []

    class FakeResponse:
        status_code = 504
        request = None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: FakeClient())
    monkeypatch.setattr("app.providers.time.sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="Overpass is unavailable"):
        OSMProvider()._run_overpass("[out:json];")

    expected = len(__import__("app.providers", fromlist=["x"]).OVERPASS_URLS) * 3
    assert len(calls) == expected, "must exhaust every mirror and attempt"


def test_google_surfaces_the_api_error_instead_of_a_bare_status(monkeypatch):
    """raise_for_status() discards Google's body, which holds the actual fix."""
    body = {
        "error": {
            "status": "PERMISSION_DENIED",
            "message": (
                "Places API (New) has not been used in project 123 before or it "
                "is disabled. Enable it by visiting https://console.developers."
                "google.com/apis/api/places.googleapis.com/overview?project=123"
            ),
        }
    }

    class FakeResponse:
        status_code = 403

        def json(self):
            return body

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: FakeClient())
    with pytest.raises(RuntimeError) as e:
        GooglePlacesProvider(api_key="k").search("dental clinic", "Chennai", 5)

    detail = str(e.value)
    assert "PERMISSION_DENIED" in detail
    assert "Enable it by visiting" in detail, "the activation URL is the whole point"


def test_google_reports_an_invalid_key_readably(monkeypatch):
    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error": {"status": "INVALID_ARGUMENT",
                              "message": "API key not valid."}}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: FakeClient())
    with pytest.raises(RuntimeError, match="API key not valid"):
        GooglePlacesProvider(api_key="bad").search("x", "y", 1)


class _CaptureClient:
    """Records every request body Google would receive."""

    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kw):
        _CaptureClient.sent.append(kw.get("json"))

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"places": [], "nextPageToken": None}

        return R()


def test_radius_is_never_injected_as_text_into_the_query(monkeypatch):
    """Google treats '(within 25 km)' as literal search terms: 60 results -> 17."""
    _CaptureClient.sent = []
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: _CaptureClient())
    monkeypatch.setattr("app.providers.geocode", lambda place: (12.92, 80.10))

    GooglePlacesProvider(api_key="k").search("dental clinic", "Perungalathur", 50, radius_m=25000)

    query = _CaptureClient.sent[0]["textQuery"]
    assert "25" not in query and "km" not in query.lower(), f"radius leaked into {query!r}"


def test_radius_is_sent_as_a_structured_location_bias(monkeypatch):
    _CaptureClient.sent = []
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: _CaptureClient())
    monkeypatch.setattr("app.providers.geocode", lambda place: (12.92, 80.10))

    GooglePlacesProvider(api_key="k").search("dental clinic", "Perungalathur", 50, radius_m=25000)

    circle = _CaptureClient.sent[0]["locationBias"]["circle"]
    assert circle["radius"] == 25000.0
    assert circle["center"] == {"latitude": 12.92, "longitude": 80.10}


def test_no_location_bias_when_no_radius_requested(monkeypatch):
    _CaptureClient.sent = []
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: _CaptureClient())
    GooglePlacesProvider(api_key="k").search("dental clinic", "Perungalathur", 50)
    assert "locationBias" not in _CaptureClient.sent[0]


def test_geocode_failure_still_searches_by_name(monkeypatch):
    """A failed geocode must not silently produce zero results."""
    _CaptureClient.sent = []
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: _CaptureClient())
    monkeypatch.setattr("app.providers.geocode", lambda place: None)

    GooglePlacesProvider(api_key="k").search("dental clinic", "Perungalathur", 50, radius_m=25000)

    assert "Perungalathur" in _CaptureClient.sent[0]["textQuery"]
    assert "locationBias" not in _CaptureClient.sent[0]


def test_google_captures_rating_reviews_and_status():
    place = {
        "id": "P1", "displayName": {"text": "Kalaa Dental Care"},
        "rating": 4.9, "userRatingCount": 568, "businessStatus": "OPERATIONAL",
    }
    b = GooglePlacesProvider(api_key="k")._to_business(place)
    assert b.rating == 4.9
    assert b.review_count == 568
    assert b.business_status == "OPERATIONAL"


def test_google_field_mask_requests_rating_fields():
    for field in ("places.rating", "places.userRatingCount", "places.businessStatus"):
        assert field in GooglePlacesProvider.FIELD_MASK


def test_osm_leaves_rating_fields_empty():
    el = {"type": "node", "id": 1, "lat": 13.0, "lon": 80.2, "tags": {"name": "X"}}
    b = OSMProvider()._to_business(el)
    assert b.rating is None and b.review_count is None and b.business_status is None
