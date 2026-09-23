import argparse
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from tqdm import tqdm

# ====================== KONFIG ======================
DELAY = 2.5
DB_PATH = "leads.db"
LOG_DIR = "logs"
EXPORT_DIR = "exports"
DEFAULT_DB_PATH = "leads.db"
DEFAULT_LOG_PATH = f"{LOG_DIR}/scraper.log"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]

KEYWORDS = ["prioritet finans", "eurofinans"]
WEAK_NAME_MARKERS = {
    "anonymous",
    "anon",
    "kunde",
    "kund",
    "customer",
    "user",
    "guest",
}

os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger(__name__)


def ensure_output_dirs(*paths):
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    for path in paths:
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)


def setup_logging(log_path):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def create_browser_context(playwright, locale="sv-SE"):
    browser = playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
    context = browser.new_context(user_agent=USER_AGENT, locale=locale)
    return browser, context


def normalize_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def accept_consent(page):
    # Hitta's consent manager can be injected shortly after the initial page load.
    try:
        gravito_accept = page.locator("#gravitoTCFCMP-layer1-accept-all")
        gravito_accept.wait_for(state="visible", timeout=2500)
        gravito_accept.click(timeout=2000)
        page.wait_for_timeout(600)
        return
    except Exception:
        pass

    button_candidates = (
        "Accept all",
        "I Accept",
        "Allow all",
        "GODKANN",
        "GODKÄNN",
        "Godkann",
        "Godkänn",
        "Acceptera",
    )

    for label in button_candidates:
        try:
            buttons = page.get_by_role("button", name=label)
            if buttons.count():
                buttons.first.click(timeout=2000)
                page.wait_for_timeout(600)
                return
        except Exception:
            continue

    # OneTrust fallback selectors used by many sites.
    for selector in (
        "#gravitoTCFCMP-layer1-accept-all",
        "button:has-text('Godkänn alla')",
        "#onetrust-accept-btn-handler",
        "button[aria-label='Accept All Cookies']",
        "button:has-text('Accept all')",
    ):
        try:
            if page.locator(selector).count():
                page.locator(selector).first.click(timeout=2000)
                page.wait_for_timeout(600)
                return
        except Exception:
            continue

    # Last resort: remove overlay if present so interactions can continue.
    try:
        page.evaluate(
            """
            () => {
                const sdk = document.querySelector('#onetrust-consent-sdk');
                if (sdk) sdk.remove();
                document.querySelectorAll('.onetrust-pc-dark-filter').forEach((n) => n.remove());
            }
            """
        )
    except Exception:
        pass


def build_absolute_url(base_url, href):
    if href.startswith("http"):
        return href
    return f"{base_url}{href}"


def safe_text(locator, default=""):
    try:
        if locator.count():
            return normalize_space(locator.first.inner_text())
    except PlaywrightError:
        return default
    return default


def is_low_confidence_name(name):
    normalized = normalize_space(name)
    if not normalized:
        return True

    tokens = [token for token in normalized.split(" ") if token]

    # Require at least first name + last name.
    if len(tokens) < 2:
        return True

    # Names with initials or single-letter trailing token are often too ambiguous.
    if len(tokens[-1].strip(".")) <= 1:
        return True

    if tokens[0].casefold() in WEAK_NAME_MARKERS:
        return True

    # Reject aliases with heavy punctuation or very short alphabetic signal.
    alpha_chars = re.sub(r"[^A-Za-zÅÄÖåäö]", "", normalized)
    if len(alpha_chars) < 5:
        return True

    return False


def infer_trustpilot_company_name(page: Page, company_url: str):
    heading = safe_text(page.locator("h1"))
    if heading:
        return heading

    domain_match = re.search(r"/review/([^/?#]+)", company_url)
    return domain_match.group(1) if domain_match else ""


def is_valid_phone_number(phone):
    candidate = normalize_space(phone)
    if not candidate:
        return False

    digits = re.sub(r"\D", "", candidate)
    # Swedish numbers are usually 8-12 digits with country code variants.
    if len(digits) < 8 or len(digits) > 12:
        return False

    if digits.startswith("46") or digits.startswith("0"):
        return True
    return False


def extract_value_from_text(text, labels):
    for label in labels:
        pattern = rf"{label}\s*[:\n]\s*([^\n]{{1,80}})"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return normalize_space(match.group(1))
    return ""


def extract_company_details_from_page(page: Page):
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_text = ""

    omsattning = extract_value_from_text(body_text, [r"Omsattning", r"Omsättning"])
    vinstmarginal = extract_value_from_text(body_text, [r"Vinstmarginal"])
    sni = extract_value_from_text(body_text, [r"SNI", r"SNI-kod"])

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body_text)
    phone_match = re.search(r"(?:\+46\s?\d{1,3}|0\d{1,3})[\s-]?\d{2,4}[\s-]?\d{2,4}[\s-]?\d{0,4}", body_text)
    website_match = re.search(r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9.-]+\.(?:se|com|nu|org|net)\b", body_text)

    return {
        "omsättning": omsattning,
        "vinstmarginal": vinstmarginal,
        "snI": sni,
        "kontakt_email": email_match.group(0) if email_match else "",
        "kontakt_telefon": normalize_space(phone_match.group(0)) if phone_match and is_valid_phone_number(phone_match.group(0)) else "",
        "webbplats": website_match.group(0) if website_match else "",
    }


def collect_companies_for_keyword(page: Page, keyword: str, max_companies: int):
    logger.info("Trustpilot keyword start: %s", keyword)
    page.goto(f"https://www.trustpilot.com/search?query={quote(keyword)}", timeout=30000)
    accept_consent(page)
    time.sleep(DELAY)

    company_links = collect_trustpilot_company_links(page, max_companies=max_companies)
    logger.info("Trustpilot keyword '%s': hittade %s bolag", keyword, len(company_links))

    companies = []
    for company_url in company_links:
        try:
            page.goto(company_url, timeout=30000)
            time.sleep(DELAY)
            accept_consent(page)

            trustpilot_company = infer_trustpilot_company_name(page, company_url)
            trustpilot_company = normalize_space(trustpilot_company)
            if not trustpilot_company:
                continue

            companies.append(
                {
                    "keyword": keyword,
                    "trustpilot_company": trustpilot_company,
                    "company_url": company_url,
                }
            )
        except Exception as e:
            logger.warning("Kunde inte lasa Trustpilot-bolag %s: %s", company_url, e)

    return companies


def scrape_trustpilot_companies(page: Page, keywords=KEYWORDS, max_pages=10):
    logger.info("Startar Trustpilot-bolagssokning...")
    all_companies = []
    seen_names = set()

    for keyword in keywords:
        try:
            companies = collect_companies_for_keyword(page, keyword=keyword, max_companies=max_pages)
            for row in companies:
                key = row["trustpilot_company"].casefold()
                if key in seen_names:
                    continue
                seen_names.add(key)
                all_companies.append(row)
        except Exception as e:
            logger.error("Fel vid Trustpilot-sokning for '%s': %s", keyword, e)

    logger.info("Hittade %s unika bolag fran Trustpilot", len(all_companies))
    return all_companies


def open_hitta_search(page: Page, query: str):
    page.goto("https://www.hitta.se/", timeout=30000)
    time.sleep(DELAY)
    accept_consent(page)

    search_input = page.locator("[data-test='autocomplete-input']")
    if search_input.count() == 0:
        raise RuntimeError("Kunde inte hitta sokrutan pa hitta.se")

    search_input.first.click()
    search_input.first.fill(query)
    search_input.first.press("Enter")
    time.sleep(DELAY)


def extract_hitta_details(page: Page):
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_text = ""

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body_text)

    phones = re.findall(r"(?:\+46\s?\d{1,3}|0\d{1,3})[\s-]?\d{2,4}[\s-]?\d{2,4}[\s-]?\d{0,4}", body_text)
    valid_phones = [normalize_space(p) for p in phones if is_valid_phone_number(p)]

    website_match = re.search(r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9.-]+\.(?:se|com|nu|org|net)\b", body_text)

    return {
        "kontakt_email": email_match.group(0) if email_match else "",
        "kontakt_telefon": valid_phones[0] if valid_phones else "",
        "webbplats": website_match.group(0) if website_match else "",
    }


def search_hitta_company(page: Page, bolag_namn: str):
    try:
        open_hitta_search(page, bolag_namn)

        # Go into first organic company hit if available.
        result_links = page.locator("a[href*='/verksamhet/']")
        if result_links.count():
            href = result_links.first.get_attribute("href") or ""
            if href:
                page.goto(build_absolute_url("https://www.hitta.se", href), timeout=30000)
                time.sleep(DELAY)

        details = extract_hitta_details(page)
        return {
            "bolag_namn": bolag_namn,
            "org_nr": "",
            **details,
        }
    except Exception as e:
        logger.warning("Kunde inte hitta kontaktuppgifter pa hitta.se for '%s': %s", bolag_namn, e)
        return {
            "bolag_namn": bolag_namn,
            "org_nr": "",
            "kontakt_email": "",
            "kontakt_telefon": "",
            "webbplats": "",
        }


def extract_review_rows(review_cards, max_reviews_per_page):
    rows = []
    seen_keys = set()

    page_count = review_cards.count()
    limit = page_count if max_reviews_per_page <= 0 else min(page_count, max_reviews_per_page)

    for index in range(limit):
        review = review_cards.nth(index)
        try:
            name = safe_text(review.locator("[data-consumer-name-typography='true']"))
            text = safe_text(review.locator("[data-relevant-review-text-typography='true']"))[:500]
            date = review.locator("time").first.get_attribute("datetime") or ""
            if not name or not text:
                continue

            review_key = (name.casefold(), date, text.casefold())
            if review_key in seen_keys:
                continue
            seen_keys.add(review_key)

            rows.append(
                {
                    "person_namn": name,
                    "trustpilot_text": text,
                    "trustpilot_datum": date,
                }
            )
        except Exception as e:
            logger.warning(f"Kunde inte lasa recension: {e}")
            continue

    return rows


def collect_company_reviews(page: Page, company_url: str, keyword: str, max_review_pages: int, max_reviews_per_page: int):
    page.goto(company_url, timeout=30000)
    time.sleep(DELAY)
    accept_consent(page)
    trustpilot_company = infer_trustpilot_company_name(page, company_url)

    reviews_link = page.get_by_role("link", name="Reviews", exact=True)
    if reviews_link.count():
        reviews_link.first.click()
        time.sleep(DELAY)

    rows = []
    seen_review_keys = set()
    visited_urls = set()
    pages_read = 0

    while True:
        current_url = page.url
        if current_url in visited_urls:
            break
        visited_urls.add(current_url)

        review_cards = page.locator("article")
        extracted = extract_review_rows(review_cards, max_reviews_per_page=max_reviews_per_page)
        for row in extracted:
            global_key = (
                row["person_namn"].casefold(),
                row["trustpilot_datum"],
                row["trustpilot_text"].casefold(),
            )
            if global_key in seen_review_keys:
                continue
            seen_review_keys.add(global_key)
            row["keyword"] = keyword
            row["trustpilot_company"] = trustpilot_company
            rows.append(row)

        pages_read += 1
        if max_review_pages > 0 and pages_read >= max_review_pages:
            break

        next_page = page.get_by_role("link", name="Next page")
        if not next_page.count():
            break

        try:
            next_page.first.click(timeout=4000)
            time.sleep(DELAY)
        except Exception:
            break

    return rows


# ====================== DATABASE ======================
def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY,
            person_namn TEXT,
            trustpilot_text TEXT,
            trustpilot_datum TEXT,
            bolag_namn TEXT,
            org_nr TEXT,
            omsättning TEXT,
            vinstmarginal TEXT,
            snI TEXT,
            roller TEXT,
            trustpilot_company TEXT,
            match_typ TEXT,
            kontakt_email TEXT,
            kontakt_telefon TEXT,
            webbplats TEXT,
            källa TEXT,
            created_at TEXT
        )
    """
    )
    ensure_db_columns(conn)
    conn.commit()
    return conn


def ensure_db_columns(conn):
    current_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(leads)").fetchall()
    }
    wanted_columns = {
        "omsättning": "TEXT",
        "vinstmarginal": "TEXT",
        "snI": "TEXT",
        "roller": "TEXT",
        "trustpilot_company": "TEXT",
        "match_typ": "TEXT",
        "kontakt_email": "TEXT",
        "kontakt_telefon": "TEXT",
        "webbplats": "TEXT",
    }

    for col_name, col_type in wanted_columns.items():
        if col_name not in current_columns:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")


# ====================== TRUSTPILOT ======================
def collect_trustpilot_company_links(page: Page, max_companies: int):
    company_links = []
    review_links = page.locator("a[href*='/review/']")
    scan_limit = review_links.count() if max_companies <= 0 else min(review_links.count(), max_companies * 5)

    for index in range(scan_limit):
        href = review_links.nth(index).get_attribute("href")
        if not href:
            continue

        absolute_url = build_absolute_url("https://www.trustpilot.com", href)
        if absolute_url not in company_links:
            company_links.append(absolute_url)
        if max_companies > 0 and len(company_links) >= max_companies:
            break

    return company_links


def scrape_trustpilot(page: Page, keywords=KEYWORDS, max_pages=3, max_review_pages=1, max_reviews_per_page=15):
    results = []
    logger.info("Startar Trustpilot-scraping...")

    for keyword in keywords:
        try:
            logger.info("Trustpilot keyword start: %s", keyword)
            page.goto(f"https://www.trustpilot.com/search?query={quote(keyword)}", timeout=30000)
            accept_consent(page)

            time.sleep(DELAY)
            company_links = collect_trustpilot_company_links(page, max_companies=max_pages)
            logger.info("Trustpilot keyword '%s': hittade %s bolag", keyword, len(company_links))

            for company_url in company_links:
                company_rows = collect_company_reviews(
                    page,
                    company_url=company_url,
                    keyword=keyword,
                    max_review_pages=max_review_pages,
                    max_reviews_per_page=max_reviews_per_page,
                )
                results.extend(company_rows)
            logger.info("Trustpilot keyword klar: %s (ackumulerat reviews=%s)", keyword, len(results))
        except Exception as e:
            logger.error(f"Fel vid Trustpilot-sokning for '{keyword}': {e}")

    logger.info(f"Hamtade {len(results)} recensioner fran Trustpilot")
    return results


# ====================== ALLABOLAG ======================
def open_allabolag_search(page: Page, person_namn: str):
    page.goto("https://www.allabolag.se/", timeout=30000)
    time.sleep(DELAY)
    accept_consent(page)

    searchbox = page.get_by_role("combobox", name=re.compile("Sok efter foretag|Sök efter företag", re.IGNORECASE))
    if searchbox.count():
        searchbox.first.click()
        searchbox.first.fill(person_namn)
        searchbox.first.press("Enter")
        time.sleep(DELAY)

    tab = page.get_by_role("tab", name=re.compile("Befattningshavare", re.IGNORECASE))
    if tab.count():
        tab.first.click()
        time.sleep(DELAY)


def open_allabolag_company_search(page: Page, bolag_namn: str):
    page.goto("https://www.allabolag.se/", timeout=30000)
    time.sleep(DELAY)
    accept_consent(page)

    searchbox = page.get_by_role("combobox", name=re.compile("Sok efter foretag|Sök efter företag", re.IGNORECASE))
    if searchbox.count():
        searchbox.first.click()
        searchbox.first.fill(bolag_namn)
        searchbox.first.press("Enter")
        time.sleep(DELAY)


def collect_company_candidates(page: Page, max_companies=8):
    bolag_list = []
    company_links = page.locator("a[href*='/foretag/']")
    seen_names = set()

    for index in range(company_links.count()):
        try:
            card = company_links.nth(index)
            bolag = normalize_space(card.inner_text())[:100]
            if not bolag or bolag in seen_names:
                continue
            seen_names.add(bolag)

            href = card.get_attribute("href") or ""
            org_match = re.search(r"\d{6}-\d{4}|\d{10}", f"{bolag} {href}")
            bolag_list.append(
                {
                    "bolag_namn": bolag,
                    "org_nr": org_match.group(0) if org_match else "",
                    "href": href,
                }
            )
            if max_companies > 0 and len(bolag_list) >= max_companies:
                break
        except Exception as e:
            logger.debug(f"Fel vid parsing av bolagskort: {e}")
            continue

    return bolag_list


def enrich_company_candidates(page: Page, candidates):
    enriched = []
    for candidate in candidates:
        item = dict(candidate)
        if item.get("href"):
            try:
                page.goto(build_absolute_url("https://www.allabolag.se", item["href"]), timeout=30000)
                time.sleep(DELAY)
                accept_consent(page)
                details = extract_company_details_from_page(page)
                item.update(details)
            except Exception as e:
                logger.debug(f"Kunde inte lasa bolagsdetaljer for {item.get('bolag_namn', '')}: {e}")
        enriched.append(item)
    return enriched


def search_allabolag(page: Page, person_namn, max_companies=8, enrich_company_details=False):
    try:
        open_allabolag_search(page, person_namn)

        person_links = page.locator("a[href*='/befattning/']")
        person_href = None
        normalized_target = normalize_space(person_namn).casefold()

        for index in range(min(person_links.count(), 10)):
            link = person_links.nth(index)
            link_text = normalize_space(link.inner_text())
            if link_text.casefold().startswith(normalized_target):
                person_href = link.get_attribute("href")
                break

        if not person_href and person_links.count():
            person_href = person_links.first.get_attribute("href")

        if not person_href:
            return []

        page.goto(build_absolute_url("https://www.allabolag.se", person_href), timeout=30000)
        time.sleep(DELAY)
        accept_consent(page)

        show_all = page.get_by_role("link", name=re.compile("Visa alla befattningar", re.IGNORECASE))
        if show_all.count():
            show_all.first.click()
            time.sleep(DELAY)

        candidates = collect_company_candidates(page, max_companies=max_companies)
        if enrich_company_details:
            return enrich_company_candidates(page, candidates)
        return candidates

    except Exception as e:
        logger.warning(f"Kunde inte hitta {person_namn} pa Allabolag: {e}")
        return []


def search_allabolag_company(page: Page, bolag_namn: str, max_companies=3, enrich_company_details=False):
    try:
        open_allabolag_company_search(page, bolag_namn)
        candidates = collect_company_candidates(page, max_companies=max_companies)
        if enrich_company_details:
            return enrich_company_candidates(page, candidates)
        return candidates
    except Exception as e:
        logger.warning(f"Kunde inte hitta bolag '{bolag_namn}' pa Allabolag: {e}")
        return []


# ====================== HUVUDLOOP ======================
def parse_args():
    parser = argparse.ArgumentParser(description="Trustpilot + hitta.se bolagsscraper")
    parser.add_argument(
        "--keywords",
        default=",".join(KEYWORDS),
        help="Komma-separerade nyckelord for Trustpilot-sokning (default: prioritet finans,eurofinans)",
    )
    parser.add_argument("--max-pages", type=int, default=10, help="Antal bolag per nyckelord (0 = alla bolag som hittas)")
    parser.add_argument(
        "--enrich-company-details",
        dest="enrich_company_details",
        action="store_true",
        help="Forsok hamta kontaktuppgifter per bolag (default: pa)",
    )
    parser.add_argument(
        "--no-enrich-company-details",
        dest="enrich_company_details",
        action="store_false",
        help="Stang av hamtning av kontaktuppgifter",
    )
    parser.set_defaults(enrich_company_details=True)
    parser.add_argument("--dry-run", action="store_true", help="Logga resultat utan att skriva till databasen")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Sokvag till SQLite-databasen")
    parser.add_argument("--log-path", default=DEFAULT_LOG_PATH, help="Sokvag till loggfilen")
    return parser.parse_args()


def main():
    args = parse_args()
    ensure_output_dirs(args.db_path, args.log_path)
    setup_logging(args.log_path)
    keywords = [normalize_space(k) for k in args.keywords.split(",") if normalize_space(k)]
    conn = None if args.dry_run else init_db(args.db_path)
    logger.info("=== Scraper startar ===")
    logger.info(
        "Konfiguration: keywords=%s max_pages=%s enrich_company_details=%s dry_run=%s db_path=%s log_path=%s",
        keywords,
        args.max_pages,
        args.enrich_company_details,
        args.dry_run,
        args.db_path,
        args.log_path,
    )

    with sync_playwright() as p:
        browser, context = create_browser_context(p, locale="en-US")
        trustpilot_page = context.new_page()
        hitta_page = context.new_page()

        print("Hamtar bolag fran Trustpilot...")
        trustpilot_companies = scrape_trustpilot_companies(
            trustpilot_page,
            keywords=keywords,
            max_pages=args.max_pages,
        )

        print(f"{len(trustpilot_companies)} bolag hittade. Hamtar kontaktuppgifter fran hitta.se...")

        for item in tqdm(trustpilot_companies, desc="Matchar bolag", unit="bolag"):
            bolag_namn = item["trustpilot_company"]
            logger.info("Soker kontaktuppgifter pa hitta.se for bolag: %s", bolag_namn)

            try:
                if args.enrich_company_details:
                    bolag = search_hitta_company(hitta_page, bolag_namn)
                else:
                    bolag = {
                        "bolag_namn": bolag_namn,
                        "org_nr": "",
                        "kontakt_email": "",
                        "kontakt_telefon": "",
                        "webbplats": "",
                    }

                if args.dry_run:
                    logger.info(
                        "[DRY-RUN] Would insert: bolag=%s trustpilot_company=%s email=%s telefon=%s webbplats=%s",
                        bolag.get("bolag_namn", ""),
                        item.get("trustpilot_company", ""),
                        bolag.get("kontakt_email", ""),
                        bolag.get("kontakt_telefon", ""),
                        bolag.get("webbplats", ""),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO leads
                        (person_namn, trustpilot_text, trustpilot_datum, trustpilot_company, bolag_namn, org_nr,
                         omsättning, vinstmarginal, snI, roller, match_typ, kontakt_email, kontakt_telefon, webbplats,
                         källa, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            "",
                            "",
                            "",
                            item.get("trustpilot_company", ""),
                            bolag.get("bolag_namn", bolag_namn),
                            bolag.get("org_nr", ""),
                            "",
                            "",
                            "",
                            "",
                            "company",
                            bolag.get("kontakt_email", ""),
                            bolag.get("kontakt_telefon", ""),
                            bolag.get("webbplats", ""),
                            "Trustpilot + hitta.se",
                            datetime.now().isoformat(),
                        ),
                    )
            except Exception as e:
                logger.error("Fel vid bearbetning av bolag %s: %s", bolag_namn, e)

            time.sleep(DELAY)

        browser.close()

    if conn is not None:
        conn.commit()
        conn.close()
        logger.info("Scraper klar! Data sparad i %s", args.db_path)
        print(f"Klart! Kolla {args.db_path} och {args.log_path}")
    else:
        logger.info("Dry-run klar! Ingen data skrevs till %s", args.db_path)
        print(f"Dry-run klart! Ingen data skrevs till {args.db_path}")


if __name__ == "__main__":
    main()
