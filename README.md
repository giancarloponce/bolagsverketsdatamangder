# Bolagsverkets datamängder

Detta projekt innehåller en Streamlit-app för att analysera bolagsdata och generera lead-listor för factoring och fakturaköp.

## Innehåll

- Streamlit dashboard för filtrering och prioritetscore
- Hämtning av data från Bolagsverket och SCB
- Lokal SQLite-databas för cache och historik
- Export av leads till CSV

## Krav

- Python 3.10+
- pip

## Installation

```bash
python -m venv .ven
source .ven/bin/activate
pip install -r requirements.txt
```

## Kör appen

```bash
streamlit run factoring_lead_app.py
```

## Projektstruktur

- `factoring_lead_app.py` – huvudapplikation
- `requirements.txt` – Python-beroenden
- `factoring_leads.db` – lokal databas (ignoreras av git)
- `factoring_errors.log` – loggfil (ignoreras av git)

## Notering

Databasen och loggfiler skapas lokalt under körning och ska inte läggas in i git.
