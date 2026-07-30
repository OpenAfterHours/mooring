"use strict";

// Unit tests for the schedules board's pure frontend helpers (schedule_fmt.js).
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/
//
// The wording rules here are the UI half of "a stale refresh is never silent", so the
// precedence tests are the load-bearing ones: overdue and paused must both outrank a
// remembered-good outcome, or the board would show green over a schedule that has not
// run for a week.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const SF = require("../../src/mooring/hub/static/schedule_fmt.js");

function row(over = {}) {
  return {
    notebook: "notebooks/board.py",
    cadence: "daily",
    cadence_text: "daily at 07:30",
    paused: false,
    verified: true,
    due: false,
    overdue: false,
    auto: true,
    consecutive_failures: 0,
    next_due: "2026-07-31T07:30",
    last_run: { at: "2026-07-30T07:30:00+00:00", outcome: "ok" },
    ...over,
  };
}

test("state: a clean recent run reads green", () => {
  const s = SF.state(row());
  assert.equal(s.text, "ran clean");
  assert.equal(s.tone, "good");
});

test("state: OVERDUE outranks a remembered-good outcome", () => {
  // The regression this guards: a schedule that ran clean on Monday and has not run
  // since must never still show green on Friday.
  const s = SF.state(row({ overdue: true }));
  assert.equal(s.tone, "bad");
  assert.match(s.text, /overdue/);
  assert.match(s.text, /last ran clean/);
});

test("state: paused outranks everything, and auto-pause reads red", () => {
  assert.equal(SF.state(row({ paused: true })).tone, "idle");
  const auto = SF.state(row({ paused: true, consecutive_failures: 3 }));
  assert.equal(auto.tone, "bad");
  assert.match(auto.text, /paused after 3 failed run\(s\)/);
});

test("state: a degraded run never reads as clean", () => {
  const s = SF.state(row({ last_run: { at: "2026-07-30T07:30:00+00:00", outcome: "degraded" } }));
  assert.equal(s.tone, "warn");
  assert.equal(s.text, "ran, degraded");
});

test("state: failing tie-outs read red", () => {
  const s = SF.state(row({ last_run: { outcome: "checks_failed" } }));
  assert.equal(s.tone, "bad");
  assert.equal(s.text, "checks failing");
});

test("state: a never-run schedule is idle, not green", () => {
  assert.equal(SF.state(row({ last_run: {} })).text, "waiting");
  assert.equal(SF.state(row({ last_run: {}, due: true })).text, "due now");
  assert.equal(SF.state(row({ last_run: {}, overdue: true })).text, "overdue · never run");
});

test("state: a missing row degrades to blank rather than throwing", () => {
  assert.deepEqual(SF.state(undefined), { text: "", tone: "idle" });
});

test("detail: counts are value-free and read in priority order", () => {
  const text = SF.detail(row({
    last_run: { outcome: "checks_failed", checks_failed: 2, inputs_changed: 1, conflicts: 3 },
  }));
  assert.equal(text, "2 tie-out check(s) failing · 1 input(s) changed · 3 file(s) in conflict");
});

test("detail: the reason only shows when no counts already say it", () => {
  // Offline carries new information and no counts...
  assert.equal(
    SF.detail(row({ last_run: { outcome: "degraded", reason: "GitHub unreachable — ran against the local copy" } })),
    "GitHub unreachable — ran against the local copy",
  );
  // ...whereas a checks reason would just restate the count.
  assert.equal(
    SF.detail(row({ last_run: { checks_failed: 2, reason: "2 of 5 tie-out check(s) failing" } })),
    "2 tie-out check(s) failing",
  );
});

test("autoHint: explains only the rows that will NOT fire by themselves", () => {
  assert.equal(SF.autoHint(row()), "");
  assert.match(SF.autoHint(row({ auto: false, paused: true })), /resume/i);
  assert.match(SF.autoHint(row({ auto: false, verified: false })), /verified/i);
  assert.match(
    SF.autoHint(row({ auto: false, last_run: { outcome: "failed" } })),
    /Last run failed/,
  );
});

test("banner: overdue outranks due, and both outrank silence", () => {
  assert.equal(SF.banner({ schedules: [], overdue: 0, due: 0 }), "");
  assert.equal(SF.banner({ schedules: [row()], overdue: 0, due: 0 }), "");
  assert.match(SF.banner({ schedules: [row()], overdue: 0, due: 2 }), /2 refresh\(es\) due/);
  const over = SF.banner({ schedules: [row({ overdue: true })], overdue: 1, due: 1 });
  assert.match(over, /1 refresh overdue/);
});

test("banner: pluralises overdue correctly", () => {
  assert.match(SF.banner({ schedules: [row()], overdue: 2 }), /2 refreshes overdue/);
  assert.match(SF.banner({ schedules: [row()], overdue: 2 }), /they have/);
});

test("banner: paused schedules are called out when nothing is overdue", () => {
  const text = SF.banner({ schedules: [row({ paused: true })], overdue: 0, due: 0 });
  assert.match(text, /1 schedule\(s\) paused/);
});

test("when: formats a timestamp and survives a corrupt one", () => {
  assert.match(SF.when("2026-07-30T07:30:00Z"), /30 Jul \d\d:\d\d/);
  assert.equal(SF.when("not a date"), "");
  assert.equal(SF.when(""), "");
  assert.equal(SF.when(undefined), "");
});

test("nextDue: blank when there is nothing to say", () => {
  assert.match(SF.nextDue(row()), /^next 31 Jul/);
  assert.equal(SF.nextDue({}), "");
});
