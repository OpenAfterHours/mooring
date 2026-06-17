"use strict";

// Unit tests for the copilot REPL's pure helpers. Zero dependencies: Node's
// built-in test runner + assert. Run with:  node --test tests/js/
// (Node >= 18). These cover the risky pure logic — the slash parser, the
// in-memory history ring, @-mention detection, the additive block, and the
// XSS-safety contract of the highlighter. The DOM/SSE wiring in chat.js is
// covered by manual QA + the Python suite (the hub is unchanged).

const { test } = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../src/mooring/hub/static/chat_core.js");

// A copy of chat.js's escapeHtml so we can prove highlightCode is safe on
// already-escaped model output (the renderer in chat.js stays byte-for-byte).
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

test("parseSlash: command without arg", () => {
  assert.deepEqual(C.parseSlash("/help"), { cmd: "help", arg: "" });
});

test("parseSlash: command with arg, case-folded name", () => {
  assert.deepEqual(C.parseSlash("/Model gpt-4.1"), { cmd: "model", arg: "gpt-4.1" });
});

test("parseSlash: bare slash is an empty command", () => {
  assert.deepEqual(C.parseSlash("/"), { cmd: "", arg: "" });
});

test("parseSlash: '//' escapes to a literal message (not a command)", () => {
  assert.equal(C.parseSlash("//not a command"), null);
  assert.equal(C.unescapeSlash("//not a command"), "/not a command");
});

test("parseSlash: ordinary prose is not a command", () => {
  assert.equal(C.parseSlash("filter to 2024"), null);
  assert.equal(C.unescapeSlash("filter to 2024"), "filter to 2024");
});

test("isSlashTyping: shows the menu only while typing the command word", () => {
  assert.equal(C.isSlashTyping("/mo"), true);
  assert.equal(C.isSlashTyping("/model "), false); // space -> arg started
  assert.equal(C.isSlashTyping("//x"), false); // escaped literal
  assert.equal(C.isSlashTyping("hello"), false);
});

test("filterCommands: prefix filter", () => {
  const names = C.filterCommands("c").map((c) => c.name);
  assert.deepEqual(names, ["clear"]);
  assert.equal(C.filterCommands("").length, C.COMMANDS.length);
});

test("HistoryRing: push dedups consecutive repeats and skips blanks", () => {
  const h = new C.HistoryRing();
  h.push("a");
  h.push("a");
  h.push("   ");
  h.push("b");
  assert.deepEqual(h.items, ["a", "b"]);
});

test("HistoryRing: prev/next walk older->newer and restore the draft", () => {
  const h = new C.HistoryRing();
  h.push("first");
  h.push("second");
  assert.equal(h.prev("draft-in-progress"), "second"); // first Up: newest
  assert.equal(h.prev(), "first");
  assert.equal(h.prev(), "first"); // clamp at oldest
  assert.equal(h.next(), "second");
  assert.equal(h.next(), "draft-in-progress"); // past newest -> stashed draft
  assert.equal(h.next(), null); // not navigating anymore
});

test("HistoryRing: respects max size", () => {
  const h = new C.HistoryRing(2);
  h.push("a");
  h.push("b");
  h.push("c");
  assert.deepEqual(h.items, ["b", "c"]);
});

test("mentionMatch: detects an @token at the caret", () => {
  assert.deepEqual(C.mentionMatch("show @sal", 9), { start: 5, query: "sal" });
  assert.deepEqual(C.mentionMatch("@x", 2), { start: 0, query: "x" });
  assert.deepEqual(C.mentionMatch("@", 1), { start: 0, query: "" });
});

test("mentionMatch: no match mid-word or after the token closed", () => {
  assert.equal(C.mentionMatch("email@addr", 10), null); // not preceded by space/start
  assert.equal(C.mentionMatch("show @sal now", 13), null); // token already ended
});

test("filterDatasets + applyMention", () => {
  const ds = ["sales.parquet", "customers.csv", "sales_2024.parquet"];
  assert.deepEqual(C.filterDatasets(ds, "sales"), ["sales.parquet", "sales_2024.parquet"]);
  assert.equal(C.applyMention("show @sal", 5, 9, "sales.parquet"), "show @sales.parquet ");
});

test("additiveBlockLines: one '+' entry per line, trailing newline trimmed", () => {
  const lines = C.additiveBlockLines("a = 1\nb = 2\n");
  assert.deepEqual(lines, [
    { gutter: "+", text: "a = 1" },
    { gutter: "+", text: "b = 2" },
  ]);
});

test("piiBadge: null status renders nothing", () => {
  assert.equal(C.piiBadge(null), null);
  assert.equal(C.piiBadge(undefined), null);
});

test("piiBadge: guard off -> red 'PII-off' with a not-protected tooltip", () => {
  const b = C.piiBadge({ enabled: false, block: true, names: false, names_active: false });
  assert.equal(b.text, "PII-off");
  assert.equal(b.cls, "off");
  assert.match(b.title, /OFF/);
  assert.match(b.title, /schema-only guarantee still holds/);
});

test("piiBadge: guard on, names ready -> green 'PII-active' naming the backend", () => {
  const b = C.piiBadge({ enabled: true, block: true, names: true, names_active: true, backend: "spacy" });
  assert.equal(b.text, "PII-active");
  assert.equal(b.cls, "on");
  assert.match(b.title, /person\/organisation names \(spacy\)/);
  assert.match(b.title, /holds the message for your confirmation/);
});

test("piiBadge: guard on, names configured but not ready -> green with a caveat", () => {
  const b = C.piiBadge({ enabled: true, block: false, names: true, names_active: false, backend: "gliner" });
  assert.equal(b.cls, "on");
  assert.match(b.title, /isn't ready yet/);
  assert.match(b.title, /still sent/); // warn-only mode
});

test("piiBadge: guard on, name detection off -> green, no names clause", () => {
  const b = C.piiBadge({ enabled: true, block: true, names: false, names_active: false });
  assert.equal(b.cls, "on");
  assert.ok(!/names/.test(b.title), "no names clause when detection is off");
});

test("highlightCode: wraps keywords/strings/comments", () => {
  const out = C.highlightCode(escapeHtml('def f():  # note\n    return "x"'));
  assert.match(out, /<span class="tok-kw">def<\/span>/);
  assert.match(out, /<span class="tok-kw">return<\/span>/);
  assert.match(out, /<span class="tok-com"># note<\/span>/);
  assert.match(out, /<span class="tok-str">"x"<\/span>/);
});

test("highlightCode: XSS-safe on hostile model output (escape-first contract)", () => {
  const hostile = 'x = "</script><img src=x onerror=alert(1)>"  # def class return';
  const out = C.highlightCode(escapeHtml(hostile));
  // No live markup may survive: only our own <span class="tok-..."> tags. The
  // structural check below is the real guarantee — every '<' begins one of our
  // spans, so no source-derived tag (<img>, </script>, …) can exist.
  assert.ok(!/<img/i.test(out), "no raw <img>");
  assert.ok(!/<\/script>/i.test(out), "no raw </script>");
  for (const m of out.matchAll(/</g)) {
    const tail = out.slice(m.index, m.index + 20);
    assert.ok(
      tail.startsWith('<span class="tok-') || tail.startsWith("</span>"),
      "stray '<' in highlighted output: " + tail
    );
  }
});

test("highlightCode: keyword highlighting never corrupts its own span attributes", () => {
  // 'class' is a Python keyword; ensure a string containing it doesn't make the
  // pass rewrite the word 'class' inside an attribute we just inserted.
  const out = C.highlightCode(escapeHtml('class C: pass'));
  assert.match(out, /<span class="tok-kw">class<\/span>/);
  assert.ok(!/class="tok-kw"[^>]*tok-kw/.test(out), "attribute not double-wrapped");
});
