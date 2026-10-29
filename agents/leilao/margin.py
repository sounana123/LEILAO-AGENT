"""Deal math.

ROI is measured against the *total* money committed (bid + every cost), which
is the number that matters when comparing opportunities:

    total_investment = bid + auction fee + fees + repairs + contingency
    net_profit       = resale estimate - total_investment
    roi_percent      = net_profit / total_investment * 100

A lot is flagged as viable when ROI >= the configured minimum.  Lots above the
configured maximum are still viable; they just carry an "above target" flag.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULT_COSTS: Dict[str, float] = {
    "auction_fee_percent": 10.0,
    "despachante_brl": 800.0,
    "transfer_brl": 350.0,
    "tow_brl": 400.0,
    "repairs_estimate_brl": 1500.0,
    "imprevistos_percent": 10.0,
}


class MarginCalculator:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config if config is not None else self._load()

    @staticmethod
    def _load() -> Dict[str, Any]:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {"costs": dict(DEFAULT_COSTS),
                    "valuation": {"min_margin_percent": 30, "max_margin_percent": 60}}

    def configure(self, config: Dict[str, Any]) -> None:
        self.config = config

    def calculate(self, bid_price: float, sale_price: Optional[float],
                  costs_override: Optional[Dict[str, float]] = None,
                  fixed_fees: float = 0.0) -> Dict[str, Any]:
        costs = dict(DEFAULT_COSTS)
        costs.update(self.config.get("costs") or {})
        if costs_override:
            costs.update({k: v for k, v in costs_override.items() if v is not None})

        valuation = self.config.get("valuation") or {}
        min_margin = float(valuation.get("min_margin_percent", 30) or 0)
        max_margin = float(valuation.get("max_margin_percent", 60) or 100)

        bid_price = float(bid_price or 0)
        sale_price = float(sale_price or 0)

        # Auctioneers that publish their own fee schedule (Sodré, for example)
        # add a percentage plus flat charges (deposit, logistics).  Those flat
        # charges join the auction fee so the breakdown stays comparable.
        auction_fee = bid_price * (float(costs.get("auction_fee_percent") or 0) / 100.0)
        auction_fee += float(fixed_fees or 0)
        despachante = float(costs.get("despachante_brl") or 0)
        transfer = float(costs.get("transfer_brl") or 0)
        tow = float(costs.get("tow_brl") or 0)
        repairs = float(costs.get("repairs_estimate_brl") or 0)
        buffer = sale_price * (float(costs.get("imprevistos_percent") or 0) / 100.0)

        total_costs = auction_fee + despachante + transfer + tow + repairs + buffer
        total_investment = bid_price + total_costs
        net_profit = sale_price - total_investment
        roi_percent = (net_profit / total_investment * 100.0) if total_investment > 0 else 0.0

        has_valuation = sale_price > 0
        is_worth_it = bool(has_valuation and roi_percent >= min_margin)
        above_target = bool(has_valuation and roi_percent > max_margin)

        # Highest bid that still clears the minimum margin.  Current bids rise
        # during the auction, so this is the number a buyer should actually bid
        # against rather than the ROI computed on today's price.
        max_bid = 0.0
        if has_valuation:
            percent = float(costs.get("auction_fee_percent") or 0) / 100.0
            flat = float(fixed_fees or 0) + despachante + transfer + tow + repairs + buffer
            margin = min_margin / 100.0
            denominator = (1.0 + percent) * (1.0 + margin)
            if denominator > 0:
                max_bid = (sale_price - flat * (1.0 + margin)) / denominator
            max_bid = max(0.0, max_bid)

        return {
            "auction_fee": round(auction_fee, 2),
            "despachante": round(despachante, 2),
            "transfer": round(transfer, 2),
            "tow": round(tow, 2),
            "repairs": round(repairs, 2),
            "buffer": round(buffer, 2),
            "total_costs": round(total_costs, 2),
            "total_investment": round(total_investment, 2),
            "net_profit": round(net_profit, 2),
            "margin_percent": round(roi_percent, 2),
            "roi_percent": round(roi_percent, 2),
            "max_bid": round(max_bid, 2),
            "is_worth_it": is_worth_it,
            "above_target": above_target,
            "has_valuation": has_valuation,
            "min_margin": min_margin,
            "max_margin": max_margin,
        }


calc = MarginCalculator()
