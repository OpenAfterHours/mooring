"use strict";

// Guards how chat_core.js's helpers are exposed to the hub's static pages. A top-level
// `const ChatCore` is a global LEXICAL binding — reachable as the bare identifier
// `ChatCore` (how chat.js and app.js use it) but NOT a property of `window`. The batch
// builder regressed on exactly that: `const C = window.ChatCore` made `C` undefined, so
// clicking "Add to queue" threw an uncaught TypeError and silently did nothing. The fix
// reads the bare global; chat_core.js now ALSO mirrors itself onto `window` so neither
// access path can break again. These tests pin BOTH paths.
//
// Zero dependencies: Node's built-in test runner + assert + fs + vm. Run with:
//   node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");
// The members batch.js reaches through `C` — a missing one is what made the page throw.
const HELPERS = ["cleanJobs", "additiveBlockLines", "diffLines", "highlightCode"];

// Execute chat_core.js the way a BROWSER does: a top-level script in a context that has
// a `window` and no CommonJS `module`. Returns {bare, win} — the bare lexical global and
// the window property — so we can assert both resolve to the same API. (A top-level
// `const` isn't a property of the vm context, so we copy it onto `window` to read it.)
function runInBrowserLike() {
  const src = fs.readFileSync(path.join(STATIC, "chat_core.js"), "utf8");
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(src + "\n;window.__bare = ChatCore;", sandbox);
  return { bare: sandbox.window.__bare, win: sandbox.window.ChatCore };
}

test("chat_core.js exposes its API as a bare global (the convention chat.js/app.js use)", () => {
  const { bare } = runInBrowserLike();
  assert.equal(typeof bare, "object");
  for (const fn of HELPERS) assert.equal(typeof bare[fn], "function", `bare ChatCore.${fn}`);
});

test("chat_core.js ALSO mirrors its API onto window (so window.ChatCore.* never throws)", () => {
  const { win } = runInBrowserLike();
  assert.equal(typeof win, "object", "window.ChatCore must be defined — this is the regression guard");
  for (const fn of HELPERS) assert.equal(typeof win[fn], "function", `window.ChatCore.${fn}`);
});

test("the bare global and window.ChatCore are the SAME object", () => {
  const { bare, win } = runInBrowserLike();
  assert.equal(bare, win);
});

test("chat_core.js still loads cleanly under CommonJS require() (Node has no window)", () => {
  // The window mirror is guarded by `typeof window !== "undefined"`, so requiring it in
  // Node must not throw and must still export the helpers the Node suite relies on.
  const C = require(path.join(STATIC, "chat_core.js"));
  for (const fn of HELPERS) assert.equal(typeof C[fn], "function", `module.exports.${fn}`);
});
