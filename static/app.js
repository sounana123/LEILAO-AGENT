const messagesEl = document.getElementById("messages");
const emptyEl = document.getElementById("empty");
const inputEl = document.getElementById("input");
const composerEl = document.getElementById("composer");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const modelEl = document.getElementById("model");
const newChatBtn = document.getElementById("new-chat");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const workspaceEl = document.getElementById("workspace");

const SESSION_KEY = "local-agent-session";
let sessionId = localStorage.getItem(SESSION_KEY) || null;
let controller = null;
let busy = false;

const renderMarkdown = (text) =>
  window.marked ? marked.parse(text, { breaks: true, gfm: true }) : escapeHtml(text);

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- setup ---------------- */

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    workspaceEl.textContent = data.workspace;

    if (data.ollama) {
      statusDot.className = "dot ok";
      statusText.textContent = "Ollama connected";
    } else {
      statusDot.className = "dot err";
      statusText.textContent = "Ollama not reachable";
    }

    const models = data.models.length ? data.models : [data.default_model];
    const saved = localStorage.getItem("local-agent-model");
    modelEl.innerHTML = "";
    for (const name of models) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      modelEl.appendChild(opt);
    }
    modelEl.value = models.includes(saved) ? saved : models.includes(data.default_model) ? data.default_model : models[0];
  } catch (err) {
    statusDot.className = "dot err";
    statusText.textContent = "Server error";
  }
}

modelEl.addEventListener("change", () => localStorage.setItem("local-agent-model", modelEl.value));

newChatBtn.addEventListener("click", async () => {
  if (sessionId) {
    fetch("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
  }
  sessionId = null;
  localStorage.removeItem(SESSION_KEY);
  messagesEl.innerHTML = "";
  messagesEl.appendChild(emptyEl);
  emptyEl.style.display = "";
  inputEl.focus();
});

document.querySelectorAll(".suggestion").forEach((btn) => {
  btn.addEventListener("click", () => {
    inputEl.value = btn.textContent.trim();
    autoGrow();
    inputEl.focus();
  });
});

/* ---------------- composer ---------------- */

function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

inputEl.addEventListener("input", autoGrow);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composerEl.requestSubmit();
  }
});

composerEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text || busy) return;
  inputEl.value = "";
  autoGrow();
  send(text);
});

stopBtn.addEventListener("click", () => {
  if (controller) controller.abort();
});

function setBusy(value) {
  busy = value;
  sendBtn.disabled = value;
  stopBtn.classList.toggle("hidden", !value);
}

/* ---------------- rendering ---------------- */

function scrollToBottom(force = false) {
  const gap = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
  if (force || gap < 160) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addUserMessage(text) {
  emptyEl.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  scrollToBottom(true);
}

function createBubble() {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble cursor";
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  scrollToBottom(true);

  let raw = "";
  let queued = false;

  return {
    append(text) {
      raw += text;
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        bubble.innerHTML = renderMarkdown(raw);
        scrollToBottom();
      });
    },
    finish() {
      bubble.classList.remove("cursor");
      bubble.innerHTML = renderMarkdown(raw);
      scrollToBottom();
    },
    error(message) {
      bubble.classList.remove("cursor");
      const p = document.createElement("p");
      p.style.color = "var(--err)";
      p.textContent = message;
      bubble.appendChild(p);
    },
  };
}

// A reply can be interrupted by tool calls: text, tool block, more text, ...
// So bubbles are created lazily and closed whenever a tool block appears.
function createStream() {
  let bubble = null;
  return {
    append(text) {
      if (!bubble) bubble = createBubble();
      bubble.append(text);
    },
    close() {
      if (!bubble) return;
      bubble.finish();
      bubble = null;
    },
    error(message) {
      if (!bubble) bubble = createBubble();
      bubble.error(message);
    },
  };
}

function addToolBlock(name, args) {
  emptyEl.style.display = "none";
  const details = document.createElement("details");
  details.className = "tool";

  const summary = document.createElement("summary");
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  const nameEl = document.createElement("span");
  nameEl.className = "name";
  nameEl.textContent = name;
  const argsEl = document.createElement("span");
  argsEl.className = "args";
  argsEl.textContent = summarizeArgs(args);
  const state = document.createElement("span");
  state.className = "state running";
  state.textContent = "running";
  summary.append(spinner, nameEl, argsEl, state);

  const pre = document.createElement("pre");
  details.append(summary, pre);
  messagesEl.appendChild(details);
  scrollToBottom(true);

  return {
    finish(output) {
      spinner.remove();
      const failed = /^(unknown tool|bad arguments|.* failed:|.* does not exist|path is outside)/i.test(output);
      state.className = `state ${failed ? "error" : "done"}`;
      state.textContent = failed ? "error" : "done";
      pre.textContent = output;
      scrollToBottom();
    },
  };
}

function summarizeArgs(args) {
  if (!args || typeof args !== "object") return "";
  if (args.command) return args.command;
  if (args.path) return args.path;
  if (args.url) return args.url;
  const first = Object.values(args)[0];
  return typeof first === "string" ? first.slice(0, 90) : "";
}

/* ---------------- chat ---------------- */

async function send(text) {
  addUserMessage(text);
  const stream = createStream();
  const toolBlocks = new Map();
  let hadError = false;

  setBusy(true);
  controller = new AbortController();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId, model: modelEl.value }),
      signal: controller.signal,
    });

    if (!res.ok || !res.body) throw new Error(`Server returned ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let index;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const payload = line.slice(6);
        if (payload === "[DONE]") continue;

        let event;
        try {
          event = JSON.parse(payload);
        } catch {
          continue;
        }
        handleEvent(event);
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      stream.append("\n\n_(stopped)_");
    } else {
      stream.error(`Connection problem: ${err.message}`);
      hadError = true;
    }
  } finally {
    stream.close();
    setBusy(false);
    controller = null;
    inputEl.focus();
  }

  function handleEvent(event) {
    switch (event.type) {
      case "session":
        sessionId = event.session_id;
        localStorage.setItem(SESSION_KEY, sessionId);
        break;
      case "token":
        stream.append(event.text);
        break;
      case "tool_start": {
        stream.close();
        const block = addToolBlock(event.name, event.arguments);
        const queue = toolBlocks.get(event.name) || [];
        queue.push(block);
        toolBlocks.set(event.name, queue);
        break;
      }
      case "tool_end": {
        const queue = toolBlocks.get(event.name);
        const block = queue && queue.shift();
        if (block) block.finish(event.output);
        break;
      }
      case "error":
        stream.error(event.message);
        hadError = true;
        break;
    }
  }

  if (!hadError) scrollToBottom();
}

/* ---------------- boot ---------------- */

loadHealth();
autoGrow();
inputEl.focus();
