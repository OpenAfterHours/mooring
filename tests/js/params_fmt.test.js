"use strict";

// The parameterised-run card's wording rules. The one failure this feature could introduce
// is a HALF-FINISHED pack that reads as a finished one, so most of these tests are about
// the summary refusing to claim completeness it cannot back up.
//
// Zero dependencies: Node's built-in test runner. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const P = require(path.join(
  __dirname, "..", "..", "src", "mooring", "hub", "static", "params_fmt.js"
));

const snap = (over) =>
  Object.assign(
    { notebook: "notebooks/board.py", param: "region", values: ["EMEA", "APAC", "AMER"],
      runs: [], running: "", done: false, cancelling: false, error: "" },
    over
  );

const ran = (value, over) =>
  Object.assign({ value, outcome: "ok", ran: true, reason: "", artifact: `out/${value}.html` }, over);

// -- the summary must never overstate ---------------------------------------

test("a complete fan-out says so", () => {
  const s = snap({ done: true, runs: [ran("EMEA"), ran("APAC"), ran("AMER")] });
  assert.match(P.summary(s), /all 3 value\(s\) ran clean/);
  assert.equal(P.tone(s), "good");
});

test("a fan-out with one failure is INCOMPLETE, never 'done'", () => {
  const s = snap({
    done: true,
    runs: [ran("EMEA"), ran("APAC", { outcome: "failed", artifact: "", reason: "1 cell failed to run" }), ran("AMER")],
  });
  const text = P.summary(s);
  assert.match(text, /INCOMPLETE/);
  assert.match(text, /2 of 3/);
  assert.match(text, /1 failed/);
  assert.equal(P.tone(s), "bad");
});

test("a cancelled fan-out names the values that never ran", () => {
  const s = snap({
    done: true,
    runs: [ran("EMEA"), { value: "APAC", outcome: "cancelled", reason: "cancelled part-way through", artifact: "" },
           { value: "AMER", outcome: "skipped", reason: "cancelled before this value ran", artifact: "" }],
  });
  const text = P.summary(s);
  assert.match(text, /INCOMPLETE/);
  assert.match(text, /2 did not run/);
  assert.equal(P.tone(s), "warn");
});

test("a fan-out still going reports which value is running, and does not read as finished", () => {
  const s = snap({ runs: [ran("EMEA")], running: "APAC" });
  const text = P.summary(s);
  assert.match(text, /Running region = APAC/);
  assert.match(text, /1 of 3 done/);
  assert.doesNotMatch(text, /Done|INCOMPLETE/);
  assert.equal(P.tone(s), "idle");
});

test("cancelling is its own state, so the button's effect is visible before it lands", () => {
  assert.match(P.summary(snap({ runs: [ran("EMEA")], running: "APAC", cancelling: true })), /Cancelling/);
});

test("a server error replaces the summary rather than sitting beside a cheerful one", () => {
  const s = snap({ done: true, error: "This workspace is already running a notebook." });
  assert.equal(P.summary(s), "This workspace is already running a notebook.");
  assert.equal(P.tone(s), "bad");
});

// -- per-value rows ----------------------------------------------------------

test("every declared value gets a row, even the ones that never ran", () => {
  const rows = P.rows(snap({ done: true, runs: [ran("EMEA")] }));
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((r) => r.value), ["EMEA", "APAC", "AMER"]);
  assert.equal(rows[1].state.text, "not run");
  assert.equal(rows[2].run, null);
});

test("the value currently executing is marked running, the rest queued", () => {
  const rows = P.rows(snap({ runs: [ran("EMEA")], running: "APAC" }));
  assert.equal(rows[1].state.text, "running…");
  assert.equal(rows[2].state.text, "queued");
});

test("a value that ran clean but wrote no artifact is NOT plain green", () => {
  const state = P.valueState(ran("EMEA", { artifact: "" }));
  assert.equal(state.tone, "warn");
  assert.match(state.text, /no artifact/);
});

test("the detail line shows the artifact, else the curated reason — never anything else", () => {
  assert.equal(P.valueDetail(ran("EMEA")), "out/EMEA.html");
  assert.equal(
    P.valueDetail(ran("APAC", { outcome: "failed", artifact: "", reason: "2 cells failed to run" })),
    "2 cells failed to run"
  );
  assert.equal(P.valueDetail(null), "");
});

// -- counts and progress -----------------------------------------------------

test("counts and progress track finished values, not started ones", () => {
  const s = snap({ runs: [ran("EMEA"), ran("APAC", { outcome: "failed", artifact: "" })], running: "AMER" });
  assert.deepEqual(P.counts(s), { total: 3, clean: 1, failed: 1, done: 2 });
  assert.equal(P.progress(s), 67);
  assert.equal(P.progress(snap()), 0);
});

test("an empty / missing snapshot never throws", () => {
  assert.equal(P.progress(undefined), 0);
  assert.deepEqual(P.rows(undefined), []);
  assert.equal(typeof P.summary(undefined), "string");
});

// -- the client-side preview of the --for box --------------------------------

test("previewValues splits a plain list", () => {
  assert.deepEqual(P.previewValues("region=EMEA,APAC,AMER"), {
    name: "region", values: ["EMEA", "APAC", "AMER"],
  });
});

test("previewValues expands the same closed range vocabulary the server does", () => {
  assert.deepEqual(P.previewValues("month=2026-01..2026-04").values,
    ["2026-01", "2026-02", "2026-03", "2026-04"]);
  assert.deepEqual(P.previewValues("q=1..4").values, ["1", "2", "3", "4"]);
  // A year boundary must not produce month 13.
  assert.deepEqual(P.previewValues("month=2025-11..2026-02").values,
    ["2025-11", "2025-12", "2026-01", "2026-02"]);
});

test("previewValues leaves anything that is not a known range alone, for the server to refuse", () => {
  assert.deepEqual(P.previewValues("d=2026-01-01..2026-01-03").values, ["2026-01-01..2026-01-03"]);
  assert.deepEqual(P.previewValues("region=EMEA").values, ["EMEA"]);
  assert.deepEqual(P.previewValues("nonsense"), { name: "", values: [] });
  assert.deepEqual(P.previewValues("").values, []);
});

test("previewValues does not promise a backwards range", () => {
  assert.deepEqual(P.previewValues("q=4..1").values, []);
});

test("params_fmt.js exposes its API as a bare global for the hub page too", () => {
  const fs = require("node:fs");
  const vm = require("node:vm");
  const file = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "params_fmt.js");
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(file, "utf8") + "\n;window.__bare = ParamsFmt;", sandbox);
  assert.equal(sandbox.window.__bare, sandbox.window.ParamsFmt);
  assert.equal(typeof sandbox.window.ParamsFmt.summary, "function");
});
