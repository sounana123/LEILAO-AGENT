"""FastAPI server for the local agent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.loop import check_ollama, list_models, new_history, run_agent
from agent.tools import WORKSPACE
from agents.leilao.runner import runner
from agents.leilao.scheduler import loop as digest_loop
from agents.leilao.bridge import bridge

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "qwen3:8b")

# Uvicorn configures its own loggers only; give the app (e.g. the digest
# scheduler) a root handler so `journalctl -u leilao-web` shows its lines.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.getLogger("leilao.app").info(
        "web server ready - daily digest scheduler started"
    )
    task = asyncio.create_task(digest_loop(runner))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        bridge.stop()


app = FastAPI(title="Local AI Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

SESSIONS: dict[str, list[dict]] = {}
LOCKS: dict[str, asyncio.Lock] = {}
MAX_SESSIONS = 50


def _lock(session_id: str) -> asyncio.Lock:
    return LOCKS.setdefault(session_id, asyncio.Lock())


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None
    model: str | None = None


class ResetRequest(BaseModel):
    session_id: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/leilao")
async def leilao_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "leilao.html")


def _csv_values(value: str | None) -> list[str] | None:
    if not value:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    return values or None


@app.get("/api/leilao/status")
async def get_leilao_status():
    return runner.get_status()


@app.get("/api/leilao/config")
async def get_leilao_config():
    return runner.get_config()


@app.post("/api/leilao/config")
async def update_leilao_config(config: dict):
    return runner.update_config(config)


@app.post("/api/leilao/search")
async def search_leilao():
    result = await runner.search_all()
    return result


@app.post("/api/leilao/recalc")
async def recalc_leilao():
    return runner.recalc()


@app.get("/api/leilao/whatsapp/preview")
async def whatsapp_preview():
    return runner.whatsapp_preview()


@app.post("/api/leilao/whatsapp/test")
async def whatsapp_test():
    return await runner.whatsapp_test()


@app.post("/api/leilao/whatsapp/digest")
async def whatsapp_digest(payload: dict | None = None):
    payload = payload or {}
    return await runner.whatsapp_digest(
        force=bool(payload.get("force")),
        dry_run=bool(payload.get("dry_run")),
    )


@app.get("/api/leilao/whatsapp/bridge/status")
async def whatsapp_bridge_status():
    return await runner.bridge_status()


@app.post("/api/leilao/whatsapp/bridge/start")
async def whatsapp_bridge_start():
    return await runner.bridge_start()


@app.post("/api/leilao/whatsapp/bridge/logout")
async def whatsapp_bridge_logout():
    return await runner.bridge_logout()


@app.post("/api/leilao/whatsapp/bridge/pair")
async def whatsapp_bridge_pair(payload: dict | None = None):
    phone = str((payload or {}).get("phone") or "").strip()
    if not phone:
        return {"ok": False, "error": "phone number is required"}
    return await runner.bridge_pair(phone)


@app.get("/api/leilao/lots")
async def get_leilao_lots(
    only_worth_it: bool = False,
    only_active: bool = True,
    min_margin: float | None = None,
    max_margin: float | None = None,
    min_bid: float | None = None,
    max_bid: float | None = None,
    sites: str | None = None,
    conditions: str | None = None,
    makes: str | None = None,
):
    return runner.get_lots(
        only_worth_it=only_worth_it,
        only_active=only_active,
        min_margin=min_margin,
        max_margin=max_margin,
        min_bid=min_bid,
        max_bid=max_bid,
        sites=_csv_values(sites),
        conditions=_csv_values(conditions),
        makes=_csv_values(makes),
    )


@app.get("/api/leilao/lots.csv")
async def export_leilao_lots(
    only_worth_it: bool = True,
    min_margin: float | None = None,
    min_bid: float | None = None,
    max_bid: float | None = None,
):
    import csv
    import io

    lots = runner.get_lots(
        only_worth_it=only_worth_it,
        min_margin=min_margin,
        min_bid=min_bid,
        max_bid=max_bid,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "source", "condition", "make", "model", "year", "km", "city", "state",
        "current_bid_brl", "fipe_brl", "resale_estimate_brl", "total_costs_brl",
        "net_profit_brl", "roi_percent", "max_bid_for_target_brl", "bid_vs_fipe_percent", "viable",
        "ends_at", "seller", "url",
    ])
    for lot in lots:
        writer.writerow([
            lot.get("site_label") or lot.get("site"),
            lot.get("condition"),
            lot.get("make"),
            lot.get("model"),
            lot.get("year"),
            lot.get("km"),
            lot.get("city"),
            lot.get("state"),
            lot.get("bid_price"),
            lot.get("fipe_price"),
            lot.get("sale_estimate"),
            lot.get("total_costs"),
            lot.get("net_profit"),
            lot.get("margin_percent"),
            lot.get("max_bid"),
            lot.get("price_vs_fipe"),
            "yes" if lot.get("is_worth_it") else "no",
            lot.get("end_date"),
            lot.get("seller"),
            lot.get("url"),
        ])
    filename = f"auction-deals-{uuid.uuid4().hex[:8]}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/health")
async def health() -> dict:
    ok, detail = await check_ollama()
    return {
        "ollama": ok,
        "detail": detail,
        "models": await list_models(),
        "default_model": DEFAULT_MODEL,
        "workspace": str(WORKSPACE),
    }


@app.post("/api/reset")
async def reset(req: ResetRequest) -> dict:
    SESSIONS.pop(req.session_id, None)
    LOCKS.pop(req.session_id, None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    session_id = req.session_id or f"s{uuid.uuid4().hex[:12]}"
    model = req.model or DEFAULT_MODEL

    if session_id not in SESSIONS:
        if len(SESSIONS) >= MAX_SESSIONS:
            SESSIONS.pop(next(iter(SESSIONS)))
        SESSIONS[session_id] = new_history()

    history = SESSIONS[session_id]
    history.append({"role": "user", "content": req.message})
    lock = _lock(session_id)

    async def events():
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
        try:
            async with lock:
                async for event in run_agent(history, model):
                    yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report failures to the UI, don't kill the stream silently
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
