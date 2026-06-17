"use strict";

// The interactive AI copilot, rendered as a terminal-style REPL. Opens beside
// the marimo notebook tab. The model streams over SSE; proposed cells are
// Applied (appended + run) into the open notebook via the hub. mooring talks to
// marimo over HTTP only, never a websocket — outputs/values never reach this
// page or the model. Pure, DOM-free helpers live in chat_core.js (ChatCore).

const $ = (id) => document.getElementById(id);
const NOTEBOOK = new URLSearchParams(location.search).get("notebook") || "";
const LS_MODEL = "mooring.ai.model";
const LS_EFFORT = "mooring.ai.effort";

const TOOL_LABELS = {
  mooring_list_datasets: "listing datasets",
  mooring_get_schema: "looking up the schema",
  mooring_read_notebook_source: "reading the notebook",
  mooring_propose_cell: "drafting a cell",
  mooring_list_tables: "listing dictionary tables",
  mooring_describe_table: "describing a table",
  mooring_search_dictionary: "searching the dictionary",
};
const STATE_LABEL = { idle: "ready", thinking: "thinking…", streaming: "streaming…", error: "error" };

let sid = null;
let source = null; // EventSource
let turnState = "idle"; // idle | thinking | streaming | error
let stick = true; // auto-scroll only when the user is near the bottom
let MODELS = [];
let DATASETS = []; // value-free dataset paths, for @-mentions (from /api/state)

// per-turn render state
let asstRow = null; // the assistant row currently being streamed
let asstRaw = ""; // accumulated raw text for that row
let thinkRow = null; // the intent "thinking" line for this turn
let pendingRow = null; // transient "· thinking▋" indicator until real content
let toolStack = []; // open tool-call rows in this turn

let latestProposal = null; // { card, code, applyBtn, note, applied, skipped }
let lastUserText = ""; // for /retry
let currentGuard = null; // outbound-PII guard status for this session (topbar badge)
const history = new ChatCore.HistoryRing(); // in-memory ONLY (never persisted)

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

let nerHideTimer = null;
function setNerStatus(text, { error = false, transient = false } = {}) {
  const el = $("ner-status");
  if (nerHideTimer) {
    clearTimeout(nerHideTimer);
    nerHideTimer = null;
  }
  el.textContent = text || "";
  el.classList.toggle("ner-error", !!error);
  el.classList.toggle("hidden", !text);
  if (text && transient) {
    nerHideTimer = setTimeout(() => el.classList.add("hidden"), 4000);
  }
}

// Paint the topbar PII-guard badge from the session's guard status (green when
// the outbound scan is active, red when off). Re-rendered when the NER model
// becomes ready/unavailable so the "names" detail stays truthful mid-session.
function setPiiBadge(guard) {
  const el = $("pii-badge");
  if (!el) return;
  const b = ChatCore.piiBadge(guard);
  if (!b) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = b.text;
  el.title = b.title;
  el.classList.remove("hidden", "synced", "danger");
  el.classList.add(b.cls === "on" ? "synced" : "danger");
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
// KEPT BYTE-FOR-BYTE. escapeHtml/inlineMd/formatProse/renderMarkdown are the
// XSS-safe renderer the whole value-blind posture leans on; do not "improve"
// them. highlightCode (chat_core.js) is only ever fed escapeHtml(...) output.

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

// -- transcript rows --------------------------------------------------------

// Append a transcript row. `build` is either a plain string (set as textContent,
// always safe) or a builder that populates the element with DOM nodes.
function addRow(cls, build) {
  const el = document.createElement("div");
  el.className = "row " + cls;
  if (typeof build === "string") el.textContent = build;
  else if (build) build(el);
  $("messages").appendChild(el);
  maybeScroll();
  return el;
}

function addUserRow(text) {
  return addRow("row-user", (el) => {
    const g = document.createElement("span");
    g.className = "row-gutter";
    g.textContent = ">";
    const t = document.createElement("span");
    t.className = "row-text";
    t.textContent = text;
    el.append(g, t);
  });
}

function addSysRow(text) {
  return addRow("row-sys", text); // textContent — safe
}

// -- pending / streaming / thinking -----------------------------------------

function showPending() {
  clearPending();
  pendingRow = addRow("row-think pending stream-cursor", (el) => {
    el.appendChild(document.createTextNode("thinking"));
  });
}

function clearPending() {
  if (pendingRow) {
    pendingRow.remove();
    pendingRow = null;
  }
}

function streamingRow() {
  if (!asstRow) {
    asstRow = addRow("row-assistant streaming stream-cursor", "");
    asstRaw = "";
  }
  return asstRow;
}

function appendDelta(text) {
  const el = streamingRow();
  asstRaw += text;
  el.textContent = asstRaw; // fast plain text while streaming
  maybeScroll();
}

function finalizeAssistant(text) {
  const el = streamingRow();
  asstRaw = text || asstRaw;
  el.classList.remove("streaming", "stream-cursor");
  const html = renderMarkdown(asstRaw);
  if (html === null) el.textContent = asstRaw;
  else el.innerHTML = html;
  asstRow = null; // next delta/message starts a new row
  maybeScroll();
}

function onIntent(text) {
  clearPending();
  if (!text) return;
  if (thinkRow) {
    thinkRow.querySelector(".think-text").textContent = text;
  } else {
    thinkRow = addRow("row-think", (el) => {
      const s = document.createElement("span");
      s.className = "think-text";
      s.textContent = text;
      el.append(s);
    });
  }
}

// -- inline tool-call lines -------------------------------------------------

function onTool(d) {
  clearPending();
  // Distinguish a tool START (carries a "name" key, possibly "") from a PROGRESS
  // (carries "progress"). A START must ALWAYS push a row — even with an empty
  // name — so the matching tool_done pops the right one instead of finalizing a
  // still-running tool's line.
  if ("name" in d) {
    const raw = d.name || "";
    const label = TOOL_LABELS[raw] ||
      (raw ? raw.replace(/^mooring_/, "").replace(/_/g, " ") : "working");
    const row = addRow("row-tool", (el) => {
      const g = document.createElement("span");
      g.className = "tool-glyph";
      g.textContent = "⏵"; // ⏵
      const l = document.createElement("span");
      l.className = "tool-label";
      l.textContent = label + "…";
      el.append(g, l);
    });
    row._detail = "";
    toolStack.push(row);
  } else if (d.progress) {
    const row = toolStack[toolStack.length - 1];
    if (row) {
      row._detail = d.progress;
      makeExpandable(row);
    }
  }
}

function onToolDone(success) {
  const row = toolStack.pop();
  if (!row) return;
  row.classList.add(success ? "ok" : "fail");
  const glyph = row.querySelector(".tool-glyph");
  if (glyph) glyph.textContent = success ? "⏺" : "✗"; // ⏺ : ✗
}

// Make a finished/progressing tool line click-to-expand its one-line detail.
function makeExpandable(row) {
  if (row._expandable) return;
  row._expandable = true;
  row.classList.add("expandable");
  row.title = "click to show detail";
  row.addEventListener("click", () => {
    if (row._detailEl) {
      row._detailEl.remove();
      row._detailEl = null;
    } else {
      const d = document.createElement("div");
      d.className = "tool-detail";
      d.textContent = row._detail || "";
      row.appendChild(d);
      row._detailEl = d;
    }
  });
}

// -- proposed cell (additive block + apply/skip) ----------------------------

function addProposal(code, rationale) {
  clearPending();
  const card = document.createElement("div");
  card.className = "proposal-card";

  const head = document.createElement("div");
  head.className = "proposal-head";
  head.appendChild(document.createTextNode("◆ proposed cell → ")); // ◆ … →
  const tn = document.createElement("span");
  tn.className = "target";
  tn.textContent = NOTEBOOK;
  head.appendChild(tn);
  card.appendChild(head);

  if (rationale && rationale.trim()) {
    const r = document.createElement("div");
    r.className = "proposal-rationale";
    r.textContent = rationale.trim();
    card.appendChild(r);
  }

  const body = document.createElement("div");
  body.className = "proposal-body";
  for (const line of ChatCore.additiveBlockLines(code)) {
    const ln = document.createElement("div");
    ln.className = "add-line";
    const g = document.createElement("span");
    g.className = "add-gutter";
    g.textContent = line.gutter;
    const c = document.createElement("span");
    c.className = "add-code";
    // escape-first, THEN highlight — highlightCode never emits unescaped source.
    c.innerHTML = ChatCore.highlightCode(escapeHtml(line.text)) || "&nbsp;";
    ln.append(g, c);
    body.appendChild(ln);
  }
  card.appendChild(body);

  const actions = document.createElement("div");
  actions.className = "proposal-actions";
  const applyBtn = document.createElement("button");
  applyBtn.className = "primary small";
  applyBtn.textContent = "Apply ▸"; // ▸
  const skipBtn = document.createElement("button");
  skipBtn.className = "small";
  skipBtn.textContent = "Skip";
  const copyBtn = document.createElement("button");
  copyBtn.className = "small";
  copyBtn.textContent = "Copy";
  const note = document.createElement("span");
  note.className = "muted";
  const hint = document.createElement("span");
  hint.className = "muted";
  hint.textContent = "appends a cell · keys: a apply, s skip";

  const prop = { card, code, applyBtn, skipBtn, note, applied: false, skipped: false };
  applyBtn.addEventListener("click", () => applyProposal(prop));
  skipBtn.addEventListener("click", () => skipProposal(prop));
  copyBtn.addEventListener("click", () => copyCode(code, note));

  actions.append(applyBtn, skipBtn, copyBtn, note, hint);
  card.appendChild(actions);
  $("messages").appendChild(card);
  maybeScroll();
  latestProposal = prop;
}

async function applyProposal(p) {
  if (!p || p.applied || p.skipped) return;
  p.applyBtn.disabled = true;
  p.note.textContent = " applying…";
  const { data } = await api("/api/ai/chat/apply", { sid, code: p.code });
  if (data.ok) {
    p.applied = true;
    p.applyBtn.textContent = "Applied";
    p.applyBtn.classList.add("applied");
    p.skipBtn.disabled = true;
    p.note.textContent = " added ✓"; // ✓
  } else {
    p.note.textContent = " — " + (data.error || "failed");
    p.applyBtn.disabled = false;
  }
}

function skipProposal(p) {
  if (!p || p.applied || p.skipped) return;
  p.skipped = true;
  p.applyBtn.disabled = true;
  p.skipBtn.disabled = true;
  p.card.style.opacity = "0.55";
  p.note.textContent = " skipped";
  if (latestProposal === p) latestProposal = null;
}

function copyCode(code, note) {
  const done = () => {
    if (note) note.textContent = " copied";
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(code).then(done, () => {});
  }
}

// -- outbound-PII guard -----------------------------------------------------

let shownPii = new Set(); // finding-set signatures already surfaced this page-load

function summarizeKinds(findings) {
  return [...new Set((findings || []).map((f) => f.kind))].join(", ");
}

function showPiiBanner(items) {
  if (!items || !items.length) return;
  const sig = items.map((i) => `${i.where}|${i.kind}`).sort().join(";");
  if (shownPii.has(sig)) return; // don't re-nag on a model/dataset re-open
  shownPii.add(sig);
  const el = addRow("row-sys row-pii", "");
  el.textContent =
    "Note: this notebook or its schema looks like it may contain " +
    summarizeKinds(items) +
    ". Schema columns that were themselves values have been withheld. Review the " +
    "notebook and avoid sending real values — this scan is best-effort, not a guarantee.";
}

// A held chat turn (block_prompt): nothing was sent; offer "Send anyway".
function addPiiHold(findings, token) {
  const wrap = addRow("row-sys row-pii", "");
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
    startTurnState();
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
  maybeScroll();
}

// A warn-only advisory (block_prompt off): the turn was already forwarded.
function addPiiNotice(findings) {
  const el = addRow("row-sys row-pii", "");
  el.textContent =
    "Heads up: your message looks like it may contain " +
    summarizeKinds(findings) +
    ". It was sent — avoid pasting real values.";
}

// -- turn lifecycle ---------------------------------------------------------

// Reset per-turn render state and show the thinking indicator.
function startTurn() {
  asstRow = null;
  asstRaw = "";
  thinkRow = null;
  toolStack = [];
  startTurnState();
}

function startTurnState() {
  setTurnState("thinking");
  showPending();
}

function isBusy() {
  return turnState === "thinking" || turnState === "streaming";
}

function setTurnState(state) {
  turnState = state;
  const busy = state === "thinking" || state === "streaming";
  $("chat-input").disabled = busy;
  setStatus(STATE_LABEL[state] || state);
  if (state === "idle" || state === "error") {
    clearPending();
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
  const model = $("chat-model").value;
  const reasoning_effort = selectedEffort();
  const { status, data } = await api("/api/ai/chat/open", {
    notebook: NOTEBOOK, model, reasoning_effort,
  });
  if (!data.sid) {
    showError(data.error || `Could not start the copilot (${status}).`);
    return;
  }
  sid = data.sid;
  source = new EventSource(`/api/ai/chat/stream/${sid}`);
  source.addEventListener("delta", (e) => {
    if (turnState === "thinking") setTurnState("streaming");
    clearPending();
    appendDelta(JSON.parse(e.data).text);
  });
  source.addEventListener("message", (e) => finalizeAssistant(JSON.parse(e.data).text));
  source.addEventListener("proposal", (e) => {
    const d = JSON.parse(e.data);
    addProposal(d.code, d.rationale);
  });
  source.addEventListener("tool", (e) => onTool(JSON.parse(e.data)));
  source.addEventListener("tool_done", (e) => onToolDone(JSON.parse(e.data).success !== false));
  source.addEventListener("intent", (e) => onIntent(JSON.parse(e.data).text));
  source.addEventListener("idle", () => setTurnState("idle"));
  source.addEventListener("pii", (e) => {
    const d = JSON.parse(e.data);
    const findings = d.findings || [];
    if (d.token) {
      setTurnState("idle"); // drop the thinking indicator; the turn is held
      addPiiHold(findings, d.token); // hold wins, even if a scan also errored
      return;
    }
    if (findings.length) addPiiNotice(findings); // advisory only; the turn was forwarded
    if (d.scan_error) {
      // Fail-open but loud: a configured scan could not run; the turn went unchecked.
      showError("PII pre-flight scan could not run — your message was sent unchecked.");
    }
  });
  source.addEventListener("ner", (e) => {
    const d = JSON.parse(e.data);
    if (d.state === "downloading") {
      const pct = typeof d.pct === "number" ? ` ${d.pct}%` : "";
      setNerStatus(`preparing name-detection model…${pct}`);
    } else if (d.state === "ready") {
      setNerStatus("name detection ready", { transient: true });
      if (currentGuard) {
        currentGuard.names_active = true;
        setPiiBadge(currentGuard); // badge tooltip now reflects that names are scanned
      }
    } else if (d.state === "error") {
      setNerStatus("name-detection model unavailable — scanned without it", { error: true });
      if (currentGuard) {
        currentGuard.names_active = false;
        setPiiBadge(currentGuard);
      }
    }
  });
  source.addEventListener("fail", (e) => {
    showError(JSON.parse(e.data).text || "The assistant reported an error.");
    setTurnState("error");
  });
  source.addEventListener("closed", () => setStatus("closed"));
  source.onerror = () => setStatus("reconnecting…");
  currentGuard = data.guard || null;
  setPiiBadge(currentGuard);
  showPiiBanner(data.pii);
  setTurnState("idle");
}

// -- composer: send / commands / history ------------------------------------

function autosize(input) {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function resetInput(input) {
  input.value = "";
  autosize(input);
  closeAutocomplete();
}

function moveCaretEnd(input) {
  const n = input.value.length;
  input.setSelectionRange(n, n);
}

async function send() {
  if (isBusy()) return; // idle OR error may send (don't get stuck after a failure)
  const input = $("chat-input");
  const raw = input.value;
  const trimmed = raw.trim();
  if (!sid || !trimmed) return;
  closeAutocomplete();

  const cmd = ChatCore.parseSlash(trimmed);
  if (cmd) {
    resetInput(input);
    runCommand(cmd);
    return;
  }

  const message = ChatCore.unescapeSlash(raw).trim();
  history.push(message);
  resetInput(input);
  stick = true;
  await submitMessage(message);
}

async function submitMessage(message) {
  lastUserText = message;
  addUserRow(message);
  startTurn();
  const { data } = await api("/api/ai/chat/send", { sid, text: message });
  if (data.error) {
    showError(data.error);
    setTurnState("error");
  }
}

function runCommand(cmd) {
  switch (cmd.cmd) {
    case "help":
      printHelp();
      break;
    case "clear":
      $("messages").innerHTML = "";
      latestProposal = null;
      printBanner();
      break;
    case "model":
      handleModelCommand(cmd.arg);
      break;
    case "apply":
      if (latestProposal && !latestProposal.applied && !latestProposal.skipped) {
        applyProposal(latestProposal);
      } else {
        addSysRow("No proposed cell to apply.");
      }
      break;
    case "diff":
      if (latestProposal) latestProposal.card.scrollIntoView({ block: "center", behavior: "smooth" });
      else addSysRow("No proposed cell yet.");
      break;
    case "retry":
      if (isBusy()) addSysRow("Wait for the current turn to finish.");
      else if (lastUserText) { stick = true; submitMessage(lastUserText); }
      else addSysRow("Nothing to resend yet.");
      break;
    case "":
      addSysRow("Type a command after “/”. Try /help.");
      break;
    default:
      addSysRow(`Unknown command: /${cmd.cmd}. Try /help.`);
  }
}

function handleModelCommand(arg) {
  if (!arg) {
    const cur = $("chat-model").value;
    addSysRow(
      "Models: " + MODELS.map((m) => m.id + (m.id === cur ? " (current)" : "")).join(", ") +
      "\nSwitch with /model <name>."
    );
    return;
  }
  const q = arg.toLowerCase();
  const hit = MODELS.find((m) => m.id.toLowerCase() === q) ||
    MODELS.find((m) => m.id.toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q));
  if (!hit) {
    addSysRow(`No model matches “${arg}”. Try /model with no argument to list them.`);
    return;
  }
  const sel = $("chat-model");
  sel.value = hit.id;
  localStorage.setItem(LS_MODEL, hit.id);
  populateEfforts();
  addSysRow(`— switching to ${hit.id} (reopening session) —`);
  openChat();
}

function printBanner() {
  addRow("row-sys", (el) => {
    const a = document.createElement("div");
    const b = document.createElement("b");
    b.textContent = "mooring copilot · schema-only.";
    a.appendChild(b);
    a.appendChild(
      document.createTextNode(
        " The assistant sees this notebook's code and the schema (column names & types) " +
        "of your datasets and loaded dataframes — never the data itself. It looks schemas up " +
        "on its own; just ask."
      )
    );
    const c = document.createElement("div");
    c.textContent = "Type /help for commands. Don't paste real values into a cell or this chat.";
    el.append(a, c);
  });
}

function printHelp() {
  const rows = [
    ["/help", "show this help"],
    ["/clear", "clear the transcript (keeps the session)"],
    ["/model [name]", "list or switch the model"],
    ["/apply", "apply the latest proposed cell"],
    ["/diff", "jump to the latest proposed cell"],
    ["/retry", "resend your last message"],
  ];
  addRow("row-sys", (el) => {
    el.appendChild(document.createTextNode("Commands:"));
    for (const [c, d] of rows) {
      const li = document.createElement("div");
      const cs = document.createElement("b");
      cs.textContent = c;
      li.append(document.createTextNode("  "), cs, document.createTextNode("  — " + d));
      el.appendChild(li);
    }
    const k = document.createElement("div");
    k.textContent =
      "Keys: Enter send · Shift+Enter newline · ↑/↓ recall input · @ reference a dataset · " +
      "a/s apply or skip a proposal (when the prompt is empty/unfocused) · Esc clear / close menu";
    el.appendChild(k);
  });
}

// -- autocomplete (slash commands + @-mentions) -----------------------------

let acItems = []; // [{name, help, kind, insert, mention?}]
let acIndex = 0;

function openAutocomplete(items) {
  acItems = items;
  acIndex = 0;
  const box = $("autocomplete");
  box.innerHTML = "";
  items.forEach((it, i) => {
    const row = document.createElement("div");
    row.className = "ac-item" + (i === 0 ? " active" : "");
    const n = document.createElement("span");
    n.className = "ac-name";
    n.textContent = it.name;
    const h = document.createElement("span");
    h.className = "ac-help";
    h.textContent = it.help || "";
    row.append(n, h);
    row.addEventListener("mousedown", (e) => {
      e.preventDefault(); // keep focus in the textarea
      acIndex = i;
      acceptAutocomplete($("chat-input"));
    });
    box.appendChild(row);
  });
  box.classList.remove("hidden");
}

function closeAutocomplete() {
  acItems = [];
  acIndex = 0;
  $("autocomplete").classList.add("hidden");
}

function moveAc(delta) {
  if (!acItems.length) return;
  acIndex = (acIndex + delta + acItems.length) % acItems.length;
  const box = $("autocomplete");
  [...box.children].forEach((c, i) => c.classList.toggle("active", i === acIndex));
  const active = box.children[acIndex];
  if (active) active.scrollIntoView({ block: "nearest" });
}

function acceptAutocomplete(input) {
  const it = acItems[acIndex];
  if (!it) return;
  if (it.kind === "slash") {
    input.value = it.insert;
    moveCaretEnd(input);
  } else if (it.kind === "mention") {
    const m = it.mention;
    const caret = input.selectionStart;
    input.value = ChatCore.applyMention(input.value, m.start, caret, it.insert);
    const pos = m.start + it.insert.length + 2; // "@<path> "
    input.setSelectionRange(pos, pos);
  }
  autosize(input);
  closeAutocomplete();
}

function updateAutocomplete(input) {
  const val = input.value;
  const caret = input.selectionStart;
  if (ChatCore.isSlashTyping(val)) {
    const items = ChatCore.filterCommands(val.slice(1)).map((c) => ({
      kind: "slash", name: "/" + c.name, help: c.help, insert: "/" + c.name + " ",
    }));
    if (items.length) { openAutocomplete(items); return; }
  }
  const mm = ChatCore.mentionMatch(val, caret);
  if (mm) {
    const items = ChatCore.filterDatasets(DATASETS, mm.query).map((d) => ({
      kind: "mention", name: "@" + d, help: "dataset", insert: d, mention: mm,
    }));
    if (items.length) { openAutocomplete(items); return; }
  }
  closeAutocomplete();
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

async function loadDatasets() {
  // Value-free: /api/state returns dataset PATHS only (schema.list_datasets) — no
  // values. Used solely to power @-mention autocomplete; the inserted token is
  // plain text that still passes the outbound PII gate when sent.
  try {
    const { data } = await api("/api/state");
    DATASETS = data.datasets || [];
  } catch (_e) {
    DATASETS = [];
  }
}

// -- init -------------------------------------------------------------------

async function init() {
  $("chat-target").textContent = NOTEBOOK || "(no notebook)";
  if (!NOTEBOOK) {
    showError("Open the copilot from a notebook's “AI” button.");
    return;
  }
  setStatus("loading…");
  printBanner();
  await Promise.all([loadModels(), loadDatasets()]);

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

  const input = $("chat-input");
  input.addEventListener("input", () => {
    autosize(input);
    updateAutocomplete(input);
  });
  input.addEventListener("keydown", onInputKeydown);
  // a/s apply/skip the latest proposal — only when the prompt isn't focused, so
  // they never hijack typing.
  document.addEventListener("keydown", onGlobalKeydown);

  await openChat();
}

function onInputKeydown(e) {
  const input = e.currentTarget;
  if (acItems.length) {
    if (e.key === "ArrowDown") { e.preventDefault(); moveAc(1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); moveAc(-1); return; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); acceptAutocomplete(input); return; }
    if (e.key === "Escape") { e.preventDefault(); closeAutocomplete(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
    return;
  }
  if (e.key === "Escape") {
    // Esc clears the draft — it does NOT interrupt the turn (there is no backend
    // cancel; pretending otherwise would silently leave a billed turn running).
    e.preventDefault();
    if (input.value) resetInput(input);
    return;
  }
  // ↑ recalls older input. Require caret-at-start to BEGIN navigating, but once
  // navigating (cursor !== -1) keep stepping regardless of caret — moveCaretEnd
  // parks the caret at the end, which would otherwise stall the second press.
  if (
    e.key === "ArrowUp" &&
    (history.cursor !== -1 || (input.selectionStart === 0 && input.selectionEnd === 0))
  ) {
    const v = history.prev(input.value);
    if (v !== null) {
      e.preventDefault();
      input.value = v;
      autosize(input);
      moveCaretEnd(input);
    }
    return;
  }
  if (
    e.key === "ArrowDown" &&
    (history.cursor !== -1 || input.selectionStart === input.value.length)
  ) {
    const v = history.next();
    if (v !== null) {
      e.preventDefault();
      input.value = v;
      autosize(input);
      moveCaretEnd(input);
    }
  }
}

function onGlobalKeydown(e) {
  // a/s apply/skip the latest proposal — but never when an interactive control
  // has focus (the prompt, the model/effort <select> type-ahead, a button, …),
  // so they can't hijack normal keyboard use of those controls.
  const ae = document.activeElement;
  const tag = ae && ae.tagName;
  if (
    tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON" ||
    (ae && ae.isContentEditable)
  ) {
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (!latestProposal || latestProposal.applied || latestProposal.skipped) return;
  if (e.key === "a") {
    e.preventDefault();
    applyProposal(latestProposal);
  } else if (e.key === "s") {
    e.preventDefault();
    skipProposal(latestProposal);
  }
}

document.addEventListener("DOMContentLoaded", init);
