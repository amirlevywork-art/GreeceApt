# scrape_xe.py
#
# XE.gr apartment listings scraper (Athens-focused).
# - Collects listing URLs from search results pages (scroll + pagination)
# - Opens each listing page and extracts details
# - Writes to data/listings.json (deduped by normalized listing URL)
#
# Output fields (per listing):
#   url, source, scraped_at, Headline, price_eur, price_per_sqm, area_sqm,
#   neighborhood, area, address_raw,
#   bedrooms, bathrooms, floor, year_built, renovation_year, energy_class,
#   photos_count, photo_urls, publication_date
#
# Rules:
# - We do NOT store "city"/"municipality" as a separate field (Athens-only project).
# - neighborhood = single canonical scoring field
# - area = ONLY the geographic prefix if present in neighborhood (Ano/Kato/Nea/Neo); else None
# - IMPORTANT: "Agia/Agios/Agioi" are NOT treated as prefixes (they remain part of the name)
#
# Examples:
#   "Ano Kypseli"    -> neighborhood="Kypseli",  area="Ano"
#   "Kato Patisia"   -> neighborhood="Patisia",  area="Kato"
#   "Agia Eleousa"   -> neighborhood="Agia Eleousa", area=None
#   "Zografou Ilisia"-> neighborhood="Zografou", area=None   (cleaned municipality)
#   "Kallithea Agia Eleousa" -> neighborhood="Kallithea", area=None (cleaned municipality)

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError

from greeceapt.cookies.cookie_manager import ensure_cookies
from greeceapt.utils import url_builder
from greeceapt.utils.helpers import (
    normalize_listing_url,
    AREA_PREFIXES,
    extract_area_prefix,
    strip_area_prefix,
)

# -----------------------------
# Paths / constants
# -----------------------------

SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = DATA_DIR / "state.json"
COOKIES_PATH = DATA_DIR / "cookies.json"

BASE_URL = "https://www.xe.gr"

# Playwright navigation timeout for listing pages (milliseconds)
NAVIGATION_TIMEOUT_MS = 60_000

# Max concurrent listing-detail page loads
DETAIL_CONCURRENCY = 5


# -----------------------------
# State (pagination)
# -----------------------------

def load_last_page() -> int:
    """Read last scanned page number from data/state.json. Returns 0 if missing/invalid."""
    if not STATE_PATH.exists():
        return 0
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get("last_page", 0)) or 0
    except Exception:
        return 0


def save_last_page(page_num: int) -> None:
    """Persist last scanned page number to data/state.json."""
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump({"last_page": int(page_num)}, f, ensure_ascii=False, indent=2)
    print(f"[STATE] Saved last_page={page_num} -> {STATE_PATH}")


# -----------------------------
# Bot verification (manual)
# -----------------------------

# Text snippets that indicate XE's "confirm you are human" / captcha page
VERIFICATION_MARKERS = [
    "Let's confirm you are human",
    "Complete the security check",
    "No robots allowed",
    "Βεβαιώσου πως είσαι μέρος της ανθρωπότητας",  # Greek: "Make sure you're part of humanity"
]


async def is_verification_page(page: Page) -> bool:
    """Check if the current page shows XE's human verification / captcha.
    Uses DOM text locators instead of fetching the full HTML."""
    try:
        for marker in VERIFICATION_MARKERS:
            if await page.get_by_text(marker, exact=False).count() > 0:
                return True
        return False
    except Exception:
        return False


async def wait_for_user_to_solve_verification(
    page: Page,
    success_selector: str = 'a[data-testid="property-ad-url"]',
    timeout_ms: int = 120_000,
) -> None:
    """
    Pause and wait for the user to solve the captcha in the browser.
    Uses asyncio.to_thread for input() so the event loop stays responsive.
    After user presses Enter, waits for success_selector to appear.
    """
    print("\n" + "=" * 60)
    print("[VERIFY] XE is showing a security check (captcha / human verification).")
    print("[VERIFY] Complete the check in the BROWSER window.")
    print("[VERIFY] Do NOT press Enter until you have finished solving it.")
    print("[VERIFY] When the page is ready again, press Enter here.")
    print("=" * 60)
    await asyncio.to_thread(input, "\n[VERIFY] Press Enter when done: ")

    try:
        await page.wait_for_selector(success_selector, timeout=timeout_ms)
        print("[VERIFY] Page ready, continuing.\n")
    except TimeoutError:
        print("[WARN] Success selector not found after timeout; continuing anyway.\n")


async def ensure_no_verification_blocking(page: Page) -> None:
    """
    If XE is showing a verification/captcha page, pause until the user solves it.
    Call this before each page operation (scroll, extract) so we never proceed
    while blocked by a captcha.
    """
    if await is_verification_page(page):
        await wait_for_user_to_solve_verification(page)


# -----------------------------
# URL helpers / dedupe
# -----------------------------

def normalize_property_url(href: str) -> str:
    """Normalize a listing href to a stable key (no query/fragment)."""
    return normalize_listing_url(urljoin(BASE_URL, href))

def dedupe_listing_urls(urls: list[str]) -> list[str]:
    """Deduplicate listing URLs by normalized path."""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = normalize_listing_url(u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    print(f"[DEDUPE] {len(urls)} raw URLs -> {len(out)} unique listing URLs")
    return out


# -----------------------------
# Parsing helpers
# -----------------------------

def parse_number(text: str) -> int | None:
    """Parse int from strings like '59.000 €' or '€59,000'."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text)
    cleaned = cleaned.replace(".", "").replace(",", "")
    return int(cleaned) if cleaned.isdigit() else None


def parse_first_int(text: str) -> int | None:
    """Extract the first integer token from free-form text (e.g. 'WC: 1 + 1')."""
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def parse_area_from_title(title: str) -> int | None:
    """Extract area (sq.m.) from a title like 'Apartment 47 sq.m.'."""
    if not title:
        return None
    m = re.search(r"(\d+)\s*sq\.m\.?", title, re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_floor(text: str) -> int | str | None:
    """Normalize floor: basement=-1, ground=0, otherwise integer if present."""
    if not text:
        return None

    parts = text.split(":", 1)
    val = parts[1].strip() if len(parts) > 1 else text.strip()
    v = val.lower()

    if "semi-basement" in v or "basement" in v:
        return -1
    if "elevated ground floor" in v or "ground floor" in v or "mezzanine" in v:
        return 0

    m = re.search(r"\d+", v)
    if m:
        return int(m.group())
    return val


def normalize_energy_class(raw: str | None) -> str | None:
    """
    Normalize energy class to:
      A+, A, A-, B+, B, C, D, E, F, G, Unknown

    Handles Greek letters often used in Greece EPC labels:
      Α, Β, Γ, Δ, Ε, Ζ, Η => A, B, C, D, E, F, G
    """
    if not raw:
        return None

    s = raw.strip()
    if not s:
        return None

    # If passed a full label like "Energy Class: Η"
    if ":" in s:
        s = s.split(":", 1)[1].strip()

    s = s.replace("–", "-").replace("—", "-").strip()
    s_upper = s.upper().replace(" ", "")

    allowed = {"A+", "A", "A-", "B+", "B", "C", "D", "E", "F", "G", "UNKNOWN"}
    if s_upper in allowed:
        return "Unknown" if s_upper == "UNKNOWN" else s_upper

    greek_map = {
        "Α+": "A+",
        "Α": "A",
        "Β+": "B+",
        "Β": "B",
        "Γ": "C",
        "Δ": "D",
        "Ε": "E",
        "Ζ": "F",
        "Η": "G",
    }

    if s_upper in greek_map:
        return greek_map[s_upper]

    # Fallback: first character mapping if it's Greek
    if s and s[0] in greek_map:
        return greek_map[s[0]]

    return s  # keep as-is if unknown format


# -----------------------------
# Location parsing -> neighborhood + prefix-only area
# -----------------------------

def parse_location_from_address_element(addr_el) -> tuple[str | None, str | None, str | None]:
    """
    Returns:
      municipality, area_raw, address_raw

    XE often has separate <a> links (municipality + area).
    If not, fallback to text parsing.
    """
    if not addr_el:
        return None, None, None

    address_raw = addr_el.get_text(" ", strip=True)

    links = [a.get_text(" ", strip=True) for a in addr_el.select("a") if a.get_text(strip=True)]
    if len(links) >= 2:
        return links[0], links[1], address_raw
    if len(links) == 1:
        return links[0], None, address_raw

    txt = address_raw

    # City (Area)
    if "(" in txt and txt.endswith(")"):
        city, rest = txt.split("(", 1)
        return city.strip() or None, rest[:-1].strip() or None, address_raw

    # City - Area
    if " - " in txt:
        p0, p1 = [p.strip() for p in txt.split(" - ", 1)]
        return p0 or None, p1 or None, address_raw

    # City, Area
    if "," in txt:
        p0, p1 = [p.strip() for p in txt.split(",", 1)]
        return p0 or None, p1 or None, address_raw

    return txt.strip() or None, None, address_raw


def normalize_municipality_and_area(
    municipality: str | None,
    area_raw: str | None
) -> tuple[str | None, str | None]:
    """
    Fix cases like:
      "Zografou Ilisia"            -> municipality="Zografou", area_raw="Ilisia"
      "Kallithea Agia Eleousa"     -> municipality="Kallithea", area_raw="Agia Eleousa"

    IMPORTANT: Do NOT break real municipalities like "Nea Smyrni".
    Heuristic:
    - If municipality has >=2 tokens
    - AND area_raw is missing
    - AND the first token is NOT one of {Ano,Kato,Nea,Neo}
      => split municipality into (first token) + (rest as area_raw)
    """
    mun = (municipality or "").strip() or None
    ar = (area_raw or "").strip() or None

    if mun and (ar is None):
        parts = mun.split()
        if len(parts) >= 2 and parts[0].lower() not in AREA_PREFIXES:
            mun = parts[0].strip() or None
            rest = " ".join(parts[1:]).strip()
            ar = rest or None

    return mun, ar


def resolve_neighborhood_and_prefix(
    municipality: str | None,
    area_raw: str | None
) -> tuple[str | None, str | None]:
    """
    Single canonical scoring field + prefix-only category.

    Step 1) Normalize weird municipality strings (e.g. "Zografou Ilisia").
    Step 2) Choose a single name candidate:
       - If municipality exists and is NOT "Athens" -> candidate = municipality
       - Else (Athens)                             -> candidate = area_raw (or municipality if area_raw missing)
    Step 3) area = prefix (Ano/Kato/Nea/Neo) extracted from candidate
            neighborhood = candidate with that prefix stripped
    """
    mun, ar = normalize_municipality_and_area(municipality, area_raw)

    if not mun and not ar:
        return None, None

    if mun and mun.lower() != "athens":
        candidate = mun
    else:
        candidate = ar or mun

    prefix = extract_area_prefix(candidate)
    neighborhood = strip_area_prefix(candidate)

    return neighborhood, prefix


# -----------------------------
# Search result extraction (per page)
# -----------------------------

async def load_all_search_results(page: Page, max_rounds: int = 20) -> None:
    """
    Scroll to load more ads.
    Stops only after repeated no-growth rounds to avoid premature early exit.
    """
    last_count = -1
    no_growth_rounds = 0
    for _ in range(max_rounds):
        count = await page.locator('a[data-testid="property-ad-url"]').count()
        if count == last_count:
            no_growth_rounds += 1
        else:
            no_growth_rounds = 0
        # Require multiple stagnant rounds before stopping.
        if no_growth_rounds >= 2:
            return
        last_count = count
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(600)


async def resolve_group_best_price_variant(ctx: BrowserContext, group_url: str) -> str | None:
    """
    For '/u/' (multiple listings) pages:
    choose the variant with the lowest visible price.
    """
    group_page = await ctx.new_page()
    try:
        await group_page.goto(group_url, wait_until="domcontentloaded")
        try:
            await group_page.wait_for_selector(
                '[data-testid="unique-property-ad-container"]', timeout=6000
            )
        except TimeoutError:
            pass

        soup = BeautifulSoup(await group_page.content(), "html.parser")

        best_url: str | None = None
        best_price: int | None = None

        cards = soup.select('div[data-testid="unique-property-ad-container"]')
        for card in cards:
            price_el = card.select_one('[data-testid="unique-ad-price"]')
            link_el = card.select_one('a[data-testid="unique-ad-url"]')
            if not link_el or not link_el.get("href"):
                continue

            full = urljoin(BASE_URL, link_el["href"])
            p = parse_number(price_el.get_text(strip=True)) if price_el else None

            if best_url is None:
                best_url, best_price = full, p
            else:
                if p is not None and (best_price is None or p < best_price):
                    best_url, best_price = full, p

        if best_url:
            return best_url

        # Fallback: any link that looks like a listing
        a = soup.select_one('a[href*="/property/d/property-for-sale/"], a[href*="/property-for-sale/"]')
        if a and a.get("href"):
            return urljoin(BASE_URL, a["href"])

        return None

    except Exception as e:
        print(f"[WARN] Failed to resolve group page {group_url}: {e}")
        return None
    finally:
        await group_page.close()


async def extract_result_links(page: Page, html: str) -> list[str]:
    """
    Extract listing URLs from search result HTML.
    Also resolves '/u/' group URLs to a single best-price variant.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Dedup within the page by normalized key
    by_key: dict[str, dict] = {}

    for a in soup.select('a[data-testid="property-ad-url"]'):
        href = a.get("href")
        if not href:
            continue

        full_url = urljoin(BASE_URL, href)
        key = normalize_property_url(href)

        if key not in by_key:
            by_key[key] = {
                "url": full_url,
                "kind": "multiple" if "/u/" in full_url else "single",
            }

    items = list(by_key.values())

    # Resolve group URLs in parallel (max 3 at a time to avoid detection)
    group_sem = asyncio.Semaphore(3)

    async def resolve_item(item: dict) -> str | None:
        u = item["url"]
        if item["kind"] != "multiple":
            return u
        async with group_sem:
            resolved = await resolve_group_best_price_variant(page.context, u)
            return resolved or u  # fallback: keep original so we don't silently drop

    resolved_items = await asyncio.gather(*[resolve_item(item) for item in items])
    return [u for u in resolved_items if u]


# -----------------------------
# Listing page parsing
# -----------------------------

def parse_listing_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    data = {
        "url": url,
        "source": "xe.gr",
        "scraped_at": datetime.utcnow().isoformat(),

        "Headline": None,
        "price_eur": None,
        "price_per_sqm": None,
        "area_sqm": None,

        # Single canonical scoring field + prefix-only category
        "neighborhood": None,
        "area": None,  # prefix only (Ano/Kato/Nea/Neo) or None

        "address_raw": None,

        "bedrooms": None,
        "bathrooms": None,
        "floor": None,
        "year_built": None,
        "renovation_year": None,
        "energy_class": None,

        "photos_count": None,
        "photo_urls": [],
        "publication_date": None,
    }

    # Title + area
    title_el = soup.select_one('[data-testid="basic-info"] .title .section-heading')
    if title_el:
        title = title_el.get_text(strip=True)
        data["Headline"] = title
        data["area_sqm"] = parse_area_from_title(title)

    # Price
    price_el = soup.select_one('[data-testid="basic-info"] .price .section-heading')
    if price_el:
        data["price_eur"] = parse_number(price_el.get_text(strip=True))

    # price_per_sqm from price and parsed area
    if data["price_eur"] and data["area_sqm"] and data["area_sqm"] > 0:
        data["price_per_sqm"] = round(data["price_eur"] / data["area_sqm"], 2)

    # Address -> neighborhood + prefix-only area
    addr_el = soup.select_one('[data-testid="basic-info"] .address')
    if addr_el:
        municipality, area_raw, raw = parse_location_from_address_element(addr_el)
        data["address_raw"] = raw

        neighborhood, prefix = resolve_neighborhood_and_prefix(municipality, area_raw)
        data["neighborhood"] = neighborhood
        data["area"] = prefix

    # Characteristics
    for li in soup.select('[data-testid="characteristics"] [data-testid="characteristic"]'):
        txt = li.get_text(" ", strip=True)

        if txt.startswith("Bedrooms:"):
            data["bedrooms"] = parse_first_int(txt)
        elif txt.startswith("Bathrooms:") or txt.startswith("WC:"):
            data["bathrooms"] = parse_first_int(txt) or 0
        elif txt.startswith("Floor:"):
            data["floor"] = parse_floor(txt)
        elif txt.startswith("Year Built:"):
            data["year_built"] = parse_first_int(txt)
        elif txt.startswith("Renovation year:"):
            data["renovation_year"] = parse_first_int(txt)
        elif txt.startswith("Energy Class:"):
            raw_ec = txt.split(":", 1)[1].strip() if ":" in txt else txt
            data["energy_class"] = normalize_energy_class(raw_ec)

    # Photos
    photo_urls = [
        div.get("data-url")
        for div in soup.select("div.common-property-ad-image[data-testid^='gallery-ad-image']")
        if div.get("data-url")
    ]
    data["photo_urls"] = photo_urls

    count_el = soup.select_one(".xe-gallery-expand + span")
    if count_el:
        data["photos_count"] = parse_number(count_el.get_text(strip=True))
    elif photo_urls:
        # Fallback: count from the gallery divs we already extracted
        data["photos_count"] = len(photo_urls)

    # Publication date (Statistics section)
    stats_section = soup.select_one('section[data-testid="statistics"]')
    if stats_section:
        pub_date_iso = None
        for p in stats_section.select("p"):
            t = p.get_text(" ", strip=True)
            if t.startswith("Publication Date") or t.startswith("Last publication date"):
                span = p.find("span")
                if span:
                    raw_date = span.get_text(strip=True)  # e.g. "October 3, 2025"
                    try:
                        dt = datetime.strptime(raw_date, "%B %d, %Y")
                        pub_date_iso = dt.date().isoformat()
                    except ValueError:
                        pub_date_iso = raw_date
                break
        data["publication_date"] = pub_date_iso

    return data


async def wait_for_full_listing_dom(page: Page) -> None:
    """Wait for at least one strong selector that indicates the listing is fully rendered."""
    selectors = [
        '[data-testid="statistics"]',
        '[data-testid="characteristics"]',
        '[data-testid="basic-info"] .section-heading',
    ]
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=8000)
            return
        except TimeoutError:
            continue
    raise TimeoutError("Listing page did not reach expected DOM selectors.")


async def scrape_listing_details(context: BrowserContext, url: str) -> dict:
    """Open a listing page and parse its HTML."""
    # Skip unresolved group URLs like '/property/u/123456' – they are already
    # handled at the search-results level by resolve_group_best_price_variant.
    path = urlsplit(url).path
    if "/property/u/" in path:
        print(f"[SKIP] Skipping unresolved group URL {url}")
        return {}

    page = await context.new_page()
    try:
        attempts = 0
        while True:
            attempts += 1
            try:
                # domcontentloaded is much faster than networkidle — we do our
                # own DOM-ready check via wait_for_full_listing_dom instead.
                await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                try:
                    await wait_for_full_listing_dom(page)
                except TimeoutError:
                    if await is_verification_page(page):
                        await wait_for_user_to_solve_verification(
                            page,
                            success_selector='[data-testid="basic-info"] .section-heading',
                            timeout_ms=120_000,
                        )
                        await wait_for_full_listing_dom(page)
                    else:
                        raise
                html = await page.content()
                return parse_listing_html(html, url)
            except TimeoutError as e:
                if attempts >= 2:
                    print(f"[WARN] Giving up on {url} after {attempts} attempts due to timeout: {e}")
                    raise
                print(f"[RETRY] Timeout loading {url}, retrying ({attempts}/2)...")
                continue
    finally:
        await page.close()


async def scrape_all_listings_to_json(
    context: BrowserContext,
    listing_urls: list[str],
    max_concurrent: int = DETAIL_CONCURRENCY,
) -> None:
    """
    Scrape all listing URLs and merge results into data/listings.json (deduped).
    Uses a concurrency limit so multiple listing pages are fetched in parallel.
    """
    if not listing_urls:
        print("[DETAIL] No listing URLs to scrape.")
        return

    sem = asyncio.Semaphore(max_concurrent)

    async def scrape_one(i: int, url: str) -> dict | None:
        async with sem:
            print(f"[DETAIL] ({i}/{len(listing_urls)}) {url}")
            try:
                return await scrape_listing_details(context, url)
            except Exception as e:
                print(f"[WARN] Failed to scrape {url}: {e}")
                return None

    tasks = [
        asyncio.create_task(scrape_one(i, url))
        for i, url in enumerate(listing_urls, start=1)
    ]
    raw_results = await asyncio.gather(*tasks)
    results: list[dict] = [r for r in raw_results if r is not None]

    out_path = DATA_DIR / "listings.json"

    # Load existing
    existing: list[dict] = []
    if out_path.exists():
        try:
            with out_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing = data
        except Exception as e:
            print(f"[WARN] Failed to load existing listings.json: {e}")

    # Merge by normalized listing URL
    by_key: dict[str, dict] = {}
    for item in existing:
        u = item.get("url")
        if u:
            by_key[normalize_listing_url(u)] = item

    for item in results:
        u = item.get("url")
        if u:
            by_key[normalize_listing_url(u)] = item  # new overwrites old

    combined = list(by_key.values())
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    print(f"[INFO] Wrote {len(results)} new listings, total {len(combined)} unique -> {out_path}")


# -----------------------------
# Pagination collector
# -----------------------------

async def collect_all_listing_urls_across_pages(
    page: Page,
    start_page: int,
    max_pages: int | None,
) -> tuple[list[str], int]:
    """
    Collect listing URLs across pages.
    Returns (urls, last_page_scraped).
    """
    all_urls: list[str] = []
    page_index = start_page
    pages_visited = 0

    while True:
        print(f"[PAGE] Collecting from page {page_index}...")

        await ensure_no_verification_blocking(page)
        try:
            await load_all_search_results(page)
        except TimeoutError:
            if await is_verification_page(page):
                print("[WARN] Bot verification detected during scroll.")
                await wait_for_user_to_solve_verification(page)
                await load_all_search_results(page)
            else:
                print("[WARN] Timeout during scroll; continuing with listings found so far.")

        html = await page.content()
        urls = await extract_result_links(page, html)
        all_urls.extend(urls)

        pages_visited += 1
        if max_pages is not None and pages_visited >= max_pages:
            print(f"[PAGE] Reached max_pages={max_pages}, stopping pagination.")
            break

        next_btn = page.locator(
            "nav[data-testid='pagination'] li.pager-next a[rel='next'][aria-disabled='false']"
        )
        if await next_btn.count() == 0:
            print("[PAGE] No enabled NEXT button found; finished.")
            break

        await next_btn.first.click()
        try:
            await page.wait_for_load_state("load", timeout=12000)
        except TimeoutError:
            if await is_verification_page(page):
                print("[WARN] Bot verification detected after page navigation.")
                await wait_for_user_to_solve_verification(page)
            else:
                print("[WARN] Timeout after next page click; continuing anyway.")

        await page.wait_for_timeout(400)
        page_index += 1

    print(f"[PAGE] Collected {len(all_urls)} raw listing URLs from {pages_visited} pages (last page={page_index}).")
    return all_urls, page_index


# -----------------------------
# Main batch runner
# -----------------------------

async def main_batch(max_pages_per_batch: int = 5) -> None:
    """
    One batch run:
    - start from last_page + 1 (state.json)
    - collect URLs across N pages
    - save last_page
    - dedupe URLs
    - scrape details and merge into listings.json
    """
    last_page = load_last_page()
    start_page = last_page + 1 if last_page > 0 else 1
    print(f"[STATE] last_page={last_page} -> starting from page={start_page}")

    start_url = url_builder.build_xe_url(
        min_price=30000,
        max_price=60000,
        building_type="apartment",
        has_photos=True,
        page=start_page,
    )
    print(f"[INFO] Start URL: {start_url}")

    async with async_playwright() as p:
        cookies = await ensure_cookies(
            playwright=p,
            cookies_path=COOKIES_PATH,
            start_url=start_url,
            auto_capture=True,
        )
        print(f"[INFO] Loaded {len(cookies)} cookies")

        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto(start_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(400)

        await ensure_no_verification_blocking(page)

        raw_urls, last_page_scraped = await collect_all_listing_urls_across_pages(
            page,
            start_page=start_page,
            max_pages=max_pages_per_batch,
        )

        save_last_page(last_page_scraped)

        resolved_urls = dedupe_listing_urls(raw_urls)
        await scrape_all_listings_to_json(context, resolved_urls)

        await browser.close()


async def run_multiple_batches(num_batches, max_pages_per_batch) -> None:
    """Run multiple batches sequentially (uses state.json to continue)."""
    for batch in range(1, num_batches + 1):
        print(f"\n========== BATCH {batch}/{num_batches} ==========\n")
        try:
            await main_batch(max_pages_per_batch=max_pages_per_batch)
        except Exception as e:
            print(f"[ERROR] Batch {batch} failed: {e}")
            break
        await asyncio.sleep(2)


if __name__ == "__main__":
    # Tune these:
    # - num_batches: how many cycles to run
    # - max_pages_per_batch: how many result pages to scan per cycle
    asyncio.run(run_multiple_batches(num_batches=21, max_pages_per_batch=2))
