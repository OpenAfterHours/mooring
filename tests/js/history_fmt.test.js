"use strict";

// Unit tests for the version-history panel's pure helpers (history_fmt.js).
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const HF = require("../../src/mooring/hub/static/history_fmt.js");

test("versionLabel leads with when and who, sha last", () => {
  const iso = new Date(2026, 5, 12, 8, 5).toISOString(); // local 12 Jun 2026 08:05
  const label = HF.versionLabel({
    sha: "abc1234def",
    short: "abc1234",
    message: "Update sales.py via mooring",
    author: "maria",
    date: iso,
  });
  assert.equal(label, "12 Jun 2026 08:05 · maria — Update sales.py via mooring (abc1234)");
});

test("versionLabel degrades gracefully on missing fields", () => {
  const label = HF.versionLabel({ sha: "abc1234def", date: "not a date" });
  assert.equal(label, "unknown date (abc1234)");
});

test("canRestoreOver gates like Revert: .py only", () => {
  assert.equal(HF.canRestoreOver("notebooks/a.py"), true);
  assert.equal(HF.canRestoreOver("data/x.csv"), false);
  assert.equal(HF.canRestoreOver("reports/Sales.pbip"), false);
  assert.equal(HF.canRestoreOver(undefined), false);
});

test("hasHistory: never-synced files have no history", () => {
  assert.equal(HF.hasHistory({ state: "synced" }), true);
  assert.equal(HF.hasHistory({ state: "modified" }), true);
  assert.equal(HF.hasHistory({ state: "deleted remotely" }), true);
  assert.equal(HF.hasHistory({ state: "new local" }), false);
  assert.equal(HF.hasHistory({ state: "local" }), false);
  assert.equal(HF.hasHistory(null), false);
});
