#!/usr/bin/env python3
"""Test phone enrichment for financial leads using Hitta.se and Allabolag."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from random import uniform
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scraper import (  # noqa: E402
    BROWSER_ARGS,
    USER_AGENT,
    accept_consent,
    build_absolute_url,
    extract_hitta_details,
    normalize_space,
)

DELAY_SECONDS = 3.0
PAGE_TIMEOUT_MS = 20000
INTERACTION_TIMEOUT_MS = 8000
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (10.0, 30.0)
ROTATE_CONTEXT_EVERY = 250
MAX_CONSECUTIVE_ERRORS = 5
ORGNR_PATTERN = re.compile(r"\b\d{6}[-\s]?\d{4}\b")
BROWSER_EXECUTABLE = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
PROXY_LIST_URL = os.environ.get(
    "PROXY_LIST_URL",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&proxy_format=protocolipport&format=text&protocol=http",
)
PROXY_LIST_FILE = os.environ.get("PROXY_LIST_FILE", "")
PROXY_SCHEME = os.environ.get("PROXY_SCHEME", "http")
PROXY_LIST_FILES = os.environ.get("PROXY_LIST_FILES", "")
PROXY_LIVE_FILE = os.environ.get("PROXY_LIVE_FILE", "")
PROXY_LIMIT = int(os.environ.get("PROXY_LIMIT", "50"))
PROXY_ROTATE_EVERY = int(os.environ.get("PROXY_ROTATE_EVERY", "1"))
PROXY_VALIDATE_URL = os.environ.get("PROXY_VALIDATE_URL", "https://www.hitta.se/")
PROXY_VALIDATE_TIMEOUT = float(os.environ.get("PROXY_VALIDATE_TIMEOUT", "5"))
PROXY_VALIDATE_WORKERS = int(os.environ.get("PROXY_VALIDATE_WORKERS", "20"))


def normalize_orgnr(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def read_leads(path: Path, limit: int, offset: int) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected = rows[offset:]
    return selected[:limit] if limit > 0 else selected


def load_proxies() -> List[str]:
    proxies = []
    sources = []
    if PROXY_LIST_FILES:
        for source in PROXY_LIST_FILES.split(","):
            path, separator, scheme = source.partition("|")
            sources.append((Path(path), scheme or PROXY_SCHEME))
    elif PROXY_LIST_FILE:
        sources.append((Path(PROXY_LIST_FILE), PROXY_SCHEME))
    elif PROXY_LIST_URL:
        request = Request(PROXY_LIST_URL, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8", errors="replace")
        sources.append((None, PROXY_SCHEME, text))

    for source in sources:
        path, scheme, *loaded_text = source
        text = loaded_text[0] if loaded_text else path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if "://" not in candidate:
                candidate = f"{scheme}://{candidate}"
            if candidate not in proxies:
                proxies.append(candidate)
            if PROXY_LIMIT > 0 and len(proxies) >= PROXY_LIMIT:
                return proxies
    return proxies


def validate_proxy(browser, proxy: str) -> str:
    context = None
    try:
        context = new_context(browser, proxy)
        page = context.new_page()
        page.goto(
            PROXY_VALIDATE_URL,
            timeout=int(PROXY_VALIDATE_TIMEOUT * 1000),
            wait_until="domcontentloaded",
        )
        return proxy
    except Exception:
        return ""
    finally:
        if context is not None:
            context.close()


def validate_proxies(browser, proxies: List[str]) -> List[str]:
    live: List[str] = []
    for index, proxy in enumerate(proxies, start=1):
        result = validate_proxy(browser, proxy)
        if result:
            live.append(result)
            print(f"Proxy live {len(live)}: kandidat {index}/{len(proxies)}", flush=True)
        elif index % 25 == 0:
            print(f"Proxytest: {index}/{len(proxies)} kandidater", flush=True)
    return live


def write_live_proxies(proxies: List[str]) -> None:
    if not PROXY_LIVE_FILE:
        return
    path = Path(PROXY_LIVE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(proxies) + ("\n" if proxies else ""), encoding="utf-8")


def new_context(browser, proxy: str = ""):
    options = {"user_agent": USER_AGENT, "locale": "sv-SE"}
    if proxy:
        parsed = urlsplit(proxy)
        proxy_options = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            proxy_options["username"] = parsed.username
        if parsed.password:
            proxy_options["password"] = parsed.password
        options["proxy"] = proxy_options
    return browser.new_context(**options)


def find_hitta_company(page, orgnr: str) -> Dict[str, str]:
    page.goto("https://www.hitta.se/", timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    time.sleep(DELAY_SECONDS)
    accept_consent(page)

    search_input = page.locator("[data-test='autocomplete-input']")
    if search_input.count() == 0:
        raise RuntimeError("Hitta-sökrutan hittades inte")

    search_input.first.fill(orgnr)
    search_input.first.press("Enter")
    time.sleep(DELAY_SECONDS)

    result_links = page.locator("a[href*='/verksamhet/']")
    candidates = []
    for index in range(min(result_links.count(), 5)):
        href = result_links.nth(index).get_attribute("href") or ""
        if href and href not in candidates:
            candidates.append(href)

    for href in candidates:
        profile_url = build_absolute_url("https://www.hitta.se", href)
        page.goto(profile_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        time.sleep(DELAY_SECONDS)
        accept_consent(page)
        body = page.locator("body").inner_text(timeout=5000)
        body_orgnrs = {normalize_orgnr(value) for value in ORGNR_PATTERN.findall(body)}
        if orgnr not in body_orgnrs:
            continue

        details = extract_hitta_details(page)
        heading = ""
        try:
            heading = normalize_space(page.locator("h1").first.inner_text())
        except Exception:
            pass
        return {
            "match_status": "verified",
            "matched_orgnr": orgnr,
            "bolag_namn": heading,
            "kontakt_telefon": details.get("kontakt_telefon", ""),
            "kontakt_email": details.get("kontakt_email", ""),
            "webbplats": details.get("webbplats", ""),
            "kontakt_kalla": "hitta.se",
            "kontakt_url": page.url,
        }

    return {
        "match_status": "no_verified_profile",
        "matched_orgnr": "",
        "bolag_namn": "",
        "kontakt_telefon": "",
        "kontakt_email": "",
        "webbplats": "",
        "kontakt_kalla": "hitta.se",
        "kontakt_url": page.url,
    }


def empty_result(lead: Dict[str, str], status: str) -> Dict[str, str]:
    result = dict(lead)
    result.update({
        "match_status": status,
        "matched_orgnr": "",
        "bolag_namn": "",
        "kontakt_telefon": "",
        "kontakt_email": "",
        "webbplats": "",
        "kontakt_kalla": "hitta.se",
        "kontakt_url": "",
    })
    return result


def read_existing_results(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def enrich_leads(
    input_path: Path,
    output_path: Path,
    limit: int,
    offset: int,
    resume_path: Path | None = None,
) -> None:
    leads = read_leads(input_path, limit, offset)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(leads[0].keys()) + [
        "match_status",
        "matched_orgnr",
        "bolag_namn",
        "kontakt_telefon",
        "kontakt_email",
        "webbplats",
        "kontakt_kalla",
        "kontakt_url",
    ] if leads else []

    existing_rows = read_existing_results(resume_path) if resume_path else []
    temporary_output = output_path.with_name(output_path.name + ".tmp")

    with temporary_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        handle.flush()

        with sync_playwright() as playwright:
            launch_options = {"headless": True, "args": BROWSER_ARGS}
            if BROWSER_EXECUTABLE:
                launch_options["executable_path"] = BROWSER_EXECUTABLE
            browser = playwright.chromium.launch(**launch_options)
            candidates = load_proxies()
            print(f"Laddade {len(candidates)} proxykandidater från {PROXY_LIST_URL}", flush=True)
            proxies = validate_proxies(browser, candidates)
            print(
                f"Validerade {len(proxies)}/{len(candidates)} proxies mot {PROXY_VALIDATE_URL}",
                flush=True,
            )
            write_live_proxies(proxies)
            if not proxies:
                raise RuntimeError("Ingen proxy klarade live-testet mot målwebbplatsen")
            context_rotate_every = PROXY_ROTATE_EVERY if proxies else ROTATE_CONTEXT_EVERY
            proxy_index = 0
            context = new_context(browser, proxies[proxy_index] if proxies else "")
            page = context.new_page()
            page.set_default_timeout(INTERACTION_TIMEOUT_MS)
            try:
                consecutive_errors = 0
                for index, lead in enumerate(leads, start=1):
                    absolute_index = offset + index - 1
                    if absolute_index < len(existing_rows):
                        existing_row = existing_rows[absolute_index]
                        if not existing_row.get("match_status", "").startswith("error:"):
                            writer.writerow(existing_row)
                            handle.flush()
                            consecutive_errors = 0
                            continue

                    orgnr = normalize_orgnr(lead.get("orgnr", ""))
                    result = empty_result(lead, "error:unprocessed")
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        try:
                            result.update(find_hitta_company(page, orgnr))
                            break
                        except Exception as exc:
                            if attempt == MAX_ATTEMPTS:
                                result = empty_result(lead, f"error:{type(exc).__name__}")
                                print(
                                    f"{orgnr}: {type(exc).__name__} efter {MAX_ATTEMPTS} försök; "
                                    "pausa längre innan fortsatt körning",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                break

                            backoff = BACKOFF_SECONDS[attempt - 1]
                            print(
                                f"{orgnr}: {type(exc).__name__}, försök {attempt}/{MAX_ATTEMPTS}; "
                                f"väntar {backoff:.0f}s",
                                file=sys.stderr,
                                flush=True,
                            )
                            page.close()
                            if proxies:
                                context.close()
                                proxy_index = (proxy_index + 1) % len(proxies)
                                context = new_context(browser, proxies[proxy_index])
                            page = context.new_page()
                            page.set_default_timeout(INTERACTION_TIMEOUT_MS)
                            time.sleep(backoff)

                    if result.get("match_status", "").startswith("error:"):
                        consecutive_errors += 1
                    else:
                        consecutive_errors = 0

                    writer.writerow(result)
                    handle.flush()
                    print(
                        f"{index}/{len(leads)} {orgnr} "
                        f"{result.get('match_status')} "
                        f"{result.get('bolag_namn', '')} "
                        f"{result.get('kontakt_telefon', '')}",
                        flush=True,
                    )
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise RuntimeError(
                            f"{MAX_CONSECUTIVE_ERRORS} konsekutiva fel; möjlig rate limit. "
                            f"Återuppta med --offset {offset + index} efter en paus."
                        )
                    time.sleep(DELAY_SECONDS + uniform(0.0, 1.5))

                    if index % context_rotate_every == 0:
                        context.close()
                        if proxies:
                            proxy_index = (proxy_index + 1) % len(proxies)
                        context = new_context(browser, proxies[proxy_index] if proxies else "")
                        page = context.new_page()
                        page.set_default_timeout(INTERACTION_TIMEOUT_MS)
            finally:
                context.close()
                browser.close()

    os.replace(temporary_output, output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Testa telefonberikning på finansiella leads")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(os.environ.get("LEADS_INPUT", "../test_leads_100.csv")),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("LEADS_OUTPUT", "exports/test_leads_100_phone_enrichment.csv")),
    )
    parser.add_argument("--limit", type=int, default=0, help="How many leads to process; 0 means all")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--resume-output",
        type=Path,
        default=None,
        help="Existing result CSV to preserve and retry error rows from",
    )
    args = parser.parse_args()
    enrich_leads(args.input, args.output, args.limit, args.offset, args.resume_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
