"use strict";

// Unit tests for the push guard's pure frontend helpers (guard_fmt.js).
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const GF = require("../../src/mooring/hub/static/guard_fmt.js");

const FINDINGS = [
  {
    path: "notebooks/a.py",
    token: "t1",
    findings: [
      { line: 2, kind: "GitHub token" },
      { line: 5, kind: "email address" },
    ],
  },
  { path: "data/export.csv", token: "t2", findings: [{ line: 1, kind: "bulk data export (~1500 rows)" }] },
];

test("rows: one value-free line per finding", () => {
  assert.deepEqual(GF.rows(FINDINGS), [
    "notebooks/a.py — line 2: GitHub token",
    "notebooks/a.py — line 5: email address",
    "data/export.csv — line 1: bulk data export (~1500 rows)",
  ]);
  assert.deepEqual(GF.rows([]), []);
  assert.deepEqual(GF.rows(undefined), []);
});

test("allTokens collects the per-file confirm tokens", () => {
  assert.deepEqual(GF.allTokens(FINDINGS), ["t1", "t2"]);
  assert.deepEqual(GF.allTokens([{ path: "x", findings: [] }]), []);
});

test("needsDialog only for responses carrying findings", () => {
  assert.equal(GF.needsDialog({ guard_findings: FINDINGS }), true);
  assert.equal(GF.needsDialog({ guard_findings: [] }), false);
  assert.equal(GF.needsDialog({ lines: [] }), false);
  assert.equal(GF.needsDialog(null), false);
});

test("canOverride: warn mode yes, block mode never", () => {
  assert.equal(GF.canOverride({ needs_confirm: true, guard_mode: "warn" }), true);
  assert.equal(GF.canOverride({ needs_confirm: false, guard_mode: "block" }), false);
  // Belt and braces: even a buggy needs_confirm never overrides block mode.
  assert.equal(GF.canOverride({ needs_confirm: true, guard_mode: "block" }), false);
});
