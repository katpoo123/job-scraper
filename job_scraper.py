"""
Searches Ashby, Greenhouse, and Lever job postings via Brave Search API,
scrapes each posting, and appends new results to a Google Sheet.

Setup:
  1. Set BRAVE_API_KEY env var (get from https://api.search.brave.com)
  2. Set GOOGLE_SHEET_ID env var
  3. Place a service account JSON key at credentials.json (or set
     GOOGLE_APPLICATION_CREDENTIALS env var to its path)
  4. Share the Google Sheet with the service account email
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
import gspread
from google.oauth2.service_account import Credentials

BRAVE_API_KEY = os.environ["BRAVE_API_KEY"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CREDENTIALS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_HEADERS = [
    "Title", "Company", "Location",
    "Remote", "Salary", "URL", "Date Found", "ATS", "Job Description",
]

MAX_PAGES = 10  # Brave's offset param maxes out at 9, so this can't exceed 10
RESULTS_PER_PAGE = 20
FRESHNESS = "pw"  # Brave freshness filter: pd=past day, pw=past week, pm=past month, py=past year
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true")
DEBUG_LIMIT_PER_COMBO = 3  # when DEBUG, cap jobs collected per (search term x ATS site)

SEARCH_TERMS = [
    "head of data",
    "head of analytics",
    "lead data scientist",
    "product data scientist",
    "finance data scientist",
]

# Maps URL domain fragment → ATS label written to the sheet
ATS_SITES = {
    "jobs.ashbyhq.com": "Ashby",
    "boards.greenhouse.io": "Greenhouse",
    "job-boards.greenhouse.io": "Greenhouse",
    "jobs.lever.co": "Lever",
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
    return False


def detect_ats(url: str) -> str:
    for domain, label in ATS_SITES.items():
        if domain in url:
            return label
    return ""


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------

def brave_search(query: str, offset: int = 0) -> list[dict]:
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
        "freshness": FRESHNESS,
    }
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
    if soup.find(string=re.compile(r"Current openings at", re.IGNORECASE)):
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
# Dispatcher
# ---------------------------------------------------------------------------

def scrape_job(browser_page: Page, url: str) -> dict:
    if "jobs.ashbyhq.com" in url:
        return scrape_ashby_job(browser_page, url)
    if "greenhouse.io" in url:
        return scrape_greenhouse_job(url)
    if "jobs.lever.co" in url:
        return scrape_lever_job(url)
    return {}


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


def ensure_headers(worksheet) -> set[str]:
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

def main():
    from datetime import date

    print("Connecting to Google Sheet...")
    worksheet = get_sheet(SHEET_ID)
    existing_urls = ensure_headers(worksheet)
    print(f"Sheet has {len(existing_urls)} existing job(s).")

    # Collect new URLs across all (search term × ATS site) combos, deduped
    new_urls: list[str] = []
    seen: set[str] = set(existing_urls)

    for term in SEARCH_TERMS:
        for domain in ATS_SITES:
            combo_count = 0
            query = f'site:{domain} "{term}"'
            max_pages = 1 if DEBUG else MAX_PAGES
            for search_page in range(max_pages):
                if DEBUG and combo_count >= DEBUG_LIMIT_PER_COMBO:
                    break
                offset = search_page  # Brave's offset is a page index (max 9), not a result index
                print(f'Searching: {query} (offset {offset})...')
                results = brave_search(query, offset=offset)
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
    new_rows = []

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
            ]
            new_rows.append(row)

        browser.close()

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"Appended {len(new_rows)} new job(s) to the sheet.")
    else:
        print("No new jobs to add.")


if __name__ == "__main__":
    main()
