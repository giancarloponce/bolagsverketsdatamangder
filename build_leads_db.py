#!/usr/bin/env python3
"""Import an enriched lead CSV into a small SQLite database."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def import_csv(input_path: Path, db_path: Path) -> int:
    with input_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise ValueError(f"CSV saknar kolumner: {input_path}")
        rows = list(reader)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        columns = ", ".join(f"{quote_identifier(name)} TEXT" for name in fieldnames)
        connection.execute(f"CREATE TABLE IF NOT EXISTS leads ({columns})")
        connection.execute("DELETE FROM leads")
        placeholders = ", ".join("?" for _ in fieldnames)
        column_names = ", ".join(quote_identifier(name) for name in fieldnames)
        connection.executemany(
            f"INSERT INTO leads ({column_names}) VALUES ({placeholders})",
            [[row.get(name, "") for name in fieldnames] for row in rows],
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_leads_orgnr ON leads (orgnr)")
        connection.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Importera berikad lead-CSV till SQLite")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    count = import_csv(args.input, args.db)
    print(f"Importerade {count} leads till {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())