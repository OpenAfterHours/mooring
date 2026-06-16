"use strict";

// The interactive AI copilot page. Opens beside the marimo notebook tab. The
// model streams over SSE; proposed cells are Applied (injected + run) into the
// open notebook via the hub. mooring talks to marimo over HTTP only, never a
// websocket — outputs/values never reach this page or the model.

const $ = (id) => document.getElementById(id);
const NOTEBOOK = new URLSearchParams(location.search).get("notebook") || "";
const LS_MODEL = "mooring.ai.model";
const LS_EFFORT = "mooring.ai.effort";

const TOOL_LABELS = {
  mooring_list_datasets: "Listing datasets…",
  mooring_get_schema: "Looking up the schema…",
  mooring_read_notebook_source: "Reading the notebook…",
  mooring_propose_cell: "Drafting a cell…",
};

let sid = null;
let source = null; // EventSource
let assistantEl = null; // the assistant bubble currently being streamed
let assistantRaw = ""; // accumulated raw text for the streaming bubble
let turnState = "idle"; // idle | thinking | streaming | applying | error
let stick = true; // auto-scroll only when the user is near the bottom
let MODELS = [];

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

// -- scrolling --------------------------------------------------------------

function isNearBottom() {
  const m = $("messages");
  return m.scrollHeight - m.scrollTop - m.clientHeight < 80;
}

function maybeScroll() {
  if (stick) $("messages").scrollTop = $("messages").scrollHeight;
}

// -- markdown (escape-first; never inject raw model output) ------------------

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

// Format a non-code text segment: blank-line paragraphs + "- " bullet lists.
function formatProse(segment) {
  const out = [];
  let list = [];
  let para = [];
  const flushList = () => {
    if (list.length) {
      out.push("<ul>" + list.map((x) => `<li>${inlineMd(x)}</li>`).join("") + "</ul>");
      list = [];
    }
  };
  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map(inlineMd).join("<br>")}</p>`);
      para = [];
    }
  };
  for (const line of segment.split("\n")) {
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      flushPara();
      list.push(li[1]);
    } else if (line.trim() === "") {
      flushPara();
      flushList();
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara();
  flushList();
  return out.join("");
}

function renderMarkdown(text) {
  try {
    // Escape first, then split on fenced code blocks. The capturing split yields
    // [text, lang, body, text, lang, body, …]; format text parts, keep code parts
    // verbatim (already escaped). No sentinels, no innerHTML of raw model output.
    const parts = escapeHtml(text).split(/```[^\n]*\n?([\s\S]*?)```/g);
    let html = "";
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        html += `<pre class="cell-code"><code>${part.replace(/\n+$/, "")}</code></pre>`;
      } else {
        html += formatProse(part);
      }
    });
    return html;
  } catch (_e) {
    return null; // caller falls back to textContent
  }
}

// -- messages ---------------------------------------------------------------

function addMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg msg-${role}`;
  el.textContent = text;
  $("messages").appendChild(el);
  maybeScroll();
  return el;
}

function showThinking() {
  const el = document.createElement("div");
  el.className = "msg msg-assistant thinking";
  el.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
  $("messages").appendChild(el);
  assistantEl = el;
  assistantRaw = "";
  maybeScroll();
}

// The bubble we stream into: reuse the thinking placeholder (clearing its dots)
// or start a fresh assistant bubble.
function streamingBubble() {
  if (assistantEl && assistantEl.classList.contains("thinking")) {
    assistantEl.classList.remove("thinking");
    assistantEl.innerHTML = "";
    assistantRaw = "";
  } else if (!assistantEl) {
    assistantEl = addMessage("assistant", "");
    assistantRaw = "";
  }
  return assistantEl;
}

function finalizeAssistant(text) {
  const el = streamingBubble();
  assistantRaw = text || assistantRaw;
  const html = renderMarkdown(assistantRaw);
  if (html === null) el.textContent = assistantRaw;
  else el.innerHTML = html;
  assistantEl = null; // next delta/message starts a new bubble
  maybeScroll();
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
    const { data } = await api("/api/ai/chat/apply", { sid, code });
    if (data.ok) {
      note.textContent = " added ✓";
      applyBtn.textContent = "Applied";
    } else {
      note.textContent = ` — ${data.error || "failed"}`;
      applyBtn.disabled = false;
    }
  });

  bar.append(label, applyBtn, note);
  wrap.append(bar, pre);
  $("messages").appendChild(wrap);
  maybeScroll();
}

// -- outbound-PII guard -----------------------------------------------------

let shownPii = new Set(); // finding-set signatures already surfaced this page-load

// Distinct, value-free kind labels (e.g. "payment card, email address").
function summarizeKinds(findings) {
  return [...new Set((findings || []).map((f) => f.kind))].join(", ");
}

// One-time banner at chat-open: the notebook/its schema looks like it has PII.
function showPiiBanner(items) {
  if (!items || !items.length) return;
  const sig = items.map((i) => `${i.where}|${i.kind}`).sort().join(";");
  if (shownPii.has(sig)) return; // don't re-nag on a model/dataset re-open
  shownPii.add(sig);
  const el = addMessage("assistant", "");
  el.className = "msg msg-assistant pii-notice";
  el.textContent =
    "Note: this notebook or its schema looks like it may contain " +
    summarizeKinds(items) +
    ". Schema columns that were themselves values have been withheld. Review the " +
    "notebook and avoid sending real values — this scan is best-effort, not a guarantee.";
}

// A held chat turn (block_prompt): nothing was sent; offer "Send anyway".
function addPiiHold(findings, token) {
  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant pii-hold";
  const p = document.createElement("p");
  p.textContent =
    "Held before sending — this message looks like it may contain " +
    summarizeKinds(findings) +
    ". Nothing was sent to the assistant. Never paste real values; send anyway only " +
    "if this is safe (e.g. a synthetic example).";
  const bar = document.createElement("div");
  bar.className = "toolbar";
  const sendBtn = document.createElement("button");
  sendBtn.className = "primary small";
  sendBtn.textContent = "Send anyway";
  const note = document.createElement("span");
  note.className = "muted";
  sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true;
    note.textContent = " sending…";
    assistantEl = null;
    setTurnState("thinking");
    showThinking();
    const { data } = await api("/api/ai/chat/send", { sid, confirm_token: token });
    if (data.error) {
      showError(data.error);
      setTurnState("error");
      sendBtn.disabled = false; // don't leave the hold card stuck on an error
      note.textContent = "";
    }
  });
  bar.append(sendBtn, note);
  wrap.append(p, bar);
  $("messages").appendChild(wrap);
  maybeScroll();
}

// A warn-only advisory (block_prompt off): the turn was already forwarded.
function addPiiNotice(findings) {
  const el = addMessage("assistant", "");
  el.className = "msg msg-assistant pii-notice";
  el.textContent =
    "Heads up: your message looks like it may contain " +
    summarizeKinds(findings) +
    ". It was sent — avoid pasting real values.";
}

// -- activity chip ----------------------------------------------------------

function setActivity(text) {
  const a = $("activity");
  a.textContent = text || "";
  a.classList.toggle("hidden", !text);
}

function clearActivity() {
  setActivity("");
}

// -- turn lifecycle ---------------------------------------------------------

function setTurnState(state) {
  turnState = state;
  const sending = state === "thinking" || state === "streaming";
  $("chat-send").disabled = sending;
  $("chat-input").disabled = sending;
  if (state === "idle" || state === "error") {
    // Drop an empty "thinking" placeholder if no tokens ever arrived.
    if (assistantEl && assistantEl.classList.contains("thinking")) {
      assistantEl.remove();
      assistantEl = null;
    }
    clearActivity();
    if (state === "idle") $("chat-input").focus();
  }
}

// -- session ----------------------------------------------------------------

function closeStream() {
  if (source) {
    source.close();
    source = null;
  }
}

function selectedEffort() {
  return $("effort-wrap").classList.contains("hidden") ? "" : $("chat-effort").value;
}

async function openChat() {
  closeStream();
  showError("");
  const dataset = $("chat-dataset").value;
  const model = $("chat-model").value;
  const reasoning_effort = selectedEffort();
  const { status, data } = await api("/api/ai/chat/open", {
    notebook: NOTEBOOK, dataset, model, reasoning_effort,
  });
  if (!data.sid) {
    showError(data.error || `Could not start the copilot (${status}).`);
    return;
  }
  sid = data.sid;
  source = new EventSource(`/api/ai/chat/stream/${sid}`);
  source.addEventListener("delta", (e) => {
    if (turnState === "thinking") setTurnState("streaming");
    const el = streamingBubble();
    assistantRaw += JSON.parse(e.data).text;
    el.textContent = assistantRaw; // fast plain text while streaming
    maybeScroll();
  });
  source.addEventListener("message", (e) => finalizeAssistant(JSON.parse(e.data).text));
  source.addEventListener("proposal", (e) => addProposal(JSON.parse(e.data).code));
  source.addEventListener("tool", (e) => {
    const d = JSON.parse(e.data);
    setActivity(d.progress || TOOL_LABELS[d.name] || "Working…");
  });
  source.addEventListener("tool_done", () => clearActivity());
  source.addEventListener("intent", (e) => setActivity(JSON.parse(e.data).text));
  source.addEventListener("idle", () => setTurnState("idle"));
  source.addEventListener("pii", (e) => {
    const d = JSON.parse(e.data);
    if (d.scan_error) {
      // Fail-open but loud: the guard could not run; the turn proceeds unchecked.
      showError("PII pre-flight scan could not run — your message was sent unchecked.");
      return;
    }
    const findings = d.findings || [];
    if (d.token) {
      setTurnState("idle"); // drop the thinking bubble; the turn is held
      addPiiHold(findings, d.token);
    } else if (findings.length) {
      addPiiNotice(findings); // advisory only; the turn was forwarded
    }
  });
  source.addEventListener("fail", (e) => {
    showError(JSON.parse(e.data).text || "The assistant reported an error.");
    setTurnState("error");
  });
  source.addEventListener("closed", () => setStatus("Session closed."));
  source.onerror = () => setStatus("Reconnecting…");
  showPiiBanner(data.pii);
  const bits = [];
  if (dataset) bits.push(`schema: ${dataset}`);
  if (model) bits.push(model + (reasoning_effort ? ` · ${reasoning_effort}` : ""));
  setStatus(bits.join("  ·  ") || "Notebook source in context.");
}

async function send() {
  if (turnState !== "idle") return;
  const input = $("chat-input");
  const text = input.value.trim();
  if (!sid || !text) return;
  stick = true;
  addMessage("user", text);
  input.value = "";
  input.style.height = "auto";
  assistantEl = null;
  setTurnState("thinking");
  showThinking();
  const { data } = await api("/api/ai/chat/send", { sid, text });
  if (data.error) {
    showError(data.error);
    setTurnState("error");
  }
}

// -- models / effort --------------------------------------------------------

function populateEfforts(preferDefault) {
  const model = MODELS.find((m) => m.id === $("chat-model").value);
  const sel = $("chat-effort");
  sel.innerHTML = "";
  const efforts = (model && model.efforts) || [];
  if (!efforts.length) {
    $("effort-wrap").classList.add("hidden");
    return;
  }
  $("effort-wrap").classList.remove("hidden");
  for (const e of efforts) {
    const o = document.createElement("option");
    o.value = e;
    o.textContent = e;
    sel.appendChild(o);
  }
  const saved = localStorage.getItem(LS_EFFORT);
  sel.value = efforts.includes(saved)
    ? saved
    : efforts.includes(preferDefault)
      ? preferDefault
      : (model && model.default_effort) || efforts[0];
}

async function loadModels() {
  const { data } = await api("/api/ai/models");
  MODELS = data.models || [];
  const sel = $("chat-model");
  sel.innerHTML = "";
  const wrap = sel.closest("label");
  if (!MODELS.length) {
    wrap.classList.add("hidden");
    $("effort-wrap").classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  for (const m of MODELS) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.name + (m.multiplier && m.multiplier > 1 ? ` · ${m.multiplier}×` : "");
    sel.appendChild(o);
  }
  const saved = localStorage.getItem(LS_MODEL);
  const wanted = [saved, data.default_model, MODELS[0].id].find((id) =>
    MODELS.some((m) => m.id === id),
  );
  sel.value = wanted;
  populateEfforts(data.default_effort);
}

// -- init -------------------------------------------------------------------

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
  setStatus("Loading models…");
  await loadModels();

  select.addEventListener("change", openChat);
  $("chat-model").addEventListener("change", () => {
    localStorage.setItem(LS_MODEL, $("chat-model").value);
    populateEfforts();
    openChat();
  });
  $("chat-effort").addEventListener("change", () => {
    localStorage.setItem(LS_EFFORT, $("chat-effort").value);
    openChat();
  });
  $("messages").addEventListener("scroll", () => {
    stick = isNearBottom();
  });
  $("chat-send").addEventListener("click", send);
  const input = $("chat-input");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  await openChat();
}

document.addEventListener("DOMContentLoaded", init);
