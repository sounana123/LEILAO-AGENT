"""Sodre Santoro scraper.

The public listing is a Nuxt app that queries an Elasticsearch-style endpoint
(`POST /api/search-lots`).  Plain HTTP requests are blocked by the site WAF, so
the query is issued from inside a real Chromium page (same origin, valid
cookies/fingerprint).  That endpoint exposes structured fields:

  lot_sinister : pequena monta | media monta | sem sinistro | sucata desmanche
  lot_origin   : financiamento | frota | judicial | seguro | ...
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

from .base import (
    BaseScraper,
    ScrapeResult,
    clean_html,
    deaccent,
    detect_brand,
    extract_city_state,
    extract_model,
    parse_datetime,
    parse_money,
    UA,
)

LISTING_URL = "https://www.sodresantoro.com.br/veiculos/lotes"
API_PATH = "/api/search-lots"

# Only auctions that are still running.
ACTIVE_FILTER = [
    {
        "bool": {
            "should": [
                {"bool": {"must": [{"term": {"auction_status": "online"}}]}},
                {
                    "bool": {
                        "must": [{"term": {"auction_status": "aberto"}}],
                        "must_not": [{"terms": {"lot_status_id": [5, 7]}}],
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    },
    {
        "bool": {
            "should": [{"bool": {"must_not": {"term": {"lot_status_id": 6}}}}],
            "minimum_should_match": 1,
        }
    },
    {
        "bool": {
            "should": [{"bool": {"must_not": {"term": {"lot_test": True}}}}],
            "minimum_should_match": 1,
        }
    },
]

FETCH_JS = """
async (payload) => {
  const r = await fetch('/api/search-lots', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
    body: JSON.stringify(payload)
  });
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) {}
  return {status: r.status, data, snippet: text.slice(0, 200)};
}
"""

SINISTER_MAP = {
    "pequena monta": "pequena monta",
    "nao aplicavel": "não aplicável",
    "media monta": None,
    "grande monta": None,
    "sucata desmanche": None,
}


CITY_NOISE = re.compile(
    r"(?i)^(bl|cj|jd|km|lt|lote|qd|quadra|rua|r|av|avenida|rod|rodovia|al|alameda|"
    r"tv|travessa|estrada|estr|praca|pra\u00e7a|galp|galpao|galp\u00e3o)\b\.?\s*"
)


def _clean_city(name: str) -> str:
    """Drop street/block noise that Sodre sometimes prefixes to the city."""
    text = name.strip(" -,.")
    while True:
        before = text
        text = re.sub(r"^\d+[a-z]?\s*", "", text, flags=re.I)
        text = CITY_NOISE.sub("", text)
        text = text.strip(" -,.")
        if text == before:
            break
    return text


def _repair_mojibake(text: str) -> str:
    """Sodre's API sometimes returns UTF-8 bytes decoded as latin-1.

    ``Comissão`` arrives as ``ComissÃ£o``.  Re-encoding to latin-1 and decoding
    as UTF-8 recovers the original text; if that is not possible the string is
    returned unchanged.
    """
    if "Ã" not in text and "Â" not in text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _parse_rate_information(text: Any) -> tuple[Optional[float], float]:
    """Extract the auctioneer's own fee schedule from ``lot_rate_information``.

    Sodre publishes text such as:
      "Comissão: 5.00% do valor do lance, Depósito de Bens: R$ 2.350,00,
       Logística e/ou Despachante: 500,00, Sedex: Valores na condição de venda"

    Returns (commission_percent, flat_fees).  Flat fees are the sums of the
    deposit, logistics/despachante and any fixed Sedex charge.
    """
    if not text:
        return None, 0.0
    raw = deaccent(_repair_mojibake(" ".join(str(text).split())))

    percent = None
    match = re.search(r"Comiss\w*:?\s*([\d.,]+)\s*%", raw, re.I)
    if match:
        number = match.group(1)
        if "," in number:
            number = number.replace(".", "").replace(",", ".")
        try:
            percent = float(number)
        except ValueError:
            percent = None

    fixed = 0.0
    pattern = re.compile(
        r"(Deposito de Bens|Logistica(?: e/ou Despachante)?|Sedex)"
        r"\s*:?\s*(?:R\$\s*)?([\d][\d.,]*)",
        re.I,
    )
    for match in pattern.finditer(raw):
        value = parse_money(match.group(2))
        if value:
            fixed += float(value)

    return percent, round(fixed, 2)


def _parse_location(lot: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    for key in ("lot_location_address", "lot_location"):
        raw = lot.get(key) or ""
        if not raw:
            continue
        m = re.search(r"([^,/\-]+?)\s*/\s*([A-Za-z]{2})\s*$", str(raw).strip())
        if m:
            city = _clean_city(" ".join(m.group(1).split()))
            city = re.sub(r"(?i)^(lote|lot)\s*\d+\s*", "", city)
            # Sodré often appends a yard/branch marker: "guarulhos i/sp".
            city = re.sub(r"(?i)\s+(i{1,3}|iv|v|vi{0,3}|ix|x)$", "", city).strip(" -,")
            city = _clean_city(city)
            if city:
                return city.title(), m.group(2).upper()
        city, state = extract_city_state(str(raw))
        if city or state:
            return city, state
    return None, None


class SodreSantoroScraper(BaseScraper):
    site = "sodre_santoro"
    label = "Sodré Santoro"

    async def search(self, config: Dict[str, Any]) -> ScrapeResult:
        makes = [m.lower() for m in config.get("filters", {}).get("makes", []) if m]
        conditions = [c.lower() for c in config.get("filters", {}).get("conditions", [])]
        max_pages = int(config.get("scraper_options", {}).get("sodre_max_pages", 8))

        if not makes:
            return ScrapeResult(site=self.site, note="No makes configured.")

        # Structured condition filters understood by the search endpoint.
        sinister_terms = []
        if any("pequena" in c for c in conditions):
            sinister_terms.append("pequena monta")
        if any("aplic" in c for c in conditions):
            sinister_terms.extend(["não aplicável", "nao aplicavel", "NAO APLICAVEL"])
        want_financing = any("financ" in c for c in conditions)

        should_clauses: List[Dict[str, Any]] = []
        if sinister_terms:
            should_clauses.append({"terms": {"lot_sinister": sinister_terms}})
        if want_financing:
            should_clauses.append({"terms": {"lot_origin": ["financiamento"]}})
        if not should_clauses:
            return ScrapeResult(site=self.site, note="No supported conditions configured.")

        payload = {
            "indices": ["veiculos", "judiciais-veiculos"],
            "query": {"bool": {"filter": ACTIVE_FILTER}},
            "post_filter": {
                "bool": {
                    "filter": [
                        {"terms": {"lot_category": ["carros"]}},
                        {"terms": {"lot_brand": makes}},
                        {
                            "bool": {
                                "should": should_clauses,
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            },
            "from": 0,
            "size": 100,
            "sort": [{"auction_date_init": {"order": "asc"}}],
        }

        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover
            return ScrapeResult(site=self.site, error=f"Playwright unavailable: {exc}")

        lots: List[Dict[str, Any]] = []
        note = ""
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                try:
                    ctx = await browser.new_context(
                        user_agent=UA,
                        viewport={"width": 1440, "height": 900},
                        locale="pt-BR",
                        timezone_id="America/Sao_Paulo",
                    )
                    page = await ctx.new_page()
                    await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2500)

                    total = None
                    page_no = 0
                    while page_no < max_pages:
                        payload["from"] = page_no * payload["size"]
                        result = await self._fetch(page, payload)
                        if result is None or result.get("status") != 200:
                            # WAF challenge: reload once and retry the same page.
                            await page.reload(wait_until="domcontentloaded", timeout=60000)
                            await page.wait_for_timeout(3500)
                            result = await self._fetch(page, payload)
                        if not result or result.get("status") != 200:
                            snippet = (result or {}).get("snippet", "")
                            note = f"Search endpoint returned HTTP {(result or {}).get('status')}."
                            if snippet and "<html" in snippet.lower():
                                note += " WAF challenge page."
                            break

                        data = result.get("data") or {}
                        if total is None:
                            total = data.get("total")
                        results = data.get("results") or []
                        if not results:
                            break
                        for raw in results:
                            lot = self._map_lot(raw)
                            if lot:
                                lots.append(lot)
                        if len(results) < payload["size"]:
                            break
                        page_no += 1

                    stats = {"api_total": total, "raw_returned": len(lots)}
                finally:
                    await browser.close()
        except Exception as exc:
            return ScrapeResult(site=self.site, error=f"{type(exc).__name__}: {exc}")

        return ScrapeResult(site=self.site, lots=lots, note=note, stats=locals().get("stats", {}))

    @staticmethod
    async def _fetch(page, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            return await page.evaluate(FETCH_JS, payload)
        except Exception:
            return None

    def _map_lot(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = clean_html(raw.get("lot_title") or "")
        description = clean_html(raw.get("lot_description") or "")
        brand_raw = (raw.get("lot_brand") or "").strip()
        model_raw = (raw.get("lot_model") or "").strip()

        brand = detect_brand(brand_raw, title, description)
        if not brand:
            return None

        sinister = (raw.get("lot_sinister") or "").strip().lower()
        origin = (raw.get("lot_origin") or "").strip().lower()

        condition = None
        condition_source = "field"
        if sinister:
            from .base import norm

            key = norm(sinister)
            if key in SINISTER_MAP:
                condition = SINISTER_MAP[key]
            elif "pequena" in key:
                condition = "pequena monta"
            elif "aplicavel" in key:
                condition = "não aplicável"
        if not condition and "financ" in origin:
            condition = "recuperado de financiamento"
        if not condition:
            # Fall back to text parsing (description/origin note).
            from .base import detect_condition

            condition = detect_condition(description, origin_text=origin)
            condition_source = "description"
        if not condition:
            return None

        year_model = raw.get("lot_year_model")
        year_manufacture = raw.get("lot_year_manufacture")
        try:
            year_model = int(year_model) if year_model else None
        except (TypeError, ValueError):
            year_model = None
        try:
            year_manufacture = int(year_manufacture) if year_manufacture else None
        except (TypeError, ValueError):
            year_manufacture = None

        bid = parse_money(raw.get("bid_actual")) or parse_money(raw.get("bid_initial"))
        if not bid:
            return None

        end_raw = raw.get("lot_date_end") or raw.get("auction_date_end")
        end_dt = parse_datetime(end_raw)

        city, state = _parse_location(raw)
        km = raw.get("lot_km")
        try:
            km = int(km) if km else None
        except (TypeError, ValueError):
            km = None
        if km == 0:
            km = None

        auction_id = raw.get("auction_id")
        lot_id = raw.get("lot_id") or raw.get("id")
        url = (
            f"https://leilao.sodresantoro.com.br/leilao/{auction_id}/lote/{lot_id}/"
            if auction_id and lot_id
            else "https://www.sodresantoro.com.br/veiculos/lotes"
        )

        display_title = title or f"{brand_raw.title()} {model_raw.title()}".strip()
        if year_manufacture and year_model:
            display_title = f"{display_title} {year_manufacture}/{year_model}"
        elif year_model:
            display_title = f"{display_title} {year_model}"

        fee_percent, fixed_fees = _parse_rate_information(raw.get("lot_rate_information"))

        pictures = raw.get("lot_pictures") or []
        image_url = None
        if isinstance(pictures, list):
            for item in pictures:
                if isinstance(item, str) and item.strip():
                    image_url = item.strip()
                    break
                if isinstance(item, dict):
                    for key in ("url", "link", "src"):
                        value = item.get(key)
                        if isinstance(value, str) and value.strip():
                            image_url = value.strip()
                            break
                if image_url:
                    break

        return self.make_lot(
            site=self.site,
            site_label=self.label,
            external_id=str(lot_id or ""),
            url=url,
            image_url=image_url,
            title=display_title,
            make=brand,
            model=model_raw or extract_model(title, brand),
            year=year_model or year_manufacture,
            year_manufacture=year_manufacture,
            km=km,
            city=city,
            state=state,
            condition=condition,
            condition_source=condition_source,
            bid_price=bid,
            fee_percent=fee_percent,
            fixed_fees=fixed_fees,
            end_date=end_dt.isoformat(sep=" ", timespec="seconds") if end_dt else None,
            seller=raw.get("client_name") or raw.get("auction_name"),
            raw_data={
                "lot_id": lot_id,
                "auction_id": auction_id,
                "lot_sinister": raw.get("lot_sinister"),
                "lot_origin": raw.get("lot_origin"),
                "auction_status": raw.get("auction_status"),
                "lot_status": raw.get("lot_status"),
                "bid_initial": raw.get("bid_initial"),
                "bid_actual": raw.get("bid_actual"),
                "lot_date_end": end_raw,
                "lot_rate_information": raw.get("lot_rate_information"),
            },
        )
