"""Superbid scraper.

Superbid exposes two JSON services that do not sit behind the site's
Cloudflare challenge:

* ``event-query.superbid.net/events/v2``   - live auction events
* ``offer-query.superbid.net/offers``      - lots

The offers service keeps the full history, so ``endDate`` range filters are
not usable (the index does not map them as dates).  The reliable way to reach
live lots is to enumerate live events and then ask for the car lots of those
auctions by id.

The filter syntax is peculiar: ``;`` is the AND separator (``AND``/``OR``/``,``
return HTTP 503) and a ``terms``-style list is written ``field:[a,b,c]``.

  auction.id:[...];product.subCategory.category.id:10000;statusId:1

returns only open car lots for those auctions.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .base import (
    BaseScraper,
    ScrapeResult,
    UA,
    clean_html,
    detect_brand,
    detect_condition,
    extract_city_state,
    extract_km,
    extract_model,
    extract_year_pair,
    norm,
    parse_datetime,
    parse_money,
)

EVENTS_URL = "https://event-query.superbid.net/events/v2/"
OFFERS_URL = "https://offer-query.superbid.net/offers/"
OFFER_WEB_URL = "https://www.superbid.net/oferta/{offer_id}"

EVENT_KEYWORDS = [
    "veiculo", "veiculos", "carro", "carros", "moto", "motos",
    "automovel", "automoveis", "automotivo", "detran", "frota",
    "sucata", "sinistrad", "transporte", "seguradora", "seguradoras",
    "banco", "financ", "aliena", "fiduciar", "santander", "bradesco",
    "itau", "votorantim", "aymore", "consorcio", "arremate", "reboque",
    "dpvat", "locadora", "caminhao", "onibus", "utilitario", "trator",
    "maquina", "implemento", "seminovo", "conservado", "recuperad",
]

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": "https://www.superbid.net",
    "Referer": "https://www.superbid.net/",
}


def _event_text(event: Dict[str, Any]) -> str:
    return f"{event.get('desc') or ''} {event.get('fullDescription') or ''}".lower()


def _is_candidate_event(event: Dict[str, Any]) -> bool:
    text = _event_text(event)
    return any(keyword in text for keyword in EVENT_KEYWORDS)


def _address_parts(address: Any) -> tuple[Optional[str], Optional[str]]:
    if isinstance(address, dict):
        city = address.get("city") or address.get("cidade")
        state = (
            address.get("stateCode")
            or address.get("state")
            or address.get("uf")
            or address.get("estado")
        )
        if city or state:
            return (city.title() if isinstance(city, str) and city else None,
                    state.upper() if isinstance(state, str) and state else None)
        joined = " ".join(str(v) for v in address.values() if isinstance(v, str))
        return extract_city_state(joined)
    if isinstance(address, str):
        return extract_city_state(address)
    return None, None


def _product_image(product: Dict[str, Any]) -> Optional[str]:
    """First usable photo URL published by Superbid for this product."""
    for key in ("thumbnailUrl", "imageUrl", "image"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    gallery = product.get("galleryJson") or product.get("gallery") or []
    if isinstance(gallery, str):
        try:
            gallery = json.loads(gallery)
        except ValueError:
            gallery = []
    if isinstance(gallery, list):
        for item in gallery:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                for key in ("thumbnailUrl", "link", "url", "image"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    return None


class SuperbidScraper(BaseScraper):
    site = "superbid"
    label = "Superbid"

    async def search(self, config: Dict[str, Any]) -> ScrapeResult:
        options = config.get("scraper_options", {})
        batch_size = int(options.get("superbid_batch_size", 40))
        max_events = int(options.get("superbid_max_events", 500))
        max_pages_per_batch = int(options.get("superbid_max_pages", 3))
        concurrency = int(options.get("superbid_concurrency", 3))
        include_conserved = bool(options.get("superbid_include_conserved_fleet", False))

        async with httpx.AsyncClient(timeout=180, headers=HEADERS, follow_redirects=True) as client:
            try:
                events = await self._fetch_events(client)
            except Exception as exc:
                return ScrapeResult(site=self.site, error=f"{type(exc).__name__}: {exc}")

            now = datetime.now()
            live = [e for e in events if (parse_datetime(e.get("endDate")) or now) >= now]
            candidates = [e for e in live if _is_candidate_event(e)][:max_events]

            if not candidates:
                return ScrapeResult(site=self.site, note="No live vehicle auctions found.",
                                    stats={"events_live": len(live)})

            semaphore = asyncio.Semaphore(max(1, concurrency))
            batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]

            async def run_batch(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                async with semaphore:
                    return await self._fetch_batch(client, batch, max_pages_per_batch)

            results = await asyncio.gather(*(run_batch(b) for b in batches), return_exceptions=True)

        lots: List[Dict[str, Any]] = []
        offers_seen = 0
        errors = 0
        conserved_skipped = 0
        for result in results:
            if isinstance(result, Exception) or result is None:
                errors += 1
                continue
            for offer in result:
                offers_seen += 1
                lot = self._map_offer(offer)
                if not lot:
                    continue
                if lot.get("condition_source") == "source" and not include_conserved:
                    conserved_skipped += 1
                    continue
                lots.append(lot)

        conserved = sum(1 for lot in lots if lot.get("condition_source") == "source")
        notes: List[str] = []
        if errors:
            notes.append(f"{errors} of {len(batches)} auction batches failed; results are partial.")
        if conserved:
            notes.append(
                f"{conserved} lots come from public 'veículos conservados' fleet auctions; "
                "those have no salvage classification, so they are reported as 'não aplicável'."
            )
        if conserved_skipped:
            notes.append(
                f"{conserved_skipped} conserved-fleet lots were skipped because the source does not "
                "declare a condition; enable 'include conserved fleet' to scan them."
            )
        note = " ".join(notes)
        stats = {
            "events_total": len(events),
            "events_live": len(live),
            "events_scanned": len(candidates),
            "offers_seen": offers_seen,
            "batches": len(batches),
            "conserved_fleet_lots": conserved,
            "conserved_fleet_skipped": conserved_skipped,
        }
        return ScrapeResult(site=self.site, lots=lots, note=note, stats=stats)

    async def _fetch_events(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for page in range(1, 12):
            response = await client.get(EVENTS_URL, params={
                "portalId": "[2,15]",
                "locale": "pt_BR",
                "timeZoneId": "America/Sao_Paulo",
                "filter": "modalityId:[1,4,5,7]",
                "pageNumber": page,
                "pageSize": 200,
                "byPage": "marketPlacePage",
            })
            if response.status_code != 200:
                break
            batch = response.json().get("events") or []
            events.extend(batch)
            if len(batch) < 200:
                break
        return events

    async def _fetch_batch(
        self, client: httpx.AsyncClient, batch: List[Dict[str, Any]], max_pages: int
    ) -> List[Dict[str, Any]]:
        ids = ",".join(str(event["id"]) for event in batch if event.get("id"))
        if not ids:
            return []
        search_filter = (
            f"auction.id:[{ids}];"
            "product.subCategory.category.id:10000;"
            "statusId:1"
        )

        offers: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            payload = await self._get_offers(client, search_filter, page)
            if payload is None:
                break
            page_offers = payload.get("offers") or []
            offers.extend(page_offers)
            if len(page_offers) < 1000:
                break
        return offers

    async def _get_offers(
        self, client: httpx.AsyncClient, search_filter: str, page: int
    ) -> Optional[Dict[str, Any]]:
        for attempt in range(3):
            try:
                response = await client.get(OFFERS_URL, params={
                    "portalId": "[2,15]",
                    "locale": "pt_BR",
                    "pageNumber": page,
                    "pageSize": 1000,
                    "filter": search_filter,
                })
            except httpx.HTTPError:
                await asyncio.sleep(1 + attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return None
            if response.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(1 + attempt * 2)
                continue
            return None
        return None

    def _map_offer(self, offer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        product = offer.get("product") or {}
        auction = offer.get("auction") or {}
        seller = offer.get("seller") or {}
        detail = offer.get("offerDetail") or {}

        short = clean_html(product.get("shortDesc") or "")
        long_desc = clean_html(product.get("detailedDescription") or product.get("description") or "")
        offer_desc = clean_html(offer.get("offerDescription") or "")
        auction_desc = clean_html(auction.get("desc") or "")

        combined = " ".join(part for part in (short, offer_desc, long_desc) if part)
        if not combined:
            return None
        if not short:
            short = long_desc[:120]

        brand = detect_brand(short, long_desc, offer_desc)
        if not brand:
            return None

        seller_name = seller.get("name") if isinstance(seller, dict) else str(seller or "")
        origin_text = " ".join(part for part in (auction_desc, seller_name or "") if part)
        condition = detect_condition(combined, origin_text=origin_text)
        condition_source = "description"
        if not condition:
            # Public fleet auctions sell conserved vehicles: there is no salvage
            # classification, which is exactly the "not applicable" case.
            auction_norm = norm(auction_desc)
            if "conservad" in auction_norm or "veiculos em uso" in auction_norm:
                condition = "não aplicável"
                condition_source = "source"
        if not condition:
            return None

        year_manufacture, year_model = extract_year_pair(short, long_desc, offer_desc)
        if not year_model:
            year_manufacture, year_model = extract_year_pair(auction_desc)

        bid = (
            parse_money(detail.get("currentMinBid"))
            or parse_money(detail.get("initialBidValue"))
            or parse_money(offer.get("price"))
        )
        if not bid:
            return None

        end_dt = parse_datetime(offer.get("endDate")) or parse_datetime(offer.get("endDateTime"))
        if not end_dt:
            return None
        if end_dt < datetime.now():
            return None

        # City and state must come from the same record, otherwise the pair can be
        # nonsense (e.g. a Belo Horizonte seller at a São Paulo auction yard).
        city, state = _address_parts(auction.get("address"))
        if not city and isinstance(seller, dict) and seller.get("city"):
            seller_city = str(seller["city"]).strip()
            if seller_city:
                city = seller_city.title()
                state = None  # the seller record carries no state, so do not guess one

        offer_id = offer.get("id")
        url = OFFER_WEB_URL.format(offer_id=offer_id) if offer_id else ""

        title = short
        if year_manufacture and year_model and str(year_manufacture) not in short:
            title = f"{short} {year_manufacture}/{year_model}"

        return self.make_lot(
            site=self.site,
            site_label=self.label,
            external_id=str(offer_id or ""),
            url=url,
            image_url=_product_image(product),
            title=title,
            make=brand,
            model=extract_model(short, brand),
            year=year_model,
            year_manufacture=year_manufacture,
            km=extract_km(combined),
            city=city,
            state=state,
            condition=condition,
            condition_source=condition_source,
            bid_price=bid,
            end_date=end_dt.isoformat(sep=" ", timespec="seconds"),
            seller=seller_name or None,
            raw_data={
                "offer_id": offer_id,
                "auction_id": auction.get("id"),
                "auction_desc": auction_desc,
                "lot_number": offer.get("lotNumber"),
                "short_desc": short,
                "detail": long_desc[:600],
                "initial_bid": detail.get("initialBidValue"),
                "current_min_bid": detail.get("currentMinBid"),
                "end_date": offer.get("endDate"),
                "status_id": offer.get("statusId"),
            },
        )
