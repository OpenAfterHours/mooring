"use strict";

// The ORDER of the chat transcript when a gateway streams a reasoning model's thinking.
//
// `reasoning_delta.test.js` beside this pins the pure decision (which channel a frame
// belongs to). This one pins the rows those decisions build, because the defect that
// prompted it is invisible to a pure helper: a think that starts AFTER the answer has
// begun used to leave the answer row above it, so the reply rendered as if the model
// had answered before it thought — and the naive repair (start a second answer row)
// prints the first half of the reply twice, because the closing `message` frame
// rewrites the current row with the FULL text of the assistant message.
//
// chat.js is a browser script, not a module, so the handful of functions under test are
// lifted out of it by name and run in a `vm` context against the smallest DOM they
// need. Zero dependencies, like the rest of tests/js. Run with:
//   node --test "tests/js/**/*.test.js"

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");
const CHAT_JS = fs.readFileSync(path.join(STATIC, "chat.js"), "utf8");
const CHAT_CORE = require(path.join(STATIC, "chat_core.js"));

// -- the smallest DOM the extracted functions touch --------------------------

function matches(el, sel) {
  if (sel.startsWith(".")) return el.className.split(/\s+/).includes(sel.slice(1));
  return el.tagName === sel;
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.childNodes = [];
    this.parentNode = null;
    this.className = "";
    this.dataset = {};
    this.open = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this._text = "";
    this._listeners = {};
  }

  get classList() {
    const self = this;
    const parts = () => self.className.split(/\s+/).filter(Boolean);
    return {
      add(...names) {
        self.className = [...new Set([...parts(), ...names])].join(" ");
      },
      remove(...names) {
        const drop = new Set(names);
        self.className = parts()
          .filter((p) => !drop.has(p))
          .join(" ");
      },
      contains(name) {
        return parts().includes(name);
      },
    };
  }

  get textContent() {
    if (this.childNodes.length) return this.childNodes.map((c) => c.textContent).join("");
    return this._text;
  }

  set textContent(v) {
    this.childNodes = [];
    this._text = String(v);
  }

  set innerHTML(v) {
    this.childNodes = [];
    this._text = String(v);
  }

  _detach(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) this.childNodes.splice(i, 1);
  }

  appendChild(node) {
    if (node.parentNode) node.parentNode._detach(node); // appendChild MOVES an existing child
    this.childNodes.push(node);
    node.parentNode = this;
    this._text = "";
    return node;
  }

  append(...nodes) {
    for (const n of nodes) this.appendChild(n);
  }

  querySelector(sel) {
    for (const child of this.childNodes) {
      if (matches(child, sel)) return child;
      const found = child.querySelector(sel);
      if (found) return found;
    }
    return null;
  }

  remove() {
    if (this.parentNode) this.parentNode._detach(this);
    this.parentNode = null;
  }

  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }

  click() {
    // What a browser does when a <summary> is activated by mouse or keyboard: fire the
    // listeners, then toggle the fold.
    for (const fn of this._listeners.click || []) fn();
    if (this.parentNode && this.parentNode.tagName === "details") {
      this.parentNode.open = !this.parentNode.open;
    }
  }
}

// -- lift the functions under test out of chat.js ----------------------------

function fnSrc(name) {
  const start = CHAT_JS.indexOf("\nfunction " + name + "(");
  assert.ok(start >= 0, "chat.js has no top-level function " + name);
  const end = CHAT_JS.indexOf("\n}", start);
  assert.ok(end > start, "could not find the end of " + name);
  return CHAT_JS.slice(start + 1, end + 2);
}

const LIFTED = [
  "maybeScroll",
  "addRow",
  "showPending",
  "clearPending",
  "streamingRow",
  "appendDelta",
  "appendReasoning",
  "endReasoning",
  "finalizeAssistant",
  "onDelta",
];

// The module-level state chat.js keeps, plus the one collaborator that is stubbed
// rather than lifted: `setTurnState` reaches the composer, the stop control and the
// input's focus, none of which this harness has. It records what it was asked for.
const PRELUDE = [
  "let asstRow = null, asstRaw = '', reasonRow = null, reasonRaw = '';",
  "let pendingRow = null, stick = true, turnState = 'thinking';",
  "const turnStates = [];",
  "function setTurnState(s) { turnState = s; turnStates.push(s); }",
].join("\n");

const EPILOGUE = [
  "globalThis.api = { streamingRow, appendDelta, appendReasoning, endReasoning,",
  "  finalizeAssistant, onDelta, showPending, clearPending };",
  "globalThis.readState = () => ({ asstRaw, reasonRaw, turnState, turnStates,",
  "  pendingRow: pendingRow, hasReasonRow: reasonRow !== null });",
].join("\n");

function newChat() {
  const messages = new El("div");
  const sandbox = {
    document: {
      createElement: (tag) => new El(tag),
      createTextNode: (t) => {
        const n = new El("#text");
        n.textContent = t;
        return n;
      },
    },
    $: (id) => {
      assert.equal(id, "messages");
      return messages;
    },
    // The real label helper (it is the thing the fold's summary is asserted against);
    // markdown rendering stubbed to "plain text please", which is chat.js's own
    // documented fallback and keeps textContent assertions readable.
    ChatCore: Object.assign({}, CHAT_CORE, { renderMarkdown: () => null }),
  };
  vm.createContext(sandbox);
  vm.runInContext(PRELUDE + "\n" + LIFTED.map(fnSrc).join("\n") + "\n" + EPILOGUE, sandbox);
  return { messages, ...sandbox.api, readState: sandbox.readState };
}

const kindOf = (r) =>
  r.className.includes("row-reasoning")
    ? "reasoning"
    : r.className.includes("row-think")
      ? "think"
      : "assistant";

const kinds = (messages) => messages.childNodes.map(kindOf);

const row = (messages, i) => messages.childNodes[i];

// -- the ordering the defect was about ---------------------------------------

test("a think that starts mid-answer never leaves the reply above it", () => {
  const c = newChat();
  c.appendReasoning("first think ");
  c.appendDelta("partial answer ");
  c.appendReasoning("second think ");
  c.appendDelta("rest of the answer");
  c.finalizeAssistant("partial answer rest of the answer"); // the closing `message` frame

  // Both thinks, then the answer. Before the fix the answer row sat between them and
  // the second think rendered BELOW a reply that was already finished.
  assert.deepEqual(kinds(c.messages), ["reasoning", "reasoning", "assistant"]);
  assert.equal(row(c.messages, 2).textContent, "partial answer rest of the answer");
});

test("the reply is printed once, not once per think", () => {
  // The trap in the obvious repair: null the answer row so the next delta opens a fresh
  // one, and the `message` frame — which carries the WHOLE assistant message — writes
  // the full reply into that fresh row while the first half is still on screen above.
  const c = newChat();
  c.appendReasoning("think one ");
  c.appendDelta("partial answer ");
  c.appendReasoning("think two ");
  c.appendDelta("rest");
  c.finalizeAssistant("partial answer rest");

  const transcript = c.messages.childNodes.map((r) => r.textContent).join("\n");
  assert.equal(transcript.split("partial answer").length - 1, 1, "the reply is duplicated");
  assert.equal(c.messages.childNodes.filter((r) => !r.className.includes("row-reasoning")).length, 1);
});

test("the ordinary shape — think, then answer — is untouched", () => {
  const c = newChat();
  c.appendReasoning("thinking about it ");
  c.appendDelta("hello");
  c.finalizeAssistant("hello");
  assert.deepEqual(kinds(c.messages), ["reasoning", "assistant"]);
  assert.equal(row(c.messages, 1).textContent, "hello");
});

test("thinking never reaches the answer row's accumulator", () => {
  const c = newChat();
  c.appendReasoning("SCRATCH_THINKING");
  c.appendDelta("the reply");
  assert.equal(c.readState().asstRaw, "the reply");
  assert.ok(!c.readState().asstRaw.includes("SCRATCH_THINKING"));
});

// -- the fold ----------------------------------------------------------------

test("a fold nobody touched closes when the answer starts", () => {
  const c = newChat();
  c.appendReasoning("thinking");
  const details = row(c.messages, 0).querySelector("details");
  assert.equal(details.open, true, "a live think is open — a folded one is a dead window");
  c.appendDelta("the reply");
  assert.equal(details.open, false);
  assert.equal(row(c.messages, 0).querySelector("summary").textContent, CHAT_CORE.reasoningSummary(false));
});

test("a fold the reader opened is left alone when the answer starts", () => {
  // Reading a live think and having it slam shut mid-sentence is the whole complaint.
  const c = newChat();
  c.appendReasoning("a long think ");
  const details = row(c.messages, 0).querySelector("details");
  const summary = row(c.messages, 0).querySelector("summary");
  summary.click(); // the reader closes it…
  summary.click(); // …and opens it again: either way, it is theirs now
  assert.equal(details.open, true);
  c.appendDelta("the reply");
  assert.equal(details.open, true, "the reader's fold was slammed shut");
  // It still stops CLAIMING to think, and it still stops streaming.
  assert.equal(summary.textContent, CHAT_CORE.reasoningSummary(false));
  assert.ok(!row(c.messages, 0).className.includes("streaming"));
});

// -- the two channels must not reach across into each other -------------------

test("an EMPTY answer delta still clears the pending row and starts streaming", () => {
  // The default (Copilot) provider broadcasts `delta_content` frames that can be "",
  // and an empty one has always been what takes down the "· thinking▋" row and flips
  // the turn state. A blanket `if (!text) return;` at the top of the handler — added
  // for the reasoning channel's benefit — silently changed that for every provider.
  const c = newChat();
  c.showPending();
  assert.deepEqual(kinds(c.messages), ["think"]);

  c.onDelta({ text: "" });

  assert.equal(c.readState().turnState, "streaming");
  assert.equal(c.readState().pendingRow, null, "the pending row survived an empty delta");
});

test("a delta with no text at all appends nothing — not the word 'undefined'", () => {
  const c = newChat();
  c.onDelta({});
  assert.equal(c.readState().asstRaw, "");
});

test("an empty THINK delta leaves the answer channel exactly as it was", () => {
  // Nothing to show, and nothing of the answer has started: the pending row and the
  // "thinking" state both belong to a reply that has not begun.
  const c = newChat();
  c.showPending();
  c.onDelta({ text: "", reasoning: true });
  assert.equal(c.readState().turnState, "thinking");
  assert.notEqual(c.readState().pendingRow, null);
  assert.deepEqual(kinds(c.messages), ["think"]);
});

test("a think delta clears the pending row but does NOT claim the answer is streaming", () => {
  const c = newChat();
  c.showPending();
  c.onDelta({ text: "hmm, the join key…", reasoning: true });
  assert.equal(c.readState().pendingRow, null);
  assert.equal(c.readState().turnState, "thinking", "the reply has not started");
  assert.deepEqual(kinds(c.messages), ["reasoning"]);
  assert.equal(c.readState().asstRaw, "");
});

test("endReasoning is a safe no-op when there is no think", () => {
  const c = newChat();
  c.appendDelta("just an answer");
  c.endReasoning();
  c.endReasoning();
  c.appendDelta(" continued");
  assert.deepEqual(kinds(c.messages), ["assistant"]);
  assert.equal(row(c.messages, 0).textContent, "just an answer continued");
});
