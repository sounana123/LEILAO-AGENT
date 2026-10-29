"""The agent loop: talk to Ollama, stream tokens, run tools, repeat."""

from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator

import httpx

from .prompts import SYSTEM_PROMPT
from .tools import TOOLS, run_tool

OLLAMA_HOST = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434").rstrip("/")
MAX_STEPS = 12
KEEP_MESSAGES = 60


def new_history() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def _options(model: str) -> dict:
    return {"temperature": 0.6, "num_ctx": 8192}


def _payload(model: str, messages: list[dict]) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "stream": True,
        "options": _options(model),
    }
    # qwen3 models think by default, which is slow and noisy for a chat UI.
    if "qwen3" in model:
        payload["think"] = False
    return payload


def _trim(history: list[dict]) -> None:
    """Keep the system prompt plus a recent window of turns."""
    if len(history) <= KEEP_MESSAGES:
        return
    system = history[0]
    tail = history[-(KEEP_MESSAGES - 1) :]
    # Never start the window on a tool result with no assistant call before it.
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    history[:] = [system] + tail


async def check_ollama() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def list_models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    return sorted(model["name"] for model in data.get("models", []))


async def run_agent(messages: list[dict], model: str) -> AsyncIterator[dict]:
    """Yield UI events and append to `messages` as the conversation grows."""
    _trim(messages)

    for step in range(1, MAX_STEPS + 1):
        assistant_text = ""
        tool_calls: list[dict] = []
        yielded_any = False

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", f"{OLLAMA_HOST}/api/chat", json=_payload(model, messages)
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")
                    yield {"type": "error", "message": f"Ollama error {response.status_code}: {body[:400]}"}
                    return

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if error := chunk.get("error"):
                        yield {"type": "error", "message": str(error)}
                        return

                    message = chunk.get("message") or {}
                    text = message.get("content") or ""
                    if text:
                        assistant_text += text
                        yielded_any = True
                        yield {"type": "token", "text": text}

                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError:
                                arguments = {"_raw": arguments}
                        tool_calls.append({"name": function.get("name", ""), "arguments": arguments})

                    if chunk.get("done"):
                        break

        if not tool_calls:
            messages.append({"role": "assistant", "content": assistant_text})
            yield {"type": "done", "steps": step}
            return

        messages.append(
            {
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": [
                    {"function": {"name": call["name"], "arguments": call["arguments"]}}
                    for call in tool_calls
                ],
            }
        )

        for call in tool_calls:
            name, args = call["name"], call["arguments"]
            yield {"type": "tool_start", "name": name, "arguments": args}
            output = await asyncio.to_thread(run_tool, name, args)
            yield {"type": "tool_end", "name": name, "output": output}
            messages.append(
                {
                    "role": "tool",
                    "content": output,
                    "tool_name": name,
                }
            )

        if not yielded_any:
            yield {"type": "step", "n": step}

    yield {
        "type": "error",
        "message": f"Stopped after {MAX_STEPS} tool rounds without a final answer.",
    }
