"""Tools the agent can call.

Every file path is sandboxed to WORKSPACE. Shell commands run inside it too.
Tool functions must be sync -- the loop runs them in a worker thread.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
from pathlib import Path

WORKSPACE = Path(
    os.environ.get("AGENT_WORKSPACE") or Path(__file__).resolve().parent.parent / "workspace"
).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

MAX_READ_CHARS = 30_000
MAX_OUTPUT_CHARS = 8_000
MAX_COMMAND_TIMEOUT = 120


# ---------------------------------------------------------------- helpers

def _resolve(path: str) -> Path:
    """Turn a user/model supplied path into a path inside the workspace."""
    raw = Path(path.strip().strip('"').strip("'") or ".")
    target = (raw if raw.is_absolute() else WORKSPACE / raw).resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise ValueError(f"Path is outside the workspace: {path}")
    return target


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


# ---------------------------------------------------------------- tools

def list_dir(path: str = ".", recursive: bool = False) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"Directory does not exist: {path}"
    if not target.is_dir():
        return f"Not a directory: {path}"

    lines: list[str] = []
    walker = target.rglob("*") if recursive else target.iterdir()
    entries = sorted(walker, key=lambda p: (p.is_file(), str(p).lower()))
    for entry in entries[:400]:
        rel = entry.relative_to(WORKSPACE).as_posix()
        if entry.is_dir():
            lines.append(f"{rel}/")
        else:
            lines.append(f"{rel}  ({entry.stat().st_size} bytes)")
    if not lines:
        return f"{path} is empty."
    if len(entries) > 400:
        lines.append(f"... and {len(entries) - 400} more entries")
    return "\n".join(lines)


def read_file(path: str, start_line: int = 1, max_lines: int = 500) -> str:
    target = _resolve(path)
    if not target.is_file():
        return f"File does not exist: {path}"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path}: {exc}"

    all_lines = text.splitlines()
    start = max(1, int(start_line))
    chunk = all_lines[start - 1 : start - 1 + max(1, int(max_lines))]
    numbered = "\n".join(f"{start + i:>5} | {line}" for i, line in enumerate(chunk))
    footer = ""
    if start - 1 + len(chunk) < len(all_lines):
        footer = f"\n... file has {len(all_lines)} lines total"
    return _clip(numbered + footer, MAX_READ_CHARS)


def write_file(path: str, content: str) -> str:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {target.relative_to(WORKSPACE).as_posix()}"


def edit_file(path: str, find: str, replace: str) -> str:
    target = _resolve(path)
    if not target.is_file():
        return f"File does not exist: {path}"
    text = target.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        return "Text to replace was not found. Read the file again and match it exactly."
    if count > 1:
        return f"That text appears {count} times. Include more surrounding lines to make it unique."
    target.write_text(text.replace(find, replace, 1), encoding="utf-8")
    return f"Updated {target.relative_to(WORKSPACE).as_posix()}"


def delete_file(path: str) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"Path does not exist: {path}"
    if target.is_dir():
        return "Refusing to delete a directory. Delete its files individually."
    target.unlink()
    return f"Deleted {target.relative_to(WORKSPACE).as_posix()}"


def run_command(command: str, timeout: int = 30) -> str:
    timeout = max(1, min(int(timeout), MAX_COMMAND_TIMEOUT))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."

    parts = [f"exit code: {proc.returncode}"]
    if proc.stdout.strip():
        parts.append(f"stdout:\n{proc.stdout.rstrip()}")
    if proc.stderr.strip():
        parts.append(f"stderr:\n{proc.stderr.rstrip()}")
    if len(parts) == 1:
        parts.append("(no output)")
    return _clip("\n".join(parts))


def fetch_url(url: str) -> str:
    import httpx

    if not url.startswith(("http://", "https://")):
        return "Only http:// and https:// URLs are supported."
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 (local-agent)"})
    except Exception as exc:  # noqa: BLE001 - surface any network failure to the model
        return f"Request failed: {exc}"

    body = resp.text
    if "html" in resp.headers.get("content-type", ""):
        body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = html.unescape(body)
        body = re.sub(r"[ \t\r\f\v]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n\n", body)
    return _clip(f"status: {resp.status_code}\n\n{body.strip()}", MAX_READ_CHARS)


# ---------------------------------------------------------------- registry

def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOLS: list[dict] = [
    _spec(
        "list_dir",
        "List files and folders in the workspace.",
        {
            "path": {"type": "string", "description": "Folder to list, relative to the workspace. Default '.'."},
            "recursive": {"type": "boolean", "description": "Recurse into subfolders."},
        },
        [],
    ),
    _spec(
        "read_file",
        "Read a text file with line numbers. Use start_line/max_lines for big files.",
        {
            "path": {"type": "string", "description": "File path inside the workspace."},
            "start_line": {"type": "integer", "description": "First line to read (1-based)."},
            "max_lines": {"type": "integer", "description": "How many lines to read (default 500)."},
        },
        ["path"],
    ),
    _spec(
        "write_file",
        "Create or overwrite a file with the given content.",
        {
            "path": {"type": "string", "description": "File path inside the workspace."},
            "content": {"type": "string", "description": "Full file content to write."},
        },
        ["path", "content"],
    ),
    _spec(
        "edit_file",
        "Replace one exact snippet in a file. Fails if the snippet is missing or appears more than once.",
        {
            "path": {"type": "string", "description": "File path inside the workspace."},
            "find": {"type": "string", "description": "Exact existing text, including indentation."},
            "replace": {"type": "string", "description": "Text to put in its place."},
        },
        ["path", "find", "replace"],
    ),
    _spec(
        "delete_file",
        "Delete a single file from the workspace.",
        {"path": {"type": "string", "description": "File path inside the workspace."}},
        ["path"],
    ),
    _spec(
        "run_command",
        "Run a shell command inside the workspace and return its output.",
        {
            "command": {"type": "string", "description": "Command line to execute."},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (max 120)."},
        },
        ["command"],
    ),
    _spec(
        "fetch_url",
        "Download a web page and return its text, useful for research and documentation.",
        {"url": {"type": "string", "description": "Full http(s) URL."}},
        ["url"],
    ),
]

HANDLERS = {
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "delete_file": delete_file,
    "run_command": run_command,
    "fetch_url": fetch_url,
}


def run_tool(name: str, args: dict) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}"
    try:
        return handler(**args)
    except TypeError as exc:
        return f"Bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - the model needs the error text, not a crash
        return f"{name} failed: {exc}"
