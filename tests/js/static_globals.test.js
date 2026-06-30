"use strict";

// Guards the way chat_core.js's helpers are shared across the hub's static pages.
// Zero dependencies: Node's built-in test runner + assert + fs. Run with:
//   node --test tests/js/
//
// chat_core.js declares `const ChatCore = (...)()` at the top level. In a browser a
// top-level `const` is a global LEXICAL binding — reachable from later scripts as the
// bare identifier `ChatCore`, but it is NOT a property of `window`. So `window.ChatCore`
// is `undefined`. The batch builder regressed on exactly this: `const C = window.ChatCore`
// made every `C.cleanJobs(...)` call throw, so clicking "Add to queue" silently did
// nothing (an uncaught TypeError, no request, no UI change). These tests pin the contract
// so a consumer can't reintroduce that trap, and prove the symbols batch.js needs exist.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const STATIC = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");

// Read a static file with comments stripped, so the guard checks CODE, not prose — a
// file is free to *describe* the window.ChatCore footgun in a comment (batch.js does).
// Block comments go first; line comments only when `//` isn't part of a `://` URL.
function read(name) {
  return fs
    .readFileSync(path.join(STATIC, name), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

// Every page that consumes ChatCore. chat_core.js itself is excluded (it is the producer).
const CONSUMERS = ["batch.js", "chat.js", "app.js"];

test("no static consumer reads `window.ChatCore` (it is a bare lexical global, not on window)", () => {
  for (const name of CONSUMERS) {
    assert.ok(
      !/window\s*\.\s*ChatCore/.test(read(name)),
      `${name} references window.ChatCore, which is always undefined — use the bare ` +
        `\`ChatCore\` global instead (this is the bug that silently broke "Add to queue").`
    );
  }
});

test("chat_core.js does not assign itself onto window (consumers must use the bare global)", () => {
  // If a future change DOES expose it on window, this test should be updated deliberately —
  // it documents the current, intended contract rather than an accident.
  assert.ok(
    !/window\s*\.\s*ChatCore\s*=/.test(read("chat_core.js")),
    "chat_core.js now assigns window.ChatCore; update this contract test if that is intended."
  );
});

test("chat_core.js exports the helpers the batch builder calls", () => {
  // The exact members batch.js reaches through `C` — a missing one is what made the page
  // throw. Loading via require() mirrors how the Node suite consumes the module.
  const C = require(path.join(STATIC, "chat_core.js"));
  for (const fn of ["cleanJobs", "additiveBlockLines", "diffLines", "highlightCode"]) {
    assert.equal(typeof C[fn], "function", `ChatCore.${fn} must be exported for batch.js`);
  }
});
