#!/usr/bin/env python3
"""Extrahera Bolagsverkets bulk ZIP-filer på VPS.

Detta skript är avsett för VPS-sökvägarna under /root/scripts/financials.
Det hanterar både vanliga bulk-ZIP-filer och kapslade årsredovisnings-ZIP-filer,
där varje årsredovisningsfil kan innehålla ytterligare ZIP-filer.
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path
from typing import Iterable, List

DEFAULT_SOURCES = [
    Path("/root/scripts/financials/arsredovisningar/2026"),
    Path("/root/scripts/financials/bolagsverket_bulkfil.zip"),
    Path("/root/scripts/financials/scb_bulkfil.zip"),
]
OUTPUT_ROOT = Path("/root/scripts/financials/extracted")


def safe_member_path(name: str) -> Path:
    cleaned = name.replace("\\", "/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".", "..")]
    return Path(*parts)


def iter_zip_files(path: Path) -> Iterable[Path]:
    if path.is_dir():
        for item in sorted(path.rglob("*.zip")):
            if item.is_file():
                yield item
    elif path.is_file() and path.suffix.lower() == ".zip":
        yield path


def write_member(target_dir: Path, member_name: str, data: bytes) -> None:
    target_path = target_dir / safe_member_path(member_name)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)


def extract_zip_bytes(payload: bytes, target_dir: Path, depth: int = 0, max_depth: int = 20) -> int:
    if depth > max_depth:
        raise RuntimeError(f"För djup kapslad ZIP-struktur vid {target_dir}")

    extracted = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue

            member_name = member.filename
            member_data = archive.read(member)
            lower_name = member_name.lower()

            if lower_name.endswith(".zip"):
                nested_dir = target_dir / Path(member_name).stem
                nested_dir.mkdir(parents=True, exist_ok=True)
                extracted += extract_zip_bytes(member_data, nested_dir, depth + 1, max_depth)
                continue

            write_member(target_dir, member_name, member_data)
            extracted += 1

    return extracted


def process_zip_file(zip_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = zip_path.read_bytes()
    return extract_zip_bytes(payload, out_dir)


def process_directory(input_dir: Path, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_dirs: List[Path] = []

    for zip_path in iter_zip_files(input_dir):
        target_dir = out_dir / zip_path.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        count = process_zip_file(zip_path, target_dir)
        extracted_dirs.append(target_dir)
        print(f"{zip_path} -> {target_dir} ({count} filer extraherade)")

    return extracted_dirs


def process_source(source: Path, output_root: Path) -> List[Path]:
    if source.is_dir():
        target_dir = output_root / source.name
        return process_directory(source, target_dir)

    if source.is_file() and source.suffix.lower() == ".zip":
        target_dir = output_root / source.stem
        count = process_zip_file(source, target_dir)
        print(f"{source} -> {target_dir} ({count} filer extraherade)")
        return [target_dir]

    raise FileNotFoundError(f"Källa finns inte eller stödjs inte: {source}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrahera ZIP-filer från Bolagsverkets bulkdata på VPS")
    parser.add_argument("--source", action="append", type=Path, help="Sökväg till ZIP-fil eller katalog att extrahera")
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT, help="Rotkatalog för extraherade filer")
    args = parser.parse_args()

    sources = args.source if args.source else DEFAULT_SOURCES
    output_root = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)

    total = 0
    for source in sources:
        if not source.exists():
            print(f"Hoppar över saknad källa: {source}", file=sys.stderr)
            continue
        target_dirs = process_source(source, output_root)
        total += len(target_dirs)

    print(f"Klar: extraherade {total} katalog(er)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
