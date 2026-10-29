"""Manage the local Node WhatsApp bridge (``whatsapp_bridge/``).

The bridge keeps a WhatsApp Web session alive and exposes a tiny HTTP API on
localhost.  This module starts it on demand, reads its status (including the QR
code to link a phone) and sends messages through it.

Nothing here touches your phone unless you scan the QR code shown in the UI.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

BRIDGE_DIR = Path(__file__).resolve().parent / "whatsapp_bridge"
ROOT = Path(__file__).resolve().parents[2]
AUTH_DIR = ROOT / "data" / "whatsapp_auth"
LOG_PATH = ROOT / "data" / "whatsapp_bridge.log"


def auth_dir() -> Path:
    """Where the linked-device session is stored.

    CI (see ``agents/leilao/ci.py``) points this at a restored directory so the
    server does not have to keep a permanent process alive.
    """
    override = os.environ.get("LEILAO_WA_AUTH_DIR")
    return Path(override).expanduser() if override else AUTH_DIR


def log_path() -> Path:
    override = os.environ.get("LEILAO_BRIDGE_LOG")
    return Path(override).expanduser() if override else LOG_PATH


class WhatsAppBridge:
    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen] = None
        self.port = 8099
        self._log_handle = None

    # ------------------------------------------------------------------
    def configure(self, port: Any = None) -> None:
        try:
            self.port = int(port) or 8099
        except (TypeError, ValueError):
            self.port = 8099

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def installed(self) -> bool:
        return (BRIDGE_DIR / "node_modules" / "@whiskeysockets" / "baileys").exists()

    def node_available(self) -> bool:
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=10, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def log_tail(self, lines: int = 6) -> str:
        try:
            data = log_path().read_text(encoding="utf-8", errors="replace").strip().splitlines()
            return " | ".join(line.strip() for line in data[-lines:])
        except OSError:
            return ""

    # ------------------------------------------------------------------
    async def health(self, timeout: float = 1.5) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self.base_url}/health")
                return response.status_code == 200
        except Exception:
            return False

    def _spawn(self) -> None:
        auth = auth_dir()
        log = log_path()
        auth.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        if self._log_handle is None or self._log_handle.closed:
            self._log_handle = open(log, "ab", buffering=0)
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            ["node", "index.js", "--port", str(self.port), "--auth", str(auth)],
            cwd=str(BRIDGE_DIR),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creation,
        )

    async def ensure_running(self, wait_seconds: float = 25.0) -> Tuple[bool, str]:
        self.configure(self.port)
        if await self.health():
            return True, "running"
        if not self.node_available():
            return False, "Node.js is not installed or not on PATH"
        if not self.installed():
            return False, (
                "bridge dependencies are missing — run: "
                "npm install (inside agents/leilao/whatsapp_bridge)"
            )

        alive = self.process is not None and self.process.poll() is None
        if not alive:
            self._spawn()

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if await self.health():
                return True, "started"
            if self.process is not None and self.process.poll() is not None:
                break
            await asyncio.sleep(0.5)

        tail = self.log_tail()
        return False, "bridge did not start" + (f": {tail}" if tail else "")

    async def status(self) -> Dict[str, Any]:
        if not await self.health():
            return {
                "running": False,
                "connected": False,
                "qr": None,
                "installed": self.installed(),
                "error": "bridge is not running",
            }
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                response = await client.get(f"{self.base_url}/status")
                data = response.json()
            data["running"] = True
            data["installed"] = True
            return data
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            return {
                "running": True,
                "connected": False,
                "qr": None,
                "installed": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def send(self, text: str, to: Optional[str] = None) -> Tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/send",
                    json={"text": text, "to": to or None},
                )
            try:
                data = response.json()
            except Exception:
                data = {}
            if response.status_code == 200 and data.get("ok"):
                return True, "sent"
            return False, str(data.get("error") or f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def logout(self) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(f"{self.base_url}/logout")
                return {"ok": response.status_code == 200, **({"detail": response.text[:200]} if response.status_code != 200 else {})}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def pair(self, phone: str) -> Dict[str, Any]:
        """Ask WhatsApp for a pairing code (alternative to scanning the QR)."""
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                response = await client.post(f"{self.base_url}/pair", json={"phone": phone})
            try:
                data = response.json()
            except Exception:
                data = {"ok": False, "error": response.text[:200]}
            return data if isinstance(data, dict) else {"ok": False, "error": str(data)[:200]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        if self._log_handle is not None and not self._log_handle.closed:
            try:
                self._log_handle.close()
            except Exception:
                pass
        self._log_handle = None


bridge = WhatsAppBridge()
