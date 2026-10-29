"""WhatsApp delivery for the daily deal digest.

Four providers are supported.  Credentials live in ``config.json`` under
``alerts.whatsapp`` and can be overridden with environment variables so they
never have to be written to disk.

    bridge     local WhatsApp Web session (see ``bridge.py``).  You scan a QR
               code once and messages arrive in your own "Message yourself"
               chat.  No account, no approval, no per-message cost.  Unofficial.
    callmebot  free personal notifications.  You send the bot one WhatsApp
               message once, it replies with an API key, and from then on it can
               message you.  No business account, no template.
    meta       WhatsApp Cloud API (graph.facebook.com).  Free-form text only
               works inside a 24-hour customer-service window, so a daily
               business-initiated digest normally needs an approved template.
    twilio     Twilio WhatsApp sender (sandbox or approved sender).  Same 24h
               rule as Meta for free-form text.

Nothing here fakes a send: every result reports the provider response.
"""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .scrapers.base import UA

DEFAULT_PROVIDER = "bridge"
DEFAULT_MAX_CHARS = {"bridge": 3500, "callmebot": 900, "meta": 3400, "twilio": 1500}

# Lot photos are downloaded and forwarded to the local bridge as base64.  Keep a
# ceiling so a broken URL cannot balloon memory or the HTTP request.
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 25.0
IMAGE_ACCEPT = "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5"

# A few photo CDNs reject plain HTTP requests (Cloudflare/WAF) but accept the
# same URL from a real browser context.  Each entry maps a host to the page that
# must be opened first and the Referer to send.
BROWSER_PHOTO_SOURCES = {
    "photos.sodresantoro.com.br": (
        "https://www.sodresantoro.com.br/veiculos/lotes",
        "https://www.sodresantoro.com.br/",
    ),
}

ENV_OVERRIDES = {
    "provider": "WHATSAPP_PROVIDER",
    "phone": "WHATSAPP_PHONE",
    "apikey": "WHATSAPP_APIKEY",
    "meta_phone_number_id": "WHATSAPP_META_PHONE_ID",
    "meta_token": "WHATSAPP_META_TOKEN",
    "twilio_account_sid": "WHATSAPP_TWILIO_SID",
    "twilio_auth_token": "WHATSAPP_TWILIO_TOKEN",
    "twilio_from": "WHATSAPP_TWILIO_FROM",
    "bridge_port": "WHATSAPP_BRIDGE_PORT",
    "bridge_recipient": "WHATSAPP_BRIDGE_RECIPIENT",
    "min_bid": "WHATSAPP_MIN_BID",
}

REQUIRED = {
    "bridge": (),
    "callmebot": ("phone", "apikey"),
    "meta": ("phone", "meta_phone_number_id", "meta_token"),
    "twilio": ("phone", "twilio_account_sid", "twilio_auth_token", "twilio_from"),
}


# --------------------------------------------------------------------- config
def settings(config: Optional[Dict[str, Any]], default_margin: float = 30.0) -> Dict[str, Any]:
    """Return the effective WhatsApp settings (config file + env overrides)."""
    raw = ((config or {}).get("alerts") or {}).get("whatsapp") or {}
    cfg: Dict[str, Any] = dict(raw)

    for key, env in ENV_OVERRIDES.items():
        value = os.environ.get(env)
        if value:
            cfg[key] = value.strip()

    cfg["provider"] = (cfg.get("provider") or DEFAULT_PROVIDER).lower()
    cfg["phone"] = normalize_phone(cfg.get("phone") or "")
    cfg["bridge_port"] = _as_int(cfg.get("bridge_port"), 8099) or 8099
    cfg["bridge_url"] = (cfg.get("bridge_url") or f"http://127.0.0.1:{cfg['bridge_port']}").rstrip("/")
    cfg["bridge_recipient"] = normalize_phone(cfg.get("bridge_recipient") or "")
    cfg["mode"] = cfg.get("mode") or "ending_today"
    cfg["daily_time"] = cfg.get("daily_time") or "18:00"
    cfg["max_deals"] = _as_int(cfg.get("max_deals"), 8)
    cfg["min_margin_percent"] = _as_float(cfg.get("min_margin_percent"), default_margin)
    cfg["min_bid"] = max(0.0, _as_float(cfg.get("min_bid"), 0.0))
    # The lot-list minimum bid is a hard floor everywhere: the digest never
    # includes anything cheaper, while its own value can raise the floor further.
    cfg["min_bid_floor"] = max(
        0.0,
        _as_float(((config or {}).get("filters") or {}).get("min_bid_price_brl"), 0.0),
    )
    cfg["notify_on_empty"] = bool(cfg.get("notify_on_empty"))
    cfg["scan_before_digest"] = bool(cfg.get("scan_before_digest", True))
    cfg["max_chars"] = _as_int(cfg.get("max_chars"), DEFAULT_MAX_CHARS.get(cfg["provider"], 900))
    return cfg


SECRET_KEYS = ("apikey", "meta_token", "twilio_auth_token")


def public_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Settings safe to hand to the browser: secrets replaced by presence flags."""
    out = {key: value for key, value in cfg.items() if key not in SECRET_KEYS}
    for key in SECRET_KEYS:
        out[f"has_{key}"] = bool(cfg.get(key))
    return out



def missing_settings(cfg: Dict[str, Any]) -> List[str]:
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    return [key for key in REQUIRED.get(provider, ()) if not cfg.get(key)]


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit() or ch == "+")
    if not digits:
        return ""
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


def _as_int(value: Any, default: int) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------- deals
def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None


def _first_seen_local(lot: Dict[str, Any]) -> Optional[date]:
    """``first_seen`` is stored by SQLite as UTC without a timezone marker."""
    raw = lot.get("first_seen")
    if not raw:
        return None
    parsed = _parse_dt(raw)
    if not parsed:
        return None
    return (parsed.replace(tzinfo=timezone.utc)).astimezone().date()


def effective_min_bid(cfg: Dict[str, Any]) -> float:
    """Digest floor: the lot-list minimum bid, raised by the digest setting."""
    return max(float(cfg.get("min_bid") or 0), float(cfg.get("min_bid_floor") or 0))


def delivery_target(cfg: Dict[str, Any]) -> str:
    """Number the digest goes to; empty means your own chat in the bridge."""
    if cfg.get("provider") == "bridge":
        return cfg.get("bridge_recipient") or ""
    return cfg.get("phone") or ""


def select_deals(
    lots: Iterable[Dict[str, Any]],
    cfg: Dict[str, Any],
    now: Optional[datetime] = None,
    cap: bool = True,
) -> List[Dict[str, Any]]:
    """Viable, still-open lots that belong in today's digest.

    ``cap=False`` returns everything eligible so the caller can drop lots that
    were already alerted *before* limiting the message size.
    """
    now = now or datetime.now()
    today = now.date()
    mode = cfg.get("mode", "ending_today")
    floor = float(cfg.get("min_margin_percent") or 0)
    min_bid = effective_min_bid(cfg)

    picked: List[Dict[str, Any]] = []
    for lot in lots:
        if not lot.get("is_worth_it") or lot.get("is_expired"):
            continue
        try:
            margin = float(lot.get("margin_percent") or 0)
            bid = float(lot.get("bid_price") or 0)
        except (TypeError, ValueError):
            continue
        if margin < floor:
            continue
        if min_bid and bid < min_bid:
            continue

        ends = _parse_dt(lot.get("end_date"))
        ends_today = bool(ends and ends.date() == today)
        new_today = _first_seen_local(lot) == today

        if mode == "ending_today" and not ends_today:
            continue
        if mode == "new_today" and not new_today:
            continue
        if mode == "both" and not (ends_today or new_today):
            continue

        picked.append(lot)

    picked.sort(key=lambda lot: (float(lot.get("margin_percent") or 0), float(lot.get("net_profit") or 0)), reverse=True)
    if not cap:
        return picked
    return picked[: max(1, int(cfg.get("max_deals") or 8))]


# ------------------------------------------------------------------- message
def _brl(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "R$ —"
    return "R$ " + f"{number:,.0f}".replace(",", ".")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _deal_title(lot: Dict[str, Any]) -> str:
    title = _text(lot.get("title")) or _text(
        " ".join(str(x) for x in (lot.get("make"), lot.get("model")) if x)
    )
    if len(title) > 72:
        title = title[:69].rstrip() + "..."
    return title or "Untitled lot"


def _source_counts(deals: Iterable[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for lot in deals:
        label = lot.get("site_label") or lot.get("site") or "Unknown"
        counts[label] = counts.get(label, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return " · ".join(f"{label} {count}" for label, count in ordered)


def _digest_header(deals: List[Dict[str, Any]], cfg: Dict[str, Any], now: datetime) -> str:
    floor = float(cfg.get("min_margin_percent") or 0)
    min_bid = effective_min_bid(cfg)
    count = len(deals)
    lines = [
        f"🚗 *AUCTION DEALS · {now:%d/%m/%Y}*",
        f"{count} profitable lot{'s' if count != 1 else ''} · ROI ≥ {floor:.0f}%",
    ]
    if min_bid:
        lines.append(f"Minimum bid: {_brl(min_bid)}")
    sources = _source_counts(deals)
    if sources:
        lines.append(sources)
    lines.append("Bid up to Max, not to ROI.")
    return "\n".join(lines)


def _deal_block(index: int, total: int, lot: Dict[str, Any]) -> str:
    title = _deal_title(lot)
    place = ", ".join(str(x) for x in (lot.get("city"), lot.get("state")) if x)
    source = lot.get("site_label") or lot.get("site") or "—"
    where = f"{place} · {source}" if place else source

    ends = _parse_dt(lot.get("end_date"))
    ends_text = ends.strftime("%d/%m %H:%M") if ends else "open date"

    margin = float(lot.get("margin_percent") or 0)
    flag = " · above target" if margin > 60 else ""
    condition = _text(lot.get("condition"))
    condition_line = f"🔧 {condition[:1].upper()}{condition[1:]}" if condition else None

    lines = [
        f"*{index}/{total} · {title}*",
        f"📍 {where}",
    ]
    if condition_line:
        lines.append(condition_line)
    lines.extend([
        "",
        f"💵 Bid: {_brl(lot.get('bid_price'))}",
        f"🎯 Max bid: {_brl(lot.get('max_bid'))}",
        f"📈 ROI: +{margin:.1f}%{flag}",
        f"🏷 FIPE: {_brl(lot.get('fipe_price'))}",
        f"💰 Net profit: {_brl(lot.get('net_profit'))}",
        "",
        f"⏰ Ends: {ends_text}",
    ])
    if lot.get("url"):
        lines.append(f"🔗 {lot['url']}")
    return "\n".join(lines)


def build_parts(
    deals: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Digest as a header followed by one photo (or text) message per lot."""
    now = now or datetime.now()
    total = len(deals)
    parts: List[Dict[str, Any]] = [{"kind": "text", "text": _digest_header(deals, cfg, now)}]
    for index, lot in enumerate(deals, 1):
        caption = _deal_block(index, total, lot)
        image_url = lot.get("image_url")
        if image_url:
            parts.append({"kind": "image", "url": image_url, "caption": caption})
        else:
            parts.append({"kind": "text", "text": caption})
    return parts


def build_messages(
    deals: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[str]:
    """Text-only digest for providers without photo support, never cutting a lot."""
    now = now or datetime.now()
    limit = int(cfg.get("max_chars") or 900)
    header = _digest_header(deals, cfg, now)
    total = len(deals)

    chunks: List[str] = []
    current = header
    for index, lot in enumerate(deals, 1):
        block = _deal_block(index, total, lot)
        candidate = f"{current}\n\n{block}"
        if len(candidate) > limit - 30 and current != header:
            chunks.append(current)
            current = f"{header}\n\n{block}"
        else:
            current = candidate
    chunks.append(current)

    if len(chunks) > 1:
        total_chunks = len(chunks)
        chunks = [f"{chunk}\n\n({index}/{total_chunks})" for index, chunk in enumerate(chunks, 1)]
    return chunks


# ------------------------------------------------------------------ delivery
def _format_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"HTTP {response.status_code}: {error.get('message') or error}"
    return f"HTTP {response.status_code}: {str(body)[:200]}"


async def _bridge_send(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    client: httpx.AsyncClient,
) -> Tuple[bool, str]:
    try:
        response = await client.post(
            f"{cfg.get('bridge_url') or 'http://127.0.0.1:8099'}/send",
            json={**payload, "to": cfg.get("bridge_recipient") or None},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a send failure
        return False, f"{type(exc).__name__}: {exc}"
    try:
        body = response.json()
    except Exception:
        body = {}
    if response.status_code == 200 and body.get("ok"):
        return True, "sent"
    return False, str(body.get("error") or f"HTTP {response.status_code}")


async def send_text(
    text: str,
    cfg: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[bool, str]:
    """Send one message.  Returns ``(ok, detail)``; never raises."""
    missing = missing_settings(cfg)
    if missing:
        return False, "missing settings: " + ", ".join(missing)

    provider = cfg["provider"]
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        if provider == "bridge":
            return await _bridge_send({"text": text}, cfg, client)

        if provider == "callmebot":
            response = await client.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": cfg["phone"], "text": text, "apikey": cfg["apikey"]},
            )
            body = " ".join(response.text.split())[:200]
            lowered = body.lower()
            ok = response.status_code == 200 and not any(
                token in lowered for token in ("error", "invalid", "not allowed", "wrong")
            )
            return ok, body or f"HTTP {response.status_code}"

        if provider == "meta":
            response = await client.post(
                f"https://graph.facebook.com/v21.0/{cfg['meta_phone_number_id']}/messages",
                headers={"Authorization": f"Bearer {cfg['meta_token']}"},
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": cfg["phone"].lstrip("+"),
                    "type": "text",
                    "text": {"body": text, "preview_url": True},
                },
            )
            if response.status_code >= 400:
                return False, _format_error(response)
            return True, "sent"

        if provider == "twilio":
            to = cfg["phone"] if cfg["phone"].startswith("whatsapp:") else f"whatsapp:{cfg['phone']}"
            sender = cfg["twilio_from"]
            if not sender.startswith("whatsapp:"):
                sender = f"whatsapp:{sender}"
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{cfg['twilio_account_sid']}/Messages.json",
                auth=(cfg["twilio_account_sid"], cfg["twilio_auth_token"]),
                data={"From": sender, "To": to, "Body": text},
            )
            if response.status_code >= 400:
                return False, _format_error(response)
            return True, "sent"

        return False, f"unknown provider: {provider}"
    except Exception as exc:  # network failures must not break the caller
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if own_client:
            await client.aclose()


async def deliver(
    messages: List[str],
    cfg: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Send every chunk, stopping at the first failure."""
    results = []
    for index, message in enumerate(messages, 1):
        ok, detail = await send_text(message, cfg, client)
        results.append({"part": index, "ok": ok, "detail": detail})
        if not ok:
            break
    sent = bool(results) and all(item["ok"] for item in results) and len(results) == len(messages)
    return {
        "sent": sent,
        "provider": cfg.get("provider"),
        "phone": cfg.get("phone"),
        "recipient": delivery_target(cfg),
        "messages": len(results),
        "images": 0,
        "results": results,
    }


# ------------------------------------------------------------------ photos
async def fetch_image(
    url: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Tuple[bytes, str]]:
    """Download a lot photo.  Returns ``(bytes, mimetype)`` or ``None``.

    Some hosts (notably Sodre Santoro's photo CDN) reject datacenter-style
    requests with 403; those lots simply fall back to a text message.
    """
    if not url:
        return None
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=IMAGE_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = await client.get(url, headers={"User-Agent": UA, "Accept": IMAGE_ACCEPT})
        if response.status_code != 200:
            return None
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            return None
        content = response.content
        if not content or len(content) > IMAGE_MAX_BYTES:
            return None
        return content, content_type
    except Exception:  # noqa: BLE001 - a missing photo must not stop the digest
        return None
    finally:
        if own_client:
            await client.aclose()


async def send_image(
    image: bytes,
    mimetype: str,
    caption: str,
    cfg: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[bool, str]:
    """Send one photo with its caption.  Only the local bridge supports media."""
    if cfg.get("provider") != "bridge":
        return False, f"{cfg.get('provider')} does not support photos here"
    missing = missing_settings(cfg)
    if missing:
        return False, "missing settings: " + ", ".join(missing)

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        payload = {
            "image": base64.b64encode(image).decode("ascii"),
            "mimetype": mimetype or "image/jpeg",
            # WhatsApp caps a photo caption at 1024 characters.
            "caption": (caption or "")[:1024],
        }
        return await _bridge_send(payload, cfg, client)
    finally:
        if own_client:
            await client.aclose()


async def fetch_images_browser(urls: Iterable[str]) -> Dict[str, Tuple[bytes, str]]:
    """Retry WAF-protected photo URLs inside a real browser context."""
    grouped: Dict[Tuple[str, str], List[str]] = {}
    for url in urls:
        host = (urlparse(url).netloc or "").lower()
        source = BROWSER_PHOTO_SOURCES.get(host)
        if source:
            grouped.setdefault(source, []).append(url)
    if not grouped:
        return {}

    try:
        from playwright.async_api import async_playwright
    except Exception:
        return {}

    photos: Dict[str, Tuple[bytes, str]] = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                for (landing, referer), group in grouped.items():
                    context = await browser.new_context(
                        user_agent=UA,
                        viewport={"width": 1280, "height": 800},
                        locale="pt-BR",
                        timezone_id="America/Sao_Paulo",
                    )
                    try:
                        page = await context.new_page()
                        try:
                            await page.goto(landing, wait_until="domcontentloaded", timeout=45000)
                            await page.wait_for_timeout(1200)
                        except Exception:
                            pass  # the cookies from a partial load are still useful

                        for url in group:
                            try:
                                response = await context.request.get(
                                    url,
                                    headers={"Referer": referer, "Accept": IMAGE_ACCEPT},
                                    timeout=30000,
                                )
                                if response.status != 200:
                                    continue
                                content_type = (
                                    (response.headers.get("content-type") or "")
                                    .split(";")[0]
                                    .strip()
                                    .lower()
                                )
                                body = await response.body()
                                if (
                                    body
                                    and content_type.startswith("image/")
                                    and len(body) <= IMAGE_MAX_BYTES
                                ):
                                    photos[url] = (body, content_type)
                            except Exception:
                                continue
                    finally:
                        await context.close()
            finally:
                await browser.close()
    except Exception:
        return photos
    return photos


def _looks_unlinked(detail: str) -> bool:
    lowered = (detail or "").lower()
    return "not linked" in lowered or "scan the qr" in lowered or "not linked yet" in lowered


async def _deliver_rich(
    deals: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Header + one photo per lot (text fallback), sent through the bridge."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    results: List[Dict[str, Any]] = []
    images_sent = 0
    try:
        parts = build_parts(deals, cfg)
        unique_urls = list(
            dict.fromkeys(part["url"] for part in parts if part["kind"] == "image")
        )
        photos: Dict[str, Tuple[bytes, str]] = {}
        if unique_urls:
            downloaded = await asyncio.gather(
                *(fetch_image(url, client) for url in unique_urls),
                return_exceptions=True,
            )
            for url, photo in zip(unique_urls, downloaded):
                if isinstance(photo, tuple):
                    photos[url] = photo
            missing = [url for url in unique_urls if url not in photos]
            if missing:
                photos.update(await fetch_images_browser(missing))

        for index, part in enumerate(parts, 1):
            if part["kind"] == "image":
                photo = photos.get(part["url"])
                if photo:
                    data, mimetype = photo
                    ok, detail = await send_image(data, mimetype, part["caption"], cfg, client)
                    if ok:
                        images_sent += 1
                        results.append({"part": index, "kind": "image", "ok": True, "detail": "sent"})
                        continue
                    if _looks_unlinked(detail):
                        results.append({"part": index, "kind": "image", "ok": False, "detail": detail})
                        break
                text = part["caption"]
                kind = "text-fallback"
            else:
                text = part["text"]
                kind = "text"

            ok, detail = await send_text(text, cfg, client)
            results.append({"part": index, "kind": kind, "ok": ok, "detail": detail})
            if not ok:
                break

        sent = bool(results) and all(item["ok"] for item in results)
        return {
            "sent": sent,
            "provider": cfg.get("provider"),
            "phone": cfg.get("phone"),
            "recipient": delivery_target(cfg),
            "messages": len(results),
            "images": images_sent,
            "results": results,
        }
    finally:
        if own_client:
            await client.aclose()


async def deliver_digest(
    deals: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Send the digest in the richest form the provider supports."""
    if not deals:
        now = datetime.now()
        floor = float(cfg.get("min_margin_percent") or 0)
        text = (
            f"*AUCTION DEALS · {now:%d/%m/%Y}*\n"
            f"No lot cleared the {floor:.0f}% ROI target today."
        )
        return await deliver([text], cfg, client)

    has_photo = cfg.get("provider") == "bridge" and any(lot.get("image_url") for lot in deals)
    if has_photo:
        return await _deliver_rich(deals, cfg, client)
    return await deliver(build_messages(deals, cfg), cfg, client)

