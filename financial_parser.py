#!/usr/bin/env python3
"""Utilities for parsing financial reports from nested ZIP/iXBRL files."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from bs4 import BeautifulSoup

HTML_EXTENSIONS = (".html", ".htm", ".xhtml", ".ixbrl", ".xml")

METRIC_ALIASES = {
    "omsattning": {
        "nettoomsattning",
        "omsattning",
        "nettoomsättning",
        "omsättning",
    },
    "resultat": {
        "resultatefterfinansiellaposter",
        "resultat",
        "resultatefterfinansiella",
        "resultatefterfinansiella",
    },
    "balansomslutning": {
        "tillgangar",
        "tillgångar",
        "totaltillgangar",
        "totala tillgångar",
        "balansomslutning",
    },
    "kundfordringar": {"kundfordringar"},
    "kassalikviditet": {"kassamedel", "kassalikviditet"},
    "kortfristiga_skulder": {"kortfristigaskulder", "kortfristiga_skulder", "kortfristiga_skulder"},
    "varulager": {"varulager"},
    "eget_kapital": {"egetkapital", "eget_kapital", "aktiekapital", "egetkapital"},
}


def _normalize_metric_name(value: str) -> str:
    if value is None:
        return ""
    cleaned = str(value)
    cleaned = cleaned.split(":")[-1]
    cleaned = cleaned.replace("_", "").replace("-", "").replace(" ", "")
    return re.sub(r"[^a-zåäö0-9]", "", cleaned.lower())


def parse_numeric(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None

    text = text.replace("\xa0", " ").replace("_", "")
    text = text.strip()
    if text.endswith("%"):
        text = text[:-1].strip()

    text = text.replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if text.count(",") > 1:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", ".")
    elif "." in text and text.count(".") > 1:
        text = text.replace(".", "")

    try:
        return float(text)
    except ValueError:
        return None


def _iter_html_chunks(payload: bytes) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as outer_zip:
        for member in outer_zip.infolist():
            name_lower = member.filename.lower()
            if name_lower.endswith(".zip"):
                inner_bytes = outer_zip.read(member)
                try:
                    for inner_name, inner_bytes_value in _iter_html_chunks(inner_bytes):
                        yield inner_name, inner_bytes_value
                except zipfile.BadZipFile:
                    continue
            elif any(name_lower.endswith(ext) for ext in HTML_EXTENSIONS):
                yield member.filename, outer_zip.read(member)


def _extract_orgnr_from_html(html_text: str) -> Optional[str]:
    candidates = re.findall(r"\b\d{6}[-\s]?\d{4}\b", html_text)
    if candidates:
        return re.sub(r"\D", "", candidates[0])

    match = re.search(r"Organisationsnummer.*?(\d{6}[-\s]?\d{4})", html_text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r"\D", "", match.group(1))

    numbers = re.findall(r"\b\d{10,12}\b", html_text)
    if numbers:
        return re.sub(r"\D", "", numbers[0])
    return None


def _extract_year_from_html(html_text: str) -> Optional[int]:
    matches = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", html_text)
    if matches:
        year = int(matches[0][0])
        return year

    matches = re.findall(r"\b(\d{4})\b", html_text)
    if matches:
        for value in matches:
            if value.startswith("20"):
                return int(value)
    return None


def _extract_metrics_from_soup(soup: BeautifulSoup) -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    seen = set()

    for tag in soup.find_all("ix:nonFraction"):
        metric_name = tag.get("name")
        if not metric_name:
            continue
        canonical = _normalize_metric_name(metric_name)
        for field, aliases in METRIC_ALIASES.items():
            allowed = {_normalize_metric_name(alias) for alias in aliases}
            if canonical in allowed:
                value = parse_numeric(tag.get_text(" ", strip=True))
                if value is not None:
                    metrics[field] = value
                    seen.add(field)

    if not metrics:
        for tag in soup.find_all(attrs={"name": True}):
            metric_name = tag.get("name")
            canonical = _normalize_metric_name(metric_name)
            if not canonical:
                continue
            for field, aliases in METRIC_ALIASES.items():
                allowed = {_normalize_metric_name(alias) for alias in aliases}
                if canonical in allowed:
                    value = parse_numeric(tag.get_text(" ", strip=True))
                    if value is not None and field not in seen:
                        metrics[field] = value
                        seen.add(field)

    return metrics


def _build_record_from_html_text(html_text: str) -> Optional[dict]:
    if not html_text:
        return None

    orgnr = _extract_orgnr_from_html(html_text)
    year = _extract_year_from_html(html_text)
    if not orgnr or not year:
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    metrics = _extract_metrics_from_soup(soup)
    if not metrics:
        return None

    return {
        "orgnr": orgnr,
        "year": year,
        "omsattning": metrics.get("omsattning"),
        "resultat": metrics.get("resultat"),
        "balansomslutning": metrics.get("balansomslutning"),
        "kundfordringar": metrics.get("kundfordringar"),
        "kassalikviditet": metrics.get("kassalikviditet"),
        "kortfristiga_skulder": metrics.get("kortfristiga_skulder"),
        "varulager": metrics.get("varulager"),
        "eget_kapital": metrics.get("eget_kapital"),
    }


def extract_financials_from_zip_bytes(payload: bytes) -> List[dict]:
    """Return parsed financial records from a ZIP archive or a raw XHTML file."""
    if not payload:
        return []

    try:
        if not zipfile.is_zipfile(io.BytesIO(payload)):
            html_text = payload.decode("utf-8", errors="replace")
            record = _build_record_from_html_text(html_text)
            return [record] if record else []
    except Exception:
        html_text = payload.decode("utf-8", errors="replace")
        record = _build_record_from_html_text(html_text)
        return [record] if record else []

    records: List[dict] = []

    for _, file_bytes in _iter_html_chunks(payload):
        try:
            html_text = file_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue

        record = _build_record_from_html_text(html_text)
        if record is not None:
            records.append(record)

    return records


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Parse nested ZIP/XHTML financial reports")
    parser.add_argument("--zip", type=str, help="ZIP file to inspect")
    args = parser.parse_args()

    if not args.zip:
        parser.error("--zip is required")

    zip_path = Path(args.zip)
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    records = extract_financials_from_zip_bytes(zip_path.read_bytes())
    print(f"Found {len(records)} financial records")
    for record in records[:5]:
        print(record)


if __name__ == "__main__":
    main()
