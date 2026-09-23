#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${DATA_DIR:-/root/scripts/financials/extracted}"
XHTML_DIR="${XHTML_DIR:-$DATA_DIR/2026}"
DB_PATH="${DB_PATH:-$APP_DIR/db/factoring_leads.db}"
LEADS_OUTPUT="${LEADS_OUTPUT:-$APP_DIR/exports/leads_all_phone_enrichment.csv}"
PLAYWRIGHT_EXECUTABLE_PATH="${PLAYWRIGHT_EXECUTABLE_PATH:-/usr/bin/chromium-browser}"

cd "$APP_DIR"

if [[ ! -x .venv/bin/python ]]; then
  echo "Skapar Python-miljö i $APP_DIR/.venv"
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
fi

source .venv/bin/activate

python -u -m lead_qualifier \
  --financial-dir "$XHTML_DIR" \
  --limit "${LEAD_LIMIT:-0}" \
  --csv "$APP_DIR/exports/leads_all.csv"

LEADS_INPUT="$APP_DIR/exports/leads_all.csv" \
LEADS_OUTPUT="$LEADS_OUTPUT" \
PLAYWRIGHT_EXECUTABLE_PATH="$PLAYWRIGHT_EXECUTABLE_PATH" \
python -u trustpilot_scraper/phone_enrichment_test.py \
  --input "$APP_DIR/exports/leads_all.csv" \
  --output "$LEADS_OUTPUT" \
  --limit "${LEAD_LIMIT:-0}"

python -u build_leads_db.py \
  --input "$LEADS_OUTPUT" \
  --db "$DB_PATH"

echo "Leadfil skapad: $LEADS_OUTPUT"
echo "Lead-databas skapad: $DB_PATH"