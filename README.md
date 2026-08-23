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

<sub>Python 3.12 · FastAPI · SQLite · openpyxl · Google Places / OpenStreetMap · Gemini · 220 tests</sub>

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
git clone https://github.com/<you>/business-research-agent.git
cd business-research-agent
uv sync
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
| `DATA_DIR` | No | Defaults to `data/` |

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
     └─ store ───→ store.py (SQLite upsert) ──→ excel.py (workbook re-render)
```

**SQLite is the source of truth. The `.xlsx` is a rendered view of it.**

That single decision explains most of the design: edits write to the database and
the sheet is re-rendered, never edited in place; renaming a workbook carries its
rows; deleting one drops them. The two can't drift apart.

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
| `store.py` | SQLite: schema, migration, upsert, row edits |
| `excel.py` | Workbook render, in-place sync, read-back |
| `workbooks.py` | Filesystem: tree, create, rename, delete, **path validation** |
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

## Deployment

The app writes SQLite and `.xlsx` files to disk and runs a search for up to two
minutes. It needs **a persistent disk and no function timeout**.

### It will not run on Vercel

Not a configuration problem — a mismatch. Serverless filesystems are ephemeral, so
the database and every workbook are lost between invocations; the in-memory result
buffer doesn't survive to the save request; and enrichment exceeds the function
timeout. Running it there means replacing SQLite with Postgres, workbooks with
blob storage, and enrichment with a queue.

### Railway or Render

```bash
# Railway
railway up          # railway.toml is included

# Render
# render.yaml is included — connect the repo and deploy
```

Both need:

1. **A volume mounted at `/data`.** Without it, every redeploy starts empty.
2. `GEMINI_API_KEY`, `GOOGLE_PLACES_API_KEY`, `SEARCH_PROVIDER`
3. **`APP_PASSWORD`** — see below.

A `Dockerfile` is included and builds from the committed lockfile.

### Set a password before you expose it

There is no per-user auth. Setting `APP_PASSWORD` puts HTTP Basic in front of
every route, including the UI. Without it, anyone with the URL can spend your
Google and Gemini quota and read, edit or delete your leads.

Blank locally — a password on `localhost` is friction with no benefit.

---

## Tests

```bash
uv run pytest          # 220 tests, no network
```

Nothing in the suite touches the network: providers and enrichment are exercised
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
| `test_workbooks` | **Path traversal**, tree, create/rename/delete |
| `test_api` | Every endpoint |
| `test_auth` | The password gate |

### Path safety

`workbooks.resolve_path` is a trust boundary: `/workbooks/download` streams
whatever it returns, so without it the endpoint would serve any file the process
can read. It rejects absolute paths, `..` traversal, symlinks escaping `data/`,
non-`.xlsx` workbook paths, and the reserved `leads.db` and `.trash` names — each
with its own test.

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
