"""SQLite persistence for auction lots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "leiloes.db"

# Columns added after the first schema version.  Applied with ALTER TABLE so an
# existing database keeps working.
MIGRATIONS: Dict[str, str] = {
    "end_date": "TEXT",
    "fee_percent": "REAL",
    "fixed_fees": "REAL",
    "max_bid": "REAL",
    "condition_source": "TEXT",
    "year_manufacture": "INTEGER",
    "valuation_model": "TEXT",
    "valuation_year": "INTEGER",
    "fipe_reference": "TEXT",
    "seller": "TEXT",
    "site_label": "TEXT",
    "is_new": "INTEGER DEFAULT 0",
    "external_id": "TEXT",
    "price_vs_fipe": "REAL",
    "image_url": "TEXT",
}


def _as_text(value: Any) -> Optional[str]:
    """Normalise datetimes coming from scrapers into a sortable ISO string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


class LeilaoDB:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lots (
                id TEXT PRIMARY KEY,
                site TEXT,
                url TEXT,
                title TEXT,
                make TEXT,
                model TEXT,
                year INTEGER,
                km INTEGER,
                state TEXT,
                city TEXT,
                condition TEXT,
                bid_price REAL,
                fipe_price REAL,
                market_price REAL,
                sale_estimate REAL,
                auction_fee REAL,
                despachante REAL,
                transfer REAL,
                tow REAL,
                repairs REAL,
                buffer REAL,
                total_costs REAL,
                net_profit REAL,
                margin_percent REAL,
                status TEXT,
                is_expired INTEGER DEFAULT 0,
                is_worth_it INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                raw_data TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_lots (
                lot_id TEXT PRIMARY KEY,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                channel TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        existing = {row["name"] for row in cur.execute("PRAGMA table_info(lots)").fetchall()}
        for column, ddl in MIGRATIONS.items():
            if column not in existing:
                cur.execute(f"ALTER TABLE lots ADD COLUMN {column} {ddl}")
        self.conn.commit()

    # ------------------------------------------------------------------
    def is_seen(self, lot_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM seen_lots WHERE lot_id = ?", (lot_id,))
        return cur.fetchone() is not None

    def mark_seen(self, lot_id: str) -> None:
        cur = self.conn.cursor()
        cur.execute("INSERT OR IGNORE INTO seen_lots (lot_id) VALUES (?)", (lot_id,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # alert delivery
    # ------------------------------------------------------------------
    def was_alerted(self, lot_id: str, channel: str = "whatsapp") -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM alerts WHERE lot_id = ? AND channel = ? LIMIT 1",
            (lot_id, channel),
        )
        return cur.fetchone() is not None

    def record_alert(self, lot_id: str, channel: str = "whatsapp") -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO alerts (lot_id, channel) VALUES (?, ?)",
            (lot_id, channel),
        )
        self.conn.commit()

    def alerts_for_day(self, day: str, channel: str = "whatsapp") -> int:
        """Number of lots already alerted on ``day`` (YYYY-MM-DD, local time)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM alerts WHERE channel = ? AND date(sent_at, 'localtime') = ?",
            (channel, day),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def save_lot(self, lot: Dict[str, Any]) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO lots (
                id, site, site_label, external_id, url, title, make, model, year,
                year_manufacture, km, state, city, condition, condition_source,
                bid_price, fipe_price, price_vs_fipe, market_price, sale_estimate,
                valuation_model, valuation_year, fipe_reference, seller,
                auction_fee, despachante, transfer, tow, repairs, buffer,
                total_costs, net_profit, margin_percent, status,
                is_expired, is_worth_it, is_new, last_seen, end_date,
                fee_percent, fixed_fees, max_bid, image_url, raw_data
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
                site=excluded.site,
                site_label=excluded.site_label,
                external_id=excluded.external_id,
                url=excluded.url,
                title=excluded.title,
                make=excluded.make,
                model=excluded.model,
                year=excluded.year,
                year_manufacture=excluded.year_manufacture,
                km=excluded.km,
                state=excluded.state,
                city=excluded.city,
                condition=excluded.condition,
                condition_source=excluded.condition_source,
                bid_price=excluded.bid_price,
                fipe_price=excluded.fipe_price,
                price_vs_fipe=excluded.price_vs_fipe,
                market_price=excluded.market_price,
                sale_estimate=excluded.sale_estimate,
                valuation_model=excluded.valuation_model,
                valuation_year=excluded.valuation_year,
                fipe_reference=excluded.fipe_reference,
                seller=excluded.seller,
                auction_fee=excluded.auction_fee,
                despachante=excluded.despachante,
                transfer=excluded.transfer,
                tow=excluded.tow,
                repairs=excluded.repairs,
                buffer=excluded.buffer,
                total_costs=excluded.total_costs,
                net_profit=excluded.net_profit,
                margin_percent=excluded.margin_percent,
                status=excluded.status,
                is_expired=excluded.is_expired,
                is_worth_it=excluded.is_worth_it,
                is_new=excluded.is_new,
                last_seen=excluded.last_seen,
                end_date=excluded.end_date,
                fee_percent=excluded.fee_percent,
                fixed_fees=excluded.fixed_fees,
                max_bid=excluded.max_bid,
                image_url=excluded.image_url,
                raw_data=excluded.raw_data
            """,
            (
                lot.get("id"),
                lot.get("site"),
                lot.get("site_label"),
                lot.get("external_id"),
                lot.get("url"),
                lot.get("title"),
                lot.get("make"),
                lot.get("model"),
                lot.get("year"),
                lot.get("year_manufacture"),
                lot.get("km"),
                lot.get("state"),
                lot.get("city"),
                lot.get("condition"),
                lot.get("condition_source"),
                lot.get("bid_price"),
                lot.get("fipe_price"),
                lot.get("price_vs_fipe"),
                lot.get("market_price"),
                lot.get("sale_estimate"),
                lot.get("valuation_model"),
                lot.get("valuation_year"),
                lot.get("fipe_reference"),
                lot.get("seller"),
                lot.get("auction_fee"),
                lot.get("despachante"),
                lot.get("transfer"),
                lot.get("tow"),
                lot.get("repairs"),
                lot.get("buffer"),
                lot.get("total_costs"),
                lot.get("net_profit"),
                lot.get("margin_percent"),
                lot.get("status", "active"),
                1 if lot.get("is_expired") else 0,
                1 if lot.get("is_worth_it") else 0,
                1 if lot.get("is_new") else 0,
                datetime.now().isoformat(sep=" ", timespec="seconds"),
                _as_text(lot.get("end_date")),
                lot.get("fee_percent"),
                lot.get("fixed_fees"),
                lot.get("max_bid"),
                lot.get("image_url"),
                json.dumps(lot.get("raw_data") or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    def get_lots(
        self,
        only_active: bool = True,
        only_worth_it: bool = False,
        min_margin: Optional[float] = None,
        max_margin: Optional[float] = None,
        sites: Optional[Iterable[str]] = None,
        conditions: Optional[Iterable[str]] = None,
        makes: Optional[Iterable[str]] = None,
        min_bid: Optional[float] = None,
        max_bid: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM lots WHERE 1=1"
        params: List[Any] = []
        if only_active:
            query += " AND is_expired = 0"
        if only_worth_it:
            query += " AND is_worth_it = 1"
        if min_margin is not None:
            query += " AND margin_percent >= ?"
            params.append(float(min_margin))
        if max_margin is not None:
            query += " AND margin_percent <= ?"
            params.append(float(max_margin))
        if min_bid is not None:
            query += " AND bid_price >= ?"
            params.append(float(min_bid))
        if max_bid is not None:
            query += " AND bid_price <= ?"
            params.append(float(max_bid))
        for column, values in (("site", sites), ("condition", conditions), ("make", makes)):
            if values:
                values = list(values)
                query += f" AND {column} IN ({','.join('?' for _ in values)})"
                params.extend(values)
        query += " ORDER BY is_worth_it DESC, margin_percent DESC, net_profit DESC"
        cur = self.conn.cursor()
        cur.execute(query, params)
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            try:
                item["raw_data"] = json.loads(item.get("raw_data") or "{}")
            except Exception:
                item["raw_data"] = {}
            rows.append(item)
        return rows

    def get_lot(self, lot_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM lots WHERE id = ?", (lot_id,))
        row = cur.fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["raw_data"] = json.loads(item.get("raw_data") or "{}")
        except Exception:
            item["raw_data"] = {}
        return item

    def mark_expired(self, lot_id: str) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE lots SET is_expired = 1 WHERE id = ?", (lot_id,))
        self.conn.commit()

    def expire_missing(self, site: str, keep_ids: Iterable[str]) -> int:
        keep = [str(value) for value in keep_ids]
        cur = self.conn.cursor()
        if not keep:
            cur.execute("UPDATE lots SET is_expired = 1 WHERE site = ? AND is_expired = 0", (site,))
        else:
            placeholders = ",".join("?" for _ in keep)
            cur.execute(
                f"UPDATE lots SET is_expired = 1 WHERE site = ? AND is_expired = 0 "
                f"AND id NOT IN ({placeholders})",
                [site, *keep],
            )
        self.conn.commit()
        return cur.rowcount

    def expire_by_date(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE lots SET is_expired = 1 "
            "WHERE end_date IS NOT NULL AND end_date != '' AND end_date < ? AND is_expired = 0",
            (now.isoformat(sep=" ", timespec="seconds"),),
        )
        self.conn.commit()
        return cur.rowcount

    def clear_expired(self) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM lots WHERE is_expired = 1")
        self.conn.commit()

    # ------------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False, default=str),
             datetime.now().isoformat(sep=" ", timespec="seconds")),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default


db = LeilaoDB()
