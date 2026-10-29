"""Daily WhatsApp digest scheduler.

A small asyncio loop, no extra dependency.  Every ``interval`` seconds it checks
whether the configured local send time has passed and today's digest has not
been sent yet, optionally runs a fresh scan first, then sends.

The slot is claimed in the ``meta`` table (``last_digest_date``) so a restart
after the send does not repeat the message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from .db import db

logger = logging.getLogger("leilao.scheduler")
_running = False


def _settings(runner) -> Dict[str, Any]:
    try:
        return runner.whatsapp_settings()
    except Exception:
        return {}


async def tick(runner, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Run one scheduler check.  Returns the digest result when one was sent."""
    global _running
    cfg = _settings(runner)
    if not cfg.get("enabled") or _running:
        return None

    now = now or datetime.now()
    try:
        hour, minute = (int(part) for part in str(cfg.get("daily_time", "18:00")).split(":")[:2])
    except (TypeError, ValueError):
        hour, minute = 18, 0

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return None
    if db.get_meta("last_digest_date") == now.strftime("%Y-%m-%d"):
        return None

    _running = True
    try:
        if cfg.get("scan_before_digest"):
            logger.info("daily digest: scanning before send")
            await runner.search_all()
        logger.info(
            "daily digest: sending (slot %s, target %s)",
            now.strftime("%Y-%m-%d %H:%M"),
            cfg.get("bridge_recipient") or "Message yourself",
        )
        result = await runner.whatsapp_digest()
        # Claim the day whatever the outcome: an error should not make the
        # scheduler retry every 30 seconds.  The button in the UI can resend.
        db.set_meta("last_digest_date", now.strftime("%Y-%m-%d"))
        db.set_meta("last_digest_result", result)
        logger.info(
            "daily digest: sent=%s deals=%s skipped=%s error=%s",
            result.get("sent"),
            result.get("deals"),
            result.get("skipped"),
            result.get("error"),
        )
        return result
    finally:
        _running = False


async def loop(runner, interval: float = 30.0) -> None:
    while True:
        try:
            await tick(runner)
        except asyncio.CancelledError:
            raise
        except Exception:  # a scheduler failure must never kill the server
            logger.exception("daily digest tick failed")
        await asyncio.sleep(interval)
