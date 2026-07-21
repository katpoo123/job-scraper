# job-scraper

Searches Ashby, Greenhouse, and Lever job postings via the Brave Search API, scrapes each posting for details, and appends new results to a Google Sheet.

## How it works

1. **Search** — for each (search term × ATS site) combination, queries Brave Search with a `site:` filter (e.g. `site:jobs.ashbyhq.com "head of data"`), paging through results.
2. **Validate & dedupe** — filters results down to URLs that match each platform's individual-job-posting pattern, then drops any URL already present in the Google Sheet.
3. **Scrape** — for each new URL, extracts title, company, location, remote status, salary, and description:
   - **Ashby** postings are a React app, so these are rendered with Playwright (headless Chromium) before parsing.
   - **Greenhouse** and **Lever** postings are server-rendered, so these are fetched directly with `requests` + BeautifulSoup.
   - Non-US postings are filtered out based on location text (see [Known limitations](#known-limitations)).
4. **Segment** — if `HF_TOKEN` is set, each scraped posting is classified by Gemma (open-weight, via the HuggingFace free tier) into fit columns: management vs IC, salary range (lower/upper), office days, commute from Oakland, tooling modernity, and specialties. Without a token these columns are left blank.
5. **Write** — appends new rows to the `Jobs` worksheet in the target Google Sheet.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Get a Brave Search API key

Sign up at [api.search.brave.com](https://api.search.brave.com) and grab an API key from the dashboard.

### 3. Create a Google Cloud service account

1. In the [Google Cloud Console](https://console.cloud.google.com), create (or reuse) a project and enable the **Google Sheets API**.
2. Create a service account, then create a JSON key for it and download it.
3. Save the key as `credentials.json` in the project root (already gitignored), or point `GOOGLE_APPLICATION_CREDENTIALS` at wherever you saved it.
4. Open the target Google Sheet and **share it** with the service account's email address (found in the JSON key as `client_email`), with Editor access.

### 4. Set environment variables

| Variable | Required | Description |
|---|---|---|
| `BRAVE_API_KEY` | Yes | Brave Search API key from step 2. |
| `GOOGLE_SHEET_ID` | Yes | The ID from the target sheet's URL (`.../spreadsheets/d/<THIS_PART>/edit`). |
| `GOOGLE_APPLICATION_CREDENTIALS` | No | Path to the service account key, if not `credentials.json` in the project root. |
| `HF_TOKEN` | No | Enables the [LLM segmentation columns](#llm-segmentation). Free token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (Read scope) — no card required. Also accept the Gemma license once at [huggingface.co/google/gemma-3-12b-it](https://huggingface.co/google/gemma-3-12b-it). |
| `DEBUG` | No | See [DEBUG mode](#debug-mode). |

A `.env` file works for local development, but the script doesn't load it automatically — export it into your shell first:

```bash
set -a && source .env && set +a
```

## Usage

```bash
python3 job_scraper.py --mode <full|incremental|smoke> [--title "..."]
```

### `--mode`

| Mode | Freshness | Search scope | Use for |
|---|---|---|---|
| `full` | past year (`py`) | all search terms × all ATS sites | occasional broad sweeps to backfill or catch anything missed |
| `incremental` (default) | past week (`pw`) | all search terms × all ATS sites | normal scheduled runs |
| `smoke` | past day (`pd`) | first search term × first ATS site only, 1 page | fast sanity check that search → scrape → sheet-write still works end to end |

### `--title`

Job title to search for. Repeatable — pass it more than once to search multiple titles in a single run:

```bash
python3 job_scraper.py --title "head of data" --title "director of analytics"
```

If `--title` is omitted entirely, the search defaults to `["head of data", "lead data scientist", "staff data scientist", "director of analytics"]`. With `--mode smoke`, only the first title is used regardless of how many are supplied (whether from `--title` or the default), since smoke mode is meant to stay fast.

Examples:

```bash
# Normal scheduled run (defaults to "head of data")
python3 job_scraper.py

# Search multiple titles in one run
python3 job_scraper.py --title "head of data" --title "lead data scientist"

# Quick check that everything's still wired up correctly
python3 job_scraper.py --mode smoke

# Broad sweep across the full past year
python3 job_scraper.py --mode full
```

## Output schema

Each row appended to the `Jobs` worksheet has these columns:

| Column | Description |
|---|---|
| Title | Job title |
| Company | Employer name |
| Location | Raw location text as scraped |
| Remote | `Yes` / `No` |
| Salary | Extracted from structured data or free text, if present |
| URL | Canonical job posting URL — used as the dedup key against existing rows |
| Date Found | Date the scraper found the posting (`YYYY-MM-DD`) |
| ATS | `Ashby`, `Greenhouse`, `Lever`, `Workday`, or `Rippling` |
| Job Description | Scraped description text, truncated to 5000 characters |
| Role Type | `IC`, `Lead (IC-leaning)`, `Manager`, or `Unclear` |
| Salary Lower | Bottom of the stated annual base range in USD as a plain number (e.g. `230000`); blank when no pay info |
| Salary Upper | Top of the stated annual base range; equals Salary Lower when a single figure is given |
| Office Days | `Fully remote`, a days-per-week count (`1`–`5`), `Hybrid (unspecified)`, or `Unknown` |
| Commute | Commute bucket from Oakland plus the office city, e.g. `BART/Bus (SF) — San Francisco`, `Bike (Berkeley/Emeryville)`, `Remote/Unknown` |
| Tooling | Stack rating plus the tools named, e.g. `Modern — dbt, Snowflake, Hex`; `Mixed`, `Legacy`, or `Unknown` |
| Specialties | Semicolon-separated domains, e.g. `Product Analytics; BI` |

## LLM segmentation

The last seven columns are produced by a single Gemma call per posting (`google/gemma-3-12b-it`, an open-weight model served via [HuggingFace Inference Providers](https://huggingface.co/docs/inference-providers)), requested in JSON mode. The prompt encodes the screening rubric — IC-leaning roles over people management, a normalized numeric salary range, commute buckets relative to Oakland (bike distance to Berkeley/Emeryville, BART to SF), a modern-stack rating anchored on Fivetran/dbt/Snowflake/Hex, and a specialty label. Edit `SEGMENT_PROMPT` in `job_scraper.py` to change the rubric.

Segmentation is best-effort: if the call fails or `HF_TOKEN` is unset, the row is still written with those columns blank. The script sleeps 2s between calls; the HuggingFace free tier is credit-metered rather than strictly rate-limited, so a very large run could exhaust the monthly free credit.

## DEBUG mode

Set `DEBUG=1` to:
- Cap results collected per (search term × ATS site) combination to `DEBUG_LIMIT_PER_COMBO` (3 by default).
- Limit each combo to a single search page.
- Print the first 2000 characters of the rendered HTML for the first scraped job, useful when a scraper's selectors stop matching.

## Known limitations

- Only Ashby, Greenhouse, Lever, Workday, and Rippling are supported — other ATS platforms aren't recognized.
- US-location filtering is heuristic (keyword and state-name matching against the scraped location text), not geocoding — ambiguous locations (e.g. just "Remote") are kept by default.
- Brave's `offset` parameter caps out at 9, so `MAX_PAGES` can't exceed 10 pages per search query.
- ATS sites are a hardcoded list in the script, not yet exposed as a CLI argument (search terms are, via `--title`).
