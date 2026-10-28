"""Copart Brazil scraper.

Copart Brazil's public search page, ``/lotSearchResults/``, drives a
DataTables grid through a form-urlencoded endpoint::

    POST https://www.copart.com.br/public/lots/search
    Content-Type: application/x-www-form-urlencoded; charset=UTF-8
    X-Requested-With: XMLHttpRequest

That endpoint is reachable **without an account** and it honours paging
(``page`` / ``size``, up to 1,000 lots per request) and Solr-style filters.
Each lot carries everything needed for valuation:

* ``hb``                    current / starting bid
* ``la``                    Copart's published FIPE value
* ``damageClassification``  Pequena Monta, Média Monta, Grande Monta, Não Aplicável
* ``lossType``              Colisão, Roubo/Furto, Financiamento, ...
* ``td`` / ``stt``          document state (Recuperável, Normal, Irrecuperável)
* ``ad``                    auction date
* ``yn``                    yard (city - state)
* ``mkn`` / ``lm`` / ``lcy`` make, model, year

The JSON variant of the same URL (``Content-Type: application/json``) is a
different, deliberately crippled path: it ignores paging and filters and only
ever returns ten unclassified, unpriced lots.  Sending a JSON body is why
Copart used to look "limited"; the DataTables form body is the working call.

Instead of walking the whole 13,798-lot catalogue, the scraper runs three
filtered queries that together cover the supported conditions and unions them::

    filter[classificacaodano] = classificacaodano:"Pequena Monta"
    filter[classificacaodano] = classificacaodano:"Não Aplicável"
    filter[condicao]          = condicao:"FINANCIAMENTO"

That is roughly 2,250 lots instead of 13,800, so a scan stays fast and only
promising lots reach the FIPE valuation stage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .base import (
    BaseScraper,
    ScrapeResult,
    UA,
    norm,
    parse_datetime,
    parse_money,
)

LISTING_URL = "https://www.copart.com.br/lotSearchResults/?free=true&query="
SEARCH_PATH = "/public/lots/search"

# Fixed DataTables parameters captured from the site's own request.  The
# endpoint ignores ``columns[...]`` entries, so only the meaningful fields are
# sent; paging and filters are injected per request.
FORM_BASE: Dict[str, str] = {
    "draw": "1",
    "order[0][column]": "1",
    "order[0][dir]": "asc",
    "search[value]": "",
    "search[regex]": "false",
    "sort": "auction_date_utc asc,brazil_default_sort asc",
    "defaultSort": "true",
    "query": "*",
    "watchListOnly": "false",
    "freeFormSearch": "false",
}

# One query per supported condition; results are unioned by lot number.
CONDITION_QUERIES: Tuple[Tuple[str, str], ...] = (
    ("classificacaodano", 'classificacaodano:"Pequena Monta"'),
    ("classificacaodano", 'classificacaodano:"Não Aplicável"'),
    ("condicao", 'condicao:"FINANCIAMENTO"'),
)

CAPTCHA_MARKERS = (
    "captcha",
    "verificação de segurança",
    "verificacao de seguranca",
    "incapsula",
)

FINANCING_LOSS = "financiamento"
REJECTED_CLASSIFICATIONS = ("media monta", "grande monta", "aguardando")
IRRECOVERABLE_DOCS = ("irrecuperavel",)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _condition(raw: Dict[str, Any]) -> Optional[str]:
    """Map Copart's fields onto one of the supported conditions."""
    classification = norm(_text(raw.get("damageClassification")))
    loss = norm(_text(raw.get("lossType")))
    document = norm(_text(raw.get("td")))
    vin_type = norm(_text(raw.get("vinType")))

    if any(marker in document for marker in IRRECOVERABLE_DOCS):
        return None
    if "sucata" in classification or "desmanche" in classification:
        return None
    if any(marker in classification for marker in REJECTED_CLASSIFICATIONS):
        return None
    if "remarcado" in vin_type or "adulterado" in norm(_text(raw.get("dd"))):
        return None

    if loss == FINANCING_LOSS:
        return "recuperado de financiamento"
    if "pequena monta" in classification:
        return "pequena monta"
    if "nao aplicavel" in classification:
        return "não aplicável"
    return None


def _split_yard(yard: str) -> Tuple[Optional[str], Optional[str]]:
    if not yard:
        return None, None
    if "-" in yard:
        left, _, right = yard.rpartition("-")
        state = right.strip().upper()
        if 2 <= len(state) <= 3 and state.isalpha():
            return (left.strip().title() or None), state
    return (yard.title() or None), None


def _to_int(value: Any) -> Optional[int]:
    try:
        number = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    return number if 1900 <= number <= 2100 else None


def _map_lot(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    bid = parse_money(raw.get("hb")) or 0.0
    if bid <= 0:
        return None  # no price yet: cannot be compared against FIPE

    lot_number = _text(raw.get("lotNumberStr")) or _text(raw.get("ln"))
    if not lot_number:
        return None

    condition = _condition(raw)
    if not condition:
        return None

    # Copart prints "Valor FIPE" on every lot page; ``la`` is that value.  It is
    # used as the valuation instead of spending an official-FIPE lookup.
    declared_fipe = parse_money(raw.get("la")) or 0.0

    year_manufacture = _to_int(raw.get("manufactureYear"))
    year = _to_int(raw.get("lcy")) or year_manufacture
    make = _text(raw.get("mkn")) or None
    model = _text(raw.get("lm"))
    city, state = _split_yard(_text(raw.get("yn")))
    end_date = parse_datetime(raw.get("ad"))
    if not end_date:
        return None

    image_url = _text(raw.get("tims")).replace("imageType=thumbnail", "imageType=large") or None

    return {
        "external_id": lot_number,
        "url": f"https://www.copart.com.br/lot/{lot_number}",
        "image_url": image_url,
        "title": _text(raw.get("ld")) or f"{make or ''} {model} {year or ''}".strip(),
        "make": make.lower() if make else None,
        "model": model,
        "year": year,
        "year_manufacture": year_manufacture,
        "km": None,
        "city": city,
        "state": state,
        "condition": condition,
        "condition_source": "source",
        "bid_price": bid,
        "declared_fipe_price": declared_fipe or None,
        "end_date": end_date,
        "seller": _text(raw.get("sellerName")) or None,
        "raw_data": {
            "damage_classification": _text(raw.get("damageClassification")),
            "loss_type": _text(raw.get("lossType")),
            "document": _text(raw.get("td")),
            "damage": _text(raw.get("dd")),
            "vin_type": _text(raw.get("vinType")),
            "yard": _text(raw.get("yn")),
            "vehicle_type": _text(raw.get("vehicleType")),
            "sale_type": _text(raw.get("saleType")),
            "copart_fipe": _text(raw.get("la")),
            "is_bin": bool(raw.get("isBin")),
            "is_initial_bid": bool(raw.get("isInitialBid")),
        },
    }


class CopartScraper(BaseScraper):
    site = "copart"
    label = "Copart Brazil"

    @staticmethod
    async def _fetch_page(
        page,
        field: str,
        expression: str,
        page_no: int,
        size: int,
    ) -> Dict[str, Any]:
        form = dict(FORM_BASE)
        form.update(
            {
                f"filter[{field}]": expression,
                f"includeTagByField[{field}]": f"{{!tag={field}}}",
                "page": str(page_no),
                "start": str(page_no * size),
                "size": str(size),
                "length": str(size),
                "draw": str(page_no + 1),
            }
        )
        result = await page.evaluate(
            """async ({path, body}) => {
                const response = await fetch(path, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'Accept': 'application/json, text/javascript, */*; q=0.01',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: body,
                });
                return {status: response.status, text: await response.text()};
            }""",
            {"path": SEARCH_PATH, "body": urlencode(form)},
        )
        return result

    async def search(self, config: Dict[str, Any]) -> ScrapeResult:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - dependency missing
            return ScrapeResult(site=self.site, error=f"Playwright unavailable: {exc}")

        import json

        filters = config.get("filters") or {}
        options = config.get("scraper_options") or {}
        allowed_makes = {str(item).lower() for item in (filters.get("makes") or [])}
        exclude_motorcycles = bool(options.get("copart_exclude_motorcycles", True))
        max_ratio = float(options.get("copart_max_bid_fipe_ratio", 0.85) or 0.85)
        size = max(20, min(int(options.get("copart_page_size", 1000) or 1000), 1000))
        max_pages = max(1, int(options.get("copart_max_pages", 3) or 3))
        headless = bool(options.get("copart_headless", True))

        stats: Dict[str, Any] = {"status": "empty"}
        raw_lots: Dict[str, Dict[str, Any]] = {}
        queries = 0
        filter_totals: Dict[str, int] = {}

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                try:
                    context = await browser.new_context(
                        user_agent=UA,
                        viewport={"width": 1440, "height": 900},
                        locale="pt-BR",
                        timezone_id="America/Sao_Paulo",
                    )
                    page = await context.new_page()
                    await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=90000)
                    await page.wait_for_timeout(4000)

                    body = ""
                    try:
                        body = (await page.inner_text("body")).lower()
                    except Exception:
                        pass
                    if any(marker in body for marker in CAPTCHA_MARKERS):
                        return ScrapeResult(
                            site=self.site,
                            note=(
                                "Copart served a bot challenge instead of results. "
                                "Retry later, or set \"copart_headless\": false."
                            ),
                            stats={"status": "blocked"},
                        )

                    for field, expression in CONDITION_QUERIES:
                        queries += 1
                        page_no = 0
                        while page_no < max_pages:
                            result = await self._fetch_page(
                                page, field, expression, page_no, size
                            )
                            if result.get("status") != 200:
                                break
                            try:
                                payload = json.loads(result.get("text") or "{}")
                                results = (payload.get("data") or {}).get("results") or {}
                            except Exception:
                                break

                            content = results.get("content") or []
                            total = int(results.get("totalElements") or 0)
                            if page_no == 0:
                                filter_totals[expression] = total
                            for item in content:
                                lot_number = _text(item.get("lotNumberStr")) or _text(
                                    item.get("ln")
                                )
                                if lot_number:
                                    raw_lots[lot_number] = item

                            page_no += 1
                            if not content or page_no * size >= total:
                                break
                finally:
                    await browser.close()
        except Exception as exc:
            return ScrapeResult(site=self.site, error=f"{type(exc).__name__}: {exc}")

        if not raw_lots:
            return ScrapeResult(
                site=self.site,
                note=(
                    "Copart returned no lots for the supported conditions. "
                    "The site may be changing its search endpoint."
                ),
                stats={"status": "empty"},
            )

        now = datetime.now()
        lots: List[Dict[str, Any]] = []
        unpriced = 0
        wrong_make = 0
        rejected_condition = 0
        expired = 0
        motorcycles = 0
        too_expensive = 0

        for raw in raw_lots.values():
            bid = parse_money(raw.get("hb")) or 0.0
            if bid <= 0:
                unpriced += 1
                continue

            make = norm(_text(raw.get("mkn")))
            if allowed_makes and make not in allowed_makes:
                wrong_make += 1
                continue

            if exclude_motorcycles and "moto" in norm(_text(raw.get("vehicleType"))):
                motorcycles += 1
                continue

            mapped = _map_lot(raw)
            if not mapped:
                rejected_condition += 1
                continue

            if mapped["end_date"] and mapped["end_date"] < now:
                expired += 1
                continue

            # Copart publishes the FIPE value.  When the live bid is already a
            # large share of it, no target margin is reachable even before
            # fees, so skip the expensive FIPE lookup for those lots.
            copart_fipe = parse_money(raw.get("la")) or 0.0
            if max_ratio > 0 and copart_fipe > 0 and bid > copart_fipe * max_ratio:
                too_expensive += 1
                continue

            lots.append(self.make_lot(site=self.site, site_label=self.label, **mapped))

        stats.update(
            {
                "filter_totals": filter_totals,
                "filters": queries,
                "sampled": len(raw_lots),
                "priced": len(raw_lots) - unpriced,
                "matched": len(lots),
                "skipped_unpriced": unpriced,
                "skipped_make": wrong_make,
                "skipped_motorcycle": motorcycles,
                "skipped_condition": rejected_condition,
                "skipped_expired": expired,
                "skipped_above_fipe_ratio": too_expensive,
            }
        )

        if lots:
            stats["status"] = "ok"
            return ScrapeResult(
                site=self.site,
                lots=lots,
                note=(
                    f"Read {len(raw_lots):,} Copart lots matching the supported conditions; "
                    f"{len(lots)} also matched the make, price and date filters."
                ),
                stats=stats,
            )

        stats["status"] = "empty"
        return ScrapeResult(
            site=self.site,
            note=(
                f"Read {len(raw_lots):,} Copart lots matching the supported conditions but "
                f"none survived: {unpriced:,} without a bid, {wrong_make:,} outside the "
                f"selected makes, {motorcycles:,} motorcycles, {too_expensive:,} already "
                f"too close to FIPE, {expired:,} closed."
            ),
            stats=stats,
        )
