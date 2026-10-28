"""Scan orchestration: scrape -> filter -> value against FIPE -> score -> store.

Every number in the output comes from a live scrape.  There is no demo/seed
data anywhere in this module: if a source cannot be read the response says so
and the source contributes zero lots.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .db import db
from .fipe import fipe
from .margin import calc
from .scrapers import SCRAPERS
from .scrapers.base import norm, parse_datetime
from . import whatsapp
from .bridge import bridge

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

CONDITION_ALIASES = {
    "pequena monta": "pequena monta",
    "nao aplicavel": "não aplicável",
    "não aplicável": "não aplicável",
    "recuperado de financiamento": "recuperado de financiamento",
    "financiamento": "recuperado de financiamento",
    "recuperado": "recuperado de financiamento",
}


def declared_cost_override(lot: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Costs published by the auctioneer for this specific lot.

    When a source states its own fee schedule (Sodré prints commission, deposit
    and logistics on every lot) those numbers replace the generic assumptions.
    The flat charges are passed separately as ``fixed_fees``; here we only
    override the percentage and drop the generic "despachante" estimate, which
    the published logistics fee already covers.
    """
    fee_percent = lot.get("fee_percent")
    if fee_percent is None:
        return None
    try:
        fee_percent = float(fee_percent)
    except (TypeError, ValueError):
        return None
    return {"auction_fee_percent": fee_percent, "despachante_brl": 0.0}


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _canonical_condition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = norm(value)
    for alias, canonical in CONDITION_ALIASES.items():
        if norm(alias) == key:
            return canonical
    if "pequena" in key:
        return "pequena monta"
    if "aplic" in key:
        return "não aplicável"
    if "financ" in key or "recuperad" in key:
        return "recuperado de financiamento"
    return value


class LeilaoRunner:
    def __init__(self) -> None:
        self.config: Dict[str, Any] = self._load_config()
        self._scan_lock = asyncio.Lock()
        calc.configure(self.config)

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    @staticmethod
    def _load_config() -> Dict[str, Any]:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def get_config(self) -> Dict[str, Any]:
        return copy.deepcopy(self.config)

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = _deep_merge(self.config, patch or {})

        # sanitise numeric cost fields
        costs = merged.setdefault("costs", {})
        for key in ("auction_fee_percent", "despachante_brl", "transfer_brl", "tow_brl",
                    "repairs_estimate_brl", "imprevistos_percent"):
            if key in costs:
                try:
                    costs[key] = max(0.0, float(costs[key]))
                except (TypeError, ValueError):
                    costs[key] = self.config["costs"].get(key, 0)
        if "auction_fee_percent" in costs:
            costs["auction_fee_percent"] = min(100.0, costs["auction_fee_percent"])
        if "imprevistos_percent" in costs:
            costs["imprevistos_percent"] = min(100.0, costs["imprevistos_percent"])

        valuation = merged.setdefault("valuation", {})
        try:
            minimum = float(valuation.get("min_margin_percent", 30))
            maximum = float(valuation.get("max_margin_percent", 60))
        except (TypeError, ValueError):
            minimum, maximum = 30.0, 60.0
        if maximum <= minimum:
            maximum = minimum + 1
        valuation["min_margin_percent"] = minimum
        valuation["max_margin_percent"] = maximum

        filters = merged.setdefault("filters", {})
        conditions = filters.get("conditions") or []
        filters["conditions"] = [c for c in (_canonical_condition(c) for c in conditions) if c]
        filters["makes"] = [str(m).lower() for m in (filters.get("makes") or [])]
        try:
            if filters.get("min_bid_price_brl") is not None:
                filters["min_bid_price_brl"] = max(0.0, float(filters["min_bid_price_brl"]))
            else:
                filters["min_bid_price_brl"] = 0.0
        except (TypeError, ValueError):
            filters["min_bid_price_brl"] = self.config["filters"].get("min_bid_price_brl") or 0.0
        try:
            if filters.get("max_bid_price_brl") is not None:
                filters["max_bid_price_brl"] = float(filters["max_bid_price_brl"])
        except (TypeError, ValueError):
            filters["max_bid_price_brl"] = self.config["filters"].get("max_bid_price_brl")

        sites = [s for s in (merged.get("sites") or []) if s in SCRAPERS]
        merged["sites"] = sites or list(self.config.get("sites") or [])

        self.config = merged
        calc.configure(self.config)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle, ensure_ascii=False, indent=2)
        return self.get_config()

    # ------------------------------------------------------------------
    # scanning
    # ------------------------------------------------------------------
    async def search_all(self) -> Dict[str, Any]:
        async with self._scan_lock:
            return await self._search_all()

    async def _search_all(self) -> Dict[str, Any]:
        started = time.time()
        config = self.config
        sites = [s for s in (config.get("sites") or []) if s in SCRAPERS]
        labels = {key: getattr(scraper, "label", key) for key, scraper in SCRAPERS.items()}

        async def run_site(site: str):
            scraper = SCRAPERS[site]
            site_started = time.time()
            try:
                result = await scraper.search(config)
            except Exception as exc:  # never let one site kill the scan
                return site, None, f"{type(exc).__name__}: {exc}", time.time() - site_started
            return site, result, "", time.time() - site_started

        raw = await asyncio.gather(*(run_site(site) for site in sites))

        report_sources: List[Dict[str, Any]] = []
        candidates: Dict[str, Dict[str, Any]] = {}
        counters = {
            "scraped": 0, "duplicates": 0, "expired": 0, "bad_condition": 0,
            "bad_make": 0, "bad_price": 0, "bad_year": 0, "bad_km": 0,
        }

        filters = config.get("filters") or {}
        allowed_makes = {str(m).lower() for m in (filters.get("makes") or [])}
        allowed_conditions = {_canonical_condition(c) for c in (filters.get("conditions") or [])}
        allowed_conditions.discard(None)
        max_bid = filters.get("max_bid_price_brl")
        min_year = filters.get("min_year")
        max_km = filters.get("max_km")
        exclude_expired = bool(filters.get("exclude_expired", True))
        now = datetime.now()

        for site, result, error, elapsed in raw:
            entry: Dict[str, Any] = {
                "site": site,
                "label": labels.get(site, site),
                "status": "ok",
                "found": 0,
                "kept": 0,
                "valued": 0,
                "viable": 0,
                "note": "",
                "error": "",
                "seconds": round(elapsed, 1),
                "stats": {},
            }
            if error:
                entry["status"] = "error"
                entry["error"] = error
                report_sources.append(entry)
                continue
            if result is None:
                entry["status"] = "error"
                entry["error"] = "No result returned."
                report_sources.append(entry)
                continue

            entry["note"] = result.note or ""
            entry["stats"] = result.stats or {}
            entry["found"] = len(result.lots)
            counters["scraped"] += len(result.lots)

            kept_ids: List[str] = []
            for lot in result.lots:
                lot["condition"] = _canonical_condition(lot.get("condition"))
                lot["make"] = (lot.get("make") or "").lower()
                lot_id = lot.get("id")
                if not lot_id or not lot.get("bid_price"):
                    continue
                if lot_id in candidates:
                    counters["duplicates"] += 1
                    continue

                end_dt = parse_datetime(lot.get("end_date"))
                if exclude_expired and end_dt and end_dt < now:
                    counters["expired"] += 1
                    continue

                if allowed_makes and lot["make"] not in allowed_makes:
                    counters["bad_make"] += 1
                    continue
                if allowed_conditions and lot["condition"] not in allowed_conditions:
                    counters["bad_condition"] += 1
                    continue
                if max_bid and float(lot["bid_price"]) > float(max_bid):
                    counters["bad_price"] += 1
                    continue
                if min_year:
                    year = lot.get("year") or lot.get("year_manufacture") or 0
                    if year and int(year) < int(min_year):
                        counters["bad_year"] += 1
                        continue
                if max_km and lot.get("km") and int(lot["km"]) > int(max_km):
                    counters["bad_km"] += 1
                    continue

                lot["is_new"] = not db.is_seen(lot_id)
                lot["is_expired"] = False
                candidates[lot_id] = lot
                kept_ids.append(lot_id)

            entry["kept"] = len(kept_ids)
            if entry["found"] == 0 and not entry["error"]:
                hint = (entry["stats"] or {}).get("status")
                if hint in ("blocked", "limited", "empty"):
                    entry["status"] = hint
                else:
                    entry["status"] = "blocked" if "captcha" in (entry["note"] or "").lower() else "empty"
            report_sources.append(entry)

        # Valuation (bounded).
        valuation_config = config.get("valuation") or {}
        cap = int(valuation_config.get("max_lots_to_value", 2000) or 2000)
        to_value = sorted(
            candidates.values(),
            key=lambda lot: (
                0 if lot.get("condition_source") != "source" else 1,
                -(int(lot.get("year") or lot.get("year_manufacture") or 0)),
                float(lot.get("bid_price") or 0),
            ),
        )[:cap]
        to_value_ids = {lot["id"] for lot in to_value}
        skipped_valuation = len(candidates) - len(to_value)

        # Some sources publish the FIPE value for the exact lot (Copart prints
        # "Valor FIPE" on every lot page).  Those lots are valued directly and
        # never consume an official-FIPE lookup; the rest are grouped by
        # model/year so the API is queried once per distinct vehicle.
        declared_count = 0
        need_lookup: List[Dict[str, Any]] = []
        for lot in to_value:
            declared = lot.get("declared_fipe_price")
            try:
                value = float(declared) if declared else 0.0
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                lot["fipe_price"] = round(value, 2)
                lot["sale_estimate"] = lot["fipe_price"]
                lot["valuation_model"] = lot.get("model") or None
                lot["valuation_year"] = lot.get("year") or lot.get("year_manufacture")
                lot["fipe_reference"] = "source-published"
                declared_count += 1
            else:
                need_lookup.append(lot)

        # Many municipal/fleet lots share the exact same model and year, so the
        # FIPE lookup is performed once per (make, model, year) group.
        groups: Dict[Tuple[str, str, Any, Any], List[Dict[str, Any]]] = {}
        for lot in need_lookup:
            key = (
                lot.get("make") or "",
                norm(lot.get("model") or lot.get("title") or ""),
                lot.get("year"),
                lot.get("year_manufacture"),
            )
            groups.setdefault(key, []).append(lot)

        async def value_group(key: Tuple[str, str, Any, Any], group: List[Dict[str, Any]]) -> None:
            make, model, year, year_manufacture = key
            fipe_row = None
            if model:
                try:
                    fipe_row = await fipe.lookup(make, model, year, year_manufacture)
                except Exception:
                    fipe_row = None
            for lot in group:
                if fipe_row:
                    lot["fipe_price"] = round(float(fipe_row["value"]), 2)
                    lot["sale_estimate"] = lot["fipe_price"]
                    lot["valuation_model"] = fipe_row.get("fipe_model")
                    lot["valuation_year"] = fipe_row.get("fipe_year")
                    lot["fipe_reference"] = fipe_row.get("reference")
                else:
                    lot["fipe_price"] = None
                    lot["sale_estimate"] = None
                    lot["valuation_model"] = None
                    lot["valuation_year"] = None
                    lot["fipe_reference"] = None

        await asyncio.gather(*(value_group(key, group) for key, group in groups.items()))

        valued = 0
        viable = 0
        saved_ids_by_site: Dict[str, List[str]] = {}

        for lot in candidates.values():
            if lot.get("fipe_price"):
                valued += 1
            calc_result = calc.calculate(
                float(lot.get("bid_price") or 0),
                lot.get("sale_estimate"),
                costs_override=declared_cost_override(lot),
                fixed_fees=float(lot.get("fixed_fees") or 0),
            )
            lot.update(calc_result)
            if lot.get("fipe_price"):
                lot["price_vs_fipe"] = round(float(lot["bid_price"]) / float(lot["fipe_price"]) * 100.0, 1)
            else:
                lot["price_vs_fipe"] = None
            if calc_result["is_worth_it"]:
                viable += 1
            lot.pop("note", None)
            db.save_lot(lot)
            saved_ids_by_site.setdefault(lot["site"], []).append(lot["id"])
            if lot.get("is_new"):
                db.mark_seen(lot["id"])

        # Expire lots that disappeared from sources that completed cleanly.
        for entry in report_sources:
            if entry["status"] in ("ok", "empty") and not entry["error"]:
                db.expire_missing(entry["site"], saved_ids_by_site.get(entry["site"], []))

        db.expire_by_date(now)

        for entry in report_sources:
            site = entry["site"]
            site_lots = [lot for lot in candidates.values() if lot["site"] == site]
            entry["valued"] = sum(1 for lot in site_lots if lot.get("fipe_price"))
            entry["viable"] = sum(1 for lot in site_lots if lot.get("is_worth_it"))
            if entry["status"] == "ok" and entry["kept"] == 0:
                entry["status"] = "empty"

        finished = time.time()
        report = {
            "started_at": datetime.fromtimestamp(started).isoformat(sep=" ", timespec="seconds"),
            "finished_at": datetime.fromtimestamp(finished).isoformat(sep=" ", timespec="seconds"),
            "duration_seconds": round(finished - started, 1),
            "scraped": counters["scraped"],
            "matched": len(candidates),
            "valued": valued,
            "viable": viable,
            "skipped_valuation_cap": skipped_valuation,
            "declared_fipe": declared_count,
            "counters": counters,
            "sources": report_sources,
            "fipe_stats": dict(fipe.stats),
            "config": {
                "max_bid_price_brl": max_bid,
                "min_margin_percent": valuation_config.get("min_margin_percent"),
                "max_margin_percent": valuation_config.get("max_margin_percent"),
            },
        }

        try:
            await fipe.close()
        except Exception:
            pass

        db.set_meta("last_report", report)
        return {**report, "lots": self.get_lots()}

    # ------------------------------------------------------------------
    # recalculation (cost edits without re-scraping)
    # ------------------------------------------------------------------
    def recalc(self) -> Dict[str, Any]:
        lots = db.get_lots(only_active=False)
        valued = 0
        viable = 0
        for lot in lots:
            sale = lot.get("fipe_price") or lot.get("sale_estimate")
            result = calc.calculate(
                float(lot.get("bid_price") or 0),
                sale,
                costs_override=declared_cost_override(lot),
                fixed_fees=float(lot.get("fixed_fees") or 0),
            )
            lot.update(result)
            if sale:
                valued += 1
            if result["is_worth_it"]:
                viable += 1
            db.save_lot(lot)
        db.expire_by_date()
        summary = {
            "recalculated": len(lots),
            "valued": valued,
            "viable": viable,
            "at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "costs": self.config.get("costs"),
            "valuation": self.config.get("valuation"),
        }
        db.set_meta("last_recalc", summary)
        return summary

    # ------------------------------------------------------------------
    # WhatsApp digest
    # ------------------------------------------------------------------
    def whatsapp_settings(self) -> Dict[str, Any]:
        valuation = self.config.get("valuation") or {}
        default_margin = float(valuation.get("min_margin_percent") or 0)
        return whatsapp.settings(self.config, default_margin=default_margin)

    def whatsapp_preview(self) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        eligible = whatsapp.select_deals(db.get_lots(only_active=True, only_worth_it=True), cfg, cap=False)
        fresh = [lot for lot in eligible if not db.was_alerted(lot["id"], "whatsapp")]
        deals = fresh[: max(1, int(cfg.get("max_deals") or 8))]
        return {
            "provider": cfg.get("provider"),
            "mode": cfg.get("mode"),
            "deals": len(deals),
            "images": sum(1 for lot in deals if lot.get("image_url")),
            "already_sent": len(eligible) - len(fresh),
            "missing": whatsapp.missing_settings(cfg),
            "messages": whatsapp.build_messages(deals, cfg) if deals else [],
            "parts": whatsapp.build_parts(deals, cfg) if deals else [],
            "settings": whatsapp.public_settings(cfg),
        }

    async def whatsapp_digest(self, force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        missing = whatsapp.missing_settings(cfg)
        eligible = whatsapp.select_deals(db.get_lots(only_active=True, only_worth_it=True), cfg, cap=False)
        fresh = [lot for lot in eligible if force or not db.was_alerted(lot["id"], "whatsapp")]
        deals = fresh[: max(1, int(cfg.get("max_deals") or 8))]

        summary: Dict[str, Any] = {
            "provider": cfg.get("provider"),
            "phone": cfg.get("phone"),
            "mode": cfg.get("mode"),
            "deals": len(deals),
            "images": sum(1 for lot in deals if lot.get("image_url")),
            "already_sent": len(eligible) - len(fresh),
        }
        if dry_run:
            return {
                **summary,
                "dry_run": True,
                "missing": missing,
                "messages": whatsapp.build_messages(deals, cfg) if deals else [],
                "parts": whatsapp.build_parts(deals, cfg) if deals else [],
            }
        if missing:
            return {**summary, "sent": False, "error": "missing settings: " + ", ".join(missing)}
        if not deals and not cfg.get("notify_on_empty"):
            return {**summary, "sent": False, "skipped": "no new deals for today"}

        bridge_error = await self._prepare_bridge(cfg)
        if bridge_error:
            payload = {**summary, "sent": False, "error": bridge_error,
                       "at": datetime.now().isoformat(sep=" ", timespec="seconds")}
            db.set_meta("last_whatsapp", payload)
            return payload

        if cfg.get("provider") == "bridge":
            state = await bridge.status()
            if not state.get("connected"):
                payload = {
                    **summary,
                    "sent": False,
                    "error": "not linked - open the WhatsApp panel and scan the QR code",
                    "at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                }
                db.set_meta("last_whatsapp", payload)
                return payload

        result = await whatsapp.deliver_digest(deals, cfg)
        if not result.get("sent") and not result.get("error"):
            failures = [
                str(item.get("detail"))
                for item in (result.get("results") or [])
                if not item.get("ok") and item.get("detail")
            ]
            result["error"] = "; ".join(failures) or "delivery failed"
        if result.get("sent"):
            for lot in deals:
                db.record_alert(lot["id"], "whatsapp")
        payload = {
            **summary,
            **result,
            "at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
        db.set_meta("last_whatsapp", payload)
        return payload

    async def _prepare_bridge(self, cfg: Dict[str, Any]) -> Optional[str]:
        """Start the local WhatsApp bridge when it is the chosen provider."""
        if cfg.get("provider") != "bridge":
            return None
        bridge.configure(cfg.get("bridge_port"))
        ok, detail = await bridge.ensure_running()
        if ok:
            return None
        return f"WhatsApp bridge not ready: {detail}"

    async def whatsapp_test(self) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        target = whatsapp.delivery_target(cfg)
        text = (
            "*Auction Deal Scanner*\n"
            f"WhatsApp delivery test — {datetime.now():%d/%m/%Y %H:%M}.\n"
            "If you can read this, the digest will reach you here."
        )
        bridge_error = await self._prepare_bridge(cfg)
        if bridge_error:
            return {
                "sent": False,
                "detail": bridge_error,
                "provider": cfg.get("provider"),
                "phone": cfg.get("phone"),
                "recipient": target,
                "missing": whatsapp.missing_settings(cfg),
            }
        ok, detail = await whatsapp.send_text(text, cfg)
        return {
            "sent": ok,
            "detail": detail,
            "provider": cfg.get("provider"),
            "phone": cfg.get("phone"),
            "recipient": target,
            "missing": whatsapp.missing_settings(cfg),
        }

    # ------------------------------------------------------------------
    # WhatsApp bridge (QR linking)
    # ------------------------------------------------------------------
    async def bridge_start(self) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        bridge.configure(cfg.get("bridge_port"))
        ok, detail = await bridge.ensure_running()
        status = await bridge.status()
        return {
            "ok": ok,
            "detail": detail,
            "installed": bridge.installed(),
            "node": bridge.node_available(),
            "status": status,
        }

    async def bridge_status(self) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        bridge.configure(cfg.get("bridge_port"))
        status = await bridge.status()
        status["installed"] = bridge.installed()
        status["node"] = bridge.node_available()
        return status

    async def bridge_logout(self) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        bridge.configure(cfg.get("bridge_port"))
        ok, detail = await bridge.ensure_running()
        if not ok:
            return {"ok": False, "error": detail}
        result = await bridge.logout()
        return {**result, "status": await bridge.status()}

    async def bridge_pair(self, phone: str) -> Dict[str, Any]:
        cfg = self.whatsapp_settings()
        bridge.configure(cfg.get("bridge_port"))
        ok, detail = await bridge.ensure_running()
        if not ok:
            return {"ok": False, "error": detail}
        result = await bridge.pair(phone)
        return {**result, "status": await bridge.status()}

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get_lots(self, **filters: Any) -> List[Dict[str, Any]]:
        return db.get_lots(**filters)

    def get_last_report(self) -> Optional[Dict[str, Any]]:
        return db.get_meta("last_report")

    def get_status(self) -> Dict[str, Any]:
        report = self.get_last_report()
        cfg = self.whatsapp_settings()
        return {
            "config": self.get_config(),
            "last_report": report,
            "labels": {key: getattr(s, "label", key) for key, s in SCRAPERS.items()},
            "available_sites": list(SCRAPERS.keys()),
            "whatsapp": {
                "settings": whatsapp.public_settings(cfg),
                "configured": not whatsapp.missing_settings(cfg),
                "missing": whatsapp.missing_settings(cfg),
                "last": db.get_meta("last_whatsapp"),
                "sent_today": db.alerts_for_day(datetime.now().strftime("%Y-%m-%d")),
                "bridge": {
                    "installed": bridge.installed(),
                    "node": bridge.node_available(),
                    "port": cfg.get("bridge_port"),
                },
            },
        }


runner = LeilaoRunner()
