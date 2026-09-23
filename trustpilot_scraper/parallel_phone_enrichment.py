#!/usr/bin/env python3
"""Parallel, resumable phone enrichment backed by a SQLite job queue."""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phone_enrichment_test import (  # noqa: E402
    BACKOFF_SECONDS,
    BROWSER_ARGS,
    BROWSER_EXECUTABLE,
    DELAY_SECONDS,
    INTERACTION_TIMEOUT_MS,
    MAX_ATTEMPTS,
    PROXY_LIVE_FILE,
    PROXY_LIST_FILES,
    PROXY_LIST_FILE,
    PROXY_LIMIT,
    PROXY_SCHEME,
    PROXY_VALIDATE_TIMEOUT,
    PROXY_VALIDATE_URL,
    PROXY_LIST_URL,
    empty_result,
    find_hitta_company,
    load_proxies,
    new_context,
    validate_proxies,
    write_live_proxies,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    row_index INTEGER PRIMARY KEY,
    lead_json TEXT NOT NULL,
    result_json TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    worker TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=60)
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(SCHEMA)
    connection.commit()
    return connection


def prepare_jobs(
    database: Path,
    leads: List[Dict[str, str]],
    existing: List[Dict[str, str]],
    reset: bool,
) -> None:
    connection = connect(database)
    if reset:
        connection.execute("DELETE FROM jobs")
    connection.execute("UPDATE jobs SET status = 'pending', worker = NULL WHERE status = 'claimed'")
    for index, lead in enumerate(leads):
        if index < len(existing) and not existing[index].get("match_status", "").startswith("error:"):
            result = existing[index]
            status = "done"
        else:
            result = None
            status = "pending"
        connection.execute(
            """
            INSERT INTO jobs(row_index, lead_json, result_json, status, attempts, worker)
            VALUES (?, ?, ?, ?, 0, NULL)
            ON CONFLICT(row_index) DO UPDATE SET
                lead_json = excluded.lead_json,
                result_json = CASE WHEN jobs.status = 'done' THEN jobs.result_json ELSE excluded.result_json END,
                status = CASE WHEN jobs.status = 'done' THEN 'done' ELSE excluded.status END,
                worker = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (index, json.dumps(lead), json.dumps(result) if result else None, status),
        )
    connection.commit()
    pending = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'").fetchone()[0]
    done = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'done'").fetchone()[0]
    connection.close()
    print(f"Jobb förberedda: {done} klara, {pending} pending av {len(leads)}", flush=True)


def claim_job(connection: sqlite3.Connection, worker: str):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT row_index, lead_json, attempts FROM jobs WHERE status = 'pending' ORDER BY row_index LIMIT 1"
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute(
        "UPDATE jobs SET status = 'claimed', attempts = attempts + 1, worker = ?, updated_at = CURRENT_TIMESTAMP WHERE row_index = ?",
        (worker, row[0]),
    )
    connection.commit()
    return row


def process_lead(page, lead: Dict[str, str], worker: str) -> Dict[str, str]:
    orgnr = "".join(ch for ch in lead.get("orgnr", "") if ch.isdigit())
    result = empty_result(lead, "error:unprocessed")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result.update(find_hitta_company(page, orgnr))
            return result
        except Exception as exc:
            if attempt == MAX_ATTEMPTS:
                return empty_result(lead, f"error:{type(exc).__name__}")
            backoff = BACKOFF_SECONDS[attempt - 1]
            print(f"{worker} {orgnr}: {type(exc).__name__}, försök {attempt}/{MAX_ATTEMPTS}; väntar {backoff:.0f}s", flush=True)
            time.sleep(backoff)
    return result


def worker_main(database: str, proxy: str, worker_number: int) -> None:
    worker = f"worker-{worker_number}-{os.getpid()}"
    connection = connect(Path(database))
    context = None
    browser = None
    try:
        with sync_playwright() as playwright:
            launch_options = {"headless": True, "args": BROWSER_ARGS}
            if BROWSER_EXECUTABLE:
                launch_options["executable_path"] = BROWSER_EXECUTABLE
            browser = playwright.chromium.launch(**launch_options)
            context = new_context(browser, proxy)
            page = context.new_page()
            page.set_default_timeout(INTERACTION_TIMEOUT_MS)
            while True:
                claimed = claim_job(connection, worker)
                if claimed is None:
                    break
                row_index, lead_json, _attempts = claimed
                lead = json.loads(lead_json)
                result = process_lead(page, lead, worker)
                if result.get("match_status", "").startswith("error:"):
                    page.close()
                    context.close()
                    context = new_context(browser, proxy)
                    page = context.new_page()
                    page.set_default_timeout(INTERACTION_TIMEOUT_MS)
                connection.execute(
                    "UPDATE jobs SET result_json = ?, status = 'done', updated_at = CURRENT_TIMESTAMP WHERE row_index = ? AND worker = ?",
                    (json.dumps(result), row_index, worker),
                )
                connection.commit()
                print(
                    f"{worker} row={row_index + 1} {lead.get('orgnr', '')} "
                    f"{result.get('match_status')} {result.get('kontakt_telefon', '')}",
                    flush=True,
                )
                time.sleep(DELAY_SECONDS)
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        connection.close()


def resolve_proxies() -> List[str]:
    if PROXY_LIVE_FILE and Path(PROXY_LIVE_FILE).exists():
        proxies = [line.strip() for line in Path(PROXY_LIVE_FILE).read_text(encoding="utf-8").splitlines() if line.strip()]
        if proxies:
            print(f"Återanvänder {len(proxies)} tidigare live-proxies", flush=True)
            return proxies

    candidates = load_proxies()
    print(f"Laddade {len(candidates)} proxykandidater från {PROXY_LIST_URL}", flush=True)
    if not candidates:
        raise RuntimeError("Inga proxykandidater hittades")
    with sync_playwright() as playwright:
        launch_options = {"headless": True, "args": BROWSER_ARGS}
        if BROWSER_EXECUTABLE:
            launch_options["executable_path"] = BROWSER_EXECUTABLE
        browser = playwright.chromium.launch(**launch_options)
        try:
            proxies = validate_proxies(browser, candidates)
        finally:
            browser.close()
    write_live_proxies(proxies)
    print(f"Validerade {len(proxies)}/{len(candidates)} proxies mot {PROXY_VALIDATE_URL}", flush=True)
    if not proxies:
        raise RuntimeError("Ingen proxy klarade live-testet mot målwebbplatsen")
    return proxies


def export_results(database: Path, output: Path, leads: List[Dict[str, str]]) -> None:
    connection = connect(database)
    rows = connection.execute("SELECT row_index, result_json, status FROM jobs ORDER BY row_index").fetchall()
    if len(rows) != len(leads) or any(status != "done" for _, _, status in rows):
        pending = sum(status != "done" for _, _, status in rows)
        connection.close()
        raise RuntimeError(f"Kan inte exportera: {pending} jobb återstår")
    fieldnames = list(leads[0].keys()) + [
        "match_status", "matched_orgnr", "bolag_namn", "kontakt_telefon",
        "kontakt_email", "webbplats", "kontakt_kalla", "kontakt_url",
    ]
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for _, result_json, _ in rows:
            writer.writerow(json.loads(result_json))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallell och återupptagningsbar Hitta-berikning")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--resume-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    leads = read_rows(args.input)
    existing = read_rows(args.resume_output) if args.resume_output.exists() else []
    args.database.parent.mkdir(parents=True, exist_ok=True)
    prepare_jobs(args.database, leads, existing, args.reset)
    proxies = resolve_proxies()
    worker_count = min(max(1, args.workers), len(proxies))
    print(f"Startar {worker_count} workers med {len(proxies)} live-proxies", flush=True)
    processes = [
        mp.Process(target=worker_main, args=(str(args.database), proxies[index], index + 1))
        for index in range(worker_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    if any(process.exitcode for process in processes):
        raise RuntimeError("Minst en worker avslutades med fel; kör samma kommando igen för resume")
    export_results(args.database, args.output, leads)
    print(f"Klar: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
