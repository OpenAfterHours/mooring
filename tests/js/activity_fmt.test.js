"use strict";

// Unit tests for the Activity page's pure formatter (activity_fmt.js). Zero
// deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const AF = require("../../src/mooring/hub/static/activity_fmt.js");

test("sentence: sync ops carry the summary", () => {
  assert.equal(AF.sentence({ op: "push", summary: "pushed 2 file(s)" }), "you pushed — pushed 2 file(s)");
  assert.equal(AF.sentence({ op: "pull", summary: "" }), "you pulled — no changes");
});

test("sentence: file ops name the file, not the whole path", () => {
  assert.equal(AF.sentence({ op: "rollback", path: "notebooks/sales.py" }),
    "you reverted sales.py to the last synced version");
  assert.equal(AF.sentence({ op: "ai_apply", path: "notebooks/sales.py" }),
    "the copilot applied a change to sales.py (you approved it)");
});

test("sentence: delete counts multi-file artifacts", () => {
  assert.equal(AF.sentence({ op: "delete", path: "reports/Sales.pbip", paths: ["a", "b", "c"] }),
    "you deleted Sales.pbip (3 files)");
  assert.equal(AF.sentence({ op: "delete", path: "notebooks/a.py", paths: ["notebooks/a.py"] }),
    "you deleted a.py");
});

test("sentence: banked pre-images are mentioned", () => {
  const s = AF.sentence({ op: "delete", path: "n/a.py", paths: ["n/a.py"], trashed: [{ path: "n/a.py", token: "t" }] });
  assert.match(s, /1 pre-image\(s\) saved to the trash/);
});

test("sentence: unknown ops degrade to the op name", () => {
  assert.equal(AF.sentence({ op: "future_thing", path: "x/y.py" }), "future_thing y.py");
  assert.equal(AF.sentence({ op: "future_thing" }), "future_thing");
});

test("relTime: minutes, then calendar-aware wording", () => {
  const now = new Date(2026, 5, 15, 12, 0); // local noon, 15 Jun 2026
  const at = (y, mo, d, h, mi) => new Date(y, mo, d, h, mi).toISOString();
  assert.equal(AF.relTime(at(2026, 5, 15, 11, 59), now.getTime()), "1 min ago");
  assert.equal(AF.relTime(at(2026, 5, 15, 12, 0), now.getTime()), "just now");
  assert.equal(AF.relTime(at(2026, 5, 15, 9, 42), now.getTime()), "today 09:42");
  assert.equal(AF.relTime(at(2026, 5, 14, 16, 42), now.getTime()), "yesterday 16:42");
  assert.equal(AF.relTime(at(2026, 5, 12, 8, 5), now.getTime()), "12 Jun 08:05");
  assert.equal(AF.relTime("not a date", now.getTime()), "");
});
