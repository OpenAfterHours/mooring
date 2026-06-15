"use strict";

// The interactive AI copilot page. Opens beside the marimo notebook tab. The
// model streams over SSE; proposed cells are Applied (injected + run) into the
// open notebook via the hub, which talks to marimo over HTTP only.

const $ = (id) => document.getElementById(id);
const NOTEBOOK = new URLSearchParams(location.search).get("notebook") || "";

let sid = null;
let source = null; // EventSource
let assistantEl = null; // the assistant bubble currently being streamed
let busy = false;

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok && !data.error) data.error = `Request failed (${resp.status})`;
  return { status: resp.status, data };
}

function showError(message) {
  const banner = $("error-banner");
  banner.textContent = message || "";
  banner.classList.toggle("hidden", !message);
}

function setStatus(text) {
  $("chat-status").textContent = text || "";
}

function addMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg msg-${role}`;
  el.textContent = text;
  $("messages").appendChild(el);
  el.scrollIntoView({ block: "end" });
  return el;
}

function addProposal(code) {
  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant proposal";

  const pre = document.createElement("pre");
  pre.className = "cell-code";
  pre.textContent = code;

  const bar = document.createElement("div");
  bar.className = "toolbar";
  const label = document.createElement("b");
  label.textContent = "Proposed cell ";
  const applyBtn = document.createElement("button");
  applyBtn.className = "primary small";
  applyBtn.textContent = "Apply ▸";
  const note = document.createElement("span");
  note.className = "muted";

  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    note.textContent = " applying…";
    const { status, data } = await api("/api/ai/chat/apply", { sid, code });
    if (data.ok) {
      note.textContent = " added & run ✓";
      applyBtn.textContent = "Applied";
    } else if (status === 409) {
      note.textContent = " — open the notebook tab first, then Apply again";
      applyBtn.disabled = false;
    } else {
      note.textContent = ` — ${data.error || "failed"}`;
      applyBtn.disabled = false;
    }
  });

  bar.append(label, applyBtn, note);
  wrap.append(bar, pre);
  $("messages").appendChild(wrap);
  wrap.scrollIntoView({ block: "end" });
}

function closeStream() {
  if (source) {
    source.close();
    source = null;
  }
}

async function openChat() {
  closeStream();
  showError("");
  const dataset = $("chat-dataset").value;
  const { status, data } = await api("/api/ai/chat/open", { notebook: NOTEBOOK, dataset });
  if (!data.sid) {
    showError(data.error || `Could not start the copilot (${status}).`);
    return;
  }
  sid = data.sid;
  source = new EventSource(`/api/ai/chat/stream/${sid}`);
  source.addEventListener("delta", (e) => {
    const { text } = JSON.parse(e.data);
    if (!assistantEl) assistantEl = addMessage("assistant", "");
    assistantEl.textContent += text;
  });
  source.addEventListener("message", (e) => {
    const { text } = JSON.parse(e.data);
    if (assistantEl) assistantEl.textContent = text;
    else addMessage("assistant", text);
    assistantEl = null;
  });
  source.addEventListener("proposal", (e) => {
    const { code } = JSON.parse(e.data);
    addProposal(code);
  });
  source.addEventListener("idle", () => setBusy(false));
  // Named "fail", not "error": a server-sent SSE event named "error" collides
  // with EventSource's native connection-error event.
  source.addEventListener("fail", (e) => {
    showError(JSON.parse(e.data).text || "The assistant reported an error.");
    setBusy(false);
  });
  source.addEventListener("closed", () => setStatus("Session closed."));
  source.onerror = () => setStatus("Reconnecting…");
  setStatus(dataset ? `Schema in context: ${dataset}` : "Notebook source in context.");
}

function setBusy(value) {
  busy = value;
  $("chat-send").disabled = value;
}

async function send() {
  if (busy) return;
  const input = $("chat-input");
  const text = input.value.trim();
  if (!sid || !text) return;
  addMessage("user", text);
  input.value = "";
  assistantEl = null;
  setBusy(true);
  const { status, data } = await api("/api/ai/chat/send", { sid, text });
  if (data.error) {
    showError(data.error);
    setBusy(false);
  }
}

async function init() {
  $("chat-target").textContent = NOTEBOOK ? `Notebook: ${NOTEBOOK}` : "(no notebook)";
  if (!NOTEBOOK) {
    showError("Open the copilot from a notebook's “AI” button.");
    return;
  }
  const { data: state } = await api("/api/state");
  const select = $("chat-dataset");
  for (const ds of state.datasets || []) {
    const opt = document.createElement("option");
    opt.value = ds;
    opt.textContent = ds;
    select.appendChild(opt);
  }
  select.addEventListener("change", openChat);
  $("chat-send").addEventListener("click", send);
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  await openChat();
}

document.addEventListener("DOMContentLoaded", init);
