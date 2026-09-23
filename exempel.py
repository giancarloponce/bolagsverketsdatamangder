#!/usr/bin/env python3
"""Cron-friendly downloader for Bolagsverkets bulk ZIP files.

Detta skript är tänkt att köras en gång per dag via cron. Det hämtar endast
nödvändiga bulkfiler om de saknas eller om de är äldre än en dag, och sparar
all data i projektets finansiella katalog.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "financials"
LOG_DIR = PROJECT_ROOT / ".logs"
LOG_FILE = LOG_DIR / "download.log"
STATE_FILE = PROJECT_ROOT / ".download_state.json"
MIN_FREE_GB = 5
DEFAULT_MAX_AGE_HOURS = 24

BULK_URLS = {
    "scb_bulkfil.zip": "https://vardefulla-datamangder.bolagsverket.se/scb/scb_bulkfil.zip",
    "bolagsverket_bulkfil.zip": "https://vardefulla-datamangder.bolagsverket.se/bolagsverket/bolagsverket_bulkfil.zip",
}

ARSREDOVISNINGAR_BASE = "https://vardefulla-datamangder.bolagsverket.se/arsredovisningar-bulkfiler/arsredovisningar/2026/"
ARSREDOVISNINGAR_DIR = DATA_DIR / "arsredovisningar" / "2026"


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> logging.Logger:
    ensure_directories()
    logger = logging.getLogger("bolagsverket_sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def get_free_gb(path: Path) -> float:
    try:
        total, used, free = shutil.disk_usage(path)
        return free / (1024**3)
    except OSError:
        return 0.0


def check_disk_space(logger: logging.Logger) -> None:
    candidates = [
        (DATA_DIR, "datamapp"),
        (PROJECT_ROOT, "projektkatalog"),
    ]

    for path, label in candidates:
        free_gb = get_free_gb(path)
        if free_gb <= 0:
            logger.warning("Kunde inte läsa ledigt utrymme för %s (%s), hoppar över kontroll.", label, path)
            continue

        logger.info("Ledigt utrymme på %s (%s): %.1f GB", label, path, free_gb)
        if free_gb < MIN_FREE_GB:
            raise RuntimeError(
                f"För lite ledigt utrymme på {label} ({path}): {free_gb:.1f} GB < {MIN_FREE_GB} GB"
            )

    logger.info("Tillräckligt med utrymme i datamappen för att fortsätta.")


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_stale(target: Path, max_age_hours: int) -> bool:
    if not target.exists():
        return True

    age = datetime.now() - datetime.fromtimestamp(target.stat().st_mtime)
    return age > timedelta(hours=max_age_hours)


def download_file(url: str, target: Path, logger: logging.Logger) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".part")

    logger.info("Hämtar %s -> %s", url, target)
    headers = {"User-Agent": "BolagsverketSync/1.0 (+cron-job)"}

    response = requests.get(url, headers=headers, timeout=120, stream=True)
    response.raise_for_status()

    with open(tmp_path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)

    tmp_path.replace(target)
    logger.info("Klar: %s", target.name)


def iter_arsredovisningar_candidates(max_section: int = 38, max_variant: int = 3):
    for section in range(1, max_section + 1):
        for variant in range(1, max_variant + 1):
            yield f"{section:02d}_{variant}.zip"


def sync_arsredovisningar(
    logger: logging.Logger,
    force: bool = False,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    dry_run: bool = False,
    max_section: int = 38,
    max_variant: int = 3,
) -> dict:
    ARSREDOVISNINGAR_DIR.mkdir(parents=True, exist_ok=True)
    updated = []
    skipped = []
    failed = []

    for section in range(1, max_section + 1):
        for variant in range(1, max_variant + 1):
            filename = f"{section:02d}_{variant}.zip"
            url = f"{ARSREDOVISNINGAR_BASE}{filename}"
            target = ARSREDOVISNINGAR_DIR / filename

            if not force and target.exists() and not is_stale(target, max_age_hours):
                skipped.append(filename)
                logger.info(
                    "Ingen uppdatering behövs för %s (senaste ändring: %s)",
                    filename,
                    datetime.fromtimestamp(target.stat().st_mtime).isoformat(),
                )
                continue

            if dry_run:
                logger.info("DRY-RUN: skulle hämta %s -> %s", url, target)
                updated.append(filename)
                continue

            try:
                response = requests.get(url, timeout=30, allow_redirects=True)
                if response.status_code == 404:
                    logger.info("Kunde inte hitta fler årsredovisningsfiler vid %s – stoppar loop.", url)
                    return {
                        "updated": updated,
                        "skipped": skipped,
                        "failed": failed,
                        "dry_run": dry_run,
                    }
                response.raise_for_status()
                download_file(url, target, logger)
                updated.append(filename)
            except requests.RequestException as exc:
                logger.warning("Misslyckades att hämta %s: %s", url, exc)
                failed.append(filename)
                return {
                    "updated": updated,
                    "skipped": skipped,
                    "failed": failed,
                    "dry_run": dry_run,
                }

    return {
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }


def sync_data(force: bool = False, max_age_hours: int = DEFAULT_MAX_AGE_HOURS, dry_run: bool = False) -> dict:
    logger = configure_logging()
    check_disk_space(logger)

    state = read_state()
    updated = []
    skipped = []
    failed = []

    for filename, url in BULK_URLS.items():
        target = DATA_DIR / filename
        if not force and target.exists() and not is_stale(target, max_age_hours):
            skipped.append(filename)
            logger.info(
                "Ingen uppdatering behövs för %s (senaste ändring: %s)",
                filename,
                datetime.fromtimestamp(target.stat().st_mtime).isoformat(),
            )
            continue

        if dry_run:
            logger.info("DRY-RUN: skulle hämta %s -> %s", url, target)
            updated.append(filename)
            continue

        try:
            download_file(url, target, logger)
            state[filename] = datetime.now().isoformat()
            updated.append(filename)
        except Exception as exc:
            logger.exception("Misslyckades att hämta %s", filename)
            failed.append(filename)
            state[filename] = {"error": str(exc), "timestamp": datetime.now().isoformat()}

    ars_result = sync_arsredovisningar(
        logger,
        force=force,
        max_age_hours=max_age_hours,
        dry_run=dry_run,
    )
    updated.extend(ars_result["updated"])
    skipped.extend(ars_result["skipped"])
    failed.extend(ars_result["failed"])

    state["arsredovisningar_last_sync"] = datetime.now().isoformat()
    write_state(state)

    logger.info(
        "Synkronisering klar: uppdaterade=%s, hoppade över=%s, misslyckades=%s",
        len(updated),
        len(skipped),
        len(failed),
    )
    return {
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hämta Bolagsverkets bulkdata en gång per dag")
    parser.add_argument("--force", action="store_true", help="Tvinga återhämtning även om filerna redan finns")
    parser.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS, help="Hur gammal en fil får vara innan den uppdateras")
    parser.add_argument("--dry-run", action="store_true", help="Visa vilka filer som skulle hämtas utan att ladda ner något")
    args = parser.parse_args()

    try:
        result = sync_data(force=args.force, max_age_hours=args.max_age_hours, dry_run=args.dry_run)
        print(f"Uppdaterade: {len(result['updated'])}")
        print(f"Hoppade över: {len(result['skipped'])}")
        print(f"Misslyckades: {len(result['failed'])}")
        raise SystemExit(0 if not result["failed"] else 1)
    except Exception as exc:
        print(f"Fel: {exc}", file=sys.stderr)
        raise SystemExit(1)
