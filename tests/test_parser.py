import pytest

from app.models import Command
from app.parser import MissingAPIKey, ParserError, RateLimited, parse_command


def test_missing_api_key_raises_named_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(MissingAPIKey, match="GEMINI_API_KEY"):
        parse_command("Find dental clinics in Chromepet")


def test_command_model_accepts_search_shape():
    c = Command(
        action="search",
        business_type="dental clinic",
        location="Chromepet, Chennai",
        quantity=None,
    )
    assert c.action == "search" and c.quantity is None


def test_command_accepts_radius_km():
    c = Command(action="search", business_type="dental clinic",
                location="Kodambakkam, Chennai", radius_km=20)
    assert c.radius_km == 20


def test_command_model_rejects_unknown_action():
    with pytest.raises(ValueError):
        Command(action="delete_everything")


def test_command_model_rejects_bad_filter_kind():
    with pytest.raises(ValueError):
        Command(action="filter", filter_kind="nonsense")


class _Recorder:
    """Captures the exact config sent to Gemini."""

    def __init__(self, parsed=None, error=None):
        self.parsed, self.error, self.config, self.model = parsed, error, None, None

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        self.model, self.config = model, config
        if self.error:
            raise self.error
        return type("R", (), {"parsed": self.parsed})()


def test_parse_returns_command_and_sends_correct_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    want = Command(
        action="search", business_type="dental clinic", location="Chromepet", quantity=50
    )
    rec = _Recorder(parsed=want)
    monkeypatch.setattr("app.parser._client", lambda: rec)

    got = parse_command("Find 50 dental clinics in Chromepet")

    assert got is want
    assert rec.model == "gemini-2.5-flash"
    # Silent-failure settings: a dropped schema returns unvalidated text, and a
    # dropped thinking budget costs 8x latency without raising anything.
    assert rec.config.response_schema is Command
    assert rec.config.response_mime_type == "application/json"
    assert rec.config.thinking_config.thinking_budget == 0


def test_model_is_overridable_by_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash")
    rec = _Recorder(parsed=Command(action="store"))
    monkeypatch.setattr("app.parser._client", lambda: rec)
    parse_command("save these")
    assert rec.model == "gemini-3.7-flash"


def test_quota_error_becomes_rate_limited(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class Boom(Exception):
        code = 429
        message = "RESOURCE_EXHAUSTED ... Please retry in 37.09s"

    rec = _Recorder(error=Boom())
    monkeypatch.setattr("app.parser._client", lambda: rec)
    with pytest.raises(RateLimited, match="rate limit"):
        parse_command("Find dental clinics in Chromepet")


def test_other_client_error_becomes_parser_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class Boom(Exception):
        code = 400
        message = "API key not valid"

    rec = _Recorder(error=Boom())
    monkeypatch.setattr("app.parser._client", lambda: rec)
    with pytest.raises(ParserError, match="API key not valid"):
        parse_command("Find dental clinics in Chromepet")


def test_unparseable_response_raises_rather_than_returning_none(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    rec = _Recorder(parsed=None)
    monkeypatch.setattr("app.parser._client", lambda: rec)
    with pytest.raises(ParserError, match="structured"):
        parse_command("Find dental clinics in Chromepet")


def test_client_is_reused_across_calls(monkeypatch):
    """A fresh Client per call can be garbage-collected mid-request.

    Regression: building one inline as `_client().models.generate_content(...)`
    let its httpx transport close under the in-flight request, producing
    "Cannot send a request, as the client has been closed."
    """
    import app.parser as parser

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(parser, "_cached", None)
    first = parser._client()
    second = parser._client()
    assert first is second


def test_client_is_rebuilt_when_the_key_changes(monkeypatch):
    import app.parser as parser

    monkeypatch.setenv("GEMINI_API_KEY", "key-one")
    monkeypatch.setattr(parser, "_cached", None)
    first = parser._client()
    monkeypatch.setenv("GEMINI_API_KEY", "key-two")
    assert parser._client() is not first


def test_daily_quota_message_says_today_not_wait_a_minute(monkeypatch):
    """Per-day and per-minute exhaustion arrive as the same 429 with different advice."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class Boom(Exception):
        code = 429
        message = (
            "RESOURCE_EXHAUSTED quotaId: "
            "'GenerateRequestsPerDayPerProjectPerModel-FreeTier' quotaValue: '20'"
        )

    rec = _Recorder(error=Boom())
    monkeypatch.setattr("app.parser._client", lambda: rec)
    with pytest.raises(RateLimited, match="per day") as e:
        parse_command("Find dental clinics")
    assert "today" in str(e.value)
    assert "per minute" not in str(e.value)


def test_per_minute_quota_message_includes_the_retry_delay(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class Boom(Exception):
        code = 429
        message = (
            "RESOURCE_EXHAUSTED quotaId: "
            "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier' quotaValue: '5' "
            "Please retry in 37.09s"
        )

    rec = _Recorder(error=Boom())
    monkeypatch.setattr("app.parser._client", lambda: rec)
    with pytest.raises(RateLimited, match="per minute") as e:
        parse_command("Find dental clinics")
    assert "37s" in str(e.value)


def test_default_model_is_the_one_with_free_headroom(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    rec = _Recorder(parsed=Command(action="store"))
    monkeypatch.setattr("app.parser._client", lambda: rec)
    parse_command("save these")
    assert rec.model == "gemini-2.5-flash"


# --- on-demand summary -----------------------------------------------------

from app.parser import summarize  # noqa: E402


class _FakeGen:
    """Captures the config sent to Gemini and returns a canned response."""

    last_config = None
    last_contents = None

    def __init__(self, text):
        self._text = text

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        _FakeGen.last_config = config
        _FakeGen.last_contents = contents
        return type("R", (), {"text": self._text})()


def test_summarize_returns_the_model_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake = _FakeGen("A dental surgery clinic in Pallavaram led by Dr. Surekha Vinoth.")
    monkeypatch.setattr("app.parser._client", lambda: fake)
    out = summarize("page text here", "Clue and Cure Dentistry")
    assert out.startswith("A dental surgery clinic")
    # Extraction, not reasoning - the same rule as command parsing.
    assert _FakeGen.last_config.thinking_config.thinking_budget == 0


def test_summarize_returns_none_when_the_model_declines(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.parser._client", lambda: _FakeGen("   "))
    assert summarize("nothing useful", "X") is None


def test_summarize_prompt_forbids_inference(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.parser._client", lambda: _FakeGen("ok"))
    summarize("text", "X")
    # Normalise whitespace: the prompt is wrapped, so a literal substring can
    # straddle a newline and miss for a reason that has nothing to do with content.
    system = " ".join(_FakeGen.last_config.system_instruction.lower().split())
    assert "never infer" in system
    assert "empty string" in system


def test_summarize_includes_the_business_name_and_page_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.parser._client", lambda: _FakeGen("ok"))
    summarize("PAGE BODY TEXT", "Clue and Cure")
    assert "Clue and Cure" in _FakeGen.last_contents
    assert "PAGE BODY TEXT" in _FakeGen.last_contents


def test_summarize_without_a_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(MissingAPIKey):
        summarize("text", "X")
