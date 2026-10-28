"""Shared helpers and result container for auction scrapers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Canonical make -> aliases found in lot titles / descriptions.
BRAND_ALIASES: Dict[str, List[str]] = {
    "toyota": ["toyota", "lexus"],
    "bmw": ["bmw"],
    "fiat": ["fiat"],
    "volkswagen": ["volkswagen", "volkswagem", "vw"],
    "chevrolet": ["chevrolet", "gm", "gmc", "chev"],
    "ford": ["ford"],
    "honda": ["honda"],
}

# Wording that indicates a financing recovery.  Deliberately strict: generic
# words like "alienação" or "financiamento" appear in auction boilerplate
# (payment terms, debt transfer notices) and would mislabel ordinary lots.
FINANCING_MARKERS = [
    "alienacao fiduciaria",
    "recuperado de financiamento",
    "recuperacao de financiamento",
    "proveniente de financiamento",
    "veiculo de financiamento",
    "veiculos de financiamento",
    "banco santander",
    "banco bradesco",
    "banco itau",
    "banco do brasil",
    "caixa economica",
    "banco votorantim",
    "banco bv",
    "aymore",
    "banco gm",
    "banco fiat",
    "banco volkswagen",
    "banco toyota",
    "banco honda",
    "financeira",
    "consorcio",
]

# Keywords that make a lot "recovered from financing" when they appear in the
# auction / seller name (bank repossessions).
FINANCING_NAME_MARKERS = [
    "santander",
    "bradesco",
    "itau",
    "votorantim",
    "aymore",
    "banco ",
    "financeira",
    "alienacao fiduciaria",
    "alienaçao fiduciaria",
]


def deaccent(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return text.encode("ascii", "ignore").decode("ascii")


def norm(text: str) -> str:
    """Lowercase, accent-free, whitespace-collapsed text."""
    return " ".join(deaccent(text).lower().split())


def clean_html(text: Any) -> str:
    if text is None:
        return ""
    if isinstance(text, dict):
        # Some APIs (e.g. Superbid) return localized blocks.
        text = " ".join(str(value) for value in text.values() if isinstance(value, (str, int, float)))
    elif not isinstance(text, str):
        text = str(text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def parse_money(value: Any) -> Optional[float]:
    """Parse Brazilian money formats: 1234.56, 'R$ 26.000,00', '58,900,00'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value else None

    text = str(value)
    text = re.sub(r"(?i)r\$\s*:?", "", text)
    text = re.sub(r"[^\d.,]", "", text).strip(".,")
    if not text:
        return None

    has_dot = "." in text
    has_comma = "," in text
    try:
        if has_dot and has_comma:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif has_comma:
            parts = text.split(",")
            if len(parts) >= 2 and len(parts[-1]) == 2:
                # 58,900,00 / 8.600,00 style: last comma is the decimal mark.
                text = "".join(parts[:-1]).replace(".", "") + "." + parts[-1]
            else:
                text = text.replace(",", "")
        elif has_dot:
            parts = text.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                text = text.replace(".", "")
        amount = float(text)
    except ValueError:
        return None
    return amount if amount > 0 else None


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            # Naive values from these sites are in Brazilian local time.
            return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone().replace(tzinfo=None)
        except ValueError:
            continue
    return None


def detect_brand(*texts: str) -> Optional[str]:
    """Return the canonical make found in the given texts, if any."""
    for text in texts:
        haystack = " " + norm(text) + " "
        for brand, aliases in BRAND_ALIASES.items():
            for alias in aliases:
                if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", haystack):
                    return brand
    return None


def detect_condition(text: str, origin_text: str = "") -> Optional[str]:
    """Return a supported condition, or None when the lot must be excluded.

    Only these are accepted:
      * pequena monta
      * nao aplicavel (written without accents -> "não aplicável")
      * recuperado de financiamento

    Lots whose text explicitly says media/grande monta, sucata or an
    irrecoverable state are never accepted.
    """
    body = norm(text)
    origin = norm(origin_text)

    if re.search(r"\b(grande monta|media monta)\b", body):
        return None
    if re.search(r"\b(sucata|sucateado|irrecuperavel|nao recuperavel|desmanche)\b", body):
        return None
    if "pequena monta" in body:
        return "pequena monta"
    if "nao aplicavel" in body or "nao se aplica" in body:
        return "não aplicável"
    if any(marker in body for marker in FINANCING_MARKERS):
        return "recuperado de financiamento"
    if any(marker in origin for marker in FINANCING_MARKERS + FINANCING_NAME_MARKERS):
        return "recuperado de financiamento"
    return None


def extract_year_pair(*texts: str) -> tuple[Optional[int], Optional[int]]:
    """Return (manufacture_year, model_year) from text like 2019/2020 or 12/13."""
    for text in texts:
        if not text:
            continue
        m = re.search(r"(?<!\d)((?:19|20)\d{2})\s*/\s*((?:19|20)?\d{2})(?!\d)", text)
        if m:
            a = int(m.group(1))
            b = int(m.group(2))
            if b < 100:
                b += 2000
            return a, b
        m = re.search(r"(?<!\d)(\d{2})\s*/\s*(\d{2})(?!\d)", text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 80 <= a <= 99:
                a += 1900
            else:
                a += 2000
            b += 1900 if 80 <= b <= 99 else 2000
            if 1980 <= a <= 2030 and 1980 <= b <= 2030:
                return a, b
        # "FIAT UNO 2010 2011" (space separated model years).
        m = re.search(r"(?<!\d)((?:19|20)\d{2})\s+((?:19|20)\d{2})(?!\d)", text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 1980 <= a <= 2030 and 1980 <= b <= 2030 and 0 <= b - a <= 2:
                return a, b
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
        if m:
            y = int(m.group(1))
            if 1980 <= y <= 2030:
                return y, y
    return None, None


def extract_km(*texts: str) -> Optional[int]:
    for text in texts:
        if not text:
            continue
        m = re.search(r"(?i)(\d{1,3}(?:\.\d{3})+|\d{4,6})\s*(?:km|kms)\b", text)
        if m:
            km = int(re.sub(r"\D", "", m.group(1)))
            if 0 < km < 2_000_000:
                return km
    return None


def extract_model(title: str, brand: Optional[str]) -> str:
    """Best-effort model string: title without the make, years or noise."""
    text = clean_html(title)
    if brand:
        for alias in BRAND_ALIASES.get(brand, []):
            m = re.search(r"(?i)(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])\s*[/\-:]?\s*", text)
            if m:
                text = text[: m.start()] + text[m.end():]
                break
    text = re.sub(r"(?i)^\s*i\s*/\s*", "", text)
    text = re.sub(r"(?<!\d)(?:19|20)\d{2}\s*/\s*(?:19|20)?\d{2}(?!\d)", " ", text)
    text = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", text)
    text = re.sub(r"(?i)\b(sucata|recuperado|recuperada|sinistrado|sinistrada|pequena monta|media monta|média monta|sem sinistro|nao aplicavel|não aplicável)\b", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"(?i)\b(placa|final|cor|ano|modelo|marca|km)\b.*$", " ", text)
    text = re.sub(r"\b(PRETO|PRETA|BRANCO|BRANCA|PRATA|CINZA|VERMELHO|VERMELHA|AZUL|VERDE|AMARELO|AMARELA|MARROM|BEGE|DOURADO|GRAFITE|VINHO)\b", " ", text, flags=re.I)
    text = re.sub(r"[|;]+", " ", text)
    text = re.sub(r"(?:\s*,\s*){1,}", " ", text)
    text = text.strip(" ,;-|")
    text = " ".join(text.split())
    # Drop leading punctuation and lot prefixes such as "I/"
    text = re.sub(r"^[\s/\-|]+", "", text)
    return text[:120]


def extract_city_state(text: str) -> tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    m = re.search(r"([A-Za-zÀ-ÿ'\.\s]{3,40})\s*/\s*([A-Z]{2})\b", text)
    if not m:
        return None, None
    city = " ".join(m.group(1).split())
    city = re.sub(r"(?i)^(lote|lot)\s*\d+\s*", "", city).strip(" -|")
    state = m.group(2).upper()
    if not city:
        return None, state
    return city.title(), state


@dataclass
class ScrapeResult:
    site: str
    lots: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""
    error: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)


class BaseScraper:
    site: str = "base"
    label: str = "Base"

    async def search(self, config: Dict[str, Any]) -> ScrapeResult:  # pragma: no cover
        raise NotImplementedError

    # -- helpers available to subclasses ---------------------------------
    @staticmethod
    def make_lot(**kwargs: Any) -> Dict[str, Any]:
        lot = {
            "site": kwargs.get("site"),
            "site_label": kwargs.get("site_label"),
            "external_id": str(kwargs.get("external_id") or ""),
            "url": kwargs.get("url") or "",
            "image_url": (kwargs.get("image_url") or "").strip() or None,
            "title": (kwargs.get("title") or "").strip(),
            "make": kwargs.get("make"),
            "model": (kwargs.get("model") or "").strip(),
            "year": kwargs.get("year"),
            "year_manufacture": kwargs.get("year_manufacture"),
            "km": kwargs.get("km"),
            "city": kwargs.get("city"),
            "state": kwargs.get("state"),
            "condition": kwargs.get("condition"),
            "condition_source": kwargs.get("condition_source") or "description",
            "bid_price": kwargs.get("bid_price"),
            "declared_fipe_price": kwargs.get("declared_fipe_price"),
            "fee_percent": kwargs.get("fee_percent"),
            "fixed_fees": kwargs.get("fixed_fees"),
            "end_date": kwargs.get("end_date"),
            "seller": kwargs.get("seller"),
            "raw_data": kwargs.get("raw_data") or {},
        }
        lot["id"] = f"{lot['site']}:{lot['external_id']}" if lot["external_id"] else f"{lot['site']}:{abs(hash(lot['url']))}"
        return lot
