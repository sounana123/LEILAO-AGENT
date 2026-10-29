"""Headless entry points for CI (GitHub Actions).

The web UI keeps the scheduler and the WhatsApp bridge alive; in CI there is no
server at all, so this module does one full pass and exits:

    python -m agents.leilao.ci digest             # scan, value, send, exit
    python -m agents.leilao.ci digest --dry-run   # print the digest, send nothing
    python -m agents.leilao.ci link --phone 5511999998888

State that must survive between runs (``data/leiloes.db``, ``data/fipe_cache.json``
and ``data/whatsapp_auth``) lives in ``data/``; the workflow restores and saves
that directory as a cache.  See ``deploy/actions/README.md``.

Exit codes: 0 sent / nothing new, 1 the scan or the send failed, 2 the bridge
did not start, 3 the pairing code was refused or timed out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .bridge import bridge

logger = logging.getLogger("leilao.ci")
RESULT_PATH = Path("digest-result.json")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Playwright and httpx are chatty at INFO; keep the digest readable.
    for noisy in ("httpx", "asyncio", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _say(text: str = "") -> None:
    print(text, flush=True)


def _identity(state: Dict[str, Any]) -> str:
    me = state.get("me") or {}
    return str(me.get("id") or me.get("name") or "your device")


async def _wait_connected(timeout: float) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = await bridge.status()
        if state.get("connected"):
            return state
        await asyncio.sleep(3.0)
    return None


# --------------------------------------------------------------------- linking
async def link(phone: str, timeout: float = 240.0) -> int:
    bridge.configure(os.environ.get("WHATSAPP_BRIDGE_PORT") or 8099)
    ok, detail = await bridge.ensure_running(wait_seconds=90)
    if not ok:
        logger.error("the bridge did not start: %s", detail)
        return 2

    state = await bridge.status()
    if state.get("connected"):
        _say(f"already linked as {_identity(state)} - nothing to do")
        return 0

    answer = await bridge.pair(phone)
    code = answer.get("code") or answer.get("pairing_code")
    if not answer.get("ok") or not code:
        logger.error("pairing failed: %s", answer.get("error") or answer)
        return 3

    _say("")
    _say("=" * 44)
    _say(f"   PAIRING CODE:  {code}")
    _say("=" * 44)
    _say("")
    _say("On the phone:")
    _say("  WhatsApp -> Settings -> Linked devices -> Link a device")
    _say(f"  -> Link with phone number -> enter {code}")
    _say("")
    _say(f"Waiting up to {int(timeout // 60)} minutes for the phone...")

    state = await _wait_connected(timeout)
    if not state:
        _say("")
        logger.error("timed out - no device was linked. Run this workflow again.")
        return 3

    _say("")
    _say(f"LINKED as {_identity(state)}")
    _say("The session was saved; the daily digest can use it now.")
    return 0


# ---------------------------------------------------------------------- digest
def _scan_line(report: Dict[str, Any]) -> str:
    keys = ("scraped", "matched", "valued", "viable")
    stats = " ".join(f"{key}={report.get(key, 0)}" for key in keys)
    return f"scan   : {stats} in {report.get('duration_seconds', 0)}s"


def _looks_like_blocking(report: Dict[str, Any]) -> bool:
    """True when every source failed, which usually means the IP is blocked."""
    sources = report.get("sources") or []
    return bool(sources) and all(item.get("status") == "error" for item in sources)


async def digest(dry_run: bool = False) -> int:
    from . import whatsapp
    from .db import db  # noqa: F401 - opening it creates data/leiloes.db
    from .runner import runner

    cfg = runner.whatsapp_settings()
    _say(f"time   : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    _say(f"target : {whatsapp.delivery_target(cfg) or 'your own chat'}")
    _say(
        "config : mode={mode} max_deals={max_deals} min_bid={min_bid:.0f} min_margin={margin:.0f}%".format(
            mode=cfg.get("mode"),
            max_deals=cfg.get("max_deals"),
            min_bid=whatsapp.effective_min_bid(cfg),
            margin=float(cfg.get("min_margin_percent") or 0),
        )
    )

    _say("scan   : starting")
    try:
        report = await runner.search_all()
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed run
        logger.exception("the scan crashed: %s: %s", type(exc).__name__, exc)
        return 1

    _say(_scan_line(report))
    for source in report.get("sources") or []:
        note = source.get("error") or source.get("note") or ""
        _say(
            "         {label:14} {status:6} found={found:<4} kept={kept:<4} "
            "viable={viable:<4} {seconds}s {note}".format(
                label=str(source.get("label") or source.get("site"))[:14],
                note=str(note)[:80],
                **{key: source.get(key, 0) for key in ("status", "found", "kept", "viable", "seconds")},
            )
        )

    if not report.get("scraped"):
        logger.error(
            "every source returned zero lots - the sites may be blocking this "
            "runner's IP, or the filters reject everything"
        )
        return 1
    if _looks_like_blocking(report):
        logger.error("all sources errored; see the per-source lines above")
        return 1

    result = await runner.whatsapp_digest(force=False, dry_run=dry_run)
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    if dry_run:
        for part in result.get("parts") or []:
            _say("-" * 44)
            _say(str(part.get("caption") or part.get("text") or ""))
        _say("-" * 44)
        _say("dry run: nothing was sent")
        return 0

    if result.get("sent"):
        _say(
            "send   : ok - {deals} deals, {images} photos, {messages} messages -> {target}".format(
                target=result.get("recipient") or "your own chat",
                **{key: result.get(key, 0) for key in ("deals", "images", "messages")},
            )
        )
        return 0

    if result.get("skipped"):
        _say(f"send   : skipped - {result.get('skipped')}")
        return 0

    error = str(result.get("error") or "unknown error")
    logger.error("send failed: %s", error)
    if "not linked" in error:
        logger.error(
            "the WhatsApp session is missing or was unlinked - run the "
            "'leilao-link' workflow once, then retry"
        )
    return 1


# ------------------------------------------------------------------------ main
async def _run(args: argparse.Namespace) -> int:
    if args.command == "link":
        return await link(args.phone, timeout=args.timeout)
    return await digest(args.dry_run)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agents.leilao.ci")
    sub = parser.add_subparsers(dest="command", required=True)

    p_link = sub.add_parser("link", help="link a WhatsApp device with a pairing code")
    p_link.add_argument(
        "--phone",
        required=True,
        help="international format, digits only (e.g. 5511999998888)",
    )
    p_link.add_argument("--timeout", type=float, default=240.0, help="seconds to wait")

    p_digest = sub.add_parser("digest", help="scan, value and send the digest once")
    p_digest.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    _setup_logging()
    try:
        return asyncio.run(_run(args))
    finally:
        bridge.stop()


if __name__ == "__main__":
    sys.exit(main())
