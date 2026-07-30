"use strict";

// Unit tests for the hub's pure staleness helpers (freshness.js). Zero deps:
// Node's built-in runner + assert. Run with: node --test tests/js/
// Covers the guard contract the Open dialog relies on: only the three
// remote-moved states warn, dismissals are keyed to the remote marker and
// re-arm when it moves again, and the banner/focus-refresh helpers.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const F = require("../../src/mooring/hub/static/freshness.js");

const file = (state, extra) =>
  Object.assign({ path: "notebooks/a.py", state }, extra || {});

test("warnState: only the three remote-moved states warn", () => {
  assert.equal(F.warnState(file("remote changed", { remote_sha: "r1" }), new Map()), "pull");
  assert.equal(F.warnState(file("deleted remotely"), new Map()), "deleted");
  assert.equal(F.warnState(file("conflict", { remote_sha: "r1" }), new Map()), "conflict");
  for (const state of [
    "synced", "modified", "new local", "deleted locally", "new remote", "in review", "local",
  ]) {
    assert.equal(F.warnState(file(state, { remote_sha: "r1" }), new Map()), null, state);
  }
});

test("warnState: null/undefined file never warns", () => {
  assert.equal(F.warnState(null, new Map()), null);
  assert.equal(F.warnState(undefined, new Map()), null);
});

test("dismissal suppresses re-warn for the same remote sha", () => {
  const f1 = file("remote changed", { remote_sha: "r1" });
  const dismissed = new Map();
  dismissed.set(f1.path, F.dismissKey(f1));
  assert.equal(F.warnState(f1, dismissed), null);
});

test("dismissal re-arms when the remote moves again (new sha)", () => {
  const f1 = file("remote changed", { remote_sha: "r1" });
  const dismissed = new Map();
  dismissed.set(f1.path, F.dismissKey(f1));
  const f2 = file("remote changed", { remote_sha: "r2" });
  assert.equal(F.warnState(f2, dismissed), "pull");
});

test("dismissKey falls back to a state marker when there is no remote sha", () => {
  // A remote deletion carries no sha; the key must still be stable and distinct.
  const gone = file("deleted remotely");
  assert.equal(F.dismissKey(gone), "@deleted remotely");
  const dismissed = new Map([[gone.path, F.dismissKey(gone)]]);
  assert.equal(F.warnState(gone, dismissed), null);
  // If the file comes back with a real remote sha, the key changes → re-arm.
  assert.equal(F.warnState(file("remote changed", { remote_sha: "r9" }), dismissed), "pull");
});

test("pullCount counts exactly the pull states", () => {
  const files = [
    file("remote changed"), file("new remote"), file("deleted remotely"),
    file("synced"), file("modified"), file("conflict"), file("local"),
  ];
  assert.equal(F.pullCount(files), 3);
  assert.equal(F.pullCount([]), 0);
  assert.equal(F.pullCount(undefined), 0);
});

test("ageText boundaries", () => {
  assert.equal(F.ageText(0), "just now");
  assert.equal(F.ageText(59_000), "just now");
  assert.equal(F.ageText(60_000), "1 min ago");
  assert.equal(F.ageText(59 * 60_000), "59 min ago");
  assert.equal(F.ageText(60 * 60_000), "1 hour ago");
  assert.equal(F.ageText(3 * 60 * 60_000), "3 hours ago");
  assert.equal(F.ageText(24 * 60 * 60_000), "1 day ago");
  assert.equal(F.ageText(49 * 60 * 60_000), "2 days ago");
  assert.equal(F.ageText(-5), "");
  assert.equal(F.ageText(NaN), "");
});

test("shouldAutoRefresh respects the throttle and needs a prior refresh", () => {
  const t0 = 1_000_000;
  assert.equal(F.shouldAutoRefresh(null, t0, 60_000), false);
  assert.equal(F.shouldAutoRefresh(t0, t0 + 59_999, 60_000), false);
  assert.equal(F.shouldAutoRefresh(t0, t0 + 60_000, 60_000), true);
  assert.equal(F.shouldAutoRefresh(t0, t0 + 3_600_000, 60_000), true);
});
