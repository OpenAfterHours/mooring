"use strict";

// Unit tests for the copilot REPL's pure helpers. Zero dependencies: Node's
// built-in test runner + assert. Run with:  node --test tests/js/
// (Node >= 18). These cover the risky pure logic — the slash parser, the
// in-memory history ring, @-mention detection, the additive block, and the
// XSS-safety contract of the highlighter. The DOM/SSE wiring in chat.js is
// covered by manual QA + the Python suite (the hub is unchanged).

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const C = require("../../src/mooring/hub/static/chat_core.js");

// chat.js's printHelp keeps its OWN hand-written rows, so a command added to COMMANDS
// (which drives the "/" autocomplete) can silently go undocumented — /review, /checks and
// /sql all did. Pin the two lists together.
test("every slash command in COMMANDS is documented in chat.js's /help", () => {
  const chatJs = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "chat.js"),
    "utf8",
  );
  // printHelp rows look like  ["/name", "…"]  or  ["/name <topic>", "…"].
  const documented = new Set(
    [...chatJs.matchAll(/\["\/([a-z]+)[^"]*",/g)].map((m) => m[1]),
  );
  const missing = C.COMMANDS.map((c) => c.name).filter((n) => !documented.has(n));
  assert.deepEqual(missing, [], `commands missing from /help: ${missing.join(", ")}`);
});

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
  assert.deepEqual(names, ["checks", "clear"]);
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

test("undo is a command and filters by prefix", () => {
  assert.ok(C.COMMANDS.some((c) => c.name === "undo"));
  assert.deepEqual(C.filterCommands("u").map((c) => c.name), ["undo"]);
});

test("diffLines: a one-line change keeps context, marks - then +", () => {
  const d = C.diffLines("a = 1\nb = 2\nc = 3", "a = 1\nb = 99\nc = 3");
  assert.deepEqual(d, [
    { gutter: " ", text: "a = 1" },
    { gutter: "-", text: "b = 2" },
    { gutter: "+", text: "b = 99" },
    { gutter: " ", text: "c = 3" },
  ]);
});

test("diffLines: empty before -> all additions; empty after -> all removals", () => {
  assert.deepEqual(C.diffLines("", "x = 1\ny = 2"), [
    { gutter: "+", text: "x = 1" },
    { gutter: "+", text: "y = 2" },
  ]);
  assert.deepEqual(C.diffLines("x = 1\ny = 2", ""), [
    { gutter: "-", text: "x = 1" },
    { gutter: "-", text: "y = 2" },
  ]);
});

test("diffLines: identical source is all context (no +/-)", () => {
  const d = C.diffLines("a = 1\nb = 2\n", "a = 1\nb = 2\n");
  assert.deepEqual(d, [
    { gutter: " ", text: "a = 1" },
    { gutter: " ", text: "b = 2" },
  ]);
});

test("diffLines: caps very large inputs with a coarse all-removed-then-added fallback", () => {
  const big = Array.from({ length: 600 }, (_, i) => `l${i}`).join("\n");
  const d = C.diffLines(big, big + "\nextra"); // 600 * 601 > the cap
  assert.equal(d.length, 1201); // 600 removed + 601 added (no minimal-LCS table built)
  assert.equal(d[0].gutter, "-");
  assert.equal(d[d.length - 1].gutter, "+");
});

test("diffLines: a pure insertion in the middle is a single + line", () => {
  const d = C.diffLines("a = 1\nc = 3", "a = 1\nb = 2\nc = 3");
  assert.deepEqual(d, [
    { gutter: " ", text: "a = 1" },
    { gutter: "+", text: "b = 2" },
    { gutter: " ", text: "c = 3" },
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

test("piiBadge: guard on, names configured but not available -> amber 'PII-partial'", () => {
  const b = C.piiBadge({ enabled: true, block: false, names: true, names_active: false, backend: "gliner" });
  assert.equal(b.text, "PII-partial");
  assert.equal(b.cls, "partial"); // NOT solid green — names aren't actually scanned
  assert.match(b.title, /NAMES are NOT/);
  assert.match(b.title, /still sent/); // warn-only mode
});

test("piiBadge: guard on, name detection off -> green, no names clause", () => {
  const b = C.piiBadge({ enabled: true, block: true, names: false, names_active: false });
  assert.equal(b.text, "PII-active");
  assert.equal(b.cls, "on");
  assert.ok(!/names/i.test(b.title), "no names clause when detection is off");
});

test("scanErrorMessage: names-only failure does NOT claim the message went unchecked", () => {
  const m = C.scanErrorMessage("names");
  assert.match(m, /Name detection couldn't run/);
  assert.match(m, /scanned for structured PII/);
  assert.ok(!/unchecked/.test(m), "a names-only failure still scanned structured PII");
});

test("scanErrorMessage: a structured failure is the only 'sent unchecked' case", () => {
  assert.match(C.scanErrorMessage("structured"), /sent unchecked/);
  assert.match(C.scanErrorMessage("both"), /sent unchecked/);
});

test("tracebackHoldSummary: counts a single redaction and states the no-raw contract", () => {
  const msg = C.tracebackHoldSummary([{ line: 3, kind: "exception message redacted" }], []);
  assert.match(msg, /1 redaction\)/);
  assert.ok(!/redactions\)/.test(msg), "singular for one redaction");
  assert.match(msg, /raw paste was not kept/);
  assert.match(msg, /Send/i);
});

test("tracebackHoldSummary: pluralises and appends deduped prose-PII kinds", () => {
  const msg = C.tracebackHoldSummary(
    [
      { line: 3, kind: "exception message redacted" },
      { line: 4, kind: "unrecognised line redacted" },
    ],
    [
      { line: 1, kind: "payment card" },
      { line: 2, kind: "payment card" },
    ]
  );
  assert.match(msg, /2 redactions/);
  assert.match(msg, /payment card/);
  assert.equal(msg.split("payment card").length - 1, 1, "kinds are deduped");
});

test("tracebackHoldSummary: zero redactions reads as clean, no PII clause", () => {
  const msg = C.tracebackHoldSummary([], []);
  assert.match(msg, /nothing needed redacting/);
  assert.ok(!/also looks like/.test(msg), "no PII sentence without findings");
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

test("cleanJobs: trims fields, keeps internal newlines, drops briefless rows", () => {
  const jobs = C.cleanJobs([
    { name: " Sales ", brief: "  chart monthly sales\nbroken out by region  ", dataset: " data/s.parquet " },
    { name: "empty", brief: "   ", dataset: "" }, // dropped: no brief
    { brief: "model churn" }, // name/dataset default to "" (server names it)
  ]);
  assert.deepEqual(jobs, [
    { name: "Sales", brief: "chart monthly sales\nbroken out by region", dataset: "data/s.parquet" },
    { name: "", brief: "model churn", dataset: "" },
  ]);
});

test("cleanJobs: tolerates empty / missing input", () => {
  assert.deepEqual(C.cleanJobs([]), []);
  assert.deepEqual(C.cleanJobs(null), []);
  assert.deepEqual(C.cleanJobs([{}, { brief: "" }]), []);
});

test("parseDeviceLogin: extracts the one-time code + URL from real CLI output", () => {
  // The exact lines `copilot login` prints to stdout (captured live).
  const r = C.parseDeviceLogin([
    "To authenticate, visit https://github.com/login/device and enter code 4B02-8583.",
    "Waiting for authorization...",
    "Failed to copy to clipboard. Please visit https://github.com/login/device and enter the code 4B02-8583 manually.",
  ]);
  assert.equal(r.code, "4B02-8583");
  assert.equal(r.url, "https://github.com/login/device"); // trailing "." stripped
  assert.equal(r.lines.length, 3);
});

test("parseDeviceLogin: no code printed yet -> empty fields, output preserved", () => {
  const r = C.parseDeviceLogin(["Waiting for authorization..."]);
  assert.equal(r.code, "");
  assert.equal(r.url, "");
  assert.deepEqual(r.lines, ["Waiting for authorization..."]);
});

test("parseDeviceLogin: tolerates missing / non-array output", () => {
  assert.deepEqual(C.parseDeviceLogin(undefined), { code: "", url: "", lines: [] });
  assert.deepEqual(C.parseDeviceLogin(null), { code: "", url: "", lines: [] });
  assert.deepEqual(C.parseDeviceLogin("nope"), { code: "", url: "", lines: [] });
});

test("parseDeviceLogin: a bare code without a URL still parses", () => {
  const r = C.parseDeviceLogin(["your code is ABCD-1234"]);
  assert.equal(r.code, "ABCD-1234");
  assert.equal(r.url, "");
});

// -- /explain: the handover walkthrough --------------------------------------
// These pin the LOAD-BEARING wording of the canned prompts. The privacy story is
// that they are pure constants — fixed, value-free text over the existing chat
// channel — so any wording change must show up here, review-visible.

test("explain is a command: parseSlash and filterCommands pick it up", () => {
  assert.ok(C.COMMANDS.some((c) => c.name === "explain"));
  assert.deepEqual(C.parseSlash("/explain"), { cmd: "explain", arg: "" });
  assert.deepEqual(C.filterCommands("ex").map((c) => c.name), ["explain"]);
});

test("explainPrompt: a pure constant — identical bytes on every call", () => {
  const p = C.explainPrompt();
  assert.equal(typeof p, "string");
  assert.equal(p, C.explainPrompt()); // no interpolation, no user text, no values
});

test("explainPrompt: reads the notebook source FIRST, schema tool second", () => {
  const p = C.explainPrompt();
  const read = p.indexOf("mooring_read_notebook_source");
  const schema = p.indexOf("mooring_get_schema");
  assert.ok(read !== -1, "names mooring_read_notebook_source");
  assert.ok(schema !== -1, "names mooring_get_schema");
  assert.ok(read < schema, "read_notebook_source is the first tool named");
  assert.match(p, /First, call mooring_read_notebook_source/);
});

test("explainPrompt: demands `cell N` anchors matching read_notebook_source framing", () => {
  const p = C.explainPrompt();
  assert.match(p, /`cell N`/); // every claim cites a checkable anchor…
  assert.match(p, /# === cell N ===/); // …in the exact framing the tool emits
  assert.match(p, /Every claim must cite the `cell N`/);
});

test("explainPrompt: opens with the verify-first header and fixes the sections", () => {
  const p = C.explainPrompt();
  assert.match(
    p,
    /Generated by the copilot from the notebook source — verify against the notebook before relying on it\./
  );
  for (const section of [
    "Purpose",
    "Inputs it reads",
    "Pipeline stages",
    "Outputs it writes",
    "Things to change each period",
  ]) {
    assert.ok(p.includes(section), "walkthrough section: " + section);
  }
  assert.match(p, /group related cells into stages/); // large-notebook grouping
});

test("explainLabel: the compact visible transcript row, also a constant", () => {
  assert.equal(C.explainLabel(), "/explain — walk me through this notebook");
  assert.equal(C.explainLabel(), C.explainLabel());
});

// /checks: propose value-free tie-out checks. Same value-free-constant discipline as
// /explain — the fixed prompt has no user text and no data values, so any wording
// change is review-visible here.

test("checks is a command: parseSlash and filterCommands pick it up", () => {
  assert.ok(C.COMMANDS.some((c) => c.name === "checks"));
  assert.deepEqual(C.parseSlash("/checks"), { cmd: "checks", arg: "" });
  assert.ok(C.filterCommands("che").map((c) => c.name).includes("checks"));
});

test("checksPrompt: a pure constant that names the value-free mooring_checks API", () => {
  const p = C.checksPrompt();
  assert.equal(typeof p, "string");
  assert.equal(p, C.checksPrompt()); // no interpolation, no user text, no values
  assert.match(p, /mooring_checks/);
  for (const fn of ["unique_key", "no_fanout", "not_null", "reconciles", "row_delta"]) {
    assert.ok(p.includes(fn), fn);
  }
  // It must propose (review-then-apply), never ask for data values.
  assert.match(p, /mooring_propose_cell/);
  assert.match(p, /never ask for data values/);
});

test("checksLabel: the compact visible transcript row, also a constant", () => {
  assert.equal(C.checksLabel(), "/checks — propose tie-out checks for this notebook");
  assert.equal(C.checksLabel(), C.checksLabel());
});

test("sqlPrompt: a pure constant that names the value-free mo.sql/DuckDB idiom", () => {
  const p = C.sqlPrompt();
  assert.equal(typeof p, "string");
  assert.equal(p, C.sqlPrompt()); // no interpolation, no user text, no values
  assert.match(p, /mo\.sql/);
  assert.match(p, /DuckDB/);
  // Propose (review-then-apply), schema-only, no SELECT * to "peek" and no data values.
  assert.match(p, /mooring_propose_cell/);
  assert.match(p, /no SELECT \*/);
  assert.match(p, /never inline a /);
  // The applied cell must actually run: the import + the duckdb dependency (review).
  assert.match(p, /import marimo as mo/);
  assert.match(p, /duckdb/);
  // Value-blindness caveat: no value->header pivots (their column names would be values).
  assert.match(p, /PIVOT/);
});

test("sqlLabel: the compact visible transcript row, also a constant", () => {
  assert.equal(C.sqlLabel(), "/sql — propose a SQL cell for this notebook");
  assert.equal(C.sqlLabel(), C.sqlLabel());
});

test("COMMANDS includes /sql", () => {
  assert.ok(C.COMMANDS.some((c) => c.name === "sql"));
});

test("investigatePrompt: a fixed wrapper around the analyst's own topic", () => {
  const p = C.investigatePrompt("how revenue is computed");
  assert.equal(typeof p, "string");
  // The topic is interpolated (this prompt is NOT a pure constant, unlike /sql).
  assert.match(p, /how revenue is computed/);
  assert.notEqual(p, C.investigatePrompt("something else"));
  // The wrapper's load-bearing demands: fan out ONCE over INDEPENDENT sub-questions,
  // the branches are read-only, no data value ever goes into a sub-question, and the
  // turn ends in ONE human-applied proposal.
  assert.match(p, /mooring_investigate ONCE/);
  assert.match(p, /INDEPENDENT sub-questions/);
  assert.match(p, /read-only assistant/);
  assert.match(p, /cannot write/);
  assert.match(p, /Never put a data value in a sub-question/);
  assert.match(p, /propose ONE\s+change/);
  // It must not fan out when the topic doesn't decompose (fan-out has real cost).
  assert.match(p, /does not actually split into independent parts/);
  // Graceful degradation: with [ai.investigate] off the tool is never registered, so the
  // prompt must tell the model to answer the question rather than apologise for a missing
  // tool the analyst deliberately turned off.
  assert.match(p, /not available to you/);
  assert.match(p, /do not mention it and do not apologise/);
  assert.match(p, /research the topic yourself with the read tools and answer/);
});

test("investigatePrompt: trims the topic and tolerates a missing one", () => {
  assert.equal(C.investigatePrompt("  spaced  "), C.investigatePrompt("spaced"));
  // A blank/absent topic must not throw — chat.js refuses it before sending, but the
  // pure helper stays total.
  for (const t of [undefined, null, "", "   "]) {
    assert.equal(typeof C.investigatePrompt(t), "string");
  }
});

test("investigateLabel: the transcript row carries the analyst's topic", () => {
  assert.equal(C.investigateLabel("join keys"), "/investigate — join keys");
  assert.equal(C.investigateLabel("  join keys "), "/investigate — join keys");
  assert.equal(C.investigateLabel(undefined), "/investigate — ");
});

test("investigate is a command: parseSlash and filterCommands pick it up", () => {
  assert.ok(C.COMMANDS.some((c) => c.name === "investigate"));
  assert.deepEqual(C.parseSlash("/investigate join keys"), {
    cmd: "investigate",
    arg: "join keys",
  });
  assert.deepEqual(C.parseSlash("/investigate"), { cmd: "investigate", arg: "" });
  assert.deepEqual(C.filterCommands("i").map((c) => c.name), ["investigate"]);
});

test("notesCellPrompt: mooring_propose_cell ONLY — edit/rewrite tools forbidden", () => {
  const p = C.notesCellPrompt();
  assert.equal(p, C.notesCellPrompt()); // constant
  assert.match(p, /ONE new/);
  assert.match(p, /mooring_propose_cell tool only/);
  // The walkthrough may never clobber an existing cell: each write tool is
  // named and forbidden, so a model can't "helpfully" reach for a rewrite.
  assert.match(p, /never\s+mooring_propose_cell_edit/);
  assert.match(p, /mooring_propose_notebook_edit/);
  assert.match(p, /mooring_propose_notebook_rewrite/);
  assert.match(p, /do not touch any existing cell/);
});

test("notesCellPrompt: disclaimer INSIDE the cell; mo.md only if marimo is imported", () => {
  const p = C.notesCellPrompt();
  assert.match(p, /must begin with this disclaimer line/);
  assert.match(
    p,
    /Generated by the copilot from the notebook source — verify against the notebook before relying on it\./
  );
  // mo.md needs `import marimo as mo`; only the model can see whether it exists,
  // and it must check the CURRENT source rather than blindly adding the import.
  assert.match(p, /mooring_read_notebook_source/);
  assert.match(p, /mo\.md\(\.\.\.\) only if `import marimo as mo` already exists/);
  assert.match(p, /never add that import/);
});

// /review: a whole-notebook value-blind LOGIC review. Same value-free-constant
// discipline as /explain — no user text, no data values; wording changes are
// review-visible here.

test("review is a command: parseSlash and filterCommands pick it up", () => {
  assert.ok(C.COMMANDS.some((c) => c.name === "review"));
  assert.deepEqual(C.parseSlash("/review"), { cmd: "review", arg: "" });
  assert.ok(C.filterCommands("rev").map((c) => c.name).includes("review"));
});

test("reviewPrompt: a pure constant — identical bytes on every call", () => {
  const p = C.reviewPrompt();
  assert.equal(typeof p, "string");
  assert.equal(p, C.reviewPrompt()); // no interpolation, no user text, no values
});

test("reviewPrompt: reads source + schema, cites cell N, and is read-only", () => {
  const p = C.reviewPrompt();
  assert.match(p, /mooring_read_notebook_source/);
  assert.match(p, /mooring_get_schema/);
  assert.match(p, /`cell N`/); // anchors findings to checkable cells
  assert.match(p, /# === cell N ===/); // the exact framing the tool emits
  // Value-blind + read-only guardrails must be present.
  assert.match(p, /never the data values/i);
  assert.match(p, /do NOT ask for data values/i);
  assert.match(p, /do NOT propose code changes/i);
});

test("reviewLabel: the compact visible transcript row, also a constant", () => {
  assert.equal(C.reviewLabel(), "/review — check this notebook's logic for risks");
  assert.equal(C.reviewLabel(), C.reviewLabel());
});

// -- renderMarkdown: GFM output rendering + the escape-first XSS contract -----
// The copilot's replies are streamed model text. renderMarkdown makes them
// read-friendly (tables, headings, lists, links, quotes) while guaranteeing NO
// raw model output ever reaches innerHTML: it escapes < > & first, then only
// splices in mooring's own tags. These pin BOTH the rendering and that contract.

test("renderMarkdown: a pipe table becomes a <table> with headers, cells, alignment", () => {
  const md = "| Region | Sales |\n|:---|---:|\n| North | 1240 |\n| South | 980 |";
  const html = C.renderMarkdown(md);
  assert.match(html, /<table/);
  assert.match(html, /<th[^>]*>Region<\/th>/);
  assert.match(html, /<th[^>]*>Sales<\/th>/);
  assert.match(html, /<td[^>]*>North<\/td>/);
  assert.match(html, /<td[^>]*>1240<\/td>/);
  assert.match(html, /class="md-al-left"/); // Region left-aligned
  assert.match(html, /class="md-al-right"/); // Sales right-aligned
  assert.match(html, /md-table-wrap/); // wrapped so it scrolls, not the pane
});

test("renderMarkdown: a lone '|' in prose is NOT a table (needs a delimiter row)", () => {
  const html = C.renderMarkdown("a | b is fine in a sentence");
  assert.ok(!/<table/.test(html));
  assert.match(html, /a \| b is fine/);
});

test("renderMarkdown: a table cell escapes HTML (no injection through a cell)", () => {
  const md = "| a | b |\n|---|---|\n| <img src=x onerror=alert(1)> | y |";
  const html = C.renderMarkdown(md);
  assert.ok(!/<img/.test(html), "a raw <img> must never appear");
  assert.match(html, /&lt;img/);
});

test("renderMarkdown: headings h1..h6 and an ordered list", () => {
  assert.match(C.renderMarkdown("# Title"), /<h1>Title<\/h1>/);
  assert.match(C.renderMarkdown("### Sub"), /<h3>Sub<\/h3>/);
  assert.match(C.renderMarkdown("###### Deep"), /<h6>Deep<\/h6>/);
  const ol = C.renderMarkdown("1. one\n2. two");
  assert.match(ol, /<ol><li>one<\/li><li>two<\/li><\/ol>/);
});

test("renderMarkdown: '#Nospace' is not a heading (ATX needs a space)", () => {
  const html = C.renderMarkdown("#tag not a heading");
  assert.ok(!/<h1>/.test(html));
  assert.match(html, /#tag not a heading/);
});

test("renderMarkdown: nested list folds by indent", () => {
  const html = C.renderMarkdown("- a\n  - b\n- c");
  assert.match(html, /<ul><li>a<ul><li>b<\/li><\/ul><\/li><li>c<\/li><\/ul>/);
});

test("renderMarkdown: a task list renders inert check markers, never <input>", () => {
  const html = C.renderMarkdown("- [ ] todo\n- [x] done");
  assert.ok(!/<input/.test(html), "no form controls");
  assert.match(html, /☐/);
  assert.match(html, /☑/);
  assert.match(html, /class="md-task"/);
});

test("renderMarkdown: blockquote — the '>' marker survives escaping and is consumed", () => {
  const html = C.renderMarkdown("> quoted line");
  assert.match(html, /<blockquote>[\s\S]*quoted line[\s\S]*<\/blockquote>/);
  assert.ok(!/&gt;/.test(html), "the > marker is consumed, not shown as text");
});

test("renderMarkdown: a safe link is linked; javascript: is neutralised to text", () => {
  const ok = C.renderMarkdown("[docs](https://example.com)");
  assert.match(ok, /<a href="https:\/\/example\.com" target="_blank" rel="noopener noreferrer">docs<\/a>/);
  const bad = C.renderMarkdown("[x](javascript:alert(1))");
  assert.ok(!/href="javascript/i.test(bad), "a javascript: URL must be rejected");
  assert.match(bad, /x/, "the label is preserved as plain text");
});

test("renderMarkdown: a link href cannot break out of the attribute", () => {
  const html = C.renderMarkdown('[x](https://a" onmouseover=alert(1))');
  // Either dropped, or the quote is entity-encoded — never a bare quote in href.
  assert.ok(!/href="https:\/\/a" onmouseover/.test(html));
});

test("renderMarkdown: data: and vbscript: URLs are rejected too", () => {
  assert.ok(!/href="data:/i.test(C.renderMarkdown("[a](data:text/html,<script>0</script>)")));
  assert.ok(!/href="vbscript:/i.test(C.renderMarkdown("[a](vbscript:msgbox(1))")));
});

test("renderMarkdown: inline bold / italic / code / strikethrough", () => {
  const html = C.renderMarkdown("**b** *i* `c` ~~s~~");
  assert.match(html, /<strong>b<\/strong>/);
  assert.match(html, /<em>i<\/em>/);
  assert.match(html, /<code>c<\/code>/);
  assert.match(html, /<del>s<\/del>/);
});

test("renderMarkdown: raw HTML in prose is escaped (the core XSS contract)", () => {
  const html = C.renderMarkdown("hello <script>alert(1)</script> <b>x</b>");
  assert.ok(!/<script>/.test(html), "no raw <script>");
  assert.ok(!/<b>x<\/b>/.test(html), "no raw <b> from the model");
  assert.match(html, /&lt;script&gt;/);
});

test("renderMarkdown: a fenced code block is kept verbatim and escaped", () => {
  const html = C.renderMarkdown("```python\nx = '<b>'\n```");
  assert.match(html, /<pre class="cell-code"><code>/);
  assert.match(html, /&lt;b&gt;/);
  assert.ok(!/<b>/.test(html));
});

test("renderMarkdown: plain paragraphs and soft line breaks are preserved", () => {
  const html = C.renderMarkdown("line one\nline two\n\nnew para");
  assert.match(html, /<p>line one<br>line two<\/p>/);
  assert.match(html, /<p>new para<\/p>/);
});

// -- renderMarkdown: hardening fixes from the adversarial review --------------

test("renderMarkdown: a leading control byte cannot smuggle a javascript: scheme", () => {
  // A browser's URL parser ignores a leading C0 control, re-exposing the scheme;
  // mdSafeHref must strip controls BEFORE the allow-list so the check isn't fooled.
  for (const code of [0x00, 0x01, 0x08, 0x1f, 0x7f]) {
    const byte = String.fromCharCode(code);
    const html = C.renderMarkdown("[click](" + byte + "javascript:alert(1))");
    assert.ok(!/href="[^"]*javascript:/i.test(html), "no javascript: href for byte 0x" + code.toString(16));
    assert.ok(!new RegExp("[" + String.fromCharCode(0, 1, 8, 0x1f, 0x7f) + "]").test(html), "no control byte in the output");
  }
  // The safe control case: a normal https link still works.
  assert.match(C.renderMarkdown("[ok](https://e.com)"), /<a href="https:\/\/e\.com"/);
});

test("renderMarkdown: a '*' inside inline code stays verbatim (no straddling <em>)", () => {
  const html = C.renderMarkdown("Match `*.py` or `*.txt` files.");
  assert.match(html, /<code>\*\.py<\/code>/);
  assert.match(html, /<code>\*\.txt<\/code>/);
  assert.ok(!/<em>/.test(html), "emphasis must not straddle the two code spans");
});

test("renderMarkdown: markdown typed inside inline code is literal, not re-parsed", () => {
  const html = C.renderMarkdown("see `[x](y)` and `**b**` literally");
  assert.match(html, /<code>\[x\]\(y\)<\/code>/);
  assert.ok(!/<a /.test(html), "a link inside code stays literal");
  assert.match(html, /<code>\*\*b\*\*<\/code>/);
  assert.ok(!/<strong>/.test(html), "bold inside code stays literal");
});

test("renderMarkdown: emphasis markers in a URL don't inject tags into the href", () => {
  const html = C.renderMarkdown("[a](http://x*y*z)");
  assert.match(html, /<a href="http:\/\/x\*y\*z"/);
  assert.ok(!/<em>/.test(html), "the URL is captured raw, never emphasis-processed");
});

// -- renderMarkdown: fixes from the SECOND adversarial pass -------------------

test("renderMarkdown: bold/italic that WRAPS an inline code span renders (no leaked **)", () => {
  const b = C.renderMarkdown("call the **`df.merge()`** method");
  assert.match(b, /<strong><code>df\.merge\(\)<\/code><\/strong>/);
  assert.ok(!/\*\*/.test(b), "no literal ** left over");
  const it = C.renderMarkdown("use *`x`* here");
  assert.match(it, /<em><code>x<\/code><\/em>/);
  // ...while a '*' INSIDE code still stays literal (the earlier fix holds).
  const inside = C.renderMarkdown("Match `*.py` or `*.txt`.");
  assert.match(inside, /<code>\*\.py<\/code>/);
  assert.ok(!/<em>/.test(inside));
});

test("renderMarkdown: a long run of unmatched '[' renders fast (no O(n^2) freeze)", () => {
  const s = "[".repeat(200000);
  const t0 = process.hrtime.bigint();
  const html = C.renderMarkdown(s);
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  assert.equal(typeof html, "string");
  assert.ok(ms < 2000, "rendered 200k '[' in " + ms.toFixed(0) + "ms — should be well under 2s");
});

test("renderMarkdown: absurdly deep blockquote nesting still renders (never falls back to null)", () => {
  const html = C.renderMarkdown(">".repeat(5000) + " deep");
  assert.equal(typeof html, "string", "must not overflow the stack and return null");
  assert.match(html, /<blockquote>/);
});

test("renderMarkdown: an inline ```code``` span keeps its text (not eaten by the fence split)", () => {
  const html = C.renderMarkdown("run ```pip install foo``` now");
  assert.match(html, /<code>pip install foo<\/code>/);
  assert.match(html, /run <code>pip install foo<\/code> now/);
  assert.ok(!/<pre/.test(html), "same-line triple backticks are inline, not a block");
});

test("renderMarkdown: a real fenced code block (``` on its own line) still renders as a block", () => {
  const html = C.renderMarkdown("```python\nx = 1\n```");
  assert.match(html, /<pre class="cell-code"><code>x = 1<\/code><\/pre>/);
});

test("renderMarkdown: whitespace-flanked '*' is not emphasis (arithmetic / glob stay literal)", () => {
  const a = C.renderMarkdown("2 * 3 * 4 = 24");
  assert.ok(!/<em>/.test(a), "multiplication must not italicise");
  assert.match(a, /2 \* 3 \* 4 = 24/);
  assert.ok(!/<em>/.test(C.renderMarkdown("SELECT * FROM t")));
  // ...but real *italic* and **bold** still render.
  assert.match(C.renderMarkdown("this is *italic* and **bold**"), /<em>italic<\/em> and <strong>bold<\/strong>/);
});

test("renderMarkdown: a U+0000 in model text can't forge a placeholder", () => {
  // The inline sentinel is U+0000; renderMarkdown strips it from the input, so a
  // model-supplied U+0000 bytes cannot smuggle in a placeholder or blank content.
  const html = C.renderMarkdown("before " + String.fromCharCode(0) + "0" + String.fromCharCode(0) + " after");
  assert.equal(html.indexOf(String.fromCharCode(0)), -1, "no raw U+0000 survives to the output");
  assert.match(html, /before 0 after/);
});
