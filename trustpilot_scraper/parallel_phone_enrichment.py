#!/usr/bin/env python3
"""Parallel, resumable phone enrichment backed by a SQLite job queue."""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import shutil
import sqlite3
import sys
import time
from tempfile import mkdtemp
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
        SKIP_PROXY_PREFLIGHT,
    empty_result,
    find_hitta_company,
    load_proxies,
    new_context,
    validate_proxies,
    write_live_proxies,
)

# Keep the aggressive mode capped so an accidental higher CLI value cannot
# create an unbounded number of Chromium processes on the VPS.
MAX_WORKERS = max(1, min(10, (os.cpu_count() or 2) * 5))
CONTEXT_ROTATE_EVERY = 25
FULL_BROWSER_RESTART_EVERY = 50
WORKER_STALE_SECONDS = 180
WATCHDOG_INTERVAL_SECONDS = 15
WORKER_TMP_ROOT = Path(os.environ.get("PHONE_ENRICHMENT_TMPDIR", "/tmp/phone-enrichment-workers"))
MIN_FREE_SPACE_MB = int(os.environ.get("PHONE_ENRICHMENT_MIN_FREE_MB", "300"))

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


def ensure_disk_space(path: Path) -> None:
    free_bytes = shutil.disk_usage(path).free
    minimum_bytes = MIN_FREE_SPACE_MB * 1024 * 1024
    if free_bytes < minimum_bytes:
        free_mb = free_bytes / (1024 * 1024)
        raise RuntimeError(
            f"För lite ledigt utrymme i {path}: {free_mb:.0f} MB kvar, "
            f"minst {MIN_FREE_SPACE_MB} MB krävs"
        )


def cleanup_stale_worker_dirs() -> None:
    WORKER_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 60 * 60
    for path in WORKER_TMP_ROOT.glob("worker-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def process_lead(page, lead: Dict[str, str], worker: str) -> Dict[str, str]:
    orgnr = "".join(ch for ch in lead.get("orgnr", "") if ch.isdigit())
    result = empty_result(lead, "error:unknown")
    result.update(find_hitta_company(page, orgnr))
    return result


def worker_main(database: str, proxies: List[str], worker_number: int) -> None:
    worker = f"worker-{worker_number}-{os.getpid()}"
    connection = connect(Path(database))
    context = None
    browser = None
    worker_tmp = None
    proxy_index = (worker_number - 1) % len(proxies)
    try:
        cleanup_stale_worker_dirs()
        worker_tmp = Path(mkdtemp(prefix=f"worker-{os.getpid()}-", dir=WORKER_TMP_ROOT))
        os.environ["TMPDIR"] = str(worker_tmp)
        os.environ["TMP"] = str(worker_tmp)
        os.environ["TEMP"] = str(worker_tmp)
        ensure_disk_space(worker_tmp)
        with sync_playwright() as playwright:
            def launch_browser():
                launch_options = {"headless": True, "args": BROWSER_ARGS}
                if BROWSER_EXECUTABLE:
                    launch_options["executable_path"] = BROWSER_EXECUTABLE
                return playwright.chromium.launch(**launch_options)

            browser = launch_browser()
            context = new_context(browser, proxies[proxy_index])
            page = context.new_page()
            page.set_default_timeout(INTERACTION_TIMEOUT_MS)
            processed = 0

            def rotate_proxy() -> None:
                nonlocal context, page, proxy_index
                page.close()
                context.close()
                proxy_index = (proxy_index + 1) % len(proxies)
                context = new_context(browser, proxies[proxy_index])
                page = context.new_page()
                page.set_default_timeout(INTERACTION_TIMEOUT_MS)

            def reset_context() -> None:
                nonlocal browser, context, page
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                if not browser.is_connected():
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = launch_browser()
                context = new_context(browser, proxies[proxy_index])
                page = context.new_page()
                page.set_default_timeout(INTERACTION_TIMEOUT_MS)

            def restart_browser() -> None:
                nonlocal browser, context, page
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                browser = launch_browser()
                context = new_context(browser, proxies[proxy_index])
                page = context.new_page()
                page.set_default_timeout(INTERACTION_TIMEOUT_MS)

            while True:
                ensure_disk_space(worker_tmp)
                claimed = claim_job(connection, worker)
                if claimed is None:
                    break
                row_index, lead_json, _attempts = claimed
                lead = json.loads(lead_json)
                orgnr = "".join(ch for ch in lead.get("orgnr", "") if ch.isdigit())
                result = empty_result(lead, "error:unknown")
                last_error = "UnknownError"
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        result = process_lead(page, lead, worker)
                        break
                    except PlaywrightTimeoutError:
                        last_error = "TimeoutError"
                        print(
                            f"{worker} {orgnr}: timeout på proxy {proxy_index + 1}/{len(proxies)}; "
                            "byter proxy direkt",
                            flush=True,
                        )
                        rotate_proxy()
                    except Exception as exc:
                        last_error = type(exc).__name__
                        target_closed = type(exc).__name__ == "TargetClosedError"
                        reset_context()
                        if target_closed:
                            print(
                                f"{worker} {orgnr}: target stängt; byggde om context och försöker igen",
                                flush=True,
                            )
                            continue
                        if attempt == MAX_ATTEMPTS:
                            result = empty_result(lead, f"error:{type(exc).__name__}")
                            break
                        backoff = BACKOFF_SECONDS[attempt - 1]
                        print(
                            f"{worker} {orgnr}: {type(exc).__name__}, försök {attempt}/{MAX_ATTEMPTS}; "
                            f"väntar {backoff:.0f}s",
                            flush=True,
                        )
                        time.sleep(backoff)
                if result.get("match_status") == "error:unknown":
                    result = empty_result(lead, f"error:{last_error}")
                connection.execute(
                    "UPDATE jobs SET result_json = ?, status = 'done', updated_at = CURRENT_TIMESTAMP WHERE row_index = ? AND worker = ?",
                    (json.dumps(result), row_index, worker),
                )
                connection.commit()
                processed += 1
                print(
                    f"{worker} row={row_index + 1} "
                    f"orgnr={lead.get('orgnr', '')} "
                    f"bolag={result.get('bolag_namn', '')} "
                    f"telefon={result.get('kontakt_telefon', '')} "
                    f"status={result.get('match_status')}",
                    flush=True,
                )
                if processed % CONTEXT_ROTATE_EVERY == 0:
                    reset_context()
                if processed % FULL_BROWSER_RESTART_EVERY == 0:
                    restart_browser()
                time.sleep(DELAY_SECONDS)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        connection.close()
        if worker_tmp is not None:
            shutil.rmtree(worker_tmp, ignore_errors=True)


def resolve_proxies() -> List[str]:
    if PROXY_LIVE_FILE and not SKIP_PROXY_PREFLIGHT and Path(PROXY_LIVE_FILE).exists():
        proxies = [line.strip() for line in Path(PROXY_LIVE_FILE).read_text(encoding="utf-8").splitlines() if line.strip()]
        if proxies:
            print(f"Återanvänder {len(proxies)} tidigare live-proxies", flush=True)
            return proxies

    candidates = load_proxies()
    print(f"Laddade {len(candidates)} proxykandidater från {PROXY_LIST_URL}", flush=True)
    if not candidates:
        raise RuntimeError("Inga proxykandidater hittades")
    if SKIP_PROXY_PREFLIGHT:
        print("Hoppar över proxy-preflight; roterar vidare vid timeout", flush=True)
        return candidates
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


def reset_worker_claims(database: Path, worker: str) -> int:
    connection = connect(database)
    connection.execute(
        "UPDATE jobs SET status = 'pending', worker = NULL, updated_at = CURRENT_TIMESTAMP "
        "WHERE status = 'claimed' AND worker = ?",
        (worker,),
    )
    reset = connection.total_changes
    connection.commit()
    connection.close()
    return reset


def stale_workers(database: Path) -> List[str]:
    connection = connect(database)
    rows = connection.execute(
        "SELECT DISTINCT worker FROM jobs "
        "WHERE status = 'claimed' AND worker IS NOT NULL "
        "AND (julianday('now') - julianday(updated_at)) * 86400 > ?",
        (WORKER_STALE_SECONDS,),
    ).fetchall()
    connection.close()
    return [row[0] for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallell och återupptagningsbar Hitta-berikning")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--resume-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    leads = read_rows(args.input)
    existing = read_rows(args.resume_output) if args.resume_output.exists() else []
    args.database.parent.mkdir(parents=True, exist_ok=True)
    prepare_jobs(args.database, leads, existing, args.reset)
    proxies = resolve_proxies()
    worker_count = min(max(1, args.workers), MAX_WORKERS, len(proxies))
    print(f"Startar {worker_count} workers med {len(proxies)} live-proxies", flush=True)
    processes = {}
    failed_exitcodes = []

    def start_worker(worker_number: int) -> None:
        process = mp.Process(target=worker_main, args=(str(args.database), proxies, worker_number))
        process.start()
        processes[worker_number] = process

    for worker_number in range(1, worker_count + 1):
        start_worker(worker_number)

    while processes:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        stale = set(stale_workers(args.database))
        for worker_number, process in list(processes.items()):
            worker_prefix = f"worker-{worker_number}-"
            worker_names = [worker for worker in stale if worker.startswith(worker_prefix)]
            if worker_names and process.is_alive():
                print(
                    f"Watchdog: {worker_names[0]} har varit stale i över "
                    f"{WORKER_STALE_SECONDS}s; startar om worker",
                    flush=True,
                )
                process.terminate()
                process.join(timeout=10)
                reset_worker_claims(args.database, worker_names[0])
                start_worker(worker_number)
            elif not process.is_alive():
                process.join()
                if process.exitcode:
                    failed_exitcodes.append(process.exitcode)
                del processes[worker_number]
    if failed_exitcodes:
        raise RuntimeError("Minst en worker avslutades med fel; kör samma kommando igen för resume")
    export_results(args.database, args.output, leads)
    print(f"Klar: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
