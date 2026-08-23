"""Reads a business's own public website for details the directory data lacks.

The doctor's name is the column that makes these leads worth more than a map
listing, and it exists in no Places API - only on the clinic's own site.

Two rules govern everything here:
  1. Never invent. A field that cannot be read stays None.
  2. Be a polite guest. robots.txt is honoured, pages are capped, requests are
     rate-limited per host, and 4xx is never retried.
"""

import asyncio
import logging
import re
import urllib.robotparser
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.models import Business
from app.normalize import normalize_phone

log = logging.getLogger(__name__)

MAX_PAGES = 5
MAX_BYTES = 1_000_000
TIMEOUT = 10.0

# 8, not 2. Each host is already serialised by its own lock, so a global cap of
# 2 throttled unrelated sites against each other for no politeness gain - and
# left only two slots for a hung site to occupy.
MAX_CONCURRENCY = 8

# Hard ceilings. Measured: the happy path is ~2-12s per site and ~20s for a
# batch of 8. One pathological site with no deadline turned a single search into
# 1668s, because per-request timeouts do not bound a page loop that keeps
# starting new requests.
PER_BUSINESS_BUDGET = 30.0
BATCH_BUDGET = 120.0

DOCTOR_CONFIDENCE_THRESHOLD = 0.75

USER_AGENT = "business-research-agent/0.1 (local lead research)"

# A capitalised "Dr" followed by 1-4 capitalised name words. Case-sensitive on
# purpose: "ask for dr. someone" is prose, not an attribution.
_DOCTOR_RE = re.compile(r"\bDr\.?\s+((?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+){0,3})")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_CREDENTIAL_RE = re.compile(r"\b(BDS|MDS|MBBS|MD|DDS|BAMS|BHMS|DNB)\b")

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "strong", "title")

_PAGE_WANTED = re.compile(r"about|team|doctor|staff|contact|profile", re.I)
_PAGE_REJECTED = re.compile(r"blog|news|privacy|terms|career|cart|login|policy", re.I)

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


# -- extraction (pure, testable without network) -------------------------


def extract_doctor(soup: BeautifulSoup, url: str) -> tuple[str, float] | None:
    """Return (name, confidence) for the most likely doctor, or None.

    Confidence is additive and capped at 1.0. Callers must not write the name
    into `doctor_name` below DOCTOR_CONFIDENCE_THRESHOLD.
    """
    url_bonus = 0.2 if re.search(r"about|team|doctor|staff", url, re.I) else 0.0
    page_text = soup.get_text(" ", strip=True)

    best: tuple[str, float] | None = None

    def consider(text: str, in_heading: bool) -> None:
        nonlocal best
        for match in _DOCTOR_RE.finditer(text):
            name = f"Dr. {match.group(1)}"
            score = 0.5 + url_bonus + (0.25 if in_heading else 0.0)
            # A dental/medical credential near the mention is strong evidence
            # this is a practitioner rather than a passing reference.
            window = page_text[
                max(0, page_text.find(match.group(0)) - 200) : page_text.find(
                    match.group(0)
                )
                + 200
            ]
            if _CREDENTIAL_RE.search(window):
                score += 0.1
            score = min(score, 1.0)
            if best is None or score > best[1]:
                best = (name, score)

    for tag in soup.find_all(_HEADING_TAGS):
        consider(tag.get_text(" ", strip=True), in_heading=True)
    consider(page_text, in_heading=False)

    return best


MIN_DESCRIPTION = 25


def extract_description(soup: BeautifulSoup) -> str | None:
    """The page's own one-line description, or None.

    Marketing copy more often than fact, which is why the caller records that it
    came from the website rather than presenting it as a neutral summary.
    """
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if not tag:
            continue
        text = " ".join((tag.get("content") or "").split())
        # A stub tag ("Dental") occupies the column without informing anyone.
        if len(text) > MIN_DESCRIPTION:
            return text
    return None


def extract_emails(soup: BeautifulSoup) -> list[str]:
    """mailto: links first (they are declarations), then body-text matches."""
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr and addr not in found:
                found.append(addr)
    for addr in _EMAIL_RE.findall(soup.get_text(" ", strip=True)):
        if addr not in found:
            found.append(addr)
    return found


def extract_phones(soup: BeautifulSoup) -> list[str]:
    """tel: links first, then body text. Non-Indian-mobile values are dropped."""
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            phone = normalize_phone(a["href"][4:])
            if phone and phone not in found:
                found.append(phone)
    for chunk in re.findall(r"[\d+][\d\s\-()]{8,}", soup.get_text(" ", strip=True)):
        phone = normalize_phone(chunk)
        if phone and phone not in found:
            found.append(phone)
    return found


def candidate_pages(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Up to 4 same-origin pages most likely to name a practitioner."""
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        host = urlparse(absolute).netloc.lower().removeprefix("www.")
        if host != base_host or absolute in seen:
            continue

        haystack = f"{href} {a.get_text(' ', strip=True)}"
        if _PAGE_REJECTED.search(haystack):
            continue
        if not _PAGE_WANTED.search(haystack):
            continue

        seen.add(absolute)
        # Pages that name people outrank a generic contact page.
        weight = 2 if re.search(r"team|doctor|staff|about", haystack, re.I) else 1
        scored.append((weight, absolute))

    scored.sort(key=lambda pair: -pair[0])
    return [url for _, url in scored[:4]]


# -- fetching ------------------------------------------------------------


async def _robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = await client.get(f"{origin}/robots.txt", timeout=TIMEOUT)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            else:
                parser = None  # no robots.txt published -> nothing disallowed
        except httpx.HTTPError:
            parser = None
        _robots_cache[origin] = parser
    parser = _robots_cache[origin]
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


async def _fetch(client: httpx.AsyncClient, url: str) -> BeautifulSoup | None:
    """GET one page with bounded retries. 4xx is never retried - it is an answer."""
    for attempt, backoff in enumerate((0.5, 1.0, None)):
        try:
            resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
            if 400 <= resp.status_code < 500:
                return None
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            if "html" not in resp.headers.get("content-type", "").lower():
                return None
            if len(resp.content) > MAX_BYTES:
                return None
            return BeautifulSoup(resp.text, "html.parser")
        except (httpx.HTTPError, httpx.TransportError):
            if backoff is None:
                return None
            await asyncio.sleep(backoff)
    return None


async def enrich(business: Business, client: httpx.AsyncClient) -> Business:
    """Read a business's website and fill only what can be evidenced."""
    if not business.website:
        return business

    url = business.website
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if not await _robots_allows(client, url):
        log.info("robots.txt disallows %s - skipping enrichment", url)
        return business

    host = urlparse(url).netloc
    updates: dict[str, object] = {}
    sources = dict(business.sources)
    notes: list[str] = [business.notes] if business.notes else []

    async with _host_locks[host]:
        home = await _fetch(client, url)
        if home is None:
            return business

        pages: list[tuple[str, BeautifulSoup]] = [(url, home)]
        for page_url in candidate_pages(home, url)[: MAX_PAGES - 1]:
            page = await _fetch(client, page_url)
            if page is not None:
                pages.append((page_url, page))

    best_doctor: tuple[str, float, str] | None = None
    for page_url, page in pages:
        found = extract_doctor(page, page_url)
        if found and (best_doctor is None or found[1] > best_doctor[1]):
            best_doctor = (found[0], found[1], page_url)

        if not business.email and "email" not in updates:
            emails = extract_emails(page)
            if emails:
                updates["email"] = emails[0]
                sources["email"] = page_url

        if not business.short_info and "short_info" not in updates:
            description = extract_description(page)
            if description:
                updates["short_info"] = description
                updates["short_info_source"] = "Website"
                sources["short_info"] = page_url

        if not business.alternate_phone and "alternate_phone" not in updates:
            others = [p for p in extract_phones(page) if p != business.phone]
            if others:
                updates["alternate_phone"] = others[0]
                sources["alternate_phone"] = page_url

    if best_doctor:
        name, confidence, page_url = best_doctor
        if confidence >= DOCTOR_CONFIDENCE_THRESHOLD:
            updates["doctor_name"] = name
            sources["doctor_name"] = page_url
        else:
            # Below threshold the name is a lead for a human, not a fact.
            notes.append(f"possible doctor: {name} (confidence {confidence:.2f}, {page_url})")

    if notes:
        updates["notes"] = " | ".join(notes)
    if sources != business.sources:
        updates["sources"] = sources

    return business.model_copy(update=updates) if updates else business


async def enrich_all(businesses: list[Business]) -> list[Business]:
    """Enrich every business with a website, under hard time bounds.

    Enrichment is a nice-to-have: a lead without a doctor name is still a lead,
    but a search that never returns is useless. So every failure mode here -
    timeout, refusal, exception - yields the un-enriched business rather than
    propagating. The result list always matches the input, in order.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    results: list[Business] = list(businesses)
    deadline = asyncio.get_event_loop().time() + BATCH_BUDGET

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(TIMEOUT, connect=5.0),
    ) as client:

        async def one(index: int, business: Business) -> None:
            if not business.website:
                return
            async with semaphore:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    log.info("batch budget spent, skipping %s", business.website)
                    return
                try:
                    results[index] = await asyncio.wait_for(
                        enrich(business, client),
                        timeout=min(PER_BUSINESS_BUDGET, remaining),
                    )
                except TimeoutError:
                    log.warning(
                        "enrichment timed out after %.0fs for %s",
                        PER_BUSINESS_BUDGET,
                        business.website,
                    )
                except Exception as exc:  # noqa: BLE001 - one site must not kill the run
                    log.warning("enrichment failed for %s: %s", business.website, exc)

        await asyncio.gather(*(one(i, b) for i, b in enumerate(businesses)))

    return results


async def fetch_page_text(business: Business) -> tuple[str, str] | None:
    """Re-fetch a business's pages and return (text, url), or None.

    Page text is not stored anywhere; a deliberate click can afford a few
    seconds. Uses the same robots check and bounded fetch as enrichment.
    """
    if not business.website:
        return None
    url = business.website
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(TIMEOUT, connect=5.0),
    ) as client:
        if not await _robots_allows(client, url):
            return None
        home = await _fetch(client, url)
        if home is None:
            return None
        chunks = [home.get_text(" ", strip=True)]
        for page_url in candidate_pages(home, url)[:2]:
            page = await _fetch(client, page_url)
            if page is not None:
                chunks.append(page.get_text(" ", strip=True))

    return " ".join(chunks)[:20000], url
