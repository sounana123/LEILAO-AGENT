"""FIPE valuation client.

Uses the official FIPE table API (the same one behind veiculos.fipe.org.br):

    POST https://veiculos.fipe.org.br/api/veiculos/ConsultarTabelaDeReferencia
    POST .../ConsultarModelos
    POST .../ConsultarAnoModelo
    POST .../ConsultarValorComTodosParametros

That service returns the authoritative value for one model/year in a single
call, which keeps a full scan well inside the free-tier limits (the public
mirror rate-limits aggressively once you start probing model generations).

Caching rules that matter for correctness:

* successful structure/value payloads are cached on disk for days;
* a *failed* request is never written to disk - it would poison later scans
  when the API is briefly unavailable;
* in-memory negative entries expire after a couple of minutes;
* the resolved (model, year) pair for a given car description is cached, so
  repeated lots reuse the same FIPE generation.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .scrapers.base import norm, parse_money

BASE_URL = "https://veiculos.fipe.org.br/api/veiculos"

BRAND_CODES: Dict[str, int] = {
    "bmw": 7,
    "fiat": 21,
    "ford": 22,
    "chevrolet": 23,
    "honda": 25,
    "toyota": 56,
    "volkswagen": 59,
}

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "fipe_cache.json"
STRUCTURE_TTL = 7 * 24 * 3600
VALUE_TTL = 12 * 3600
NEGATIVE_TTL = 120

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://veiculos.fipe.org.br",
    "Referer": "https://veiculos.fipe.org.br/",
}

# Tokens that carry no discriminating power for FIPE model matching.
NOISE_TOKENS = {
    "flex", "gasolina", "diesel", "alcool", "etanol", "aut", "automatico",
    "automatica", "manual", "mec", "sedan", "hatch", "suv", "pickup", "picape",
    "cabine", "dupla", "simples", "4p", "2p", "4x2", "4x4", "awd",
}


def _tokens(value: str) -> List[str]:
    text = norm(value)
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    return [token for token in text.split() if token and token not in NOISE_TOKENS]


def _year_from_name(name: str) -> Optional[int]:
    match = re.search(r"((?:19|20)\d{2})", name or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if 1980 <= year <= 2030 else None


def _coarse(token: str) -> Optional[str]:
    """Numeric family of a token: 320i / 320iA / 320iA4 -> '320'."""
    match = re.match(r"^(\d{2,4})([a-z]{0,4})$", token or "")
    return match.group(1) if match else None


def _coarse_equal(left: str, right: str) -> bool:
    a, b = _coarse(left), _coarse(right)
    return bool(a and b and a == b)


class Fipe:
    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._negative: Dict[str, float] = {}
        self._loaded = False
        self._dirty = False
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(2)
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_interval = 0.4  # the FIPE API throttles bursts with HTTP 429
        self._tabela: Optional[int] = None
        self.stats = {"requests": 0, "cache_hits": 0, "misses": 0, "errors": 0}

    # ------------------------------------------------------------------
    # cache plumbing
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if CACHE_PATH.exists():
                with open(CACHE_PATH, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._cache = data
        except Exception:
            self._cache = {}

    async def _save(self) -> None:
        if not self._dirty:
            return
        async with self._lock:
            try:
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_PATH.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(self._cache, handle, ensure_ascii=False)
                tmp.replace(CACHE_PATH)
                self._dirty = False
            except Exception:
                pass

    def _get(self, key: str, ttl: int) -> Any:
        self._load()
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - float(entry.get("ts") or 0) > ttl:
            return None
        return entry.get("data")

    def _set(self, key: str, data: Any) -> None:
        self._load()
        self._cache[key] = {"ts": time.time(), "data": data}
        self._dirty = True

    def _is_negative(self, key: str) -> bool:
        ts = self._negative.get(key)
        if ts is None:
            return False
        if time.time() - ts > NEGATIVE_TTL:
            self._negative.pop(key, None)
            return False
        return True

    def _mark_negative(self, key: str) -> None:
        self._negative[key] = time.time()

    # ------------------------------------------------------------------
    # http
    # ------------------------------------------------------------------
    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=40, headers=HEADERS, follow_redirects=True)
        return self._client

    async def _throttle(self) -> None:
        """Space requests out; the FIPE API rejects bursts with HTTP 429."""
        async with self._rate_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _post(self, endpoint: str, payload: Dict[str, Any]) -> Tuple[str, Any]:
        """Return (status, payload) with status in ok / notfound / error."""
        client = await self._client_get()
        for attempt in range(3):
            try:
                async with self._semaphore:
                    await self._throttle()
                    self.stats["requests"] += 1
                    response = await client.post(f"{BASE_URL}/{endpoint}", json=payload)
                if response.status_code == 200:
                    try:
                        return "ok", response.json()
                    except ValueError:
                        await asyncio.sleep(0.6)
                        continue
                if response.status_code == 404:
                    return "notfound", None
                if response.status_code == 429:
                    self.stats["throttled"] = self.stats.get("throttled", 0) + 1
                    await asyncio.sleep(15.0 + attempt * 15.0)
                    continue
                if response.status_code in (500, 502, 503, 504):
                    await asyncio.sleep(2.0 + attempt * 3.0)
                    continue
                return "error", None
            except Exception:
                await asyncio.sleep(1.0 + attempt * 1.5)
        self.stats["errors"] += 1
        return "error", None

    # ------------------------------------------------------------------
    # reference table
    # ------------------------------------------------------------------
    async def tabela(self) -> Optional[int]:
        if self._tabela is not None:
            return self._tabela
        cached = self._get("tabela", 24 * 3600)
        if cached:
            self._tabela = int(cached)
            return self._tabela
        status, payload = await self._post("ConsultarTabelaDeReferencia", {})
        if status == "ok" and isinstance(payload, list) and payload:
            code = int(payload[0]["Codigo"])
            self._tabela = code
            self._set("tabela", code)
            await self._save()
            return code
        return None

    # ------------------------------------------------------------------
    # structure
    # ------------------------------------------------------------------
    async def models(self, brand: str) -> List[Dict[str, Any]]:
        code = BRAND_CODES.get(brand)
        if not code:
            return []
        key = f"models:{brand}"
        cached = self._get(key, STRUCTURE_TTL)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached
        if self._is_negative(key):
            return []

        reference = await self.tabela()
        if reference is None:
            self._mark_negative(key)
            return []

        status, payload = await self._post("ConsultarModelos", {
            "codigoTipoVeiculo": 1,
            "codigoTabelaReferencia": reference,
            "codigoMarca": code,
        })
        if status == "error":
            self._mark_negative(key)
            return []

        raw = (payload or {}).get("Modelos") if isinstance(payload, dict) else None
        models = [
            {"codigo": item.get("Value"), "nome": (item.get("Label") or "").strip()}
            for item in (raw or [])
            if item.get("Value") is not None
        ]
        self._set(key, models)
        await self._save()
        return models

    async def years(self, brand: str, model_code: Any) -> Optional[List[Dict[str, Any]]]:
        """Years available for a model. None means the request failed."""
        code = BRAND_CODES.get(brand)
        if not code:
            return []
        key = f"years:{brand}:{model_code}"
        cached = self._get(key, STRUCTURE_TTL)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached
        if self._is_negative(key):
            return None

        reference = await self.tabela()
        if reference is None:
            self._mark_negative(key)
            return None

        status, payload = await self._post("ConsultarAnoModelo", {
            "codigoTipoVeiculo": 1,
            "codigoTabelaReferencia": reference,
            "codigoMarca": code,
            "codigoModelo": model_code,
        })
        if status == "error":
            self._mark_negative(key)
            return None
        years = [
            {"codigo": item.get("Value"), "nome": (item.get("Label") or "").strip()}
            for item in (payload if isinstance(payload, list) else [])
            if item.get("Value")
        ]
        self._set(key, years)
        await self._save()
        return years

    async def value_for_year(self, brand: str, model_code: Any,
                             year_code: str) -> Optional[Dict[str, Any]]:
        code = BRAND_CODES.get(brand)
        if not code:
            return None
        key = f"value:{brand}:{model_code}:{year_code}"
        cached = self._get(key, VALUE_TTL)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached or None
        if self._is_negative(key):
            return None

        reference = await self.tabela()
        if reference is None:
            return None

        try:
            year_text, fuel_text = str(year_code).split("-", 1)
            ano_modelo = int(year_text)
            tipo_combustivel = int(fuel_text)
        except (TypeError, ValueError):
            return None

        status, payload = await self._post("ConsultarValorComTodosParametros", {
            "codigoTipoVeiculo": 1,
            "codigoTabelaReferencia": reference,
            "codigoMarca": code,
            "codigoModelo": model_code,
            "anoModelo": ano_modelo,
            "codigoTipoCombustivel": tipo_combustivel,
            "tipoConsulta": "tradicional",
        })
        if status == "error":
            self._mark_negative(key)
            return None
        if not isinstance(payload, dict) or not payload.get("Valor"):
            self._set(key, None)
            await self._save()
            self.stats["misses"] += 1
            return None

        self._set(key, payload)
        await self._save()
        return payload

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------
    @staticmethod
    def _model_year_conflict(name: str, targets: List[int]) -> bool:
        """True when a FIPE model name embeds a year far from the lot's year."""
        embedded = [int(m) for m in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", name or "")]
        if not embedded:
            return False
        targets = [t for t in targets if t]
        if not targets:
            return False
        return not any(abs(year - target) <= 2 for year in embedded for target in targets)

    @staticmethod
    def ranked_models(model_text: str, models: List[Dict[str, Any]],
                      year_model: Optional[int], year_manufacture: Optional[int],
                      limit: int = 12) -> List[Tuple[Dict[str, Any], float]]:
        ordered_tokens = _tokens(model_text)
        if not ordered_tokens:
            return []
        lot_tokens = set(ordered_tokens)
        lot_first = ordered_tokens[0]
        targets = [year_model, year_manufacture]

        scored: List[Tuple[float, int, int, Dict[str, Any]]] = []
        for candidate in models:
            name = candidate.get("nome") or ""
            if Fipe._model_year_conflict(name, targets):
                continue
            candidate_tokens = set(_tokens(name))
            if not candidate_tokens:
                continue

            full = lot_tokens & candidate_tokens
            coarse = {
                token for token in lot_tokens - full
                for other in candidate_tokens
                if _coarse_equal(token, other)
            }
            if not full and not coarse:
                continue

            score = (len(full) + 0.5 * len(coarse)) / len(lot_tokens)
            candidate_first = _tokens(name)[0]
            if candidate_first == lot_first:
                score += 0.35
            elif _coarse_equal(candidate_first, lot_first):
                score += 0.2
            if score < 0.4:
                continue
            scored.append((round(score, 4), -len(candidate_tokens), -len(name), candidate))

        if not scored:
            return []

        # When the lot's family token identifies the model, never consider a
        # different family (e.g. "320i" must not fall back to "M 135i").
        anchored = [
            item for item in scored
            if _tokens(item[3].get("nome") or "")[0] == lot_first
            or _coarse_equal(_tokens(item[3].get("nome") or "")[0], lot_first)
        ]
        pool = anchored or scored
        pool.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [(candidate, score) for score, _, _, candidate in pool[:limit]]

    @staticmethod
    def pick_year(years: List[Dict[str, Any]], year_model: Optional[int],
                  year_manufacture: Optional[int],
                  max_delta: int = 2) -> Optional[Tuple[Dict[str, Any], int]]:
        if not years:
            return None
        parsed = [(_year_from_name(y.get("nome") or ""), y) for y in years]
        parsed = [(year, y) for year, y in parsed if year]

        targets = [target for target in (year_model, year_manufacture) if target]
        if not targets:
            return None

        for target in targets:
            exact = [pair for pair in parsed if pair[0] == target]
            if exact:
                flex = [pair for pair in exact if "flex" in (pair[1].get("nome") or "").lower()]
                return (flex or exact)[0][1], 0

        best: Optional[Tuple[Dict[str, Any], int]] = None
        for target in targets:
            for year, entry in parsed:
                delta = abs(year - target)
                if delta <= max_delta and (best is None or delta < best[1]):
                    best = (entry, delta)
        return best

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def lookup(self, make: str, model: str, year: Optional[int] = None,
                     year_manufacture: Optional[int] = None) -> Optional[Dict[str, Any]]:
        brand = (make or "").lower().strip()
        if brand not in BRAND_CODES or not model:
            return None

        models = await self.models(brand)
        if not models:
            self.stats["misses"] += 1
            return None

        resolution_key = f"resolved:{brand}:{norm(model)}:{year}:{year_manufacture}"
        resolved = self._get(resolution_key, STRUCTURE_TTL)
        if isinstance(resolved, dict) and resolved.get("model_code") and resolved.get("year_code"):
            payload = await self.value_for_year(brand, resolved["model_code"], str(resolved["year_code"]))
            value = parse_money((payload or {}).get("Valor"))
            if value:
                self.stats["cache_hits"] += 1
                return {
                    "value": value,
                    "fipe_model": payload.get("Modelo") or resolved.get("fipe_model"),
                    "fipe_year": payload.get("AnoModelo") or resolved.get("fipe_year"),
                    "fipe_code": payload.get("CodigoFipe"),
                    "reference": payload.get("MesReferencia"),
                    "fuel": payload.get("Combustivel"),
                    "match_score": resolved.get("match_score"),
                    "model_code": resolved["model_code"],
                    "year_code": resolved["year_code"],
                }

        # Ambiguous descriptions ("gol", "palio") match dozens of FIPE trims, so
        # candidate probing is tiered: the closest few models resolve most lots,
        # and wider sweeps only run when nothing was found.
        token_count = len(_tokens(model))
        tiers = (4, 12, 48) if token_count <= 2 else (3, 8, 24)
        ranked = self.ranked_models(model, models, year, year_manufacture, limit=tiers[0])
        if not ranked:
            self.stats["misses"] += 1
            self._mark_negative(resolution_key)
            return None

        async def probe(candidates: List[Tuple[Dict[str, Any], float]]):
            fallback_local = None
            for start in range(0, len(candidates), 6):
                chunk = candidates[start:start + 6]
                years_lists = await asyncio.gather(
                    *(self.years(brand, candidate.get("codigo")) for candidate, _ in chunk)
                )
                eligible: List[Tuple[int, float, int, int, Dict[str, Any], Dict[str, Any], float]] = []
                for (candidate, score), years in zip(chunk, years_lists):
                    if years is None:
                        continue  # request failed: unknown, not "no years"
                    picked = self.pick_year(years, year, year_manufacture)
                    if not picked:
                        continue
                    picked_year, delta = picked
                    eligible.append((delta, -score, len(_tokens(candidate.get("nome") or "")),
                                     len(candidate.get("nome") or ""), candidate, picked_year, score))
                if eligible:
                    eligible.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
                    if eligible[0][0] == 0:
                        return (eligible[0][4], eligible[0][5], eligible[0][6]), fallback_local
                    if fallback_local is None or eligible[0][:4] < fallback_local[:4]:
                        fallback_local = eligible[0]
            return None, fallback_local

        chosen = None
        best_fallback = None
        probed = 0
        for tier in tiers:
            if tier <= probed:
                continue
            candidates = self.ranked_models(model, models, year, year_manufacture, limit=tier)
            fresh = candidates[probed:]
            probed = tier
            if not fresh:
                continue
            chosen, tier_fallback = await probe(fresh)
            if tier_fallback is not None and (best_fallback is None or tier_fallback[0] < best_fallback[0]):
                best_fallback = tier_fallback
            if chosen is not None:
                break
            if best_fallback is not None:
                break  # exact-year candidates exhausted; a near year is good enough

        if chosen is None and best_fallback is not None:
            chosen = (best_fallback[4], best_fallback[5], best_fallback[6])

        if not chosen:
            self.stats["misses"] += 1
            self._mark_negative(resolution_key)
            return None

        candidate, picked_year, score = chosen
        payload = await self.value_for_year(
            brand, candidate.get("codigo"), str(picked_year.get("codigo"))
        )
        if not payload:
            self.stats["misses"] += 1
            return None
        value = parse_money(payload.get("Valor"))
        if not value:
            self.stats["misses"] += 1
            return None

        result = {
            "value": value,
            "fipe_model": payload.get("Modelo") or candidate.get("nome"),
            "fipe_year": payload.get("AnoModelo") or _year_from_name(picked_year.get("nome") or ""),
            "fipe_code": payload.get("CodigoFipe"),
            "reference": payload.get("MesReferencia"),
            "fuel": payload.get("Combustivel"),
            "match_score": round(score, 3),
            "model_code": candidate.get("codigo"),
            "year_code": picked_year.get("codigo"),
        }
        self._set(resolution_key, {
            "model_code": result["model_code"],
            "year_code": result["year_code"],
            "fipe_model": result["fipe_model"],
            "fipe_year": result["fipe_year"],
            "match_score": result["match_score"],
        })
        await self._save()
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._save()


fipe = Fipe()
