"""Master Leiloes scraper.

Two distinct properties are grouped under the "Leilao Master" source:

* ``masterfinanciados.com/carros`` - Master Financiados, a seller of
  repossessed/financed vehicles (direct sale, asking price on the page).
* ``maxterleiloes.com.br`` - Master Leiloes' auction platform.  Its lot list
  is served by ``/app/lotes`` which is only reachable from inside the site's
  own browser context, so it is captured with Playwright.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from .base import (
    BaseScraper,
    ScrapeResult,
    UA,
    clean_html,
    detect_brand,
    detect_condition,
    extract_model,
    extract_year_pair,
    parse_datetime,
    parse_money,
)

MASTER_FINANCIADOS_URL = "https://www.masterfinanciados.com/carros"
MAXTER_HOME = "https://www.maxterleiloes.com.br/home"
MAXTER_API_MARKER = "/app/lotes"

LABEL_PATTERN = re.compile(
    r"MARCA\s*:\s*(?P<make>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\- ]{1,20}?)\s*"
    r"MODELO\s*:\s*(?P<model>.+?)\s*"
    r"ANO\s*:\s*(?P<year>\d{2,4}(?:\s*/\s*\d{2,4})?)",
    re.IGNORECASE | re.DOTALL,
)

PLAIN_PATTERN = re.compile(
    r"^(?P<make>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-]{1,20})\s+"
    r"(?P<model>.+?)\s+"
    r"(?P<year>\d{2,4}(?:\s*/\s*\d{2,4})?)$",
    re.DOTALL,
)

PRICE_PATTERN = re.compile(r"R\$\s*:?\s*([\d][\d.,]*)")


class MasterScraper(BaseScraper):
    site = "leilao_master"
    label = "Master Leilões"

    async def search(self, config: Dict[str, Any]) -> ScrapeResult:
        lots: List[Dict[str, Any]] = []
        notes: List[str] = []
        stats: Dict[str, Any] = {}

        financiados = await self._scrape_financiados()
        if isinstance(financiados, Exception):
            notes.append(f"Master Financiados failed: {financiados}")
        else:
            lots.extend(financiados)
            stats["master_financiados_lots"] = len(financiados)

        maxter = await self._scrape_maxter()
        if isinstance(maxter, Exception):
            notes.append(f"Maxter failed: {maxter}")
        else:
            maxter_lots, maxter_total = maxter
            lots.extend(maxter_lots)
            stats["maxter_lots_total"] = maxter_total
            stats["maxter_lots_matched"] = len(maxter_lots)
            if maxter_total and not maxter_lots:
                notes.append(
                    f"Maxter: {maxter_total} open lot(s), none declaring a supported "
                    "condition (pequena monta / não aplicável / recuperado de financiamento)."
                )

        return ScrapeResult(site=self.site, lots=lots, note=" ".join(notes), stats=stats)

    # ------------------------------------------------------------------
    # Master Financiados
    # ------------------------------------------------------------------
    async def _scrape_financiados(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(
            timeout=60, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}
        ) as client:
            response = await client.get(MASTER_FINANCIADOS_URL, follow_redirects=True)
        response.encoding = "utf-8"
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        text = " ".join(soup.get_text(" ").split())

        start = text.lower().find("categoria - carros")
        if start < 0:
            start = text.lower().find("categoria")
        if start < 0:
            return []
        end = text.find("Master Financiados", start + 10)
        segment = text[start:end if end > start else len(text)]

        lots: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for chunk in re.split(r"(?i)DISPON[IÍ]VEL", segment):
            prices = list(PRICE_PATTERN.finditer(chunk))
            if not prices:
                continue
            price_match = prices[-1]
            price = parse_money(price_match.group(1))
            description = clean_html(chunk[: price_match.start()])
            description = re.sub(r"(?i)CATEGORIA\s*-\s*CARROS", " ", description).strip(" -|")
            if not price or not description:
                continue

            make_text = model_text = None
            year_text = None
            labelled = LABEL_PATTERN.search(description)
            if labelled:
                make_text = labelled.group("make").strip()
                model_text = labelled.group("model").strip(" -:")
                year_text = labelled.group("year")
            else:
                plain = PLAIN_PATTERN.match(description.strip())
                if plain:
                    make_text = plain.group("make").strip()
                    model_text = plain.group("model").strip(" -:")
                    year_text = plain.group("year")

            if not make_text or not model_text:
                continue

            brand = detect_brand(make_text, model_text)
            if not brand:
                continue

            year_manufacture, year_model = extract_year_pair(year_text or "", description)
            year_model = year_model or year_manufacture

            title = f"{make_text.upper()} {model_text.upper()}"
            if year_text:
                title = f"{title} {year_text.replace(' ', '')}"

            external_id = re.sub(r"[^a-z0-9]+", "-", f"{brand}-{model_text}-{year_text}-{int(price)}".lower()).strip("-")
            if external_id in seen:
                continue
            seen.add(external_id)

            lots.append(
                self.make_lot(
                    site=self.site,
                    site_label="Master Financiados",
                    external_id=external_id,
                    url=MASTER_FINANCIADOS_URL,
                    title=title,
                    make=brand,
                    model=model_text or extract_model(title, brand),
                    year=year_model,
                    year_manufacture=year_manufacture,
                    km=None,
                    city=None,
                    state=None,
                    condition="recuperado de financiamento",
                    condition_source="source",
                    bid_price=price,
                    end_date=None,
                    seller="Master Financiados",
                    raw_data={"source": "masterfinanciados", "description": description[:300],
                              "price_raw": price_match.group(1)},
                )
            )
        return lots

    # ------------------------------------------------------------------
    # Maxter (Master Leiloes platform)
    # ------------------------------------------------------------------
    async def _scrape_maxter(self) -> tuple[List[Dict[str, Any]], int]:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Playwright unavailable: {exc}")

        captured: List[Dict[str, Any]] = []

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
                    user_agent=UA, viewport={"width": 1400, "height": 900},
                    locale="pt-BR", timezone_id="America/Sao_Paulo",
                )
                page = await ctx.new_page()

                async def on_response(response):
                    if MAXTER_API_MARKER not in response.url:
                        return
                    try:
                        if response.status != 200:
                            return
                        payload = await response.json()
                    except Exception:
                        return
                    if isinstance(payload, dict) and isinstance(payload.get("lotes"), list):
                        captured.append(payload)

                page.on("response", on_response)
                await page.goto(MAXTER_HOME, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(4000)

                if not captured:
                    # Retry the fetch from inside the same context.
                    try:
                        data = await page.evaluate(
                            """async () => {
                                const r = await fetch('/app/lotes', {headers: {'Accept': 'application/json'}});
                                if (!r.ok) return null;
                                return await r.json();
                            }"""
                        )
                        if isinstance(data, dict) and isinstance(data.get("lotes"), list):
                            captured.append(data)
                    except Exception:
                        pass
            finally:
                await browser.close()

        all_lotes: List[Dict[str, Any]] = []
        for payload in captured:
            all_lotes.extend(payload.get("lotes") or [])

        now = datetime.now()
        lots: List[Dict[str, Any]] = []
        for index, raw in enumerate(all_lotes):
            name = clean_html(raw.get("nome") or "")
            if not name:
                continue
            brand = detect_brand(name)
            if not brand:
                continue

            status = (raw.get("status") or "").strip().lower()
            if status and status not in ("aberto", "em andamento", "aberto para lances"):
                continue

            end_dt = parse_datetime(raw.get("data"))
            if end_dt and end_dt < now:
                continue

            price = parse_money(raw.get("valor")) or parse_money(raw.get("inicial"))
            if not price:
                continue

            origin_text = " ".join(str(raw.get(key) or "") for key in ("vara", "origem", "leilao", "praca"))
            condition = detect_condition(name, origin_text=origin_text)
            if not condition:
                continue

            year_manufacture, year_model = extract_year_pair(name)
            city, state = self._parse_local(raw.get("local"))
            url = raw.get("url") or "https://www.maxterleiloes.com.br/home"

            lots.append(
                self.make_lot(
                    site=self.site,
                    site_label="Maxter Leilões",
                    external_id=str(raw.get("id") or f"maxter-{index}"),
                    url=url,
                    image_url=raw.get("image"),
                    title=f"{name} {raw.get('lote') or ''}".strip(),
                    make=brand,
                    model=extract_model(name, brand),
                    year=year_model,
                    year_manufacture=year_manufacture,
                    km=None,
                    city=city,
                    state=state,
                    condition=condition,
                    condition_source="description",
                    bid_price=price,
                    end_date=end_dt.isoformat(sep=" ", timespec="seconds") if end_dt else None,
                    seller="Maxter Leilões",
                    raw_data={k: raw.get(k) for k in ("id", "lote", "status", "valor", "avaliacao",
                                                       "local", "vara", "origem", "data", "leilao")},
                )
            )

        return lots, len(all_lotes)

    @staticmethod
    def _parse_local(value: Any) -> tuple[Optional[str], Optional[str]]:
        if not isinstance(value, str) or not value.strip():
            return None, None
        m = re.match(r"^(?P<city>.+?)\s*-\s*(?P<state>[A-Z]{2})$", value.strip())
        if m:
            return m.group("city").strip().title(), m.group("state").upper()
        return None, None
