"use strict";

// Unit tests for the review panel's pure helpers (diff_fmt.js), plus the
// read-only / no-injection pins on the renderer: diff text is untrusted
// notebook source, so it must only ever reach the DOM via textContent, and the
// panel must never grow editable regions (a merge tool is a different product).
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");
const DF = require(path.join(STATIC, "diff_fmt.js"));

test("cellLabel: one honest line per status, 1-based for humans", () => {
  assert.equal(DF.cellLabel({ status: "added", index_base: null, index_local: 2 }), "Cell 3 — new");
  assert.equal(DF.cellLabel({ status: "removed", index_base: 0, index_local: null }), "Cell 1 — removed");
  assert.equal(DF.cellLabel({ status: "changed", index_base: 1, index_local: 1 }), "Cell 2 — changed");
  assert.equal(
    DF.cellLabel({ status: "changed", index_base: 0, index_local: 3 }),
    "Cell 4 — changed (was cell 1)",
  );
  assert.equal(DF.cellLabel({ status: "unchanged", index_base: 0, index_local: 0 }), "Cell 1 — unchanged");
  assert.equal(
    DF.cellLabel({ status: "unchanged", index_base: 2, index_local: 0 }),
    "Cell 1 — unchanged (moved from cell 3)",
  );
  // Unmatched never claims a pairing — both sides say "no confident match".
  assert.equal(
    DF.cellLabel({ status: "unmatched", index_base: 1, index_local: null }),
    "Cell 2 (last synced) — no confident match in your copy",
  );
  assert.equal(
    DF.cellLabel({ status: "unmatched", index_base: null, index_local: 1 }),
    "Cell 2 (your copy) — no confident match in the last-synced version",
  );
});

test("buildBlocks collapses unchanged cells to a single label line", () => {
  const blocks = DF.buildBlocks([
    { status: "unchanged", index_base: 0, index_local: 0, diff: "" },
    { status: "changed", index_base: 1, index_local: 1, diff: "-y = 2\n+y = 3" },
  ]);
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].collapsed, true);
  assert.equal(blocks[0].diff, "");
  assert.equal(blocks[1].collapsed, false);
  assert.equal(blocks[1].diff, "-y = 2\n+y = 3");
  assert.deepEqual(DF.buildBlocks(undefined), []);
});

test("summary counts cell fates and appends the server note", () => {
  const result = {
    kind: "cells",
    note: "",
    cells: [
      { status: "changed" },
      { status: "changed" },
      { status: "added" },
      { status: "unchanged" },
    ],
  };
  assert.equal(DF.summary(result), "2 changed · 1 new · 1 unchanged");
  result.note = "some cells could not be matched confidently";
  assert.equal(
    DF.summary(result),
    "2 changed · 1 new · 1 unchanged — some cells could not be matched confidently",
  );
  assert.equal(DF.summary({ kind: "cells", cells: [] }), "no cells");
});

test("summary for line/binary results is the server note", () => {
  assert.equal(
    DF.summary({ kind: "binary", note: "changed, 2.1 MB → 2.3 MB — contents not shown" }),
    "changed, 2.1 MB → 2.3 MB — contents not shown",
  );
  assert.equal(DF.summary({ kind: "lines", note: "" }), "");
  assert.equal(DF.summary(null), "");
});

test("diff_fmt.js emits strings only — no innerHTML, no editable elements", () => {
  const src = fs.readFileSync(path.join(STATIC, "diff_fmt.js"), "utf8");
  assert.ok(!src.includes("innerHTML"), "diff_fmt must never touch innerHTML");
  assert.ok(!/contenteditable/i.test(src), "diff_fmt must never emit editable regions");
});

test("app.js renderReview writes untrusted diff text via textContent only, read-only", () => {
  const appSrc = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  const start = appSrc.indexOf("function renderReview");
  assert.ok(start > -1, "renderReview must exist in app.js");
  const end = appSrc.indexOf("\nfunction ", start + 1);
  const body = appSrc.slice(start, end === -1 ? undefined : end);
  assert.ok(!body.includes("innerHTML"), "the renderer must use textContent, never innerHTML");
  assert.ok(!/contenteditable/i.test(body), "the panel is read-only");
  for (const tag of ['"input"', '"textarea"', '"select"']) {
    assert.ok(
      !body.includes(`createElement(${tag})`),
      `the renderer must not create editable ${tag} elements`,
    );
  }
});

test("diff_fmt.js exposes both the bare global and window.DiffFmt (browser + Node)", () => {
  // The static_globals.test.js posture: run it as a browser top-level script and
  // check the bare lexical global and the window mirror are the same object.
  const src = fs.readFileSync(path.join(STATIC, "diff_fmt.js"), "utf8");
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(src + "\n;window.__bare = DiffFmt;", sandbox);
  assert.equal(typeof sandbox.window.DiffFmt, "object");
  assert.equal(sandbox.window.__bare, sandbox.window.DiffFmt);
  for (const fn of ["cellLabel", "buildBlocks", "summary"]) {
    assert.equal(typeof sandbox.window.DiffFmt[fn], "function", `window.DiffFmt.${fn}`);
  }
});
