# Business Research Agent

Turn a plain-language command into a reviewed, deduplicated list of local-business
leads — enriched from each business's own website, tracked through your outreach,
and kept in an Excel workbook you can open in Finder.

```
Find dental clinics in Kodambakkam
```

→ searches a business-data provider → reads each site for a doctor's name and
contacts → deduplicates against what you've already saved → shows you everything
**before** a single row is written.

<sub>Python 3.12 · FastAPI · Postgres · openpyxl · Google Places / OpenStreetMap · Gemini · 218 tests</sub>

---

## Why it exists

Lead research is a loop: search, weed out what you already have, look up who runs
the place, call them, write down what happened. Spreadsheets handle the last step
and nothing else, so the rest happens in browser tabs and gets re-done every week.

This closes the loop in one place, and refuses to guess at any point in it.

## What it does

| | |
|---|---|
| **Search** | Natural-language commands, parsed by Gemini. `Find 50 dental clinics within 20 km of Tambaram` |
| **Enrich** | Visits each business's site for doctor names, emails, alternate numbers — bounded, robots-respecting |
| **Deduplicate** | Five-tier matching with confidence bands. Nothing below HIGH is auto-merged or auto-deleted |
| **Review** | Results are tagged NEW / EXISTING / UPDATED / REVIEW and held until you approve them |
| **Organise** | Multiple workbooks in folders, each with its own dedupe scope |
| **Edit** | Every cell editable in-app; changes write to the database and re-render the sheet |
| **Track** | Call outcome, interest, meeting date and place, follow-up flag and date |
| **Summarise** | One-click factual summary of a business from its own pages |

## The one rule everything else follows

**Never invent data.** A field that cannot be sourced stays empty.

It sounds obvious and it costs something on almost every feature:

- A doctor's name below 0.75 extraction confidence goes to `Notes`, not the
  `Doctor Name` column.
- The AI summary returns nothing rather than padding when a site is thin — and
  the app tells you that's what happened.
- `Area` comes from the business's own tags, never from the location you searched.
- A business with no rating sorts **last**, in both directions. Missing data is
  not a low score.

Every value the app didn't get from a provider carries the URL it came from.

---

## Quick start

```bash
git clone https://github.com/rharshavardhanan/business-research-agent.git
cd business-research-agent
uv sync

# Postgres — any instance will do; this is the one the tests use.
docker run -d --name bra-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=bra \
  -p 55432:5432 postgres:16-alpine

cp .env.example .env          # add your Gemini key
uv run uvicorn app.main:app --reload
```

Open <http://localhost:8000>.

### Configuration

| Variable | Required | Notes |
|---|---|---|
| `GEMINI_API_KEY` | **Yes** | Command parsing is LLM-only with no fallback. Free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | No | Defaults to `gemini-2.5-flash`. **Avoid `gemini-3.5-flash`** — free tier caps it at 20 requests *per day* |
| `SEARCH_PROVIDER` | No | `osm` (default) or `google_places` |
| `GOOGLE_PLACES_API_KEY` | For Google | Requires **Places API (New)** enabled and billing on the project |
| `APP_PASSWORD` | For deployment | Blank locally. Set it before exposing the app to a network |
| `DATABASE_URL` | **Yes** | Postgres connection string. Free tier at [neon.tech](https://neon.tech) |

---

## Choosing a provider

Measured on the same query — dental clinics, Kodambakkam, 15 results:

| | `osm` | `google_places` |
|---|---|---|
| Phone | **0%** | **73%** |
| Website | **0%** | **66%** |
| Doctor name found | 0 | 3 |
| Rating | none | 10/10 |

OpenStreetMap is free and needs no card, but for Chennai clinics it holds names
and coordinates and little else. Because doctor-name enrichment needs a website
to read, that whole feature is dormant on OSM.

Google Places needs a card on file; its free monthly allowance covers
personal-scale research. Switch with two lines in `.env` — the providers share
one interface and no code changes.

> **Note:** it must be **Places API (New)**. The legacy "Places API" is a
> different product and returns `PERMISSION_DENIED`.

---

## Architecture

```
POST /command
     │
parser.py ──── Gemini ──→ Command{action, business_type, location, radius_km, …}
     │
     ├─ search ──→ providers.py        OSM │ Google Places
     │                  ↓
     │             normalize.py        canonical phone / url / name / address
     │                  ↓
     │             enrichment.py       ≤5 pages, robots-checked, time-bounded
     │                  ↓
     │             dedupe.py ⟷ store.py    5-tier match, per-workbook scope
     │                  ↓
     │             tagged NEW/EXISTING/UPDATED/REVIEW ──→ buffer ──→ UI
     │
     └─ store ───→ store.py (Postgres upsert)
```

**Postgres is the source of truth. The `.xlsx` is generated from it on demand.**

That single decision explains most of the design: edits write to the database and
any download reflects them immediately; renaming a workbook carries its rows via
a foreign-key cascade; deleting one hides them. There is no second copy to drift.

`research()` deliberately **writes nothing** — results are buffered and persisted
only when you say so. That review gap is the product.

### Modules

| File | Responsibility |
|---|---|
| `models.py` | `Business` and `Command` |
| `parser.py` | Natural language → `Command`; on-demand summaries |
| `providers.py` | Search provider interface + OSM and Google implementations |
| `enrichment.py` | Fetch and parse business websites |
| `normalize.py` | Canonical forms for everything compared |
| `dedupe.py` | Match hierarchy and merge policy |
| `store.py` | Postgres: schema, upsert, row edits, soft delete |
| `excel.py` | Workbook rendering, in memory |
| `workbooks.py` | Workbook rows: tree, create, rename, delete, path validation |
| `research.py` | The pipeline |
| `main.py` | Routes, auth, result buffer |

Scraping logic never touches Excel logic, and `workbooks.py` touches neither.

---

## Deduplication

Checked in order; first hit wins.

| Tier | Match on | Confidence | Behaviour |
|---|---|---|---|
| 1 | provider ID | HIGH | merge |
| 2 | normalized phone | HIGH | merge |
| 3 | normalized website | HIGH | merge |
| 4 | normalized name + address | HIGH | merge |
| 5 | fuzzy name ≥88 + same area | MEDIUM | flag for review, keep separate |
| 6 | fuzzy name 75–87 | LOW | keep separate |

Merging fills empty fields and **never overwrites** one you corrected. Ratings and
review counts are the one exception — they're observations of a changing world, so
a re-search refreshes them while your hand-fixed phone number survives.

`ABC Dental Clinic` and `ABC Dental Care` stay two leads until something stronger
than a name says otherwise.

**Dedupe is scoped per workbook.** Appending a second search to one workbook never
re-adds what it already holds; the same clinic saved to a second workbook is a
legitimate separate row there.

---

## Deployment — Vercel + Neon

Both have free tiers that cover this app, and together they cost nothing.

**1. Database.** Create a project at [neon.tech](https://neon.tech) and copy the
**pooled** connection string.

**2. Deploy.** Import the repo at [vercel.com/new](https://vercel.com/new).
`vercel.json` routes every path to the ASGI app in `api/index.py`, so one
function serves the API and the UI.

**3. Environment variables** in the Vercel dashboard:

```
DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require
GEMINI_API_KEY=...
GOOGLE_PLACES_API_KEY=...
SEARCH_PROVIDER=google_places
APP_PASSWORD=...
```

**4. Bring your data across** (optional):

```bash
uv run python scripts/migrate_sqlite_to_postgres.py \
    --source data/leads.db --dsn "$DATABASE_URL"
```

It reports per-workbook counts and refuses to run twice against a non-empty
database without `--force`.

### Set a password

There is no per-user auth. `APP_PASSWORD` puts HTTP Basic in front of every
route, including the UI. A Vercel URL is public by default, and every route
spends billable Google and Gemini quota. Leave it blank locally, where a
password is friction with no benefit.

### What being serverless costs

There is no disk, which shapes three things:

- **The `.xlsx` is generated at download**, never stored. You can download a
  workbook, but rows typed into that file cannot be read back — edit in the app's
  grid instead, which does everything the sheet does.
- **The browser holds unsaved search results.** A refresh loses them. Nothing
  already saved is ever at risk.
- **Enrichment runs one request per business**, driven by the page. Results
  appear immediately and detail fills in as it arrives, rather than one long
  request that no function timeout would allow.

Deleting is soft: a `deleted_at` column hides rows rather than destroying them.


## Tests

```bash
uv run pytest          # 218 tests, no network
```

Tests run against a **real Postgres**, each in its own throwaway schema — not
SQLite standing in for it, which would prove nothing about the database actually
in production.

Nothing in the suite touches the internet: providers and enrichment are exercised
against fixtures, and the Gemini client is stubbed — including deleting
`GEMINI_API_KEY` from the environment so a real key can't send a test to the live
API and burn free-tier quota.

| Suite | Covers |
|---|---|
| `test_normalize` | Phone, URL, name and address canonicalisation |
| `test_dedupe` | Tier hierarchy, confidence bands, merge policy |
| `test_store_excel` | Persistence, migration, workbook render and read-back |
| `test_store_edit` | Cell edits, validation, the follow-up coupling |
| `test_providers` | Both providers, radius parsing, error passthrough |
| `test_enrichment` | Extraction, page selection, time bounds |
| `test_parser` | Command schema, error mapping, summary prompt |
| `test_workbooks` | Path validation, tree, create/rename/delete |
| `test_api` | Every endpoint |
| `test_auth` | The password gate |

### Path validation

Workbook paths are database keys, not filesystem paths — folders are prefixes,
not directories. `workbooks.validate` still rejects absolute paths, `..`, empty
segments and unexpected characters, because a malformed key is one nobody can
address. Each rule has its own test.

---

## Being a good guest

Enrichment reads other people's websites, so it:

- honours `robots.txt`
- reads at most 5 pages per business
- caps page size at 1 MB
- never retries a `4xx` — that's an answer
- serialises requests per host and bounds every batch (30s per business, 120s total)

The OSM provider sends an identifying `User-Agent` and rate-limits Nominatim to
one request per second per its usage policy. Overpass queries retry across two
public mirrors, which routinely return 429 and 504 under load.

## Limits worth knowing

- **Google Places returns at most 60 results per query.** Ask for 150 and you get
  60 plus a notice explaining why. Run narrower searches by neighbourhood — dedupe
  collapses the overlaps.
- **Gemini's free tier is 5 requests/minute** with a daily cap. That's why AI
  summaries are one-click-per-row rather than automatic.
- **Phone normalization is India-only** (`+91XXXXXXXXXX`), marked in the code with
  its upgrade path.

## License

MIT
