import re
from typing import Dict, List, Optional

from html import unescape

FINANCIAL_LABELS = {
    "omsattning": [
        "omsättning", "omsattning", "nettoomsättning", "nettoomsattning", "intäkter", "försäljning",
        "se-gen-base:nettoomsattning", "se-gen-base:nettoomsättning", "nettoomsattning"
    ],
    "resultat": [
        "resultat", "nettoresultat", "rörelseresultat", "resultat efter finansiella poster",
        "se-gen-base:resultatafterfinansiellaposter", "se-gen-base:resultatefterfinansiellaposter",
        "resultatefterfinansiellaposter"
    ],
    "balansomslutning": [
        "balansomslutning", "totala tillgångar", "totala tillgångar", "tillgångar",
        "se-gen-base:tillgangar", "se-gen-base:totala tillgångar", "tillgangar"
    ],
    "kundfordringar": [
        "kundfordringar", "fordringar på kunder", "kundfordringar och fordringar",
        "se-gen-base:kundfordringar", "se-gen-base:fordringar", "kundfordringar"
    ],
    "kassalikviditet": [
        "kassalikviditet", "likviditet", "cash ratio", "se-gen-base:kassamedel", "se-gen-base:likviditet", "kassamedel"
    ],
    "kortfristiga_skulder": [
        "kortfristiga skulder", "kortfristiga skulder och upplupna kostnader",
        "se-gen-base:kortfristigaskulder", "se-gen-base:kortfristiga skulder", "kortfristigaskulder"
    ],
    "varulager": ["varulager", "lager", "se-gen-base:varulager", "varulager"],
    "eget_kapital": ["eget kapital", "eget kapital och reserver", "se-gen-base:egetkapital", "egetkapital"],
}


def normalize_whitespace(value: Optional[str]) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_alias(value: str) -> str:
    value = unescape(value or "")
    mapping = {"å": "a", "ä": "a", "ö": "o", "Å": "a", "Ä": "a", "Ö": "o"}
    normalized = "".join(mapping.get(ch, ch) for ch in value.lower())
    normalized = re.sub(r"[^a-z0-9]", "", normalized)
    return normalized


def normalize_orgnr(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).replace("-", "").replace(" ", "").strip()
    if not cleaned:
        return None
    return cleaned


def normalize_year(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    matches = re.findall(r"(\d{4})", text)
    if not matches:
        return None
    return int(matches[0])


def parse_numeric(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None

    cleaned = normalize_whitespace(value)
    cleaned = cleaned.replace("\xa0", "")
    cleaned = re.sub(r"(?i)\b(?:sek|kr|eur|usd|gbp)\b", "", cleaned)
    cleaned = cleaned.replace("%", "")
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.strip("\t\r\n;:()[]{}")

    if not cleaned:
        return None

    sign = 1
    if cleaned.startswith("-"):
        sign = -1
        cleaned = cleaned[1:]
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:]
    elif cleaned.startswith("(") and cleaned.endswith(")"):
        sign = -1
        cleaned = cleaned[1:-1]

    if not re.search(r"\d", cleaned):
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) > 1 and len(parts[-1]) <= 2:
            cleaned = ".".join(parts)
        else:
            cleaned = "".join(parts)
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts) > 1 and len(parts[-1]) <= 2 and len(parts) == 2:
            cleaned = ".".join(parts)
        else:
            cleaned = "".join(parts)

    cleaned = re.sub(r"[^0-9.-]", "", cleaned)
    if cleaned in {"", ".", "-", "-."}:
        return None

    try:
        return sign * float(cleaned)
    except ValueError:
        return None


def _match_metric(name: str, aliases: List[str]) -> bool:
    norm_name = _normalize_alias(name)
    for alias in aliases:
        if norm_name == _normalize_alias(alias):
            return True
        if _normalize_alias(alias) in norm_name or norm_name in _normalize_alias(alias):
            return True
    return False


def _extract_metric_from_xbrl(html_text: str, aliases: List[str]) -> Optional[float]:
    pattern = re.compile(r'<ix:nonFraction\b[^>]*name="([^"]+)"[^>]*>(.*?)</ix:nonFraction>', re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(html_text):
        name, value = match.groups()
        if _match_metric(name, aliases):
            numeric = parse_numeric(value)
            if numeric is not None:
                return numeric
    return None


def extract_financials_from_html(html_text: str) -> Dict[str, object]:
    results: Dict[str, object] = {}

    for key, aliases in FINANCIAL_LABELS.items():
        value = _extract_metric_from_xbrl(html_text, aliases)
        if value is not None:
            results[key] = value

    orgnr = None
    for pattern in [r'<xbrli:identifier[^>]*>(\d{10,12})</xbrli:identifier>', r'<se-ar-base:Organisationsnummer[^>]*>(\d{10,12})</se-ar-base:Organisationsnummer>', r'(\d{10,12})']:
        matches = re.findall(pattern, html_text)
        for match in matches:
            candidate = normalize_orgnr(match)
            if candidate and len(candidate) >= 10:
                orgnr = candidate
                break
        if orgnr:
            break

    if orgnr:
        results["orgnr"] = orgnr

    year = None
    for pattern in [
        r'<xbrli:endDate[^>]*>(\d{4}-\d{2}-\d{2})</xbrli:endDate>',
        r'\b(\d{4})-(\d{2})-(\d{2})\b',
        r'\b(\d{4})\b'
    ]:
        matches = re.findall(pattern, html_text)
        if matches:
            value = matches[0]
            if isinstance(value, tuple):
                year_text = value[0]
            else:
                year_text = value
            year_candidate = normalize_year(year_text)
            if year_candidate and 2000 <= year_candidate <= 2100:
                year = year_candidate
                break

    if year:
        results["year"] = year

    return results


def extract_financials_from_zip_bytes(zip_bytes: bytes) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    try:
        from io import BytesIO
        import zipfile

        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".zip"):
                    continue
                try:
                    inner_bytes = zf.read(name)
                except Exception:
                    continue
                try:
                    with zipfile.ZipFile(BytesIO(inner_bytes)) as inner_zip:
                        for inner_name in inner_zip.namelist():
                            if not inner_name.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                                continue
                            try:
                                content = inner_zip.read(inner_name)
                                text = content.decode("utf-8", errors="ignore")
                            except Exception:
                                continue
                            if not text:
                                continue
                            record = extract_financials_from_html(text)
                            if record and record.get("orgnr") and record.get("year"):
                                records.append(record)
                except Exception:
                    continue
    except Exception:
        return []

    return records
