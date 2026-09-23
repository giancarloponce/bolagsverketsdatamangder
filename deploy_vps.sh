#!/usr/bin/env bash
set -euo pipefail

# Deploy application code only. Financial data stays in the existing VPS tree.
VPS_HOST="${VPS_HOST:-root@136.148.210.184}"
VPS_SSH_KEY="${VPS_SSH_KEY:-$HOME/Dokument/goldcallinghemsida1/ssh-keys/deploy_key}"
VPS_APP_DIR="${VPS_APP_DIR:-/root/scripts/leads_app}"
VPS_DATA_DIR="${VPS_DATA_DIR:-/root/scripts/financials/extracted}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_OPTS=(-i "$VPS_SSH_KEY" -o StrictHostKeyChecking=no)

if command -v rsync >/dev/null 2>&1; then
  rsync -av --delete \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='.ven/' \
    --exclude='__pycache__/' \
    --exclude='financials/' \
    --exclude='*.db' \
    --exclude='*.log' \
    --exclude='trustpilot_scraper/exports/' \
    --exclude='trustpilot_scraper/logs/' \
    --exclude='trustpilot_scraper/runs/' \
    -e "ssh -i $VPS_SSH_KEY -o StrictHostKeyChecking=no" \
    "$ROOT_DIR/" "$VPS_HOST:$VPS_APP_DIR/"
else
  echo "rsync saknas; använder tar över SSH"
  tar -C "$ROOT_DIR" \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.ven' \
    --exclude='__pycache__' \
    --exclude='financials' \
    --exclude='*.db' \
    --exclude='*.log' \
    --exclude='trustpilot_scraper/exports' \
    --exclude='trustpilot_scraper/logs' \
    --exclude='trustpilot_scraper/runs' \
    -cf - . | ssh "${SSH_OPTS[@]}" "$VPS_HOST" \
      "mkdir -p '$VPS_APP_DIR' && tar -xf - -C '$VPS_APP_DIR'"
fi

ssh "${SSH_OPTS[@]}" "$VPS_HOST" "mkdir -p '$VPS_APP_DIR/exports' '$VPS_APP_DIR/logs' '$VPS_APP_DIR/db' '$VPS_DATA_DIR'"

echo "Deploy klar: $VPS_HOST:$VPS_APP_DIR"
echo "Befintliga XHTML/bulkfiler lämnas i: $VPS_HOST:$VPS_DATA_DIR"
echo "Nästa steg: kör cd $VPS_APP_DIR && ./vps_migrate.sh"