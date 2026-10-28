# Local AI Agent

A small, readable AI agent that runs entirely on your machine.

- **Brain:** any Ollama model with tool-calling (default `qwen3:8b`)
- **Tools:** list/read/write/edit/delete files, run shell commands, fetch web pages
- **Sandbox:** all file access and commands are confined to `workspace/`
- **UI:** local web chat that streams tokens and shows each tool call

## Run it

```powershell
pip install -r requirements.txt
python -m uvicorn app:app --port 8000
```

Then open <http://127.0.0.1:8000>. On Windows you can also double-click `run.bat`.

Ollama must be running (`ollama serve`) and the model pulled:

```powershell
ollama pull qwen3:8b
```

## Pick a model

Use the dropdown in the sidebar. Models that follow tool calls well: `qwen3:8b`,
`qwen2.5:7b`, `llama3.1:8b`, `mistral`. Small models sometimes ignore tools —
bigger ones are more reliable.

Set a different default without touching code:

```powershell
$env:AGENT_MODEL = "qwen2.5:7b"
```

## What the agent can do

| Tool | What it does |
|------|--------------|
| `list_dir` | List files in the workspace |
| `read_file` | Read a text file with line numbers |
| `write_file` | Create or overwrite a file |
| `edit_file` | Replace one exact snippet |
| `delete_file` | Remove a single file |
| `run_command` | Run a shell command in the workspace |
| `fetch_url` | Download a page and return its text |

## How it works

```
browser ──SSE──> FastAPI (app.py)
                    │
                    └─> agent/loop.py ──HTTP──> Ollama /api/chat
                              │
                              └─> agent/tools.py  (executes tool calls)
```

`agent/loop.py` is the whole agent loop: send messages, stream the reply, run any
tool calls, append the results, repeat until the model answers without tools
(max 12 rounds).

## Safety notes

- `run_command` executes real commands with your user permissions. It is confined
  to `workspace/`, but a command can still reach outside it (`cd ..`, absolute paths).
  Only run this on your own machine, and review anything you point it at.
- Conversation history lives in memory and is lost when the server stops.

## Where to extend

- Add a tool: write a function in `agent/tools.py`, then add its spec to `TOOLS`
  and its name to `HANDLERS`.
- Change personality/rules: edit `agent/prompts.py`.
- Tune limits: `MAX_STEPS` and `num_ctx` in `agent/loop.py`.
