from bs4 import BeautifulSoup

from app.enrichment import (
    candidate_pages,
    extract_doctor,
    extract_emails,
    extract_phones,
)


def soup(html):
    return BeautifulSoup(html, "html.parser")


def test_extracts_doctor_from_heading_with_high_confidence():
    got = extract_doctor(soup("<h2>Dr. Priya Kumar</h2><p>BDS, MDS</p>"), "https://x.com/about")
    assert got is not None
    name, conf = got
    assert name == "Dr. Priya Kumar" and conf >= 0.75


def test_low_confidence_doctor_is_returned_but_flagged():
    got = extract_doctor(
        soup("<p>ask for dr. someone at reception</p>"), "https://x.com/pricing"
    )
    assert got is None or got[1] < 0.75


def test_no_doctor_returns_none_never_a_guess():
    assert extract_doctor(soup("<p>Welcome to our clinic</p>"), "https://x.com") is None


def test_body_text_doctor_scores_below_heading_doctor():
    body = extract_doctor(soup("<p>Dr. Ravi Menon consults here</p>"), "https://x.com/")
    head = extract_doctor(soup("<h2>Dr. Ravi Menon</h2>"), "https://x.com/about")
    assert body is not None and head is not None
    assert body[1] < head[1]


def test_prefers_mailto_over_body_text():
    s = soup('<a href="mailto:hi@abc.com">mail</a><p>spam@other.com</p>')
    assert extract_emails(s)[0] == "hi@abc.com"


def test_extracts_tel_links_normalized():
    s = soup('<a href="tel:+91 98765 43210">call</a>')
    assert extract_phones(s) == ["+919876543210"]


def test_candidate_pages_prefers_about_and_team_and_caps_at_four():
    s = soup(
        """<a href="/about">About</a><a href="/our-team">Our Team</a>
           <a href="/doctors">Doctors</a><a href="/contact">Contact</a>
           <a href="/blog">Blog</a><a href="/privacy">Privacy</a>"""
    )
    pages = candidate_pages(s, "https://x.com")
    assert len(pages) <= 4
    assert "https://x.com/blog" not in pages
    assert "https://x.com/about" in pages


def test_candidate_pages_ignores_offsite_links():
    s = soup('<a href="https://facebook.com/about">About us</a>')
    assert candidate_pages(s, "https://x.com") == []


# --- time bounds -----------------------------------------------------------

import asyncio  # noqa: E402

from app import enrichment as E  # noqa: E402
from app.models import Business  # noqa: E402


async def test_one_hanging_site_cannot_stall_the_batch(monkeypatch):
    """A site with no deadline stalled a whole search for 28 minutes."""

    async def hang(business, client):
        await asyncio.sleep(3600)

    monkeypatch.setattr(E, "enrich", hang)
    monkeypatch.setattr(E, "PER_BUSINESS_BUDGET", 0.05)

    out = await E.enrich_all([Business(business_name="Slow", website="https://slow.example")])
    assert out[0].business_name == "Slow", "the un-enriched business is still returned"


async def test_batch_budget_stops_a_pathological_run(monkeypatch):
    async def hang(business, client):
        await asyncio.sleep(3600)

    monkeypatch.setattr(E, "enrich", hang)
    monkeypatch.setattr(E, "PER_BUSINESS_BUDGET", 0.05)
    monkeypatch.setattr(E, "BATCH_BUDGET", 0.2)

    items = [
        Business(business_name=f"S{i}", website=f"https://s{i}.example") for i in range(40)
    ]
    started = asyncio.get_event_loop().time()
    out = await E.enrich_all(items)
    elapsed = asyncio.get_event_loop().time() - started

    assert len(out) == 40, "every business is returned, enriched or not"
    assert elapsed < 5, f"batch must stop at its budget, took {elapsed:.1f}s"


async def test_businesses_without_a_website_are_returned_untouched():
    items = [Business(business_name="No Site"), Business(business_name="Also None")]
    out = await E.enrich_all(items)
    assert [b.business_name for b in out] == ["No Site", "Also None"]


def test_concurrency_allows_more_than_two_distinct_hosts():
    """Per-host locks protect each site; a global cap of 2 only slowed the batch."""
    assert E.MAX_CONCURRENCY >= 6


# --- short info ------------------------------------------------------------

from app.enrichment import extract_description  # noqa: E402


def test_extracts_meta_description():
    s = soup('<meta name="description" content="A specialised dental surgery clinic '
             'in Pallavaram offering implants and root canal therapy.">')
    assert extract_description(s).startswith("A specialised dental surgery clinic")


def test_falls_back_to_og_description():
    s = soup('<meta property="og:description" content="Family dentistry in Chromepet '
             'since 2009, led by Dr. Priya Kumar.">')
    assert extract_description(s).startswith("Family dentistry in Chromepet")


def test_prefers_name_description_over_og():
    s = soup('<meta name="description" content="The canonical description of this '
             'dental clinic in Chennai.">'
             '<meta property="og:description" content="The social card blurb here.">')
    assert extract_description(s).startswith("The canonical")


def test_ignores_a_too_short_description():
    assert extract_description(soup('<meta name="description" content="Dental">')) is None


def test_no_description_returns_none_never_a_guess():
    assert extract_description(soup("<p>Welcome to our clinic</p>")) is None


def test_collapses_whitespace_in_a_description():
    s = soup('<meta name="description" content="  A dental clinic\n   in '
             'Pallavaram, Chennai.  ">')
    assert extract_description(s) == "A dental clinic in Pallavaram, Chennai."
