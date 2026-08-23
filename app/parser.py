"""Turns a natural-language instruction into a validated `Command`.

Runs on Google Gemini. There is deliberately no rules-based fallback: a tool that
sometimes uses an LLM parser and sometimes a regex is a tool whose behaviour
cannot be reasoned about. Without a key this raises MissingAPIKey.

The free tier is small (5 requests/minute), so the three failure modes that a
free-key user will actually hit each raise a named error carrying its own fix.
"""

import os
import re

from google import genai
from google.genai import types

from app.models import Command

# gemini-2.5-flash, not 3.5-flash. Measured against a real free key:
# gemini-3.5-flash caps at 20 requests PER DAY
# (GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue 20), which runs
# out after twenty typed commands. 2.5-flash has usable free headroom and parses
# these commands identically. Override with GEMINI_MODEL to use any other.
DEFAULT_MODEL = "gemini-2.5-flash"

SYSTEM = """You convert local-business research commands into structured actions.

- search: find businesses. Extract business_type, location, and quantity if a
  number is given. Expand a bare area to include its city ("Chromepet" ->
  "Chromepet, Chennai").
  If the user states a distance ("within 5 km of X", "20 km surrounding of X",
  "10km radius of X", "near X within 20 km"), put ONLY the bare place name in
  location and the number of KILOMETRES in radius_km. Never leave a distance
  phrase inside location.
- store: SAVE the results currently on screen. Use for "add these to Excel",
  "save these", "add to my sheet". This WRITES the pending results.
- export: RE-WRITE the whole workbook from what is already saved. Only for
  explicit "export" wording. If the user says "add" or "save", choose store.
- deduplicate: remove duplicates already stored.
- filter: show a subset of what is stored. Set filter_kind to one of
  without_website, with_phone, without_doctor.
- unknown: nothing above matches.

Never guess a location that was not stated. Leave fields null, never invented."""


SUMMARY_SYSTEM = """You write one short factual description of a business from
its own web pages.

Rules:
- At most two sentences.
- Use ONLY what the page text states. Never infer, never estimate, never use
  outside knowledge about the business or its area.
- Name services, specialisms or practitioners only if the page says so.
- No marketing language. Do not repeat claims like "the best in the city".
- If the pages do not say enough for a factual description, return an empty
  string. An empty answer is correct and preferred over a plausible guess."""


class MissingAPIKey(RuntimeError):
    """No Gemini credentials configured."""


class RateLimited(RuntimeError):
    """Free-tier quota exhausted."""


class ParserError(RuntimeError):
    """Gemini rejected the request or returned nothing usable."""


def _model() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


# (api_key, client). Cached because a Client owns an httpx transport: building a
# fresh one per call let the previous one be garbage-collected mid-request, which
# surfaced as "Cannot send a request, as the client has been closed."
_cached: tuple[str, genai.Client] | None = None


def _client() -> genai.Client:
    """Return the shared client, failing loudly and late rather than at import."""
    global _cached
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise MissingAPIKey(
            "GEMINI_API_KEY is not set. Command parsing requires it and has no "
            "fallback. Get a free key at https://aistudio.google.com/apikey, "
            "add it to .env, and restart the server."
        )
    if _cached is None or _cached[0] != key:
        _cached = (key, genai.Client(api_key=key))
    return _cached[1]


def parse_command(text: str) -> Command:
    """Parse `text` into a Command.

    Raises MissingAPIKey when unconfigured, RateLimited on free-tier quota, and
    ParserError when Gemini rejects the request or returns unusable output.
    """
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=Command,
        # Extraction, not reasoning. Measured: 2.1-8.7s with thinking on, ~1.1s
        # off, with identical answers. At 5 requests/minute the tokens matter too.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        # No tools are declared; this silences the SDK's AFC advisory.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    # Bound to a local, not called inline: the Client must stay strongly
    # referenced for the whole request or its transport can be closed under it.
    client = _client()
    try:
        response = client.models.generate_content(
            model=_model(), contents=text, config=config
        )
    except MissingAPIKey:
        raise
    except Exception as exc:  # noqa: BLE001 - translated below, never swallowed
        raise _translate(exc) from exc

    command = response.parsed
    if not isinstance(command, Command):
        raise ParserError(
            "Gemini did not return structured output for that command. Try "
            "rephrasing it, or check that GEMINI_MODEL supports JSON schemas."
        )
    return command


def _translate(exc: Exception) -> RuntimeError:
    """Map an SDK error onto an error whose message names the fix.

    Duck-typed on `.code`/`.message` rather than catching
    `google.genai.errors.ClientError`, which is not a stable public import
    across SDK versions.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)

    if code == 429 or "RESOURCE_EXHAUSTED" in message:
        return RateLimited(_quota_message(message))
    return ParserError(f"Gemini rejected the request: {message}")


def _quota_message(message: str) -> str:
    """Explain WHICH quota was hit.

    Google returns per-minute and per-day exhaustion through the same 429, and
    the advice differs completely: one means wait a moment, the other means the
    model is done until tomorrow. A message that always says "per minute" sends
    the user to refresh forever.
    """
    limit = re.search(r"quotaValue['\"]?:\s*['\"]?(\d+)", message)
    retry = re.search(r"retry in ([\d.]+)s", message)
    amount = limit.group(1) if limit else "the free-tier"

    if "PerDay" in message:
        return (
            f"Gemini's free tier for {_model()} is exhausted for today "
            f"(limit: {amount} requests per day). Either wait until the quota "
            f"resets, or set GEMINI_MODEL to another model in .env - "
            f"gemini-2.5-flash has more free headroom."
        )

    wait = f" Retry in about {float(retry.group(1)):.0f}s." if retry else ""
    return (
        f"Gemini's free tier rate limit for {_model()} is currently exceeded "
        f"(limit: {amount} requests per minute).{wait}"
    )


def summarize(text: str, name: str) -> str | None:
    """Summarise a business from its own page text, or None if it cannot be done.

    Deliberately per-business and on demand: the free tier allows 5 requests a
    minute, so summarising a 60-result search automatically would stall for
    twelve minutes or exhaust the daily quota.
    """
    client = _client()
    config = types.GenerateContentConfig(
        system_instruction=SUMMARY_SYSTEM,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    try:
        response = client.models.generate_content(
            model=_model(),
            contents=f"Business name: {name}\n\nPage text:\n{text[:20000]}",
            config=config,
        )
    except MissingAPIKey:
        raise
    except Exception as exc:  # noqa: BLE001 - translated, never swallowed
        raise _translate(exc) from exc

    # An empty answer means the pages did not say enough. Storing None is the
    # correct outcome under the never-invent rule.
    return (response.text or "").strip() or None
