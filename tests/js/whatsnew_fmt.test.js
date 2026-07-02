"use strict";

// Unit tests for the What's-new pull-digest panel's pure helpers
// (whatsnew_fmt.js). Zero deps: Node's built-in runner + assert. Run with:
//   node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const WF = require("../../src/mooring/hub/static/whatsnew_fmt.js");

// -- relativeTime -------------------------------------------------------------

test("relativeTime walks the human ladder: minutes, hours, days", () => {
  const now = Date.parse("2026-07-02T12:00:00Z");
  const at = (iso) => WF.relativeTime(iso, now);
  assert.equal(at("2026-07-02T11:59:40Z"), "just now");
  assert.equal(at("2026-07-02T11:55:00Z"), "5 minutes ago");
  assert.equal(at("2026-07-02T11:59:00Z"), "1 minute ago");
  assert.equal(at("2026-07-02T09:00:00Z"), "3 hours ago");
  assert.equal(at("2026-06-30T12:00:00Z"), "2 days ago");
});

test("relativeTime falls back to the plain date past ~4 weeks", () => {
  const now = Date.parse("2026-07-02T12:00:00Z");
  const label = WF.relativeTime("2026-05-01T12:00:00Z", now);
  assert.match(label, /May 2026$/); // e.g. "1 May 2026" (local-time day)
});

test("relativeTime is empty for junk and never negative for future skew", () => {
  const now = Date.parse("2026-07-02T12:00:00Z");
  assert.equal(WF.relativeTime("not a date", now), "");
  assert.equal(WF.relativeTime("", now), "");
  // A teammate's clock slightly ahead must not print "-3 minutes ago".
  assert.equal(WF.relativeTime("2026-07-02T12:03:00Z", now), "just now");
});

// -- entry / group labels -----------------------------------------------------

test("entryLabel leads with who and when, message last", () => {
  const now = Date.parse("2026-07-02T12:00:00Z");
  const label = WF.entryLabel(
    { authors: ["maria"], date: "2026-06-30T12:00:00Z", messages: ["fix the June totals"] },
    now,
  );
  assert.equal(label, "maria · 2 days ago — fix the June totals");
});

test("entryLabel degrades gracefully as attribution thins out", () => {
  const now = Date.parse("2026-07-02T12:00:00Z");
  assert.equal(
    WF.entryLabel({ authors: ["maria"], date: "", messages: [] }, now),
    "maria",
  );
  assert.equal(WF.entryLabel({ authors: [], date: "", messages: ["note"] }, now), "note");
  assert.equal(WF.entryLabel({ authors: [], date: "", messages: [] }, now), "");
});

test("groupLabel shows the collapsed push with its commit count", () => {
  const now = Date.parse("2026-07-02T12:00:00Z");
  const label = WF.groupLabel(
    { author: "maria", message: "fix the June totals", date: "2026-06-30T12:00:00Z", count: 3 },
    now,
  );
  assert.equal(label, "maria — fix the June totals (3 commits) · 2 days ago");
  const single = WF.groupLabel({ author: "", message: "", date: "junk", count: 1 }, now);
  assert.equal(single, "unknown — (no message)");
});

// -- detailSummary ------------------------------------------------------------

test("detailSummary renders cell counts, line counts, and the binary note", () => {
  assert.equal(
    WF.detailSummary({ kind: "cells", changed: 2, added: 1, removed: 0, unmatched: 0 }),
    "2 cells changed, 1 added",
  );
  assert.equal(
    WF.detailSummary({ kind: "cells", changed: 1, added: 0, removed: 0, unmatched: 0 }),
    "1 cell changed",
  );
  assert.equal(
    WF.detailSummary({ kind: "cells", changed: 0, added: 0, removed: 0, unmatched: 0 }),
    "no cell changes",
  );
  assert.equal(WF.detailSummary({ kind: "lines", added: 12, removed: 3 }), "+12 / −3 lines");
  assert.equal(WF.detailSummary({ kind: "lines", added: 0, removed: 0 }), "no line changes");
  assert.equal(
    WF.detailSummary({ kind: "binary", base_size: 4, head_size: 2 }),
    "contents not shown (binary or too large)",
  );
  assert.equal(WF.detailSummary(null), "");
});

// -- the watch set round-trip ---------------------------------------------------

test("watch set round-trips through its serialized form", () => {
  const set = new Set(["notebooks/a.py", "data/x.csv"]);
  const raw = WF.watchSerialize(set);
  const back = WF.watchSet(raw);
  assert.deepEqual(Array.from(back).sort(), ["data/x.csv", "notebooks/a.py"]);
});

test("watchSet tolerates junk storage content", () => {
  assert.equal(WF.watchSet(null).size, 0);
  assert.equal(WF.watchSet("not json").size, 0);
  assert.equal(WF.watchSet('{"a":1}').size, 0);
  assert.deepEqual(Array.from(WF.watchSet('["a.py", 7, "b.py"]')), ["a.py", "b.py"]);
});

test("watchKey is per-repo", () => {
  assert.equal(WF.watchKey("acme/nbs"), "mooring.watch.acme/nbs");
  assert.notEqual(WF.watchKey("acme/nbs"), WF.watchKey("acme/lab"));
});

// -- watched-first sorting ------------------------------------------------------

test("sortEntries puts watched entries first, keeping order within halves", () => {
  const entries = [
    { path: "a.py" },
    { path: "b.py" },
    { path: "c.py" },
    { path: "d.py" },
  ];
  const sorted = WF.sortEntries(entries, new Set(["c.py", "a.py"]));
  assert.deepEqual(sorted.map((e) => e.path), ["a.py", "c.py", "b.py", "d.py"]);
});

test("sortEntries copes with no entries and no watch set", () => {
  assert.deepEqual(WF.sortEntries(null, new Set()), []);
  const entries = [{ path: "a.py" }];
  assert.deepEqual(WF.sortEntries(entries, null).map((e) => e.path), ["a.py"]);
});
