"use strict";

// Unit tests for the hub's pure first-run-checklist helper (checklist.js). Zero deps:
// Node's built-in runner + assert (+ fs/vm for the browser-like export pin). Run with:
// node --test tests/js/
// Covers the derivation contract app.js renders from: pulled/duplicated re-derive from
// the file rows, opened/pushed come from stored flags (pushed also from an open
// review), per-repo storage keying, and the dual bare-global/window/module exports.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CL = require("../../src/mooring/hub/static/checklist.js");

const f = (p, state) => ({ path: p, state: state || "local" });

const byId = (items) => Object.fromEntries(items.map((i) => [i.id, i.done]));

test("derive: a fresh repo-mode workspace has nothing ticked", () => {
  const done = byId(CL.derive([f("notebooks/a.py", "new local")], null, {}));
  assert.deepEqual(done, { pulled: false, opened: false, duplicated: false, pushed: false });
});

test("derive: only states that need a synced base prove a pull", () => {
  for (const state of ["synced", "modified", "deleted locally", "remote changed",
    "conflict", "mixed", "in review"]) {
    const items = CL.derive([f("notebooks/a.py", state)], null, {});
    assert.equal(byId(items).pulled, true, `state "${state}" should tick pulled`);
  }
  // Local-only states never do — they exist without any remote tracking — and
  // neither do "new remote"/"deleted remotely": those come straight from the
  // remote diff BEFORE any pull, so a new joiner (whole repo showing as
  // "new remote") must still see step 1 unticked.
  for (const state of ["local", "new local", "new remote", "deleted remotely"]) {
    const items = CL.derive([f("notebooks/a.py", state)], null, {});
    assert.equal(byId(items).pulled, false, `state "${state}" must not tick pulled`);
  }
});

test("derive: duplicated ticks from a -draft.py row OR the stored flag", () => {
  const rows = [f("notebooks/sales-phil-draft.py", "new local")];
  assert.equal(byId(CL.derive(rows, null, {})).duplicated, true);
  assert.equal(byId(CL.derive([], null, { duplicated: true })).duplicated, true);
  // A numbered collision copy still counts; the flag survives a deleted draft.
  const numbered = [f("notebooks/sales-draft-2.py", "new local")];
  assert.equal(byId(CL.derive(numbered, null, {})).duplicated, true);
});

test("derive: opened and pushed come from stored flags", () => {
  const done = byId(CL.derive([], null, { opened: true, pushed: true }));
  assert.equal(done.opened, true);
  assert.equal(done.pushed, true);
});

test("derive: an open proposal review also proves a push/propose", () => {
  const review = { branch: "mooring/phil/x", compare_url: "https://x" };
  assert.equal(byId(CL.derive([], review, {})).pushed, true);
  assert.equal(byId(CL.derive([], null, {})).pushed, false);
});

test("derive: tolerates missing inputs", () => {
  const items = CL.derive(undefined, undefined, undefined);
  assert.equal(items.length, 4);
  assert.ok(items.every((i) => i.done === false));
});

test("isDone: true only when every item is ticked", () => {
  assert.equal(CL.isDone(CL.derive([], null, {})), false);
  const all = CL.derive(
    [f("notebooks/a.py", "synced"), f("notebooks/a-phil-draft.py", "new local")],
    null,
    { opened: true, pushed: true },
  );
  assert.equal(CL.isDone(all), true);
  assert.equal(CL.isDone([]), false); // an empty list is not "done"
});

test("storageKey: keyed per repo slug so a second repo ramps afresh", () => {
  assert.equal(CL.storageKey("acme/nbs"), "mooring.checklist.acme/nbs");
  assert.notEqual(CL.storageKey("acme/nbs"), CL.storageKey("acme/lab"));
  assert.equal(CL.storageKey(""), "mooring.checklist.default");
});

test("DRAFT_RE: matches exactly the names Duplicate as draft mints", () => {
  for (const name of [
    "notebooks/sales-draft.py",
    "notebooks/sales-phil-draft.py",
    "notebooks/sales-phil-draft-2.py",
    "sales-draft-10.py",
  ]) {
    assert.ok(CL.DRAFT_RE.test(name), `${name} should match`);
  }
  for (const name of ["notebooks/draft.py", "notebooks/firstdraft.py", "my-drafts.py",
    "sales-draft.csv"]) {
    assert.ok(!CL.DRAFT_RE.test(name), `${name} must not match`);
  }
});

// -- the dual-export contract (the files_tree.js idiom) -----------------------

test("checklist.js exposes the same API as a bare global and on window", () => {
  // Execute it the way a BROWSER does: a top-level script with a window and no
  // CommonJS module (the static_globals.test.js pattern).
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "checklist.js"),
    "utf8",
  );
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(src + "\n;window.__bare = Checklist;", sandbox);
  assert.equal(sandbox.window.__bare, sandbox.window.Checklist);
  for (const fn of ["derive", "isDone", "storageKey"]) {
    assert.equal(typeof sandbox.window.Checklist[fn], "function", `window.Checklist.${fn}`);
  }
  // DRAFT_RE is shared with app.js's bulk-push confirm — it must ride the export.
  assert.ok(sandbox.window.Checklist.DRAFT_RE.test("notebooks/sales-phil-draft.py"));
});
