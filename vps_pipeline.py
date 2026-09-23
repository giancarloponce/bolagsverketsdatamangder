#!/usr/bin/env python3
"""Rebuild the project database from raw ZIP sources on a VPS."""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable, List

import requests
from tqdm import tqdm

from financial_parser import extract_financials_from_zip_bytes


BOLAGSVERKET_URLS = {
    "scb": "https://vardefulla-datamangder.bolagsverket.se/scb/scb_bulkfil.zip",
    "bolagsverket": "https://vardefulla-datamangder.bolagsverket.se/bolagsverket/bolagsverket_bulkfil.zip",
}


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def reset_sqlite_db(db_path: Path) -> None:
    ensure_dir(db_path.parent)
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path if suffix == "" else db_path.with_name(f"{db_path.name}{suffix}")
        if candidate.exists():
            candidate.unlink()


def download_file(url: str, out_path: Path) -> None:
    print(f"Downloading {url} -> {out_path}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)


def iter_zip_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.zip")):
        if path.is_file():
            yield path


def extract_xhtml_from_raw(raw_dir: Path, out_dir: Path) -> List[Path]:
    output_paths: List[Path] = []
    for zippath in iter_zip_files(raw_dir):
        try:
            with zipfile.ZipFile(zippath) as outer:
                for member in outer.infolist():
                    if not member.filename.lower().endswith(".zip"):
                        continue
                    try:
                        inner_bytes = outer.read(member)
                        with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
                            for inner_member in inner.infolist():
                                if inner_member.filename.lower().endswith((".html", ".htm", ".xhtml", ".ixbrl", ".xml")):
                                    data = inner.read(inner_member)
                                    rel_path = Path(member.filename).stem
                                    target = ensure_dir(out_dir / rel_path)
                                    out_file = target / Path(inner_member.filename).name
                                    out_file.write_bytes(data)
                                    output_paths.append(out_file)
                    except zipfile.BadZipFile:
                        continue
        except zipfile.BadZipFile:
            continue
    return output_paths


def populate_database(raw_dir: Path, db_path: Path, reset: bool = False) -> int:
    ensure_dir(db_path.parent)
    if reset:
        reset_sqlite_db(db_path)

    conn = sqlite3.connect(db_path, timeout=120.0)
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financials (
            orgnr TEXT,
            year INTEGER,
            omsattning REAL,
            resultat REAL,
            balansomslutning REAL,
            kundfordringar REAL,
            kassalikviditet REAL,
            kortfristiga_skulder REAL,
            varulager REAL,
            eget_kapital REAL,
            source TEXT,
            PRIMARY KEY (orgnr, year)
        )
    """)
    conn.commit()

    total = 0
    for zippath in iter_zip_files(raw_dir):
        try:
            payload = zippath.read_bytes()
            records = extract_financials_from_zip_bytes(payload)
        except Exception:
            continue

        for record in records:
            orgnr = record.get("orgnr")
            year = record.get("year")
            if not orgnr or not year:
                continue
            conn.execute(
                """
                INSERT INTO financials (orgnr, year, omsattning, resultat, balansomslutning,
                kundfordringar, kassalikviditet, kortfristiga_skulder, varulager, eget_kapital, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(orgnr, year) DO UPDATE SET
                    omsattning = excluded.omsattning,
                    resultat = excluded.resultat,
                    balansomslutning = excluded.balansomslutning,
                    kundfordringar = excluded.kundfordringar,
                    kassalikviditet = excluded.kassalikviditet,
                    kortfristiga_skulder = excluded.kortfristiga_skulder,
                    varulager = excluded.varulager,
                    eget_kapital = excluded.eget_kapital,
                    source = excluded.source
                """,
                (
                    str(orgnr),
                    int(year),
                    record.get("omsattning"),
                    record.get("resultat"),
                    record.get("balansomslutning"),
                    record.get("kundfordringar"),
                    record.get("kassalikviditet"),
                    record.get("kortfristiga_skulder"),
                    record.get("varulager"),
                    record.get("eget_kapital"),
                    zippath.name,
                ),
            )
            total += 1
    conn.commit()
    conn.close()
    return total


def download_bulk(raw_dir: Path) -> List[Path]:
    ensure_dir(raw_dir)
    saved: List[Path] = []
    for name, url in BOLAGSVERKET_URLS.items():
        target = raw_dir / f"{name}.zip"
        if not target.exists():
            download_file(url, target)
        saved.append(target)
    return saved


def main():
    parser = argparse.ArgumentParser(description="VPS pipeline for rebuilding Bolagsverket financial DB")
    parser.add_argument("--download", action="store_true", help="Download raw ZIP files")
    parser.add_argument("--extract", action="store_true", help="Extract XHTML from raw ZIP files")
    parser.add_argument("--populate", action="store_true", help="Populate SQLite database")
    parser.add_argument("--raw-dir", default="raw_downloads", help="Folder for raw ZIP files")
    parser.add_argument("--xhtml-dir", default="extracted_xhtml", help="Folder for extracted XHTML files")
    parser.add_argument("--db-path", default="db/factoring_leads.db", help="SQLite database output path")
    parser.add_argument("--reset", action="store_true", help="Delete any existing SQLite DB/WAL/SHM files before populating")
    args = parser.parse_args()

    if not any([args.download, args.extract, args.populate]):
        parser.error("Choose at least one action: --download, --extract or --populate")

    if args.download:
        files = download_bulk(Path(args.raw_dir))
        print(f"Downloaded {len(files)} ZIP bundles")

    if args.extract:
        count = len(extract_xhtml_from_raw(Path(args.raw_dir), Path(args.xhtml_dir)))
        print(f"Extracted {count} XHTML files")

    if args.populate:
        count = populate_database(Path(args.raw_dir), Path(args.db_path), reset=args.reset)
        print(f"Inserted {count} financial records into SQLite")


if __name__ == "__main__":
    main()
