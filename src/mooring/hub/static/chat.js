"use strict";

// The interactive AI copilot, rendered as a terminal-style REPL. Opens beside
// the marimo notebook tab. The model streams over SSE; proposals (append a cell,
// edit a cell, or rewrite the notebook) are reviewed and Applied into the open
// notebook via the hub, and the last Apply can be Undone. mooring talks to marimo
// over HTTP only, never a websocket — outputs/values never reach this page or the
// model. Pure, DOM-free helpers live in chat_core.js (ChatCore).

const $ = (id) => document.getElementById(id);
const NOTEBOOK = new URLSearchParams(location.search).get("notebook") || "";
// The hub's "Explain" action opens this window with &explain=1: run /explain
// automatically once the session is ready (see maybeAutoExplain). "Review logic"
// opens the same window with &review=1 and auto-runs /review the same way.
const EXPLAIN = new URLSearchParams(location.search).get("explain") === "1";
const REVIEW = new URLSearchParams(location.search).get("review") === "1";
// Notebook choices are browser-local overrides, scoped by the server's opaque
// workspace id and the normalised notebook path. Empty means "inherit Settings";
// PROVIDER keeps each notebook's effort override distinct across providers.
let PROVIDER = "";
let PREFERENCE_SCOPE = "local";
let DEFAULT_MODEL = "";
let DEFAULT_EFFORT = "";
const notebookStore = (field) => ChatCore.notebookPreferenceKey(PREFERENCE_SCOPE, NOTEBOOK, field);
const modelStore = () => notebookStore("general_model");
const trustedModelStore = () => notebookStore("trusted_model");
const routingStore = () => notebookStore("routing_preference");
const effortStore = () => notebookStore(ChatCore.effortKey(PROVIDER));

function browserStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function saveNotebookOverride(key, value) {
  return ChatCore.safeStorageSet(browserStorage(), key, value);
}

function readNotebookOverride(key) {
  return ChatCore.safeStorageGet(browserStorage(), key);
}

function readValidNotebookOverride(key, choose) {
  const saved = readNotebookOverride(key);
  const selected = choose(saved);
  if (saved && !selected) saveNotebookOverride(key, "");
  return selected;
}

function rememberNotebookOverride(key, value, select) {
  if (saveNotebookOverride(key, value)) return value;
  select.value = "";
  addSysRow("Browser storage is unavailable; this chat will use the Settings default.");
  return "";
}
// Appearance is owned by the shared theme.js module (loaded before this file):
// it follows the hub's /api/state theme and re-themes this window live on a
// cross-tab change. Alias applyTheme for the /api/state follow below.
const applyTheme = window.MooringTheme.applyTheme;

const TOOL_LABELS = {
  mooring_list_datasets: "listing datasets",
  mooring_get_schema: "looking up the schema",
  mooring_read_notebook_source: "reading the notebook",
  mooring_propose_notebook_edit: "drafting changes",
  mooring_list_tables: "listing dictionary tables",
  mooring_describe_table: "describing a table",
  mooring_search_dictionary: "searching the dictionary",
  mooring_investigate: "investigating",
  mooring_get_semantic_model: "reading the semantic model",
  mooring_describe_model_table: "describing a model table",
  mooring_get_measure: "fetching a measure's DAX",
};
const STATE_LABEL = {
  idle: "ready",
  connecting: "connecting…", // the copilot session is still starting (handshake)
  thinking: "thinking…",
  streaming: "streaming…",
  error: "error",
  unavailable: "unavailable",
};

let sid = null;
let source = null; // EventSource
let turnState = "idle"; // idle | thinking | streaming | error
let stick = true; // auto-scroll only when the user is near the bottom
let MODELS = [];
let ROUTING = null; // safe picker metadata only; endpoint/key/classifier never reach this page
let DATASETS = []; // value-free dataset paths, for @-mentions (from /api/state)

// per-turn render state
let asstRow = null; // the assistant row currently being streamed
let asstRaw = ""; // accumulated raw text for that row
let thinkRow = null; // the intent "thinking" line for this turn
let pendingRow = null; // transient "· thinking▋" indicator until real content
let toolStack = []; // open tool-call rows in this turn

let latestProposal = null; // { card, kind, ops, copyText, applyBtn, note, applied, skipped }
let lastUndoBtn = null; // the single visible "Undo" button (the last applied change)
let lastUserText = ""; // for /retry
let lastUserLabel = ""; // its compact visible label (so /retry re-shows it too)
let explainFired = false; // &explain=1 auto-runs at most ONCE per window — openChat()
// is re-invoked on model/effort switches, sign-in, and AI re-enable, and none of
// those may silently burn a second explain turn.
let reviewFired = false; // &review=1 auto-runs /review at most ONCE per window (same reason)
let explainTurnActive = false; // the turn answering /explain (offers "Add as notes cell")
let currentGuard = null; // outbound-PII guard status for this session (topbar badge)
const history = new ChatCore.HistoryRing(); // in-memory ONLY (never persisted)
const openGate = ChatCore.latestRequestGate();
let openController = null;

async function api(path, body, { signal } = {}) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  if (signal) opts.signal = signal;
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
  el.classList.remove("hidden", "synced", "danger", "warn");
  el.classList.add({ on: "synced", partial: "warn" }[b.cls] || "danger");
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
// The XSS-safe assistant-prose renderer now lives in chat_core.js
// (ChatCore.renderMarkdown) so it can be unit-tested under Node — including the
// value-blind XSS contract. It escapes ALL model text first, then only ever
// splices in mooring's own allow-listed tags (never innerHTML of raw output).
// escapeHtml stays HERE for the code/diff line builder (addCodeLine), which
// feeds ChatCore.highlightCode already-escaped source.

function escapeHtml(s) {
  return s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
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
  const html = ChatCore.renderMarkdown(asstRaw);
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
      (raw ? raw.replace(/^mooring_/, "").replaceAll("_", " ") : "working");
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
    row._baseLabel = label;
    toolStack.push(row);
  } else if (d.progress) {
    const row = toolStack.at(-1);
    if (row) {
      row._detail = d.progress;
      // Surface progress INLINE, not only behind the click-to-expand detail: a fan-out
      // ("investigating") blocks the turn for as long as its slowest branch, so a static
      // line would read as a hang.
      const label = row.querySelector(".tool-label");
      if (label && row._baseLabel) label.textContent = `${row._baseLabel} · ${d.progress}`;
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

// -- proposed change (append = additive block; edit/rewrite = diff) ----------

// Static per-kind chrome. "append" keeps the original additive framing; the rest
// render a real old→new diff (the model's edit/rewrite REPLACES existing source).
const PROPOSAL_KIND = {
  append: { head: "◆ proposed cell → ", hint: "appends a cell" },
  edit: { head: "✎ proposed edit → ", hint: "edits a cell" },
  patch: { head: "✎ proposed changes → ", hint: "edits the notebook" },
  rewrite: { head: "↻ proposed rewrite → ", hint: "rewrites the notebook" },
};

// One source/diff line: escape-first, THEN highlight — highlightCode never emits
// unescaped source. `lineClass` is add-line | del-line | ctx-line (styled in CSS).
function addCodeLine(container, gutter, text, lineClass) {
  const ln = document.createElement("div");
  ln.className = lineClass;
  const g = document.createElement("span");
  g.className = "add-gutter";
  g.textContent = gutter;
  const c = document.createElement("span");
  c.className = "add-code";
  c.innerHTML = ChatCore.highlightCode(escapeHtml(text)) || "&nbsp;";
  ln.append(g, c);
  container.appendChild(ln);
}

const GUTTER_CLASS = { "+": "add-line", "-": "del-line", " ": "ctx-line" };

// `d` is the proposal SSE payload: {kind, rationale, code?, ops?, diffs?}. A bare
// {code, rationale} (the append proposal, and the stub) defaults to kind "append".
function addProposal(d) {
  clearPending();
  const kind = d?.kind || "append";
  const meta = PROPOSAL_KIND[kind] || PROPOSAL_KIND.append;
  const card = document.createElement("div");
  card.className = "proposal-card" + (kind === "append" ? "" : " proposal-edit");

  const head = document.createElement("div");
  head.className = "proposal-head";
  head.appendChild(document.createTextNode(meta.head));
  const tn = document.createElement("span");
  tn.className = "target";
  tn.textContent = NOTEBOOK;
  head.appendChild(tn);
  card.appendChild(head);

  if (d.rationale?.trim()) {
    const r = document.createElement("div");
    r.className = "proposal-rationale";
    r.textContent = d.rationale.trim();
    card.appendChild(r);
  }

  const body = document.createElement("div");
  body.className = "proposal-body";
  let ops;
  let copyText;
  if (kind === "append") {
    const code = d.code || "";
    ops = [{ op: "append", code }];
    copyText = code;
    for (const line of ChatCore.additiveBlockLines(code)) {
      addCodeLine(body, line.gutter, line.text, "add-line");
    }
  } else {
    ops = d.ops || [];
    const diffs = d.diffs || [];
    // Copy the new source; for a delete (after === "") copy the removed source so Copy
    // is still meaningful instead of copying an empty string.
    copyText = diffs.map((s) => s.after || s.before).filter(Boolean).join("\n\n");
    diffs.forEach((sec) => {
      const section = document.createElement("div");
      section.className = "diff-section";
      if (sec.label) {
        const lab = document.createElement("div");
        lab.className = "diff-label";
        lab.textContent = sec.label;
        section.appendChild(lab);
      }
      for (const line of ChatCore.diffLines(sec.before, sec.after)) {
        addCodeLine(section, line.gutter, line.text, GUTTER_CLASS[line.gutter] || "ctx-line");
      }
      body.appendChild(section);
    });
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
  hint.textContent = meta.hint + " · keys: a apply, s skip";

  // `held`/`holdRow`: set when the apply gate answers 428 (see holdProposal). While
  // held, the ONLY route to an apply is the hold row's own confirm button.
  // `fixTried` counts the corrective re-proposals this proposal has asked for (bounded
  // by ChatCore.MAX_FIX_ATTEMPTS); `noFix` marks a refusal re-proposing cannot answer.
  const prop = {
    card, kind, ops, copyText, applyBtn, skipBtn, note,
    applied: false, skipped: false, held: false, holdRow: null,
    fixTried: 0, noFix: false,
  };
  applyBtn.addEventListener("click", () => applyProposal(prop));
  skipBtn.addEventListener("click", () => skipProposal(prop));
  copyBtn.addEventListener("click", () => copyCode(copyText, note));

  actions.append(applyBtn, skipBtn, copyBtn, note, hint);
  card.appendChild(actions);
  $("messages").appendChild(card);
  maybeScroll();
  // Dim the previous still-pending card so it's clear which proposal is current (it
  // stays applicable; the apply path's anchor re-check is the real safety net).
  if (latestProposal && !latestProposal.applied && !latestProposal.skipped) {
    latestProposal.card.classList.add("superseded");
  }
  latestProposal = prop;
}

// `gateToken` is passed ONLY by the hold card's confirm button (addGateHold): it
// re-POSTs the identical body plus the token the 428 handed us. The server
// re-scans and re-derives that token, so this argument can only ever unlock the
// exact change that was shown — never a different one, and never a stale one.
async function applyProposal(p, gateToken) {
  if (!p || p.applied || p.skipped) return;
  // A held proposal has exactly one way forward: its own hold card. The card's
  // Apply button is disabled, but /apply and the `a` key reach here too.
  if (p.held && !gateToken) return;
  p.applyBtn.disabled = true;
  p.note.textContent = " applying…";
  const body = { sid, ops: p.ops };
  if (gateToken) body.gate_token = gateToken;
  const { status, data } = await api("/api/ai/chat/apply", body);
  if (data.reason === "notebook_disabled") {
    // AI was turned off for this notebook (here, the hub, or a teammate's sync)
    // before the apply landed — lock the window instead of "asking the AI to fix".
    p.note.textContent = " — AI is off for this notebook";
    lockForDisabled();
    return;
  }
  if (status === 428) {
    // The apply gate held it (HTTP 428 Precondition Required — 409 already means a
    // staleness conflict on this endpoint). NOT an error: nothing was written, and
    // the analyst gets the decision. A 428 on a CONFIRMED re-POST means the token no
    // longer matches — the code, the findings, or the notebook underneath moved — so
    // render the NEW hold rather than failing silently.
    const gate = ChatCore.gateFromResponse(data);
    if (gate) {
      holdProposal(p, gate);
      return;
    }
    // A 428 we can't read is still a refusal — fall through to the error path below,
    // but never to askAiToFix (re-proposing wouldn't answer a gate). This must not
    // CONSUME a fix attempt either: the model didn't get anything wrong here.
    p.noFix = true;
  }
  if (data.ok) {
    p.applied = true;
    p.held = false;
    p.applyBtn.textContent = "Applied";
    // A held card dropped the accent tint (see holdProposal); restore it so a
    // confirmed apply ends in exactly the same "Applied" state as an ordinary one.
    p.applyBtn.classList.add("primary", "applied");
    p.skipBtn.disabled = true;
    p.note.textContent = APPLIED;
    offerUndo(p);
    offerRunReport(p);
    return;
  }
  // A held card keeps its inert button — the hold row below owns the retry.
  p.applyBtn.disabled = !!p.held;
  const err = data.error || "the change could not be applied";
  const plan = ChatCore.applyFailureAction(status, p.fixTried, p.noFix);
  p.fixTried = plan.tried;
  if (plan.action === "conflict") {
    // A staleness conflict (the cell changed since it was proposed) — re-reading,
    // not a re-write, is what's needed, so don't auto-ask the AI to "fix" it.
    p.note.textContent = " — that cell changed";
    addSysRow(err + " Ask me to redo it against the current notebook.");
  } else if (plan.action === "fix") {
    // A parse/write failure (e.g. the model malformed a cell) — hand the exact error
    // back to the assistant so it can re-propose a corrected version.
    p.note.textContent = " — couldn't apply";
    askAiToFix(err, plan.tried);
  } else {
    p.note.textContent = " — couldn't apply";
    addSysRow(err);
  }
}

// -- the apply gate ---------------------------------------------------------
// Apply writes a cell AND marimo runs it immediately, so Undo — which restores the
// notebook's bytes — is a complete remedy for ordinary code and no remedy at all
// for code that deleted a file. The server holds those applies (HTTP 428) and this
// is the ask. It deliberately reuses the PII/traceback hold idiom (an inline row in
// the transcript, an explanation, a confirm carrying a token) rather than a modal:
// the decision stays in the transcript, and there is one shape to learn.

// Put a proposal into the held state: the card's own Apply stops being a live
// primary Apply, and the decision moves to a new row below it. The reflex this
// feature exists to break is POSITIONAL, so re-using that button would defeat it.
function holdProposal(p, gate) {
  const rehold = !!p.holdRow;
  p.held = true;
  p.applyBtn.disabled = true;
  p.applyBtn.classList.remove("primary");
  p.applyBtn.textContent = "Held";
  p.note.textContent = " — held (see below)";
  if (p.holdRow) {
    p.holdRow.remove(); // a re-hold replaces the stale card; two live asks is worse
    p.holdRow = null;
  }
  p.holdRow = addGateHold(p, gate, rehold);
}

// Render one hold card. Everything server-supplied reaches the DOM through
// textContent — a finding label is data, never markup.
function addGateHold(p, gate, rehold) {
  const w = ChatCore.gateHoldWording(gate);
  const wrap = addRow("row-sys row-pii row-gate" + (w.floor ? " gate-floor" : ""), "");

  if (rehold) {
    const again = document.createElement("p");
    again.className = "gate-restale";
    again.textContent =
      "Your confirmation no longer matched — the change or the notebook moved " +
      "underneath it. Nothing was applied. This is the current one:";
    wrap.appendChild(again);
  }
  const summary = document.createElement("p");
  summary.className = "gate-summary";
  summary.textContent = w.summary;
  const mechanism = document.createElement("p");
  mechanism.textContent = w.mechanism;
  wrap.append(summary, mechanism);
  if (w.lead) {
    const lead = document.createElement("p");
    lead.className = "gate-lead";
    lead.textContent = w.lead;
    const list = document.createElement("ul");
    list.className = "gate-findings";
    for (const item of w.items) {
      const li = document.createElement("li");
      li.textContent = item.text; // plain-English label from the server — never a slug
      // In a MIXED verdict, say which individual lines are the irreversible ones.
      if (item.mark) {
        const mark = document.createElement("span");
        mark.className = "gate-mark";
        mark.textContent = " — " + item.mark;
        li.appendChild(mark);
      }
      list.appendChild(li);
    }
    wrap.append(lead, list);
  }
  // The line this whole feature turns on: the analyst believes Undo is a remedy.
  const undo = document.createElement("p");
  undo.className = "gate-undo";
  undo.textContent = w.undoNote;
  wrap.appendChild(undo);

  const bar = document.createElement("div");
  bar.className = "toolbar";
  const note = document.createElement("span");
  note.className = "muted";
  // The SAFE choice takes the prominent first slot; the irreversible one is a plain
  // button beside it. A reflex click here lands on "Don't apply".
  const noBtn = document.createElement("button");
  noBtn.className = "primary small";
  noBtn.textContent = w.cancelLabel;
  let yesBtn = null;
  if (gate.token) {
    yesBtn = document.createElement("button");
    yesBtn.className = "small gate-confirm";
    yesBtn.textContent = w.confirmLabel;
    yesBtn.addEventListener("click", async () => {
      if (p.applied || p.skipped) return;
      yesBtn.disabled = true;
      noBtn.disabled = true;
      note.textContent = " applying…";
      p.held = false; // this ask has been answered; a fresh 428 holds it again
      await applyProposal(p, gate.token);
      if (p.applied) {
        wrap.classList.add("gate-done");
        note.textContent = " applied ✓"; // ✓
        return;
      }
      if (p.held) return; // a NEW hold replaced this row — this one is detached
      yesBtn.disabled = false;
      noBtn.disabled = false;
      note.textContent = " — couldn't apply";
    });
  } else {
    note.textContent =
      " — this hold arrived without a confirmation token, so it can't be confirmed " +
      "here. Ask for the change again.";
  }
  noBtn.addEventListener("click", () => {
    if (p.applied || p.skipped) return;
    p.held = false;
    skipProposal(p); // the ordinary skip path — the card dims and stops being current
    p.applyBtn.textContent = "Not applied";
    p.note.textContent = " not applied";
    noBtn.disabled = true;
    if (yesBtn) yesBtn.disabled = true;
    wrap.classList.add("gate-done");
    note.textContent = " not applied";
  });
  bar.appendChild(noBtn);
  if (yesBtn) bar.appendChild(yesBtn);
  bar.appendChild(note);
  wrap.appendChild(bar);
  maybeScroll();
  return wrap;
}

// Feed an Apply failure back to the assistant for a corrective re-proposal, clearly
// narrated so the analyst knows what's happening (no silent billed turn). `tried` is
// which attempt this is (1-based, bounded by ChatCore.MAX_FIX_ATTEMPTS).
function askAiToFix(error, tried) {
  if (isBusy()) {
    addSysRow("Couldn't apply that change: " + error);
    return;
  }
  const nth = tried > 1 ? ` (attempt ${tried} of ${ChatCore.MAX_FIX_ATTEMPTS})` : "";
  addSysRow("That change didn't apply — asking the assistant to fix it" + nth + ".");
  const msg = ChatCore.applyFixPrompt(error, tried);
  lastUserText = msg;
  lastUserLabel = ""; // /retry after a fix attempt resends (and shows) the fix text
  startTurn();
  api("/api/ai/chat/send", { sid, text: msg }).then(({ data }) => {
    if (data.error) {
      showError(data.error);
      setTurnState("error");
    }
  });
}

// Show a single "Undo" button on the just-applied card (the change /undo reverts).
// A new apply moves it; using it (or /undo) removes it. Deeper history stays
// reachable via /undo.
function offerUndo(p) {
  if (lastUndoBtn) {
    lastUndoBtn.remove();
    lastUndoBtn = null;
  }
  const btn = document.createElement("button");
  btn.className = "small";
  btn.textContent = "Undo";
  btn.addEventListener("click", () => undoLast(btn));
  p.applyBtn.parentNode.insertBefore(btn, p.note);
  lastUndoBtn = btn;
}

// -- "did it actually run?" -------------------------------------------------
// The repair loop above only ever sees failures mooring can find WITHOUT running
// anything — a cell that won't parse, a patch that won't write. A wrong column, a
// bad API call, a NameError: those exist only at runtime, and mooring never opens a
// marimo websocket (that is the channel carrying outputs and values), so it cannot
// see them. This is the one route back, and it is a deliberate click: it re-runs
// EVERY cell in the notebook, which is exactly what the apply gate exists to keep
// deliberate. So the card says what it will do before it does it, and nothing here
// is reachable from Apply itself, a timer, or a page load.

// The single visible "Run & report" offer (the latest applied card), with the sentence
// that explains it. One at a time, like the Undo button: the action is the notebook, not
// the card, so several buttons would all do the same thing while looking like they didn't.
let lastRunOffer = null; // { btn, note }

const APPLIED = " applied ✓"; // ✓ — the Apply state, kept visible through a run

function offerRunReport(p) {
  if (lastRunOffer) {
    lastRunOffer.btn.remove();
    lastRunOffer.note.remove();
    lastRunOffer = null;
  }
  const btn = document.createElement("button");
  btn.className = "small";
  btn.textContent = "Run & report";
  const says = document.createElement("div");
  says.className = "muted run-report-note";
  says.textContent =
    "Run & report runs every cell in this notebook locally (it can take a while), " +
    "then tells the assistant which errors came back — the error kind and a " +
    "value-safe rewrite of its message, never a traceback and never a data value. " +
    "You'll see exactly what was sent.";
  btn.addEventListener("click", () => runAndReport(p, btn));
  p.applyBtn.parentNode.insertBefore(btn, p.note);
  p.card.appendChild(says);
  lastRunOffer = { btn, note: says };
}

async function runAndReport(p, btn) {
  if (isBusy()) {
    addSysRow("Wait for the current turn to finish, then run and report.");
    return;
  }
  btn.disabled = true;
  p.note.textContent = APPLIED + " · running…";
  // The composer is deliberately left usable: the run is server-side and can take
  // minutes, and there is no cancel, so locking the input for it would be worse than
  // the mess it prevents. That means the analyst CAN start a turn meanwhile — hence
  // the isBusy() guards below, which keep this handler from stamping on one.
  setStatus("running the notebook…");
  const { status, data } = await api("/api/ai/chat/run-report", { sid });
  const settle = () => {
    if (!isBusy()) setTurnState("idle"); // restore the status line we borrowed
  };
  if (data.reason === "notebook_disabled") {
    lockForDisabled(); // AI off for this notebook — the send is refused too
    return;
  }
  if (!data.ok) {
    settle();
    p.note.textContent = APPLIED + " · couldn't run";
    addSysRow(data.error || `The notebook couldn't be run (${status}).`);
    btn.disabled = false;
    return;
  }
  if (data.ran_clean) {
    settle();
    p.note.textContent = APPLIED + " · ran clean ✓"; // ✓
    btn.textContent = "Ran clean";
    addSysRow("Ran the notebook: every cell ran clean. Nothing to report.");
    return;
  }
  p.note.textContent = APPLIED + " · the run failed";
  if (!data.sent) {
    // It failed, but marimo's stderr held no line mooring recognises — so there is
    // nothing value-safe to hand over. Say that rather than inventing a summary.
    settle();
    btn.disabled = false;
    addSysRow(
      "Ran the notebook: it failed, but mooring couldn't read a value-safe reason " +
      "from the run. Open the notebook to see the error."
    );
    return;
  }
  btn.textContent = "Reported";
  addRunReportSent(data.sent, data.redactions || []);
  // The report IS a turn — the assistant answers it with a new proposal. Skipped when
  // a turn the analyst started is already streaming: startTurn resets the streaming
  // row, which would split that reply in two.
  if (!isBusy()) startTurn();
}

// Show the EXACT text the assistant was given. The click was the consent; this is the
// receipt — the analyst never saw the raw message, so showing them the rewrite after
// the fact is the only honest account of what left the machine.
function addRunReportSent(sent, redactions) {
  const wrap = addRow("row-sys row-pii", "");
  const p = document.createElement("p");
  p.textContent = "Reported the failure to the assistant. This is exactly what was sent:";
  const pre = document.createElement("pre");
  pre.className = "cell-code";
  pre.textContent = sent; // textContent — model-adjacent text is data, never markup
  wrap.append(p, pre);
  if (redactions.length) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent =
      redactions.length +
      (redactions.length === 1 ? " part was" : " parts were") +
      " withheld as possibly value-bearing: " +
      summarizeKinds(redactions) + ".";
    wrap.appendChild(note);
  }
  maybeScroll();
}

async function undoLast(srcBtn) {
  if (srcBtn) srcBtn.disabled = true;
  const { data } = await api("/api/ai/chat/rollback", { sid });
  if (data.reason === "notebook_disabled") {
    lockForDisabled(); // AI off for this notebook — the rollback write is refused too
    return;
  }
  if (data.ok) {
    if (lastUndoBtn) {
      lastUndoBtn.remove();
      lastUndoBtn = null;
    }
    const more = data.undo_depth || 0;
    let earlier = "";
    if (more) {
      const plural = more > 1 ? "s" : "";
      earlier = ` (${more} earlier change${plural} still undoable with /undo)`;
    }
    addSysRow("Reverted the last applied change." + earlier);
  } else {
    addSysRow(data.error || "Nothing to undo.");
    if (srcBtn) srcBtn.disabled = false;
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
  if (!code) {
    if (note) note.textContent = " nothing to copy";
    return;
  }
  const done = () => {
    if (note) note.textContent = " copied";
  };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(code).then(done, () => {});
  }
}

// -- "Add as notes cell" (the /explain follow-up) -----------------------------

// A walkthrough that lives only in this transcript rots; one that syncs with the
// notebook greets the next inheritor. After an explain turn goes idle, offer to
// send the canned follow-up (chat_core.js notesCellPrompt — one appended markdown
// cell via the propose tool's `appends` only). The resulting proposal rides the normal
// card → Apply → Undo path untouched, so it still gets the human review step.
function offerNotesCell() {
  const rows = $("messages").querySelectorAll(".row-assistant");
  const last = rows[rows.length - 1];
  if (!last) return; // the turn produced no assistant row (e.g. it failed)
  const bar = document.createElement("div");
  bar.className = "toolbar";
  const btn = document.createElement("button");
  btn.className = "small";
  btn.textContent = "Add as notes cell";
  btn.addEventListener("click", () => {
    if (!sid || isBusy() || turnState === "connecting") {
      addSysRow("Wait for the current turn to finish.");
      return;
    }
    btn.disabled = true;
    stick = true;
    submitMessage(ChatCore.notesCellPrompt(), "add the walkthrough as a notes cell");
  });
  bar.appendChild(btn);
  last.appendChild(bar);
  maybeScroll();
}

// -- outbound-PII guard -----------------------------------------------------

let shownPii = new Set(); // finding-set signatures already surfaced this page-load

function summarizeKinds(findings) {
  return [...new Set((findings || []).map((f) => f.kind))].join(", ");
}

function showPiiBanner(items) {
  if (!items?.length) return;
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

function showRoutingNotice(route, switched = false) {
  const text = ChatCore.routingNotice(route, switched);
  if (text) addSysRow(text);
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
    if (data.reason === "notebook_disabled") {
      lockForDisabled();
      return;
    }
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

// -- traceback guard ----------------------------------------------------------

// A held traceback turn: the message contained a pasted Python traceback, which
// was rewritten value-safe server-side. ONLY the sanitised rewrite is held under
// the token — there is deliberately NO "send raw anyway" button, because no raw
// copy exists to forward (an escape hatch here would recreate the leak one click
// deep). The preview is rendered via textContent: it is data, never markup.
function addTracebackHold(preview, redactions, piiFindings, token) {
  const wrap = addRow("row-sys row-pii", "");
  const p = document.createElement("p");
  p.textContent = ChatCore.tracebackHoldSummary(redactions, piiFindings);
  const pre = document.createElement("pre");
  pre.textContent = preview;
  const bar = document.createElement("div");
  bar.className = "toolbar";
  const sendBtn = document.createElement("button");
  sendBtn.className = "primary small";
  sendBtn.textContent = "Send sanitised";
  const note = document.createElement("span");
  note.className = "muted";
  sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true;
    note.textContent = " sending…";
    startTurnState();
    const { data } = await api("/api/ai/chat/send", { sid, confirm_token: token });
    if (data.reason === "notebook_disabled") {
      lockForDisabled();
      return;
    }
    if (data.error) {
      showError(data.error);
      setTurnState("error");
      sendBtn.disabled = false; // don't leave the hold card stuck on an error
      note.textContent = "";
    }
  });
  bar.append(sendBtn, note);
  wrap.append(p, pre, bar);
  maybeScroll();
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
  // "connecting" disables input too: the session isn't ready to take a turn until
  // the provider handshake finishes (a "ready" event flips it to idle).
  const busy = state === "thinking" || state === "streaming" || state === "connecting" || state === "unavailable";
  $("chat-input").disabled = busy;
  const generalRelevant = ChatCore.generalModelRelevant(
    ROUTING,
    ChatCore.effectiveRoutingPreference(ROUTING, $("chat-routing-preference").value),
  );
  $("chat-model").disabled = busy || !generalRelevant;
  $("chat-effort").disabled = busy || !generalRelevant;
  const generalHint = generalRelevant
    ? ""
    : "Not used while routing is set to Always use approved";
  $("chat-model").closest("label").title = generalHint;
  $("effort-wrap").title = generalHint;
  const trustedOptions = ChatCore.trustedModelOptions(ROUTING);
  $("chat-trusted-model").disabled = busy || trustedOptions.length <= 1;
  $("chat-routing-preference").disabled = busy || trustedOptions.length === 0;
  setStatus(STATE_LABEL[state] || state);
  if (state === "idle" || state === "error") {
    clearPending();
    if (state === "idle") $("chat-input").focus();
  }
}

// -- session ----------------------------------------------------------------

// Opened with &explain=1 (the hub's "Explain" action): run /explain once the
// session can take a turn. Consumed at BOTH readiness paths — the SSE "ready"
// event (backgrounded Copilot handshake) and openChat's immediate-ready branch —
// and never on the disabled/no-sid early returns (they bail before sid is set).
// The once-per-window flag is what keeps a later /model or effort switch (each
// re-invokes openChat) from silently re-sending the walkthrough turn.
function maybeAutoExplain() {
  if (!EXPLAIN || explainFired || !sid) return;
  explainFired = true;
  runCommand({ cmd: "explain", arg: "" });
}

// Opened with &review=1 (the hub's "Review logic" action): run /review once the
// session can take a turn. Same once-per-window discipline as maybeAutoExplain.
function maybeAutoReview() {
  if (!REVIEW || reviewFired || !sid) return;
  reviewFired = true;
  runCommand({ cmd: "review", arg: "" });
}

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
  const generation = openGate.begin();
  if (openController) openController.abort();
  const controller = new AbortController();
  openController = controller;
  closeStream();
  sid = null;
  clearSigninNotice();
  showError("");
  setTurnState("connecting");
  const model = $("chat-model").value;
  const reasoning_effort = selectedEffort();
  const overrides = ChatCore.notebookOverridePayload({
    model,
    reasoning_effort,
    trusted_model: ROUTING?.enabled === true ? $("chat-trusted-model").value : "",
    routing_preference: ROUTING?.enabled === true ? $("chat-routing-preference").value : "",
  });
  const body = { notebook: NOTEBOOK, ...overrides };
  if (ROUTING && !ChatCore.trustedRoutingAvailable(ROUTING)) {
    openController = null;
    showError("Approved customer-data routing is unavailable. Reload or contact your administrator.");
    setTurnState("unavailable");
    return;
  }
  let response;
  try {
    response = await api("/api/ai/chat/open", body, { signal: controller.signal });
  } catch (error) {
    if (!openGate.isCurrent(generation) || error?.name === "AbortError") return;
    openController = null;
    showError("Could not start the copilot.");
    setTurnState(ROUTING ? "unavailable" : "error");
    return;
  }
  if (!openGate.isCurrent(generation)) return;
  openController = null;
  const { status, data } = response;
  if (data.reason === "notebook_disabled") {
    lockForDisabled();
    return;
  }
  if (!data.sid) {
    showError(data.error || `Could not start the copilot (${status}).`);
    setTurnState(ROUTING ? "unavailable" : "error");
    return;
  }
  const resolvedRoutingInvalid = ROUTING && !ChatCore.resolvedRoutingValuesValid(
    ROUTING,
    data.trusted_model,
    data.routing_preference,
  );
  const explicitRoutingMismatch = ROUTING && !ChatCore.resolvedRoutingMatchesRequest(data, overrides);
  if (
    !ChatCore.routingExpectationMatches(ROUTING, data.route) ||
    resolvedRoutingInvalid ||
    explicitRoutingMismatch
  ) {
    showError("Approved routing changed while this chat was opening. Reload before sending anything.");
    setPrivacyChrome({ enabled: true, trusted_models: [], error: true });
    addSysRow("Approved routing changed; the earlier privacy guidance is no longer current. Reload this chat.");
    setTurnState("unavailable");
    return;
  }
  applyResolvedRoutingDefaults(data);
  sid = data.sid;
  source = new EventSource(`/api/ai/chat/stream/${sid}`);
  source.addEventListener("delta", (e) => {
    if (turnState === "thinking") setTurnState("streaming");
    clearPending();
    appendDelta(JSON.parse(e.data).text);
  });
  source.addEventListener("message", (e) => finalizeAssistant(JSON.parse(e.data).text));
  source.addEventListener("proposal", (e) => addProposal(JSON.parse(e.data)));
  source.addEventListener("tool", (e) => onTool(JSON.parse(e.data)));
  source.addEventListener("tool_done", (e) => onToolDone(JSON.parse(e.data).success !== false));
  source.addEventListener("intent", (e) => onIntent(JSON.parse(e.data).text));
  source.addEventListener("routing", (e) => showRoutingNotice(JSON.parse(e.data), true));
  source.addEventListener("idle", () => {
    setTurnState("idle");
    if (explainTurnActive) {
      explainTurnActive = false;
      offerNotesCell(); // the walkthrough landed — offer to keep it with the notebook
    }
  });
  // The (backgrounded) Copilot session finished starting — unblock the input. The
  // hub also REPLAYS this on (re)connect, so we catch it even if it fired first.
  source.addEventListener("ready", () => {
    if (turnState === "connecting") setTurnState("idle");
    maybeAutoExplain();
    maybeAutoReview();
  });
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
      // Fail-open but accurate: only a structured-scan failure means "unchecked";
      // a names-only failure still scanned structured PII (see ChatCore).
      showError(ChatCore.scanErrorMessage(d.scan_error));
    }
  });
  source.addEventListener("traceback", (e) => {
    // A pasted traceback was sanitised and the turn HELD: show the exact rewrite
    // (the only text that can be sent) with the one "Send sanitised" confirm.
    const d = JSON.parse(e.data);
    if (!d.token) return;
    setTurnState("idle"); // drop the thinking indicator; the turn is held
    addTracebackHold(d.preview || "", d.redactions || [], d.pii_findings || [], d.token);
    if (d.scan_error) showError(ChatCore.scanErrorMessage(d.scan_error));
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
    const d = JSON.parse(e.data);
    // Copilot isn't signed in — Copilot's sign-in is separate from the GitHub login,
    // so offer an in-app sign-in button instead of a dead error string.
    if (d.reason === "not_connected") {
      showCopilotSignin(d.text);
      return;
    }
    showError(d.text || "The assistant reported an error.");
    setTurnState("error");
  });
  source.addEventListener("closed", () => setStatus("closed"));
  source.onerror = () => setStatus("reconnecting…");
  // Routed chat uses the approved classifier rather than the legacy PII guard;
  // never paint that intentionally-disabled legacy guard as a red "PII-off" warning.
  currentGuard = ROUTING ? null : (data.guard || null);
  setPiiBadge(currentGuard);
  if (!ROUTING) showPiiBanner(data.pii);
  showRoutingNotice(data.route);
  // If the session is still starting (backgrounded handshake), show "connecting…"
  // and keep the input disabled until the "ready" event arrives; an already-ready
  // session (data.ready) is usable immediately.
  setTurnState(data.ready === false ? "connecting" : "idle");
  if (data.ready !== false) {
    maybeAutoExplain(); // still-connecting: the "ready" event fires it
    maybeAutoReview();
  }
}

// -- per-notebook AI off-switch ---------------------------------------------
// This window can turn the copilot OFF for its notebook (the off switch for "this
// notebook now handles PII — don't let AI touch it by mistake"). The decision is
// written to the synced mooring.toml, so it travels to teammates. Disabling locks
// this window; the backend also refuses any further open/send/apply for it.

async function disableAiForNotebook() {
  const { data } = await api("/api/ai/notebook/toggle", { notebook: NOTEBOOK, disabled: true });
  if (data.error) {
    showError(data.error);
    return;
  }
  lockForDisabled();
}

async function enableAiForNotebook() {
  const { data } = await api("/api/ai/notebook/toggle", { notebook: NOTEBOOK, disabled: false });
  if (data.error) {
    showError(data.error);
    return;
  }
  const notice = $("disabled-notice");
  notice.classList.add("hidden");
  notice.innerHTML = "";
  $("disable-ai-btn").classList.remove("hidden");
  $("chat-input").disabled = false;
  await openChat(); // reconnect a fresh session
}

// Lock the window: AI is off for this notebook (turned off here, from the hub, or
// by a teammate's sync). Tear down the stream, freeze the composer, and offer to
// turn it back on. Idempotent — safe to call from open/send/apply failures.
function lockForDisabled() {
  closeStream();
  sid = null;
  turnState = "idle";
  clearPending();
  const input = $("chat-input");
  input.disabled = true;
  input.blur();
  $("disable-ai-btn").classList.add("hidden");
  showError("");
  setStatus("AI disabled");
  const notice = $("disabled-notice");
  notice.innerHTML = "";
  const msg = document.createElement("span");
  msg.textContent = "AI is turned off for this notebook. ";
  const btn = document.createElement("button");
  btn.className = "small";
  btn.textContent = "Enable AI";
  btn.addEventListener("click", enableAiForNotebook);
  notice.append(msg, btn);
  notice.classList.remove("hidden");
}

// -- Copilot sign-in (separate from the GitHub login) -----------------------
// The copilot uses GitHub Copilot, which signs in independently of mooring's
// GitHub login — it can even be a different account. When a session fails to
// start because Copilot isn't connected, show an in-app sign-in panel here
// instead of dumping a "run mooring ai login" CLI string at the user.

function showCopilotSignin(detail) {
  closeStream();
  sid = null;
  turnState = "idle";
  clearPending();
  const input = $("chat-input");
  input.disabled = true;
  input.blur();
  showError("");
  setStatus("not signed in");
  const box = $("signin-notice");
  box.innerHTML = "";
  const msg = document.createElement("p");
  msg.textContent =
    detail?.trim() ||
    "You're not signed in to GitHub Copilot.";
  const sub = document.createElement("p");
  sub.className = "muted";
  sub.textContent =
    "Copilot signs in separately from your GitHub login — it can even be a different account.";
  const bar = document.createElement("div");
  bar.className = "toolbar";
  const btn = document.createElement("button");
  btn.className = "primary small";
  btn.textContent = "Sign in to Copilot";
  const note = document.createElement("span");
  note.className = "muted";
  btn.addEventListener("click", () => startCopilotLogin(btn, note));
  bar.append(btn, note);
  // The device-flow code `copilot login` prints lands here once polling sees it
  // (the CLI's own clipboard copy often fails, and switching account needs the
  // code typed into the device page) — empty until then.
  const codeBox = document.createElement("div");
  codeBox.id = "signin-code-box";
  codeBox.className = "hidden";
  box.append(msg, sub, bar, codeBox);
  box.classList.remove("hidden");
}

// Render the captured device-login code + URL into the sign-in notice. Built as
// DOM (textContent), never innerHTML of the raw CLI lines, so nothing can inject.
function renderSigninCode(login) {
  const box = $("signin-code-box");
  if (!box) return;
  if (!login.code) {
    box.classList.add("hidden");
    return;
  }
  if (box.dataset.code === login.code) return; // already shown — don't rebuild
  box.dataset.code = login.code;
  box.innerHTML = "";
  const p = document.createElement("p");
  p.append(document.createTextNode("In the browser, enter this code at "));
  const a = document.createElement("a");
  a.href = login.url || "https://github.com/login/device";
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = (login.url || "github.com/login/device").replace(/^https?:\/\//, "");
  p.append(a, document.createTextNode(":"));
  const code = document.createElement("div");
  code.className = "code";
  code.textContent = login.code;
  const copy = document.createElement("button");
  copy.className = "small";
  copy.textContent = "Copy code";
  copy.addEventListener("click", () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(login.code).then(
        () => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy code"; }, 1500); },
        () => {},
      );
    }
  });
  box.append(p, code, copy);
  box.classList.remove("hidden");
}

function clearSigninNotice() {
  const box = $("signin-notice");
  box.classList.add("hidden");
  box.innerHTML = "";
}

async function startCopilotLogin(btn, note) {
  btn.disabled = true;
  note.textContent = " opening a browser to sign in…";
  const { data } = await api("/api/ai/login/start", {});
  if (data.error) {
    btn.disabled = false;
    note.textContent = "";
    showError(data.error);
    return;
  }
  note.textContent = " waiting for you to authorize in the browser…";
  pollCopilotLogin(btn, note);
}

async function pollCopilotLogin(btn, note) {
  const { data } = await api("/api/ai/login/poll");
  if (data.status === "ok") {
    clearSigninNotice();
    showError("");
    $("chat-input").disabled = false;
    await openChat(); // reconnect a fresh session now that Copilot is signed in
    return;
  }
  if (data.status === "error") {
    btn.disabled = false;
    note.textContent = "";
    showError(data.detail || "Copilot sign-in didn't complete. Try again.");
    return;
  }
  // Still pending: show the device code so the user can enter it in the browser.
  const login = ChatCore.parseDeviceLogin(data.output);
  if (login.code) {
    note.textContent = " waiting for you to authorize in the browser…";
    renderSigninCode(login);
  }
  setTimeout(() => pollCopilotLogin(btn, note), 2500); // still pending — keep polling
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
  if (turnState === "connecting") return; // the session isn't ready to take a turn yet
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

// Send one user turn. `visibleLabel`, when given, is what the transcript row
// shows in place of the full text — used by the canned /explain prompts so the
// transcript reads compactly. lastUserText stays the FULL prompt (what /retry
// resends), with lastUserLabel alongside so the retried row reads the same way.
async function submitMessage(message, visibleLabel) {
  lastUserText = message;
  lastUserLabel = visibleLabel || "";
  // The turn answering the /explain prompt (a pinned constant, so equality is
  // reliable — including via /retry) offers "Add as notes cell" when it lands;
  // any other turn clears a stale tag left by an errored one.
  explainTurnActive = message === ChatCore.explainPrompt();
  addUserRow(visibleLabel || message);
  startTurn();
  const { data } = await api("/api/ai/chat/send", { sid, text: message });
  if (data.reason === "notebook_disabled") {
    addSysRow("AI was turned off for this notebook — your message was not sent.");
    lockForDisabled();
    return;
  }
  if (data.error) {
    showError(data.error);
    setTurnState("error");
  }
}

// The canned-prompt commands (/explain, /review, /checks, /sql) share one shape: refuse
// if a turn is in flight or the session is still connecting, optionally show a one-line
// advisory, then send a FIXED, value-free prompt (chat_core.js) over the ordinary send
// path so the PII valve and the per-notebook off-switch apply. send() can't reach
// runCommand while busy, but the auto-run path can — hence the guard here.
function submitFixedCommand(prompt, label, advisory) {
  if (isBusy() || turnState === "connecting") {
    addSysRow("Wait for the session to be ready.");
    return;
  }
  if (advisory) addSysRow(advisory);
  stick = true;
  submitMessage(prompt, label);
}

function runCommand(cmd) {
  switch (cmd.cmd) {
    case "help":
      printHelp();
      break;
    case "explain":
      // The handover walkthrough: generated from the notebook source, so it carries an
      // advisory to verify it against the notebook.
      submitFixedCommand(
        ChatCore.explainPrompt(),
        ChatCore.explainLabel(),
        "The walkthrough is generated from the notebook source — verify it against " +
          "the notebook before relying on it."
      );
      break;
    case "checks":
      // Propose value-free tie-out checks (a mooring_checks cell); the analyst applies it.
      submitFixedCommand(ChatCore.checksPrompt(), ChatCore.checksLabel());
      break;
    case "sql":
      // Propose a marimo SQL (DuckDB) cell. SQL is authored code marimo runs locally — the
      // model never sees the result — so this opens no new data channel.
      submitFixedCommand(ChatCore.sqlPrompt(), ChatCore.sqlLabel());
      break;
    case "review":
      // A whole-notebook LOGIC review: it reasons only over source + schema and is a
      // REVIEW, not an answer checker, so it carries the "flags risks, can't confirm a
      // number" advisory.
      submitFixedCommand(
        ChatCore.reviewPrompt(),
        ChatCore.reviewLabel(),
        "A logic review from the notebook's code and schema — it flags structural risks " +
          "(it can't confirm a number is correct). Check each point against the notebook."
      );
      break;
    case "investigate":
      // Fan out read-only sub-agents over independent sub-questions, then propose ONE
      // change. Unlike the canned commands this prompt carries the analyst's own topic —
      // ordinary user prose on the ordinary send path, so the PII valve applies to it.
      if (!cmd.arg) {
        addSysRow(
          "Give it a topic: /investigate <what to look into> — e.g. " +
            "/investigate how revenue is computed across the monthly notebooks."
        );
      } else {
        submitFixedCommand(
          ChatCore.investigatePrompt(cmd.arg),
          ChatCore.investigateLabel(cmd.arg)
        );
      }
      break;
    case "clear":
      $("messages").innerHTML = "";
      latestProposal = null;
      lastUndoBtn = null; // the transcript (and its Undo button) is gone
      printBanner();
      break;
    case "model":
      handleModelCommand(cmd.arg);
      break;
    case "apply":
      if (latestProposal && latestProposal.held) {
        // Same rule as the `a` key: a held proposal is decided on its hold card, so
        // /apply must not be a quieter way around it.
        addSysRow("That change is held — confirm it on the card above, or don't.");
        if (latestProposal.holdRow) {
          latestProposal.holdRow.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      } else if (latestProposal && !latestProposal.applied && !latestProposal.skipped) {
        applyProposal(latestProposal);
      } else {
        addSysRow("No proposal to apply.");
      }
      break;
    case "diff":
      if (latestProposal) latestProposal.card.scrollIntoView({ block: "center", behavior: "smooth" });
      else addSysRow("No proposal yet.");
      break;
    case "undo":
      undoLast(null);
      break;
    case "retry":
      if (isBusy()) addSysRow("Wait for the current turn to finish.");
      else if (lastUserText) { stick = true; submitMessage(lastUserText, lastUserLabel); }
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
  let selected = hit.id;
  sel.value = selected;
  selected = rememberNotebookOverride(modelStore(), selected, sel);
  populateEfforts();
  reopenForRoutingChange(
    selected ? `general model overridden for this notebook: ${selected}` : "general model now uses Settings",
  );
}

function printBanner() {
  const copy = ChatCore.privacyChrome(ROUTING);
  addRow("row-sys", (el) => {
    const a = document.createElement("div");
    const b = document.createElement("b");
    b.textContent = copy.lead;
    a.appendChild(b);
    a.appendChild(
      document.createTextNode(
        copy.body
      )
    );
    const c = document.createElement("div");
    c.textContent = copy.footer;
    el.append(a, c);
  });
}

function printHelp() {
  const rows = [
    ["/help", "show this help"],
    ["/explain", "walk through what this notebook does"],
    ["/review", "review the notebook's logic for correctness risks"],
    ["/checks", "propose tie-out / data-quality checks"],
    ["/sql", "propose a marimo SQL (DuckDB) cell"],
    ["/investigate <topic>", "research independent sub-questions in parallel"],
    ["/clear", "clear the transcript (keeps the session)"],
    ["/model [name]", "list or switch the model"],
    ["/apply", "apply the latest proposal"],
    ["/diff", "jump to the latest proposal"],
    ["/undo", "undo the last applied change"],
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

// -- models / effort / trusted routing -------------------------------------

function populateTrustedRouting(routing) {
  ROUTING = routing?.enabled === true ? routing : null;
  const modelWrap = $("trusted-model-wrap");
  const preferenceWrap = $("routing-preference-wrap");
  const modelSelect = $("chat-trusted-model");
  const preferenceSelect = $("chat-routing-preference");
  modelSelect.innerHTML = "";
  preferenceSelect.innerHTML = "";
  if (!ROUTING) {
    modelWrap.classList.add("hidden");
    preferenceWrap.classList.add("hidden");
    modelSelect.disabled = true;
    preferenceSelect.disabled = true;
    return;
  }
  const options = ChatCore.trustedModelOptions(ROUTING);
  const available = ChatCore.trustedRoutingAvailable(ROUTING);
  const profileLabel = $("trusted-profile-label");
  const profile = typeof ROUTING.profile_label === "string" ? ROUTING.profile_label.trim() : "";
  profileLabel.textContent = profile ? `(${profile})` : "";
  profileLabel.classList.toggle("hidden", !profile);
  if (options.length) {
    const inherit = document.createElement("option");
    inherit.value = "";
    inherit.textContent = `Use Settings default (${ROUTING.default_trusted_model})`;
    modelSelect.appendChild(inherit);
  }
  for (const model of options) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `Override this notebook: ${model.name}`;
    modelSelect.appendChild(option);
  }
  if (!options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Not configured";
    modelSelect.appendChild(option);
  }
  modelSelect.value = readValidNotebookOverride(
    trustedModelStore(),
    (saved) => ChatCore.chooseNotebookTrustedOverride(ROUTING, saved),
  );
  const inheritRouting = document.createElement("option");
  inheritRouting.value = "";
  const globalRouting = ChatCore.chooseRoutingPreference(
    ROUTING,
    ROUTING.default_routing_preference,
  );
  inheritRouting.textContent =
    `Use Settings default (${globalRouting === "trusted" ? "Always use approved" : "Automatic"})`;
  preferenceSelect.appendChild(inheritRouting);
  for (const [value, label] of [["auto", "Override this notebook: Automatic"], ["trusted", "Override this notebook: Always use approved"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    preferenceSelect.appendChild(option);
  }
  preferenceSelect.value = readValidNotebookOverride(
    routingStore(),
    (saved) => ChatCore.chooseNotebookRoutingOverride(ROUTING, saved),
  );
  const selectable = options.length > 1;
  // "approved by your firm" is a claim about who vetted the endpoint, so it may only
  // be made for a managed profile — the same rule the badge and route notices follow.
  const vetted =
    ChatCore.routingProfileKind(ROUTING) === "managed" ? " approved by your firm" : "";
  modelSelect.title = selectable
    ? `Choose from the customer-data models${vetted}`
    : options.length === 1
      ? `This is the only customer-data model${vetted || " configured"}`
      : "No customer-data model is configured";
  modelSelect.disabled = !available || !selectable;
  preferenceSelect.disabled = !available;
  modelWrap.classList.remove("hidden");
  preferenceWrap.classList.remove("hidden");
}

function applyResolvedRoutingDefaults(data) {
  if (!ROUTING) return;
  const trustedModel = typeof data.trusted_model === "string" ? data.trusted_model.trim() : "";
  const preference = typeof data.routing_preference === "string"
    ? data.routing_preference.trim().toLowerCase()
    : "";
  if (!ChatCore.resolvedRoutingValuesValid(ROUTING, trustedModel, preference)) return;
  const trustedSelect = $("chat-trusted-model");
  const routingSelect = $("chat-routing-preference");
  if (!trustedSelect.value) {
    ROUTING = { ...ROUTING, default_trusted_model: trustedModel };
    trustedSelect.options[0].textContent = `Use Settings default (${trustedModel})`;
  }
  if (!routingSelect.value) {
    ROUTING = { ...ROUTING, default_routing_preference: preference };
    const label = preference === "trusted" ? "Always use approved" : "Automatic";
    routingSelect.options[0].textContent = `Use Settings default (${label})`;
  }
}

function setPrivacyChrome(routing = ROUTING) {
  const copy = ChatCore.privacyChrome(routing);
  const badge = $("privacy-badge");
  badge.textContent = copy.badge;
  badge.title = copy.title;
  badge.classList.remove("hidden", "synced", "danger", "warn");
  badge.classList.add(copy.badgeClass);
}

function reopenForRoutingChange(message) {
  // Controls are disabled while a turn/handshake is active. This guard also
  // protects programmatic change events and keeps the current session intact.
  if (!ChatCore.routingChangeAllowed(turnState)) return;
  addSysRow(`— ${message}; starting a fresh conversation —`);
  openChat();
}

// Takes no argument on purpose: the configured default is remembered in
// DEFAULT_EFFORT, so a model switch (/model, the dropdown) can't lose it.
function populateEfforts() {
  const selectedModel = $("chat-model").value;
  const effectiveModel = selectedModel || DEFAULT_MODEL || MODELS[0]?.id || "";
  const model = MODELS.find((m) => m.id === effectiveModel);
  const sel = $("chat-effort");
  sel.innerHTML = "";
  const efforts = model?.efforts || [];
  if (!efforts.length) {
    $("effort-wrap").classList.add("hidden");
    return;
  }
  $("effort-wrap").classList.remove("hidden");
  const inherit = document.createElement("option");
  inherit.value = "";
  inherit.textContent = `Use Settings default (${DEFAULT_EFFORT || "provider default"})`;
  sel.appendChild(inherit);
  for (const e of efforts) {
    const o = document.createElement("option");
    o.value = e;
    o.textContent = e;
    sel.appendChild(o);
  }
  sel.value = readValidNotebookOverride(
    effortStore(),
    (saved) => ChatCore.chooseNotebookOverride(efforts, saved),
  );
}

async function loadModels() {
  const { data } = await api("/api/ai/models");
  MODELS = data.models || [];
  PROVIDER = data.provider || "";
  PREFERENCE_SCOPE = typeof data.preference_scope === "string" && data.preference_scope.trim()
    ? data.preference_scope.trim()
    : "local";
  DEFAULT_MODEL = data.default_model || "";
  DEFAULT_EFFORT = data.default_effort || "";
  populateTrustedRouting(data.routing);
  setPrivacyChrome();
  const sel = $("chat-model");
  sel.innerHTML = "";
  const wrap = sel.closest("label");
  if (!MODELS.length) {
    wrap.classList.add("hidden");
    $("effort-wrap").classList.add("hidden");
    // If the provider REJECTED the list (e.g. a 403 "not authorized" — a signed-in
    // but unlicensed account), say so. A not-signed-in account returns no error
    // here; the session's "fail" event shows the sign-in panel instead.
    if (data.error) showError(data.error);
    return;
  }
  wrap.classList.remove("hidden");
  const inherit = document.createElement("option");
  inherit.value = "";
  inherit.textContent = `Use Settings default (${DEFAULT_MODEL || "provider default"})`;
  sel.appendChild(inherit);
  for (const m of MODELS) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.name + (m.multiplier && m.multiplier > 1 ? ` · ${m.multiplier}×` : "");
    sel.appendChild(o);
  }
  sel.value = readValidNotebookOverride(
    modelStore(),
    (saved) => ChatCore.chooseNotebookOverride(MODELS.map((m) => m.id), saved),
  );
  populateEfforts();
}

async function loadDatasets() {
  // Value-free: /api/ai/datasets returns dataset PATHS only (schema.list_datasets) —
  // no values. Used solely to power @-mention autocomplete; the inserted token is
  // plain text that still passes the outbound PII gate when sent. This is the LIGHT
  // endpoint, not /api/state — the latter makes GitHub sync round-trips this window
  // doesn't need, which used to ride on every chat-open.
  try {
    const { data } = await api("/api/ai/datasets");
    DATASETS = data.datasets || [];
    applyTheme(data.ui_theme); // follow the hub's appearance
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
  // Only the model list is needed before opening (it decides the model sent to
  // /chat/open). The dataset list just feeds @-mention autocomplete, so it loads
  // fire-and-forget and hydrates after the chat is already usable — it no longer
  // sits in front of the open.
  loadDatasets();
  await loadModels();
  printBanner();

  $("chat-model").addEventListener("change", () => {
    const selected = ChatCore.chooseNotebookOverride(
      MODELS.map((m) => m.id),
      $("chat-model").value,
    );
    $("chat-model").value = selected;
    const remembered = rememberNotebookOverride(modelStore(), selected, $("chat-model"));
    populateEfforts();
    reopenForRoutingChange(
      remembered
        ? `general model overridden for this notebook: ${remembered}`
        : "general model now uses Settings",
    );
  });
  $("chat-effort").addEventListener("change", () => {
    const select = $("chat-effort");
    const allowed = Array.from(select.options, (option) => option.value).filter(Boolean);
    const selected = ChatCore.chooseNotebookOverride(allowed, select.value);
    select.value = selected;
    const remembered = rememberNotebookOverride(effortStore(), selected, select);
    reopenForRoutingChange(
      remembered ? `effort overridden for this notebook: ${remembered}` : "effort now uses Settings",
    );
  });
  $("chat-trusted-model").addEventListener("change", () => {
    const selected = ChatCore.chooseNotebookTrustedOverride(
      ROUTING,
      $("chat-trusted-model").value,
    );
    $("chat-trusted-model").value = selected;
    const remembered = rememberNotebookOverride(
      trustedModelStore(),
      selected,
      $("chat-trusted-model"),
    );
    reopenForRoutingChange(
      remembered
        ? `customer-data model overridden for this notebook: ${remembered}`
        : "customer-data model now uses Settings",
    );
  });
  $("chat-routing-preference").addEventListener("change", () => {
    const preference = ChatCore.chooseNotebookRoutingOverride(
      ROUTING,
      $("chat-routing-preference").value,
    );
    $("chat-routing-preference").value = preference;
    const remembered = rememberNotebookOverride(
      routingStore(),
      preference,
      $("chat-routing-preference"),
    );
    const label = remembered === "trusted"
      ? "routing overridden for this notebook: Always use approved"
      : remembered === "auto"
        ? "routing overridden for this notebook: Automatic"
        : "routing now uses Settings";
    reopenForRoutingChange(label);
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
  $("disable-ai-btn").addEventListener("click", disableAiForNotebook);
  // a/s apply/skip the latest proposal — only when the prompt isn't focused, so
  // they never hijack typing.
  document.addEventListener("keydown", onGlobalKeydown);
  // Cross-tab appearance changes are followed by the shared theme.js module.

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
  const tag = ae?.tagName;
  if (
    tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON" ||
    ae?.isContentEditable
  ) {
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (!latestProposal || latestProposal.applied || latestProposal.skipped) return;
  if (e.key === "a") {
    e.preventDefault();
    // A HELD proposal has no one-key path. `a` applies with no dialog at all, and a
    // single keystroke must never reach something Undo can't take back — so here it
    // only walks the analyst to the hold card's own two-button decision.
    if (latestProposal.held) {
      if (latestProposal.holdRow) {
        latestProposal.holdRow.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      return;
    }
    applyProposal(latestProposal);
  } else if (e.key === "s") {
    e.preventDefault();
    skipProposal(latestProposal);
  }
}

document.addEventListener("DOMContentLoaded", init);
