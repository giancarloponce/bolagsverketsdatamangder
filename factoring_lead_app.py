import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import requests
import os
import glob
import threading
import time
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

LOAD_STATUS = {
    "title": "Väntar",
    "detail": "",
    "progress": 0,
    "running": False,
    "done": False,
    "error": None,
    "df": None,
    "started_at": None,
    "current_file": None,
    "processed_count": 0,
    "total_count": 0,
}


def _set_load_status(title, detail=None, progress=0, current_file=None, processed_count=None, total_count=None):
    LOAD_STATUS["title"] = title
    LOAD_STATUS["detail"] = detail or ""
    LOAD_STATUS["progress"] = max(0, min(int(progress), 100))
    if current_file is not None:
        LOAD_STATUS["current_file"] = current_file
    if processed_count is not None:
        LOAD_STATUS["processed_count"] = int(processed_count)
    if total_count is not None:
        LOAD_STATUS["total_count"] = int(total_count)
    if LOAD_STATUS.get("started_at") is None:
        LOAD_STATUS["started_at"] = datetime.now()


def _update_load_status(progress_bar, status_text, percentage, title, detail=None, current_file=None, processed_count=None, total_count=None):
    _set_load_status(title, detail, percentage, current_file=current_file, processed_count=processed_count, total_count=total_count)
    if progress_bar is not None:
        progress_bar.progress(min(max(percentage, 0), 100))
    if status_text is not None:
        status_text.empty()
        status_text.markdown(f"**{title}**")
        if detail:
            status_text.write(detail)


def _render_load_status():
    progress = LOAD_STATUS.get("progress", 0)
    st.progress(progress)
    st.markdown(f"**{LOAD_STATUS.get('title', 'Laddar')}**")
    detail = LOAD_STATUS.get("detail")
    if detail:
        st.caption(detail)

    current_file = LOAD_STATUS.get("current_file")
    if current_file:
        st.code(f"Aktuell fil: {current_file}")

    processed = LOAD_STATUS.get("processed_count")
    total = LOAD_STATUS.get("total_count")
    if processed is not None or total is not None:
        st.caption(f"Bearbetat: {processed if processed is not None else 0} / {total if total is not None else 0} steg")

    started_at = LOAD_STATUS.get("started_at")
    if started_at is not None and not LOAD_STATUS.get("done"):
        elapsed = (datetime.now() - started_at).total_seconds()
        eta_seconds = None
        if progress > 0:
            eta_seconds = max(0, int((elapsed / max(progress, 1)) * (100 - progress)))
        if eta_seconds is not None:
            st.caption(f"Tid sedan start: {int(elapsed)} s • Beräknad återstående tid: {eta_seconds} s")
        else:
            st.caption(f"Tid sedan start: {int(elapsed)} s")

    step_map = {
        "Startar dataladdning": "1. Initierar applikationen",
        "Läser lokala finansiella ZIP-filer": "2. Kollar om lokala ZIP-filer finns",
        "Importerar finansiella uppgifter": "3. Bearbetar lokala finansiella filer",
        "Nedladdning": "4. Hämtar extern bulkdata",
        "Förbereder": "5. Förbereder ZIP-innehåll",
        "Bearbetar": "6. Läser CSV/ZIP i chunks",
        "Importerar": "7. Sparar data i databasen",
        "Förbereder lead-data": "8. Bygger lead-dataset",
        "Databas uppdaterad": "9. Klart",
        "Klar": "9. Klart",
    }
    headline = LOAD_STATUS.get("title", "")
    if headline in step_map:
        st.caption(f"Steg: {step_map[headline]}")


def _download_with_retry(url, headers=None, timeout=300, max_retries=3):
    headers = headers or {}
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, stream=True, timeout=timeout)
            if response.status_code == 200:
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:  # pragma: no cover - network path only
            last_error = exc
        if attempt < max_retries:
            time.sleep(2 * attempt)
    raise last_error or RuntimeError(f"Download failed for {url}")


@st.cache_data(ttl=3600)
def load_data(force_download=False, min_age=3):
    start_time = datetime.now()
    LOAD_STATUS["started_at"] = start_time
    conn = get_db_connection()
    create_error_log_table(conn)
    create_financials_table(conn)

    _update_load_status(None, None, 2, "Startar dataladdning", "Initierar databas och verifierar cache-status")

    financial_paths = sorted(glob.glob(os.path.join("financials", "*.zip")))
    total_local_files = len(financial_paths)
    if financial_paths:
        _update_load_status(None, None, 5, "Läser lokala finansiella ZIP-filer", f"Hittade {total_local_files} filer i mappen financials", current_file="financials/", processed_count=0, total_count=total_local_files)

    for index, path in enumerate(financial_paths, start=1):
        filename = os.path.basename(path)
        _update_load_status(
            None,
            None,
            5 + int((index / max(total_local_files, 1)) * 25),
            "Importerar finansiella uppgifter",
            f"{index}/{total_local_files}: bearbetar {filename}",
            current_file=filename,
            processed_count=index,
            total_count=total_local_files,
        )
        import_financial_zip_file(conn, path)

    last_update = get_last_update(conn)
    needs_update = force_download or last_update is None or (datetime.now() - last_update).days > 7

    if needs_update:
        urls = {
            "scb": "https://vardefulla-datamangder.bolagsverket.se/scb/scb_bulkfil.zip",
            "bolagsverket": "https://vardefulla-datamangder.bolagsverket.se/bolagsverket/bolagsverket_bulkfil.zip"
        }

        total_steps = len(urls) * 4
        current_step = 0

        for name, url in urls.items():
            try:
                filename = url.rsplit("/", 1)[-1]
                logger.info(f"[{name}] Downloading {filename}...")
                _update_load_status(
                    None,
                    None,
                    30 + int((current_step / max(total_steps, 1)) * 50),
                    f"Nedladdning: {name}",
                    f"Hämtar {filename} från {url}",
                    current_file=filename,
                    processed_count=current_step,
                    total_count=total_steps,
                )
                current_step += 1

                headers = {"User-Agent": "Mozilla/5.0"}
                try:
                    response = _download_with_retry(url, headers=headers, timeout=300, max_retries=3)
                except Exception as exc:
                    _update_load_status(
                        None,
                        None,
                        30 + int((current_step / max(total_steps, 1)) * 50),
                        f"Nedladdning misslyckades för {name}",
                        f"{exc}",
                    )
                    raise

                if response.status_code != 200:
                    _update_load_status(
                        None,
                        None,
                        30 + int((current_step / max(total_steps, 1)) * 50),
                        f"Hoppar över {name}",
                        f"HTTP {response.status_code} från datakälla",
                    )
                    continue

                _update_load_status(
                    None,
                    None,
                    30 + int((current_step / max(total_steps, 1)) * 50),
                    f"Förbereder {name}",
                    f"Läser nedladdat ZIP-innehåll från {filename}",
                    current_file=filename,
                    processed_count=current_step,
                    total_count=total_steps,
                )
                current_step += 1

                zip_content = BytesIO(response.content)

                _update_load_status(
                    None,
                    None,
                    30 + int((current_step / max(total_steps, 1)) * 50),
                    f"Bearbetar {name}",
                    f"Läser CSV/ZIP i chunks från {filename}",
                    current_file=filename,
                    processed_count=current_step,
                    total_count=total_steps,
                )
                current_step += 1

                if name == "scb":
                    reader = pd.read_csv(zip_content, sep="\t", encoding="ISO-8859-1",
                                         compression="zip", low_memory=False, on_bad_lines="skip",
                                         chunksize=50000)
                else:
                    reader = pd.read_csv(zip_content, sep=";", encoding="utf-8",
                                         compression="zip", low_memory=False, quotechar='"',
                                         escapechar="\\", on_bad_lines="skip", chunksize=50000)

                chunk_index = 0
                for chunk in reader:
                    chunk_index += 1
                    chunk.columns = [c.strip().lower().replace(" ", "_") for c in chunk.columns]
                    create_companies_table(conn, chunk.columns)
                    chunk.to_sql("companies", conn, if_exists="append", index=False, chunksize=5000)
                    _update_load_status(
                        None,
                        None,
                        30 + int((current_step / max(total_steps, 1)) * 50) + min(10, int((chunk_index / max(1, chunk_index + 1)) * 10)),
                        f"Importerar {name}",
                        f"Bearbetade chunk {chunk_index} i {filename}",
                        current_file=filename,
                        processed_count=chunk_index,
                        total_count=max(chunk_index, 1),
                    )

                logger.info(f"[{name}] Import finished")
                _update_load_status(
                    None,
                    None,
                    80 + int((current_step / max(total_steps, 1)) * 20),
                    f"Klar: {name}",
                    "Datakälla importerad",
                )
                current_step += 1
                st.success(f"Importerade {name}")

            except Exception as e:
                error_msg = f"{str(e)}\n{traceback.format_exc()}"
                logger.error(f"[{name}] {error_msg}")
                log_to_db(conn, name, "ERROR", str(e))
                st.error(f"Fel vid {name}")

        set_last_update(conn, datetime.now())
        _update_load_status(None, None, 100, "Databas uppdaterad", "Alla datakällor är nu importerade")
    else:
        _update_load_status(
            None,
            None,
            25,
            "Använder lokal cache",
            "Databasen är nyligen uppdaterad, så inga stora nedladdningar körs nu",
        )

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
    _update_load_status(None, None, 90, "Förbereder lead-data", "Läser och filtrerar företagsdata")
    df = pd.read_sql(query, conn)
    conn.close()

    if not df.empty:
        df['orgnr'] = df['orgnr'].map(normalize_orgnr)
        df = df.dropna(subset=['orgnr'])

    if 'organisationsform' in df.columns:
        df['sni'] = df['organisationsform'].fillna('')

    elapsed = datetime.now() - start_time
    _update_load_status(
        None,
        None,
        100,
        "Klar",
        f"{len(df) if df is not None else 0} företag laddade på {elapsed.total_seconds():.0f} sekunder",
    )
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


def _start_background_refresh(force_download=False, min_age=3):
    if "refresh_thread" in st.session_state and st.session_state["refresh_thread"].is_alive():
        return

    def worker():
        try:
            st.session_state["last_status_rerun"] = 0
            LOAD_STATUS["running"] = True
            LOAD_STATUS["done"] = False
            LOAD_STATUS["error"] = None
            df = load_data(force_download=force_download, min_age=min_age)
            LOAD_STATUS["df"] = df
            LOAD_STATUS["done"] = True
            LOAD_STATUS["running"] = False
            st.session_state["last_status_rerun"] = 0
        except Exception as exc:  # pragma: no cover - runtime path only
            LOAD_STATUS["error"] = str(exc)
            LOAD_STATUS["running"] = False
            LOAD_STATUS["done"] = True
            st.session_state["last_status_rerun"] = 0

    thread = threading.Thread(target=worker, daemon=True)
    st.session_state["refresh_thread"] = thread
    thread.start()


def _trigger_refresh(force_download=False, min_age=3):
    st.session_state.pop("refresh_thread", None)
    st.session_state.pop("data_loaded", None)
    st.session_state.pop("df_cache", None)
    LOAD_STATUS["df"] = None
    _start_background_refresh(force_download=force_download, min_age=min_age)


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

    if st.sidebar.button("🔄 Uppdatera data nu"):
        _trigger_refresh(force_download=force_download, min_age=min_age)

    if "refresh_thread" not in st.session_state or not st.session_state["refresh_thread"].is_alive():
        if not st.session_state.get("data_loaded", False):
            _start_background_refresh(force_download=force_download, min_age=min_age)

    if "refresh_thread" in st.session_state and st.session_state["refresh_thread"].is_alive():
        _render_load_status()
        st.caption("Databasen uppdateras i bakgrunden. Du kan lämna sidan öppen medan processen körs.")
        now = time.time()
        last_rerun = st.session_state.get("last_status_rerun", 0)
        if now - last_rerun >= 1.0:
            st.session_state["last_status_rerun"] = now
            st.rerun()
        return

    last_update = None
    try:
        conn = get_db_connection()
        last_update = get_last_update(conn)
        conn.close()
    except Exception:
        last_update = None

    if last_update:
        st.sidebar.caption(f"Senast uppdaterad: {last_update.strftime('%Y-%m-%d %H:%M')}")
    else:
        st.sidebar.caption("Senast uppdaterad: aldrig")

    if LOAD_STATUS.get("error"):
        st.error(f"Datainläsning misslyckades: {LOAD_STATUS['error']}")
        return

    df = LOAD_STATUS.get("df")
    if df is None:
        if st.session_state.get("data_loaded"):
            df = st.session_state.get("df_cache")
        else:
            df = load_data(force_download=force_download, min_age=min_age)

    st.session_state["data_loaded"] = True
    st.session_state["df_cache"] = df
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
