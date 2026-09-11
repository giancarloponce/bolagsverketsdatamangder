import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import requests
from io import BytesIO
from datetime import datetime
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('factoring_errors.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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

DB_PATH = "factoring_leads.db"

def get_db_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def create_error_log_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS error_log (
        timestamp TEXT, source TEXT, level TEXT, message TEXT
    )""")
    conn.commit()

def create_companies_table(conn, columns):
    cols = ", ".join([f'"{c}" TEXT' for c in columns])
    conn.execute(f"CREATE TABLE IF NOT EXISTS companies ({cols})")
    conn.commit()

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
def load_data(force_download=False):
    conn = get_db_connection()
    create_error_log_table(conn)

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
            postadress AS adress
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
    return df

df = load_data(force_download=force_download)

if df.empty:
    st.error("Ingen data. Kontrollera loggen eller kör med force_download=True.")
    st.stop()

# ====================== FÖRBÄTTRAD SCORING ======================
df['bolag_age'] = (datetime.now() - pd.to_datetime(df['registreringsdatum'], errors='coerce')).dt.days / 365.25

def calculate_score(row):
    score = 40

    sni = str(row.get('sni', ''))

    if sni.startswith(('41', '42', '43')):          # Bygg
        score += 25
    elif sni.startswith(('49', '50', '51', '52', '53')):  # Åkeri / Transport
        score += 22
    elif sni.startswith('78'):                      # Bemanning
        score += 20
    elif sni.startswith(('46', '47')):              # Partihandel / Detaljhandel
        score += 12

    age = row.get('bolag_age', 0)
    if 3 < age < 15:
        score += 10
    elif age >= 15:
        score += 5

    return max(0, min(100, score))

df['lead_score'] = df.apply(calculate_score, axis=1)
df['prioritet'] = pd.cut(df['lead_score'], bins=[0,40,60,80,100], labels=['Låg','Medel','Hög','Mycket hög'])

# ====================== DASHBOARD ======================
st.subheader("📊 BI Dashboard & Prioriterade Leads")

col1, col2 = st.columns(2)
with col1:
    st.metric("Antal leads (max 50 000)", len(df))
    st.metric("Genomsnittlig score", round(df['lead_score'].mean(), 1))

with col2:
    fig = px.pie(df, names='prioritet', title="Lead-fördelning")
    st.plotly_chart(fig, width='stretch')

st.subheader("Topp 100 leads")
top_leads = df.sort_values('lead_score', ascending=False).head(100)
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
