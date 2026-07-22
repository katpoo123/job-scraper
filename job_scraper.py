"""
Searches Ashby, Greenhouse, and Lever job postings via Brave Search API,
scrapes each posting, and appends new results to a Google Sheet.

Setup:
  1. Set BRAVE_API_KEY env var (get from https://api.search.brave.com)
  2. Set GOOGLE_SHEET_ID env var
  3. Place a service account JSON key at credentials.json (or set
     GOOGLE_APPLICATION_CREDENTIALS env var to its path)
  4. Share the Google Sheet with the service account email

The env vars above (plus the optional HF_TOKEN) may be placed in a .env
file in the repo root; it is auto-loaded at startup, so no manual
`source .env` is needed. Real environment variables take precedence.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# Auto-load the .env sitting next to this script, so the required vars are
# available no matter which directory you launch from. Real environment
# variables still win over .env values.
load_dotenv(Path(__file__).resolve().parent / ".env")

BRAVE_API_KEY = os.environ["BRAVE_API_KEY"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CREDENTIALS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

# Optional — free token at https://huggingface.co/settings/tokens. When unset,
# the segmentation columns are left blank.
HF_TOKEN = os.environ.get("HF_TOKEN", "")
# Open-weight Gemma served through HuggingFace Inference Providers, which expose
# an OpenAI-compatible chat endpoint. Gemma is a gated model — the token's account
# must accept the license once at https://huggingface.co/google/gemma-3-12b-it.
# Swap for gemma-3-4b-it (cheaper/faster) or gemma-3-27b-it (higher quality); run
# GET /v1/models against the router to see which variants your providers serve.
HF_MODEL = "google/gemma-3-12b-it"
HF_URL = "https://router.huggingface.co/v1/chat/completions"
SEGMENT_DELAY_SECONDS = 2  # HF free tier is credit-metered; small pause to be safe

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_HEADERS = [
    "Title", "Company", "Location",
    "Remote", "Salary", "URL", "Date Found", "ATS", "Job Description",
    "Role Type", "Salary Lower", "Salary Upper", "Office Days", "Commute", "Tooling", "Specialties",
]
# JSON keys returned by segment_job, in the same order as the sheet columns above
SEGMENT_FIELDS = ["role_type", "salary_lower", "salary_upper", "office_days", "commute", "tooling", "specialties"]

MAX_PAGES = 10  # Brave's offset param maxes out at 9, so this can't exceed 10
RESULTS_PER_PAGE = 20
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true")
DEBUG_LIMIT_PER_COMBO = 3  # when DEBUG, cap jobs collected per (search term x ATS site)

# Brave freshness filter per --mode: pd=past day, pw=past week, py=past year.
MODE_FRESHNESS = {
    "full": "py",
    "incremental": "pw",
    "smoke": "pd",
}

# Used when --title isn't passed at all.
DEFAULT_SEARCH_TERMS = [
    "head of data",
    "lead data scientist",
    "staff data scientist",
    "director of analytics",
]

# Maps URL domain fragment → ATS label written to the sheet
ATS_SITES = {
    "jobs.ashbyhq.com": "Ashby",
    "boards.greenhouse.io": "Greenhouse",
    "job-boards.greenhouse.io": "Greenhouse",
    "jobs.lever.co": "Lever",
    "myworkdayjobs.com": "Workday",
    "ats.rippling.com": "Rippling",
}

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Canonical US state abbreviations for City/State parsing
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
STATE_ABBREVS = {v for v in US_STATES.values()}

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def is_valid_job_url(url: str) -> bool:
    """Return True only for URLs that point to individual job postings."""
    if "jobs.ashbyhq.com" in url:
        # jobs.ashbyhq.com/<company>/<uuid>
        return bool(_UUID_RE.search(url))
    if "greenhouse.io" in url:
        # (job-)boards.greenhouse.io/<company>/jobs/<numeric-id>
        return bool(re.search(r"greenhouse\.io/[^/]+/jobs/\d+", url))
    if "jobs.lever.co" in url:
        # jobs.lever.co/<company>/<uuid>
        return bool(_UUID_RE.search(url))
    if "myworkdayjobs.com" in url:
        # <company>.wd<N>.myworkdayjobs.com/<board>/job/<location>/<title-slug>
        return bool(re.search(r"myworkdayjobs\.com/[^?#]+/job/[^?#]+", url))
    if "ats.rippling.com" in url:
        # ats.rippling.com/<board>/jobs/<uuid>
        return bool(_UUID_RE.search(url))
    return False


def detect_ats(url: str) -> str:
    for domain, label in ATS_SITES.items():
        if domain in url:
            return label
    return ""


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------

def brave_search(query: str, offset: int = 0, freshness: Optional[str] = None) -> list[dict]:
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {
        "q": query,
        "count": RESULTS_PER_PAGE,
        "offset": offset,
        "search_lang": "en",
        "country": "us",
    }
    if freshness:
        params["freshness"] = freshness
    resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("web", {}).get("results", [])


# ---------------------------------------------------------------------------
# Shared extraction helpers
# ---------------------------------------------------------------------------

def _extract_jsonld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, IndexError):
            continue
    return {}


NON_US_MARKERS = [
    "canada", "united kingdom", "uk", "england", "scotland", "wales", "ireland",
    "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium",
    "switzerland", "sweden", "norway", "denmark", "finland", "poland", "austria",
    "india", "china", "japan", "singapore", "philippines", "australia",
    "new zealand", "mexico", "brazil", "argentina", "colombia", "chile",
    "emea", "apac", "latam", "europe", "asia",
]


def is_us_location(location: str) -> bool:
    """Best-effort check that a job's location is US-based (or unspecified)."""
    if not location:
        return True  # unknown — don't drop for lack of data
    loc = location.lower()
    if any(marker in loc for marker in ["united states", "usa", "u.s.a", "u.s."]):
        return True
    if any(re.search(rf"\b{re.escape(abbr)}\b", location) for abbr in STATE_ABBREVS):
        return True
    if any(state in loc for state in US_STATES):
        return True
    if any(marker in loc for marker in NON_US_MARKERS):
        return False
    return True  # ambiguous (e.g. just "Remote") — keep by default


def _salary_from_text(text: str) -> str:
    patterns = [
        r"\$[\d,]+(?:k)?\s*[-–—to]+\s*\$[\d,]+(?:k)?(?:\s*(?:USD|CAD|per year|\/yr|annually))?",
        r"\$[\d,]+(?:k)?(?:\s*(?:USD|CAD|per year|\/yr|annually))",
        r"[\d,]{5,}\s*[-–—]\s*[\d,]{5,}(?:\s*(?:USD|CAD))?",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return ""


def _extract_salary(soup: BeautifulSoup, jsonld: dict) -> str:
    bs = jsonld.get("baseSalary", {})
    if isinstance(bs, dict):
        value = bs.get("value", {})
        if isinstance(value, dict):
            lo = value.get("minValue", "")
            hi = value.get("maxValue", "")
            currency = bs.get("currency", "USD")
            unit = value.get("unitText", "")
            if lo and hi:
                if isinstance(lo, (int, float)):
                    return f"${lo:,} – ${hi:,} {currency} {unit}".strip()
                return f"${lo} – ${hi} {currency}".strip()
            if lo:
                return f"${lo} {currency}".strip()
    return _salary_from_text(soup.get_text(" ", strip=True))


# ---------------------------------------------------------------------------
# Ashby scraper (React app — requires Playwright)
# ---------------------------------------------------------------------------

def scrape_ashby_job(page: Page, url: str) -> dict:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        return {}

    # Closed/missing postings redirect off the individual-job URL pattern —
    # either to the company's job list (no uuid) or off Ashby entirely.
    if not _UUID_RE.search(page.url):
        return {}

    JOB_SELECTORS = [
        '[class*="jobPosting"]',
        '[class*="job-posting"]',
        '[class*="JobPosting"]',
        '[class*="ashby-job"]',
        '[class*="job-details"]',
        '[class*="JobDetails"]',
        "h1",
    ]
    for selector in JOB_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=10000)
            break
        except PlaywrightTimeoutError:
            continue

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(string=re.compile(r"Job not found", re.IGNORECASE)):
        return {}
    jsonld = _extract_jsonld(soup)

    title = _ashby_title(soup, jsonld)
    company = _ashby_company(soup, url, jsonld)
    location_raw, remote = _ashby_location_remote(soup, jsonld)
    if not is_us_location(location_raw):
        return {}
    salary = _extract_salary(soup, jsonld)
    description = _ashby_description(soup, jsonld)

    return {
        "title": title,
        "company": company,
        "location": location_raw,
        "remote": remote,
        "salary": salary,
        "description": description,
    }


def _ashby_title(soup: BeautifulSoup, jsonld: dict) -> str:
    if jsonld.get("title"):
        return jsonld["title"].strip()
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    title_tag = soup.find("title")
    if title_tag:
        text = re.sub(r"\s+at\s+.+$", "", title_tag.get_text(strip=True), flags=re.IGNORECASE)
        if text:
            return text
    for selector in ["h1.job-title", "h1[class*='title']", "h1[class*='Title']", "h1"]:
        tag = soup.select_one(selector)
        if tag:
            return tag.get_text(strip=True)
    return ""


def _ashby_company(soup: BeautifulSoup, url: str, jsonld: dict) -> str:
    org = jsonld.get("hiringOrganization", {})
    if isinstance(org, dict) and org.get("name"):
        return org["name"].strip()
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return og["content"].strip()
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)", url)
    if m:
        return m.group(1).replace("-", " ").title()
    return ""


def _ashby_location_remote(soup: BeautifulSoup, jsonld: dict) -> tuple[str, str]:
    location_text = ""

    jl = jsonld.get("jobLocation", {})
    if isinstance(jl, dict):
        addr = jl.get("address", {})
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality", ""), addr.get("addressRegion", ""), addr.get("addressCountry", "")]
            location_text = ", ".join(p for p in parts if p)
    elif isinstance(jl, list) and jl:
        addr = jl[0].get("address", {})
        parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
        location_text = ", ".join(p for p in parts if p)

    if not location_text:
        alr = jsonld.get("applicantLocationRequirements")
        if alr:
            if isinstance(alr, list):
                alr = alr[0]
            location_text = alr.get("name", "") if isinstance(alr, dict) else str(alr)

    if not location_text:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            m = re.search(r"Location[:\s]+([^\n•|·]+)", og_desc["content"], re.IGNORECASE)
            if m:
                location_text = m.group(1).strip()

    if not location_text:
        for selector in ["[class*='location']", "[class*='Location']", "[data-testid*='location']", "[class*='job-location']"]:
            tag = soup.select_one(selector)
            if tag:
                location_text = tag.get_text(separator=" ", strip=True)
                break

    if not location_text:
        body_text = soup.get_text(" ", strip=True)
        for pattern in [r"Location[:\s]+([^\n•|·]{3,80})", r"(Remote[\w\s,\-]{0,60})"]:
            m = re.search(pattern, body_text, re.IGNORECASE)
            if m:
                location_text = m.group(1).strip()
                break

    is_remote_type = jsonld.get("jobLocationType", "") == "TELECOMMUTE"
    remote_in_text = bool(re.search(r"\bremote\b", location_text, re.IGNORECASE))
    remote = "Yes" if (is_remote_type or remote_in_text) else "No"
    return location_text[:200], remote


def _ashby_description(soup: BeautifulSoup, jsonld: dict) -> str:
    if jsonld.get("description"):
        return BeautifulSoup(jsonld["description"], "html.parser").get_text(" ", strip=True)[:5000]
    for selector in ["[class*='description']", "[class*='Description']", "[class*='job-details']", "[class*='JobDetails']", "article", "main"]:
        tag = soup.select_one(selector)
        if tag:
            text = tag.get_text(" ", strip=True)
            if len(text) > 200:
                return text[:5000]
    return soup.get_text(" ", strip=True)[:5000]


# ---------------------------------------------------------------------------
# Greenhouse scraper (server-rendered — requests only)
# ---------------------------------------------------------------------------

def scrape_greenhouse_job(url: str) -> dict:
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return {}

    # Closed/missing postings redirect off the individual-job URL pattern —
    # either to the company board (?error=true, bare company root) or
    # entirely off Greenhouse to the company's own careers page.
    if not re.search(r"greenhouse\.io/[^/]+/jobs/\d+", resp.url):
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    jsonld = _extract_jsonld(soup)
    # A genuine board page shows "Current openings at <company>" as a visible
    # heading. Greenhouse's Remix app also embeds that phrase in a <script> data
    # blob on every real job page, so match visible text only — otherwise every
    # live posting gets dropped as if it were a board page.
    if any(
        node.parent.name not in ("script", "style")
        for node in soup.find_all(string=re.compile(r"Current openings at", re.IGNORECASE))
    ):
        return {}

    # Title
    title = ""
    if jsonld.get("title"):
        title = jsonld["title"].strip()
    else:
        for sel in ["h1.app-title", "h2.job-title", "h1", "h2"]:
            tag = soup.select_one(sel)
            if tag:
                title = tag.get_text(strip=True)
                break

    # Company
    company = ""
    org = jsonld.get("hiringOrganization", {})
    if isinstance(org, dict) and org.get("name"):
        company = org["name"].strip()
    else:
        m = re.search(r"boards\.greenhouse\.io/([^/]+)", url)
        if m:
            company = m.group(1).replace("-", " ").replace("_", " ").title()

    # Location
    location_raw = ""
    if jsonld.get("jobLocation"):
        location_raw, remote = _ashby_location_remote(soup, jsonld)
    else:
        for sel in [".location", "p.location", ".job__location", "[class*='location']"]:
            tag = soup.select_one(sel)
            if tag:
                location_raw = tag.get_text(strip=True)
                break
        remote = "Yes" if re.search(r"\bremote\b", location_raw, re.IGNORECASE) else "No"

    if not is_us_location(location_raw):
        return {}
    salary = _extract_salary(soup, jsonld)

    # Description
    description = ""
    if jsonld.get("description"):
        description = BeautifulSoup(jsonld["description"], "html.parser").get_text(" ", strip=True)[:5000]
    else:
        for sel in ["#content", ".job__description", "[class*='job-description']", "article", "main"]:
            tag = soup.select_one(sel)
            if tag:
                text = tag.get_text(" ", strip=True)
                if len(text) > 200:
                    description = text[:5000]
                    break

    return {
        "title": title,
        "company": company,
        "location": location_raw,
        "remote": remote,
        "salary": salary,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Lever scraper (server-rendered — requests only)
# ---------------------------------------------------------------------------

def scrape_lever_job(url: str) -> dict:
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return {}

    # Closed/missing postings redirect off the individual-job URL pattern —
    # either to the company's job list (no uuid) or off Lever entirely.
    if not _UUID_RE.search(resp.url):
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    jsonld = _extract_jsonld(soup)

    # Title
    title = ""
    if jsonld.get("title"):
        title = jsonld["title"].strip()
    else:
        for sel in ['h2[data-qa="posting-name"]', ".posting-headline h2", "h2", "h1"]:
            tag = soup.select_one(sel)
            if tag:
                title = tag.get_text(strip=True)
                break

    # Company
    company = ""
    org = jsonld.get("hiringOrganization", {})
    if isinstance(org, dict) and org.get("name"):
        company = org["name"].strip()
    else:
        m = re.search(r"jobs\.lever\.co/([^/]+)", url)
        if m:
            company = m.group(1).replace("-", " ").replace("_", " ").title()

    # Location
    location_raw = ""
    if jsonld.get("jobLocation"):
        location_raw, remote = _ashby_location_remote(soup, jsonld)
    else:
        for sel in ['.posting-categories .location', '[data-qa="posting-location"]', "[class*='location']"]:
            tag = soup.select_one(sel)
            if tag:
                location_raw = tag.get_text(strip=True)
                break
        remote = "Yes" if re.search(r"\bremote\b", location_raw, re.IGNORECASE) else "No"

    if not is_us_location(location_raw):
        return {}
    salary = _extract_salary(soup, jsonld)

    # Description
    description = ""
    for sel in [".posting-description", '[data-qa="posting-description"]', "[class*='posting-content']", "main"]:
        tag = soup.select_one(sel)
        if tag:
            text = tag.get_text(" ", strip=True)
            if len(text) > 200:
                description = text[:5000]
                break

    return {
        "title": title,
        "company": company,
        "location": location_raw,
        "remote": remote,
        "salary": salary,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Workday scraper (SPA — requires Playwright)
# ---------------------------------------------------------------------------

def scrape_workday_job(page: Page, url: str) -> dict:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        return {}

    # Closed/missing postings redirect off the individual-job URL pattern —
    # back to the board root or search page (no /job/ segment).
    if not re.search(r"myworkdayjobs\.com/[^?#]+/job/", page.url):
        return {}

    for selector in [
        '[data-automation-id="jobPostingHeader"]',
        '[data-automation-id="jobPostingPage"]',
        "h1",
    ]:
        try:
            page.wait_for_selector(selector, timeout=10000)
            break
        except PlaywrightTimeoutError:
            continue

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    soup = BeautifulSoup(page.content(), "html.parser")
    # Workday is an SPA, so removed postings keep the /job/ URL and render an
    # error message instead ("The page you are looking for doesn't exist." /
    # "no longer available")
    if soup.find(string=re.compile(
        r"(job (is )?(not available|no longer available)"
        r"|page you are looking for do(es)?n.t exist)", re.IGNORECASE)):
        return {}
    jsonld = _extract_jsonld(soup)

    # Title
    title = ""
    if jsonld.get("title"):
        title = jsonld["title"].strip()
    else:
        tag = soup.select_one('[data-automation-id="jobPostingHeader"]') or soup.find("h1")
        if tag:
            title = tag.get_text(strip=True)

    # Company
    company = ""
    org = jsonld.get("hiringOrganization", {})
    if isinstance(org, dict) and org.get("name"):
        company = org["name"].strip()
    else:
        m = re.search(r"https?://([^./]+)\.wd\d+\.myworkdayjobs\.com", url)
        if m:
            company = m.group(1).replace("-", " ").replace("_", " ").title()

    # Location
    location_raw, remote = _ashby_location_remote(soup, jsonld)
    if not location_raw:
        tag = soup.select_one('[data-automation-id="locations"]')
        if tag:
            location_raw = tag.get_text(" ", strip=True)[:200]
            if re.search(r"\bremote\b", location_raw, re.IGNORECASE):
                remote = "Yes"

    if not is_us_location(location_raw):
        return {}
    salary = _extract_salary(soup, jsonld)

    # Description
    description = ""
    tag = soup.select_one('[data-automation-id="jobPostingDescription"]')
    if tag:
        description = tag.get_text(" ", strip=True)[:5000]
    else:
        description = _ashby_description(soup, jsonld)

    return {
        "title": title,
        "company": company,
        "location": location_raw,
        "remote": remote,
        "salary": salary,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Rippling scraper (SPA — requires Playwright)
# ---------------------------------------------------------------------------

def scrape_rippling_job(page: Page, url: str) -> dict:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        return {}

    # Closed/missing postings redirect off the individual-job URL pattern —
    # to the board's job list (no uuid) or off Rippling entirely.
    if not _UUID_RE.search(page.url):
        return {}

    for selector in ["h1", "[class*='job-title']", "[class*='JobTitle']"]:
        try:
            page.wait_for_selector(selector, timeout=10000)
            break
        except PlaywrightTimeoutError:
            continue

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    soup = BeautifulSoup(page.content(), "html.parser")
    if soup.find(string=re.compile(r"(job not found|no longer accepting applications)", re.IGNORECASE)):
        return {}
    jsonld = _extract_jsonld(soup)

    title = _ashby_title(soup, jsonld)

    # Company
    company = ""
    org = jsonld.get("hiringOrganization", {})
    if isinstance(org, dict) and org.get("name"):
        company = org["name"].strip()
    else:
        og = soup.find("meta", property="og:site_name")
        if og and og.get("content"):
            company = og["content"].strip()
        else:
            m = re.search(r"ats\.rippling\.com/([^/]+)", url)
            if m:
                company = m.group(1).replace("-", " ").replace("_", " ").title()

    location_raw, remote = _ashby_location_remote(soup, jsonld)
    if not is_us_location(location_raw):
        return {}
    salary = _extract_salary(soup, jsonld)
    description = _ashby_description(soup, jsonld)

    return {
        "title": title,
        "company": company,
        "location": location_raw,
        "remote": remote,
        "salary": salary,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def scrape_job(browser_page: Page, url: str) -> dict:
    if "jobs.ashbyhq.com" in url:
        job = scrape_ashby_job(browser_page, url)
    elif "greenhouse.io" in url:
        job = scrape_greenhouse_job(url)
    elif "jobs.lever.co" in url:
        job = scrape_lever_job(url)
    elif "myworkdayjobs.com" in url:
        job = scrape_workday_job(browser_page, url)
    elif "ats.rippling.com" in url:
        job = scrape_rippling_job(browser_page, url)
    else:
        job = {}
    # A posting with no extractable title is a dead/error page some check
    # above didn't recognize — never write it to the sheet.
    if job and not job.get("title"):
        return {}
    return job


# ---------------------------------------------------------------------------
# LLM segmentation (Gemma via HuggingFace free tier)
# ---------------------------------------------------------------------------

SEGMENT_PROMPT = """\
You are screening job postings for a senior data person based in Oakland, CA.
Classify the posting below and return JSON with exactly these keys:

role_type — management vs individual contributor:
- "IC": pure individual-contributor role, no direct reports
- "Lead (IC-leaning)": lead/staff/principal or player-coach role that is primarily
  hands-on but might involve some management
- "Manager": people-management-first role
- "Unclear": the posting doesn't say

salary_lower / salary_upper — the annual base salary range in USD, from the
salary field or any pay range in the description, as plain integers with no
currency symbols or commas (e.g. "230000" and "285000"):
- when a single figure is stated, use it for both
- convert hourly/monthly figures to annual equivalents
- use "" for both when no pay information is stated

office_days — required in-office days per week:
- "Fully remote" if no office attendance is required
- "1"–"5" when a specific count is stated (e.g. "3" for 3 days/week hybrid)
- "Hybrid (unspecified)" when hybrid but no count is given
- "Unknown" when the posting doesn't say

commute — where the office is relative to Oakland, formatted "<bucket> — <city>"
(just the bucket when no city is stated):
- "Oakland": office in Oakland
- "Bike (Berkeley/Emeryville)": office in Berkeley or Emeryville
- "BART/Bus (SF)": office in San Francisco
- "Other Bay Area": elsewhere in the SF Bay Area
- "Out of area": outside the Bay Area
- "Remote/Unknown": fully remote or no location given

tooling — rate the data stack, formatted "<rating> — <tools named in posting>"
(just the rating when no tools are named):
- "Modern": stack centers on tools like Fivetran, dbt, Snowflake, Hex, BigQuery,
  Databricks, Airflow, or similar modern data platforms
- "Mixed": both modern and legacy tools
- "Legacy": mostly older tools like Tableau, Excel, SSIS, SSRS
- "Unknown": no tools named

specialties — the role's main domain(s), semicolon-separated, e.g.
"Machine Learning; Product Analytics; BI; AI; Risk; Data Engineering;
Experimentation". Pick the closest match(es) or name the domain yourself.

Posting:
"""


def segment_job(job: dict) -> dict:
    """Classify a scraped job into the segmentation columns via Gemma (HuggingFace)."""
    if not HF_TOKEN:
        return {}

    posting = json.dumps({
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "remote": job.get("remote", ""),
        "salary": job.get("salary", ""),
        "description": job.get("description", ""),
    })
    payload = {
        "model": HF_MODEL,
        # Gemma has no system role — the whole instruction goes in the user turn.
        "messages": [{"role": "user", "content": SEGMENT_PROMPT + posting}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    data = {}
    for attempt in range(2):
        try:
            resp = requests.post(
                HF_URL,
                headers={"Authorization": f"Bearer {HF_TOKEN}"},
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429 and attempt == 0:
                print("    HuggingFace rate limit hit; waiting 60s...")
                time.sleep(60)
                continue
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            if not text:
                # Some providers return null/empty content (filtered or empty
                # completion); treat it as a failure, not a crash.
                raise ValueError("empty completion content")
            # Gemma occasionally wraps JSON in ```json fences — strip them.
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            data = json.loads(text)
            break
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            # json.JSONDecodeError is a subclass of ValueError, so it's covered.
            print(f"    Segmentation failed: {e}")
            break

    time.sleep(SEGMENT_DELAY_SECONDS)  # stay under the free-tier rate limit
    # A provider that ignores response_format can return valid non-object JSON
    # (an array or scalar); fall back to blank columns rather than crashing.
    if not isinstance(data, dict):
        data = {}
    out = {field: str(data.get(field, "")) for field in SEGMENT_FIELDS}
    # The model sometimes answers "Unknown" despite the schema — keep the
    # salary columns strictly numeric or blank.
    for field in ("salary_lower", "salary_upper"):
        out[field] = re.sub(r"\D", "", out[field])
    return out


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_sheet(sheet_id: str):
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    try:
        worksheet = spreadsheet.worksheet("Jobs")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Jobs", rows=1000, cols=len(SHEET_HEADERS))
    return worksheet


def append_row_with_retry(worksheet, row: list, attempts: int = 3) -> bool:
    """Append one row, retrying on transient API/network errors.

    Long scrape runs leave the Sheets connection idle for many minutes, and the
    first write afterwards can hit a connection reset.
    """
    for attempt in range(attempts):
        try:
            worksheet.append_rows([row], value_input_option="USER_ENTERED")
            return True
        except (requests.RequestException, gspread.exceptions.APIError, ConnectionError) as e:
            if attempt == attempts - 1:
                print(f"    Sheet write failed after {attempts} attempts: {e}")
                return False
            time.sleep(5 * (attempt + 1))
    return False


def ensure_headers(worksheet) -> set[str]:
    # Sheets created before the segmentation columns existed have a narrower grid
    if worksheet.col_count < len(SHEET_HEADERS):
        worksheet.resize(cols=len(SHEET_HEADERS))
    existing = worksheet.get_all_values()
    if not existing:
        worksheet.append_row(SHEET_HEADERS)
        return set()
    if existing[0] != SHEET_HEADERS:
        worksheet.update("A1", [SHEET_HEADERS])
    url_col = SHEET_HEADERS.index("URL")
    return {row[url_col] for row in existing[1:] if len(row) > url_col and row[url_col]}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Ashby, Greenhouse, and Lever job postings and append new results to a Google Sheet."
    )
    parser.add_argument(
        "--mode",
        choices=["full", "incremental", "smoke"],
        default="incremental",
        help=(
            "full: past-year freshness, all search terms and ATS sites (broadest, slowest); "
            "incremental: past-week freshness, all search terms and ATS sites (default, for scheduled runs); "
            "smoke: past-day freshness, first search term and first ATS site only, 1 page "
            "(fast end-to-end test of search -> scrape -> sheet write)"
        ),
    )
    parser.add_argument(
        "--title",
        action="append",
        help=(
            "Job title to search for; repeatable, e.g. --title \"head of data\" "
            "--title \"director of analytics\". Defaults to "
            f"{DEFAULT_SEARCH_TERMS!r} if omitted."
        ),
    )
    return parser.parse_args()


def main():
    from datetime import date

    args = parse_args()
    freshness = MODE_FRESHNESS[args.mode]
    title_terms = args.title if args.title else DEFAULT_SEARCH_TERMS
    search_terms = title_terms[:1] if args.mode == "smoke" else title_terms
    ats_domains = list(ATS_SITES)[:1] if args.mode == "smoke" else list(ATS_SITES)
    mode_max_pages = 1 if args.mode == "smoke" else MAX_PAGES

    print(f"Mode: {args.mode} (freshness={freshness or 'none'})")
    if not HF_TOKEN:
        print("HF_TOKEN not set — segmentation columns will be left blank.")
    print("Connecting to Google Sheet...")
    worksheet = get_sheet(SHEET_ID)
    existing_urls = ensure_headers(worksheet)
    print(f"Sheet has {len(existing_urls)} existing job(s).")

    # Collect new URLs across all (search term × ATS site) combos, deduped
    new_urls: list[str] = []
    seen: set[str] = set(existing_urls)

    for term in search_terms:
        for domain in ats_domains:
            combo_count = 0
            query = f'site:{domain} "{term}"'
            max_pages = 1 if DEBUG else mode_max_pages
            for search_page in range(max_pages):
                if DEBUG and combo_count >= DEBUG_LIMIT_PER_COMBO:
                    break
                offset = search_page  # Brave's offset is a page index (max 9), not a result index
                print(f'Searching: {query} (offset {offset})...')
                results = brave_search(query, offset=offset, freshness=freshness)
                first_url = results[0].get("url", "") if results else ""
                print(f"  Got {len(results)} result(s). First URL: {first_url}")
                if not results:
                    break
                for r in results:
                    if DEBUG and combo_count >= DEBUG_LIMIT_PER_COMBO:
                        break
                    url = r.get("url", "")
                    if is_valid_job_url(url) and url not in seen:
                        seen.add(url)
                        new_urls.append(url)
                        combo_count += 1
                time.sleep(1)

    print(f"\nFound {len(new_urls)} new job URL(s) to scrape.")
    today = date.today().isoformat()
    appended = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        browser_page = context.new_page()

        for i, url in enumerate(new_urls):
            ats = detect_ats(url)
            print(f"  [{ats}] Scraping: {url}")
            job = scrape_job(browser_page, url)
            if DEBUG and i == 0:
                print("\n--- DEBUG: first 2000 chars of rendered HTML ---")
                print(browser_page.content()[:2000])
                print("--- END DEBUG ---\n")
            if not job:
                continue
            segments = segment_job(job)
            row = [
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("remote", ""),
                job.get("salary", ""),
                url,
                today,
                ats,
                job.get("description", ""),
            ] + [segments.get(field, "") for field in SEGMENT_FIELDS]
            # Append immediately so a crash mid-run doesn't lose prior rows;
            # already-written rows are skipped by dedupe on the next run.
            if append_row_with_retry(worksheet, row):
                appended += 1
                print(f"    Added to sheet ({appended} so far).")

        browser.close()

    if appended:
        print(f"Appended {appended} new job(s) to the sheet.")
    else:
        print("No new jobs to add.")


if __name__ == "__main__":
    main()
