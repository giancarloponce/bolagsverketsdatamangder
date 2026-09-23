# Bolagsverkets datamängder

Detta projekt innehåller en Streamlit-app för att analysera bolagsdata och generera lead-listor för factoring och fakturaköp.

## VPS-flöde för återuppbyggnad

Detta projekt ska köras som en enkel, reproducerbar pipeline på en VPS:

1. Hämta rå ZIP-filer från Bolagsverkets bulkdata
2. Extrahera XHTML/iXBRL-filer från ZIP:arna
3. Parsa finansiella nyckeltal från HTML-källan
4. Populera en SQLite-databas
5. Låta Streamlit-appen läsa från databasen

Detta gör att du inte är bunden till en lokal cache-databas eller temporära arbetsfiler.

## Kärnstruktur

- [factoring_lead_app.py](factoring_lead_app.py) – huvudapplikation
- [financial_parser.py](financial_parser.py) – parser för nested ZIP/XHTML-finansdata
- [cli_populate.py](cli_populate.py) – batch/CLI för import
- [financials](financials) – primär rådatamängd för finansiella ZIP-filfiler

## Kör appen

```bash
python -m venv .ven
source .ven/bin/activate
pip install -r requirements.txt
streamlit run factoring_lead_app.py
```

## VPS-pipeline

```bash
python vps_pipeline.py --download --raw-dir raw_downloads --xhtml-dir extracted_xhtml --db-path db/factoring_leads.db
python vps_pipeline.py --extract --raw-dir raw_downloads --xhtml-dir extracted_xhtml
python vps_pipeline.py --populate --raw-dir raw_downloads --db-path db/factoring_leads.db
```

Detta låter dig återbygga databasen från källan på nytt, vilket är det säkraste sättet att driftsätta projektet på en VPS.

## Migrering till befintlig VPS-data

VPS:en har redan följande datakatalog:

```text
/root/scripts/financials/extracted/
├── 2026/                  # XHTML/iXBRL-filer
├── bolagsverket_bulkfil/  # bulkfiler
└── scb_bulkfil/           # bulkfiler
```

Kopiera kod och körfiler utan att kopiera om den stora finansiella datamängden:

```bash
VPS_HOST=root@136.148.210.184 \
VPS_SSH_KEY="$HOME/Dokument/goldcallinghemsida1/ssh-keys/deploy_key" \
./deploy_vps.sh

ssh -i "$HOME/Dokument/goldcallinghemsida1/ssh-keys/deploy_key" \
	-o StrictHostKeyChecking=no root@136.148.210.184 \
	'cd /root/scripts/leads_app && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium'

ssh -i "$HOME/Dokument/goldcallinghemsida1/ssh-keys/deploy_key" \
	-o StrictHostKeyChecking=no root@136.148.210.184 \
	'cd /root/scripts/leads_app && ./vps_migrate.sh'
```

`vps_migrate.sh` läser rekursivt från `/root/scripts/financials/extracted/2026`, skapar en lead-export, kör telefonberikningen och importerar resultatet till SQLite. Ändra sökvägar utan att redigera skriptet genom att sätta `DATA_DIR`, `XHTML_DIR`, `LEAD_LIMIT`, `LEADS_OUTPUT` eller `DB_PATH` på kommandoraden.

Följande behövs på VPS:en: `lead_qualifier.py`, `financial_parser.py`, `build_leads_db.py`, `test_leads_100.csv`, `trustpilot_scraper/phone_enrichment_test.py`, `trustpilot_scraper/scraper.py`, `requirements.txt` samt Chromium för Playwright. Rådata, SQLite-filer, loggar och tidigare exporter migreras inte av deploy-skriptet.
