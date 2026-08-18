"use strict";

// The panel's RECEIPTS ledger makes claims about a notebook — did it run, do its
// tie-outs pass, did its inputs hold, who depends on it. The rules that keep those
// claims honest are the same ones the row badges had, so they are pinned here:
// an absent payload produces NO line, a failure never reads as a pass, and a stale
// lineage claim keeps its date instead of quietly disappearing.
//   node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const LineageFmt = require("../../src/mooring/hub/static/lineage_fmt.js");
const ReceiptsFmt = require("../../src/mooring/hub/static/receipts_fmt.js");

const lines = (file) => ReceiptsFmt.lines(file, LineageFmt);

test("a file with no receipts produces no lines at all", () => {
  // Never a placeholder and never a reassuring "nothing to report": silence is the
  // only honest rendering of an absent payload.
  assert.deepEqual(lines({ path: "a.py", state: "synced" }), []);
  assert.deepEqual(lines({}), []);
  assert.deepEqual(lines(null), []);
});

test("a clean verify reads green and carries its date", () => {
  const [r] = lines({ verified: { passed: true, ran_at: "2026-08-12T09:15:00" } });
  assert.equal(r.code, "ok ");
  assert.equal(r.tone, "ok");
  assert.equal(r.text, "ran clean · 12 Aug");
  assert.match(r.title, /ran clean end-to-end/);
  assert.match(r.title, /clears when you edit it/);
});

test("a failed verify never reads as a pass", () => {
  const [r] = lines({ verified: { passed: false, cells_failed: 3, ran_at: "2026-08-12" } });
  assert.equal(r.code, "!! ");
  assert.notEqual(r.tone, "ok");
  assert.equal(r.text, "3 cells failed · 12 Aug");
  const [one] = lines({ verified: { passed: false, cells_failed: 1 } });
  assert.equal(one.text, "1 cell failed");
  const [none] = lines({ verified: { passed: false } });
  assert.equal(none.text, "failed to run");
});

test("an undated receipt degrades to an undated line, never 'Invalid Date'", () => {
  const [r] = lines({ verified: { passed: true, ran_at: "not-a-date" } });
  assert.equal(r.text, "ran clean");
  assert.equal(ReceiptsFmt.dayText(""), "");
  assert.equal(ReceiptsFmt.dayText(undefined), "");
});

test("tie-out checks report pass and failure differently", () => {
  const [pass] = lines({ checks: { total: 12, failed: 0 } });
  assert.equal(pass.code, "ok ");
  assert.equal(pass.text, "12 tie-out checks pass");
  const [fail] = lines({ checks: { total: 3, failed: 2 } });
  assert.equal(fail.code, "!! ");
  assert.equal(fail.text, "2 of 3 tie-out checks failing");
  // No checks recorded at all is not "0 checks pass" — it is no line.
  assert.deepEqual(lines({ checks: { total: 0, failed: 0 } }), []);
});

test("input fingerprints go amber when an input OR an output moved", () => {
  const [held] = lines({ inputs: { total: 3, changed: 0, outputs: 2, outputs_changed: 0 } });
  assert.equal(held.code, "ok ");
  assert.equal(held.text, "3 inputs pinned, 2 outputs");
  const [moved] = lines({ inputs: { total: 3, changed: 1, outputs: 0 } });
  assert.equal(moved.code, "chg");
  assert.equal(moved.tone, "warn");
  assert.equal(moved.text, "1 of 3 pinned inputs changed");
  // An output that moved is the same alarm from the other direction.
  const [out] = lines({ inputs: { total: 2, changed: 0, outputs: 4, outputs_changed: 1 } });
  assert.equal(out.code, "chg");
  assert.match(out.text, /1 of 4 outputs changed/);
});

test("lineage reuses LineageFmt's wording, minus its pill glyph", () => {
  const [r] = lines({ lineage: { readers: 3, as_of: "2026-08-10" } });
  assert.equal(r.code, "lin");
  assert.equal(r.tone, "accent");
  assert.equal(r.text, "3 notebooks read this");
  assert.ok(!r.text.includes("⇄"), "the ⇄ glyph is the pill's job, not the ledger's");
  assert.match(r.title, /Recorded lineage/);
  assert.match(r.title, /no badge is not/); // the floor caveat survives
});

test("a stale lineage claim is kept, dated and muted — never silently dropped", () => {
  const [r] = lines({ lineage: { readers: 2, as_of: "2026-01-04", stale: true } });
  assert.equal(r.tone, "muted");
  assert.match(r.text, /2026-01-04/);
  assert.match(r.text, /not confirmed since/);
});

test("a lineage payload that supports no claim produces no line", () => {
  assert.deepEqual(lines({ lineage: {} }), []);
  assert.deepEqual(lines({ lineage: { as_of: "2026-08-10" } }), []);
});

test("the ledger reads in a fixed order: ran, tied out, held, depended on", () => {
  const out = lines({
    verified: { passed: true, ran_at: "2026-08-12" },
    checks: { total: 12, failed: 0 },
    inputs: { total: 3, changed: 1 },
    lineage: { readers: 3, as_of: "2026-08-10" },
  });
  assert.deepEqual(out.map((r) => r.code), ["ok ", "ok ", "chg", "lin"]);
  // Every code is exactly three characters, so the ledger stays a column.
  for (const r of out) assert.equal(r.code.length, 3);
});

test("every line carries a title, so no claim is made without its caveat", () => {
  const out = lines({
    verified: { passed: false, cells_failed: 1 },
    checks: { total: 2, failed: 1 },
    inputs: { total: 1, changed: 0 },
    lineage: { readers: 1, as_of: "2026-08-10" },
  });
  assert.equal(out.length, 4);
  for (const r of out) assert.ok(r.title && r.title.length > 20, `title for ${r.code}`);
});
