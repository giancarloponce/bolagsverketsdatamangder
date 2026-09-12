import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import requests
import os
import glob
from io import BytesIO
from datetime import datetime
import logging
import traceback

from financial_parser import extract_financials_from_zip_bytes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('factoring_errors.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = "factoring_leads.db"


def normalize_orgnr(value):
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) in (10, 12):
        return digits
    return None


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def create_error_log_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS error_log (
        timestamp TEXT, source TEXT, level TEXT, message TEXT
    )""")
    conn.commit()


def create_companies_table(conn, columns):
    cols = ", ".join([f'"{c}" TEXT' for c in columns])
    conn.execute(f"CREATE TABLE IF NOT EXISTS companies ({cols})")
    conn.commit()


def create_financials_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS financials (
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
    )""")
    conn.commit()


def import_financial_zip_file(conn, zip_path):
    if not os.path.exists(zip_path):
        return 0

    try:
        with open(zip_path, "rb") as f:
            payload = f.read()
        records = extract_financials_from_zip_bytes(payload)
        if not records:
            return 0

        inserted = 0
        for record in records:
            orgnr = record.get("orgnr")
            year = record.get("year")
            if not orgnr or not year:
                continue

            values = {
                "orgnr": str(orgnr),
                "year": int(year),
                "omsattning": record.get("omsattning"),
                "resultat": record.get("resultat"),
                "balansomslutning": record.get("balansomslutning"),
                "kundfordringar": record.get("kundfordringar"),
                "kassalikviditet": record.get("kassalikviditet"),
                "kortfristiga_skulder": record.get("kortfristiga_skulder"),
                "varulager": record.get("varulager"),
                "eget_kapital": record.get("eget_kapital"),
                "source": os.path.basename(zip_path),
            }

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
                    values["orgnr"],
                    values["year"],
                    values["omsattning"],
                    values["resultat"],
                    values["balansomslutning"],
                    values["kundfordringar"],
                    values["kassalikviditet"],
                    values["kortfristiga_skulder"],
                    values["varulager"],
                    values["eget_kapital"],
                    values["source"],
                ),
            )
            inserted += 1

        conn.commit()
        return inserted
    except Exception as exc:
        logger.exception("Failed to import financial zip %s: %s", zip_path, exc)
        return 0

def get_last_update(conn):
    cur = conn.cursor()
    cur.execute("SELECT value FROM metadata WHERE key = 'last_update'")
    row = cur.fetchone()
    return datetime.fromisoformat(row[0]) if row else None

def set_last_update(conn, dt):
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_update', ?)", (dt.isoformat(),))
    conn.commit()

def log_to_db(conn, source, level, message):
    create_error_log_table(conn)
    conn.execute("INSERT INTO error_log VALUES (?, ?, ?, ?)",
                 (datetime.now().isoformat(), source, level, message))
    conn.commit()

@st.cache_data(ttl=3600)
def load_data(force_download=False, min_age=3):
    conn = get_db_connection()
    create_error_log_table(conn)
    create_financials_table(conn)

    financial_paths = sorted(glob.glob(os.path.join("financials", "*.zip")))
    for path in financial_paths:
        import_financial_zip_file(conn, path)

    last_update = get_last_update(conn)
    needs_update = force_download or last_update is None or (datetime.now() - last_update).days > 7

    if needs_update:
        urls = {
            "scb": "https://vardefulla-datamangder.bolagsverket.se/scb/scb_bulkfil.zip",
            "bolagsverket": "https://vardefulla-datamangder.bolagsverket.se/bolagsverket/bolagsverket_bulkfil.zip"
        }

        progress_bar = st.progress(0)
        status_text = st.empty()
        total_steps = len(urls) * 4
        current_step = 0

        for name, url in urls.items():
            try:
                logger.info(f"[{name}] Downloading...")
                status_text.text(f"Laddar ner {name}...")
                progress_bar.progress(int((current_step / total_steps) * 100))
                current_step += 1

                headers = {"User-Agent": "Mozilla/5.0"}
                response = requests.get(url, headers=headers, stream=True, timeout=300)

                if response.status_code != 200:
                    continue

                progress_bar.progress(int((current_step / total_steps) * 100))
                current_step += 1

                zip_content = BytesIO(response.content)

                progress_bar.progress(int((current_step / total_steps) * 100))
                current_step += 1

                if name == "scb":
                    reader = pd.read_csv(zip_content, sep="\t", encoding="ISO-8859-1",
                                         compression="zip", low_memory=False, on_bad_lines="skip",
                                         chunksize=50000)
                else:
                    reader = pd.read_csv(zip_content, sep=";", encoding="utf-8",
                                         compression="zip", low_memory=False, quotechar='"',
                                         escapechar="\\", on_bad_lines="skip", chunksize=50000)

                for chunk in reader:
                    chunk.columns = [c.strip().lower().replace(" ", "_") for c in chunk.columns]
                    create_companies_table(conn, chunk.columns)
                    chunk.to_sql("companies", conn, if_exists="append", index=False, chunksize=5000)

                logger.info(f"[{name}] Import finished")
                progress_bar.progress(int((current_step / total_steps) * 100))
                current_step += 1
                st.success(f"Importerade {name}")

            except Exception as e:
                error_msg = f"{str(e)}\n{traceback.format_exc()}"
                logger.error(f"[{name}] {error_msg}")
                log_to_db(conn, name, "ERROR", str(e))
                st.error(f"Fel vid {name}")

        set_last_update(conn, datetime.now())
        progress_bar.progress(100)
        status_text.text("Databas uppdaterad!")
        st.balloons()

    query = f"""
        SELECT 
            organisationsidentitet AS orgnr,
            organisationsnamn AS namn,
            registreringsdatum,
            verksamhetsbeskrivning,
            postadress AS adress,
            COALESCE(organisationsform, '') AS organisationsform
        FROM companies
        WHERE registreringsdatum IS NOT NULL
          AND julianday('now') - julianday(
                CASE 
                    WHEN length(registreringsdatum) = 8 THEN 
                        substr(registreringsdatum,1,4) || '-' || 
                        substr(registreringsdatum,5,2) || '-' || 
                        substr(registreringsdatum,7,2)
                    ELSE registreringsdatum 
                END
              ) >= {min_age * 365}
        LIMIT 50000
    """
    df = pd.read_sql(query, conn)
    conn.close()

    if not df.empty:
        df['orgnr'] = df['orgnr'].map(normalize_orgnr)
        df = df.dropna(subset=['orgnr'])

    if 'organisationsform' in df.columns:
        df['sni'] = df['organisationsform'].fillna('')

    return df

def calculate_score(row):
    score = 15

    sni = str(row.get('sni', '') or '')
    if sni.startswith(('41', '42', '43')):
        score += 18
    elif sni.startswith(('49', '50', '51', '52', '53')):
        score += 16
    elif sni.startswith('78'):
        score += 14
    elif sni.startswith(('46', '47')):
        score += 12

    age = float(row.get('bolag_age') or 0)
    if 3 < age < 15:
        score += 10
    elif age >= 15:
        score += 6

    omsattning = float(row.get('omsattning') or 0)
    resultat = float(row.get('resultat') or 0)
    balansomslutning = float(row.get('balansomslutning') or 0)
    kundfordringar = float(row.get('kundfordringar') or 0)
    eget_kapital = float(row.get('eget_kapital') or 0)
    kortfristiga_skulder = float(row.get('kortfristiga_skulder') or 0)
    likviditet = row.get('kassalikviditet')
    if pd.notna(likviditet):
        likviditet = float(likviditet)
    else:
        likviditet = None

    if omsattning > 0:
        if omsattning >= 15_000_000:
            score += 18
        elif omsattning >= 5_000_000:
            score += 12
        elif omsattning >= 1_000_000:
            score += 8

        margin = resultat / omsattning
        if margin > 0.10:
            score += 18
        elif margin > 0.05:
            score += 12
        elif margin > 0.00:
            score += 6
        elif margin < -0.05:
            score -= 12

        if kundfordringar > 0:
            share = kundfordringar / omsattning
            if share < 0.15:
                score += 12
            elif share < 0.30:
                score += 8
            elif share > 0.60:
                score -= 8

        if kortfristiga_skulder > 0:
            dept_ratio = kortfristiga_skulder / omsattning
            if dept_ratio < 0.50:
                score += 8
            elif dept_ratio > 1.00:
                score -= 10

    if balansomslutning > 0 and eget_kapital > 0:
        equity_ratio = eget_kapital / balansomslutning
        if equity_ratio > 0.35:
            score += 12
        elif equity_ratio > 0.20:
            score += 6
        elif equity_ratio < 0.10:
            score -= 8

    if likviditet is not None:
        if likviditet >= 1.0:
            score += 12
        elif likviditet >= 0.8:
            score += 8
        elif likviditet < 0.5:
            score -= 10

    if omsattning > 0 and resultat < 0:
        score = max(0, score - 6)

    return max(0, min(100, score))


def main():
    st.set_page_config(page_title="Factoring Lead Generator", layout="wide")
    st.title("🇸🇪 Factoring & Fakturaköp – Lead Generator & BI (Optimerad)")

    st.sidebar.header("🔧 Inställningar & Filter")

    sni_options = {
        "41-43": "Byggverksamhet",
        "46": "Partihandel",
        "49-53": "Transport & Åkeri",
        "69-75": "Professionella tjänster",
        "78": "Bemanning",
        "82": "Kontorstjänster",
        "10-33": "Tillverkning",
        "45-47": "Handel"
    }
    selected_sni = st.sidebar.multiselect("Välj SNI-koder", list(sni_options.keys()), default=["41-43", "46", "49-53"])
    min_age = st.sidebar.slider("Minsta bolagsålder (år)", 0, 30, 3)
    force_download = st.sidebar.checkbox("Tvinga ny nedladdning", value=False)

    df = load_data(force_download=force_download, min_age=min_age)

    if df.empty:
        st.error("Ingen data. Kontrollera loggen eller kör med force_download=True.")
        return

    df['bolag_age'] = (datetime.now() - pd.to_datetime(df['registreringsdatum'], errors='coerce')).dt.days / 365.25

    conn = get_db_connection()
    try:
        financial_df = pd.read_sql(
            """
            SELECT orgnr, year, omsattning, resultat, balansomslutning, kundfordringar,
                   kassalikviditet, kortfristiga_skulder, varulager, eget_kapital
            FROM financials
            ORDER BY year DESC
            """,
            conn,
        )
        if not financial_df.empty:
            financial_df['orgnr'] = financial_df['orgnr'].map(normalize_orgnr)
            financial_df = financial_df.dropna(subset=['orgnr'])
            financial_latest = financial_df.drop_duplicates(subset=['orgnr'], keep='first')
            df = df.merge(financial_latest, on='orgnr', how='left')
    finally:
        conn.close()

    df['lead_score'] = df.apply(calculate_score, axis=1)
    df['prioritet'] = pd.cut(df['lead_score'], bins=[0,40,60,80,100], labels=['Låg','Medel','Hög','Mycket hög'])

    st.subheader("📊 BI Dashboard & Prioriterade Leads")

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Antal leads (max 50 000)", len(df))
        st.metric("Genomsnittlig score", round(df['lead_score'].mean(), 1))

    with col2:
        fig = px.pie(df, names='prioritet', title="Lead-fördelning")
        st.plotly_chart(fig, width='stretch')

    st.subheader("Topp 100 leads")
    top_leads = df.sort_values('lead_score', ascending=False).dropna(subset=['lead_score']).head(100)
    if top_leads.empty:
        st.warning("Det finns inga lead-data att ranka efter den aktuella finansiella datamängden.")
        return
    cols_to_show = [c for c in ['namn', 'orgnr', 'adress', 'lead_score', 'prioritet', 'verksamhetsbeskrivning'] if c in top_leads.columns]
    st.dataframe(top_leads[cols_to_show], width='stretch')

    if st.button("📥 Exportera alla leads till CSV"):
        csv = df.to_csv(index=False).encode('utf-8')
        st.download_button("Ladda ner CSV", csv, "factoring_leads.csv", "text/csv")

    st.caption("Minnesoptimerad version med förbättrad bransch-prioritering (Bygg, Åkeri, Bemanning).")

    if st.checkbox("Visa fel-loggar"):
        conn = get_db_connection()
        create_error_log_table(conn)
        errors = pd.read_sql("SELECT * FROM error_log ORDER BY timestamp DESC LIMIT 30", conn)
        st.dataframe(errors)
        conn.close()


if __name__ == "__main__":
    main()
