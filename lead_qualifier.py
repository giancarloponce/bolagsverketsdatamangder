#!/usr/bin/env python3
"""Qualify strong B2B leads for factoring based on annual report data.

This script scans the XHTML annual reports in the local financials directory,
extracts the key financial metrics, aggregates them per organisation number,
then ranks companies by a simple score that is useful for factoring lead
qualification.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from bs4 import BeautifulSoup

from financial_parser import METRIC_ALIASES, _normalize_metric_name, parse_numeric


@dataclass
class CompanyLead:
    orgnr: str
    latest_year: int
    revenue: float
    result: float
    equity: float
    balance: float
    receivables: float
    liquidity: float
    short_term_debt: float
    score: int
    reasons: List[str]


def normalize_orgnr(value: object) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return digits
    return None


def extract_report_year(html_text: str) -> Optional[int]:
    end_dates = re.findall(r"(?:xbrli:)?endDate[^>]*>(20\d{2})-(\d{2})-(\d{2})<", html_text, re.IGNORECASE)
    if end_dates:
        return max(int(year) for year, _, _ in end_dates)

    title_dates = re.findall(r"20\d{2}-\d{2}-\d{2}", html_text[:5000])
    if title_dates:
        return max(int(value[:4]) for value in title_dates)

    return None


def extract_metrics_from_html(html_text: str) -> Dict[str, float]:
    soup = BeautifulSoup(html_text, "html.parser")
    metrics: Dict[str, float] = {}

    for tag in soup.find_all(True):
        tag_name = str(tag.name or "").lower()
        if tag_name not in {"ix:nonfraction", "nonfraction"}:
            continue

        metric_name = tag.get("name")
        if not metric_name:
            continue
        canonical = _normalize_metric_name(metric_name)
        if not canonical:
            continue

        for field, aliases in METRIC_ALIASES.items():
            allowed = {_normalize_metric_name(alias) for alias in aliases}
            if canonical in allowed:
                value = parse_numeric(tag.get_text(" ", strip=True))
                if value is not None:
                    metrics[field] = value
                    break

    return metrics


def extract_company_data_from_html(path: Path) -> Optional[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")

    orgnr_match = re.search(r"\b\d{6}[-\s]?\d{4}\b", text)
    if not orgnr_match:
        return None

    orgnr = normalize_orgnr(orgnr_match.group(0))
    if not orgnr:
        return None

    year = extract_report_year(text)
    if year is None:
        return None

    metrics = extract_metrics_from_html(text)
    if not metrics:
        return None

    company = {
        "orgnr": orgnr,
        "year": year,
        "nettoomsattning": metrics.get("omsattning"),
        "resultat": metrics.get("resultat"),
        "balansomslutning": metrics.get("balansomslutning"),
        "kundfordringar": metrics.get("kundfordringar"),
        "kassalikviditet": metrics.get("kassalikviditet"),
        "kortfristiga_skulder": metrics.get("kortfristiga_skulder"),
        "varulager": metrics.get("varulager"),
        "eget_kapital": metrics.get("eget_kapital"),
        "source": str(path),
    }
    return company


def aggregate_company_records(records: Iterable[dict]) -> dict:
    by_orgnr: Dict[str, List[dict]] = {}
    for record in records:
        if not record:
            continue
        orgnr = record.get("orgnr")
        if not orgnr:
            continue
        by_orgnr.setdefault(orgnr, []).append(record)

    best: Dict[str, dict] = {}
    for orgnr, items in by_orgnr.items():
        latest = max(items, key=lambda r: r.get("year", 0))
        best[orgnr] = latest

    return best


def build_lead_score(metrics: dict) -> tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    revenue = float(metrics.get("nettoomsattning") or 0)
    result = float(metrics.get("resultat") or 0)
    equity = float(metrics.get("eget_kapital") or 0)
    balance = float(metrics.get("balansomslutning") or 0)
    receivables = float(metrics.get("kundfordringar") or 0)
    liquidity = float(metrics.get("kassalikviditet") or 0)
    short_term_debt = float(metrics.get("kortfristiga_skulder") or 0)

    if revenue >= 20_000_000:
        score += 25
        reasons.append("Omsättning stark")
    elif revenue >= 5_000_000:
        score += 18
        reasons.append("Tillräcklig omsättning")
    elif revenue > 0:
        score += 8
        reasons.append("Liten men positiv omsättning")
    else:
        reasons.append("Omsättning saknas")

    if result > 0:
        score += 25
        reasons.append("Positivt resultat")
    elif result == 0:
        score += 5
        reasons.append("Resultat i nollan")
    else:
        reasons.append("Negativt resultat")

    if equity > 0:
        score += 20
        reasons.append("Positivt eget kapital")
    else:
        reasons.append("Negativt eget kapital")

    if balance > 0:
        score += 5
        reasons.append("Balans finns")

    if short_term_debt > 0 and equity > 0:
        debt_ratio = short_term_debt / equity
        if debt_ratio <= 1.0:
            score += 15
            reasons.append("Skuldsättning hanterbar")
        elif debt_ratio <= 1.5:
            score += 8
            reasons.append("Skuldsättning acceptabel")
        else:
            reasons.append("Hög skuldsättning")
    else:
        if equity > 0:
            score += 10
            reasons.append("Låg skuldbörda")

    if liquidity > 0:
        score += 10
        reasons.append("Likviditet finns")
    else:
        reasons.append("Likviditet saknas")

    if receivables > 0 and revenue > 0:
        receivables_ratio = receivables / revenue
        if 0.05 <= receivables_ratio <= 0.8:
            score += 10
            reasons.append("Kundfordringar i rimlig nivå")
        elif receivables_ratio > 0.8:
            reasons.append("Mycket kundfordringar")

    if score >= 90:
        reasons = list(dict.fromkeys(reasons))
        return score, reasons

    reasons = list(dict.fromkeys(reasons))
    return score, reasons


def qualify_leads_from_directory(financial_dir: Path, limit: int = 0) -> List[CompanyLead]:
    records: List[dict] = []
    paths = sorted(financial_dir.rglob("*.xhtml"))
    print(f"XHTML-filer att bearbeta: {len(paths)}", flush=True)
    for index, path in enumerate(paths, start=1):
        record = extract_company_data_from_html(path)
        if record:
            records.append(record)
        if index == 1 or index % 100 == 0 or index == len(paths):
            print(
                f"Bearbetat XHTML: {index}/{len(paths)}; giltiga poster: {len(records)}",
                flush=True,
            )

    aggregated = aggregate_company_records(records)
    leads: List[CompanyLead] = []

    for orgnr, metrics in aggregated.items():
        score, reasons = build_lead_score(metrics)
        if score < 50:
            continue

        leads.append(
            CompanyLead(
                orgnr=orgnr,
                latest_year=int(metrics.get("year", 0)),
                revenue=float(metrics.get("nettoomsattning") or 0),
                result=float(metrics.get("resultat") or 0),
                equity=float(metrics.get("eget_kapital") or 0),
                balance=float(metrics.get("balansomslutning") or 0),
                receivables=float(metrics.get("kundfordringar") or 0),
                liquidity=float(metrics.get("kassalikviditet") or 0),
                short_term_debt=float(metrics.get("kortfristiga_skulder") or 0),
                score=score,
                reasons=reasons,
            )
        )

    leads.sort(key=lambda x: x.score, reverse=True)
    return leads[:limit] if limit > 0 else leads


def write_csv(leads: List[CompanyLead], out_path: Path) -> None:
    fieldnames = [
        "orgnr",
        "latest_year",
        "score",
        "revenue",
        "result",
        "equity",
        "balance",
        "receivables",
        "liquidity",
        "short_term_debt",
        "reasons",
    ]
    rows = []
    for lead in leads:
        rows.append(
            {
                "orgnr": lead.orgnr,
                "latest_year": lead.latest_year,
                "score": lead.score,
                "revenue": lead.revenue,
                "result": lead.result,
                "equity": lead.equity,
                "balance": lead.balance,
                "receivables": lead.receivables,
                "liquidity": lead.liquidity,
                "short_term_debt": lead.short_term_debt,
                "reasons": "; ".join(lead.reasons),
            }
        )

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rank strong factoring leads from annual reports")
    parser.add_argument("--financial-dir", type=Path, default=Path("financials"), help="Directory with XHTML annual report files")
    parser.add_argument("--limit", type=int, default=0, help="How many leads to print/export; 0 means all")
    parser.add_argument("--csv", type=Path, default=None, help="Optional output CSV path")
    args = parser.parse_args()

    financial_dir = args.financial_dir
    if not financial_dir.exists():
        raise FileNotFoundError(f"Directory not found: {financial_dir}")

    leads = qualify_leads_from_directory(financial_dir, limit=args.limit)
    if args.csv:
        write_csv(leads, args.csv)

    print(f"Qualified leads: {len(leads)}")
    print("ORGNR | YEAR | SCORE | REVENUE | RESULT | EQUITY | REASONS")
    for lead in leads:
        print(
            f"{lead.orgnr} | {lead.latest_year} | {lead.score} | "
            f"{lead.revenue:,.0f} | {lead.result:,.0f} | {lead.equity:,.0f} | "
            f"{'; '.join(lead.reasons[:4])}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
