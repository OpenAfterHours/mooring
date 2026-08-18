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

// -- the one-shot ("once") cadence -----------------------------------------------------
// A one-shot that has run is finished. The regression these guard is the opposite of the
// staleness ones above: the board must not keep nagging about a schedule that has already
// done the single thing it was created to do.

function once(over = {}) {
  return row({
    cadence: "once",
    cadence_text: "once on 2026-08-20 at 15:00",
    date: "2026-08-20",
    complete: true,
    next_due: "2026-08-20T15:00",
    last_run: { at: "2026-08-20T15:00:00+00:00", outcome: "ok" },
    ...over,
  });
}

test("state: a finished one-shot reads done, and keeps its outcome", () => {
  const s = SF.state(once());
  assert.equal(s.text, "done · ran clean");
  assert.equal(s.tone, "good");
});

test("state: done outranks overdue AND paused — both describe a run that will never come", () => {
  assert.match(SF.state(once({ overdue: true })).text, /^done/);
  assert.equal(SF.state(once({ overdue: true })).tone, "good");
  // A finished one-shot the user then paused by hand. The AUTO-paused shape is deliberately
  // not tested here: auto-pause is spent by a FAILED run, and the server excludes a failed
  // run from `complete`, so complete + consecutive_failures > 0 is no longer reachable.
  assert.match(SF.state(once({ paused: true })).text, /^done/);
});

test("state: a one-shot that did not run is never dressed up as done", () => {
  const broke = { at: "2026-08-20T15:00:00+00:00", outcome: "failed" };
  // The GUARD itself, first and directly: "done" is the server's derived claim and nothing
  // else. Nothing about a row — its once cadence, its date being past, its having a run
  // receipt — is allowed to infer it here, so a row that does not carry the flag is not
  // done. Asserting only the overdue wording below would never reach this line at all.
  const failed = once({ complete: false, overdue: true, last_run: broke });
  assert.equal(SF.isDone(failed), false);
  assert.doesNotMatch(SF.state(failed).text, /done/);
  // Absent reads the same as false — a payload from an older server, or one the server
  // declined to derive the flag for, must not fall through to done by accident.
  const noFlag = once({ overdue: true, last_run: broke });
  delete noFlag.complete;
  assert.equal(SF.isDone(noFlag), false);
  assert.doesNotMatch(SF.state(noFlag).text, /done/);
  // The server no longer calls a FAILED one-shot complete (schedule.is_complete excludes it),
  // so it arrives here as an ordinary overdue row and keeps the loudest badge — the same one
  // a failed daily gets. This is precisely the run the alarm exists for.
  assert.equal(SF.state(failed).text, "overdue · last did not run");
  assert.equal(SF.state(failed).tone, "bad");
  // And the done branch does not launder a failure either, in case a future server ever
  // derives the flag more loosely: given a row claiming complete over a run that did not
  // finish, it still names that outcome and still reads red. This side of the wire does not
  // rely on the server's exclusion — "done" is never the word that hides a failed run.
  const claimed = SF.state(once({ last_run: broke }));
  assert.equal(claimed.text, "done · did not run");
  assert.equal(claimed.tone, "bad");
  // ...whereas CHECKS_FAILED IS still done: the notebook ran, the numbers merely stopped
  // tying out. The outcome still leads the colour, so "done" cannot hide it.
  const ran = { at: "2026-08-20T15:00:00+00:00", outcome: "checks_failed" };
  assert.match(SF.state(once({ last_run: ran })).text, /^done · /);
  assert.equal(SF.state(once({ last_run: ran })).tone, "bad");
});

test("state: an UNFINISHED one-shot is an ordinary schedule", () => {
  // Before its date it waits; after it, the ordinary staleness wording applies.
  assert.equal(SF.state(once({ complete: false, last_run: {} })).text, "waiting");
  assert.equal(SF.state(once({ complete: false, last_run: {}, due: true })).text, "due now");
  assert.equal(
    SF.state(once({ complete: false, last_run: {}, overdue: true })).text,
    "overdue · never run",
  );
});

test("nextDue: a finished one-shot promises no next run", () => {
  assert.equal(SF.nextDue(once()), "");
  assert.match(SF.nextDue(once({ complete: false })), /^next 20 Aug/);
});

test("isDone: derived server-side, and safe on junk", () => {
  assert.equal(SF.isDone(once()), true);
  assert.equal(SF.isDone(row()), false);
  assert.equal(SF.isDone(undefined), false);
});

test("autoHint: a finished one-shot is not nagged about a run that isn't coming", () => {
  // may_auto_run is false for every finished one-shot (its window is spent), so `auto` is
  // false on all of them — without the guard EVERY one would draw a hint. The guard has to
  // beat all three of the reasons below, which is why each is asserted separately.
  assert.equal(SF.autoHint(once({ auto: false })), "");
  assert.equal(SF.autoHint(once({ auto: false, paused: true })), "");
  assert.equal(SF.autoHint(once({ auto: false, verified: false })), "");
  // ...while an unfinished one still explains itself, or the guard would be silencing live
  // schedules too.
  assert.match(SF.autoHint(once({ complete: false, auto: false, paused: true })), /^Paused/);
});

test("banner: a finished one-shot is never called due, overdue or paused", () => {
  // Paused is the case the frontend decides on its own (the counts come from the server),
  // so it is the one that could regress here.
  assert.equal(SF.banner({ schedules: [once({ paused: true })], overdue: 0, due: 0 }), "");
  assert.equal(SF.banner({ schedules: [once()], overdue: 0, due: 0 }), "");
  // ...and a finished one alongside a live paused one still reports only the live one.
  const mixed = SF.banner({
    schedules: [once({ paused: true }), row({ paused: true })],
    overdue: 0,
    due: 0,
  });
  assert.match(mixed, /^1 schedule\(s\) paused/);
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

// -- the one-shot date the form offers ---------------------------------------
// A LOCAL clock is what these are about, so every fixture below is built with the local
// Date constructor rather than an ISO string (which would be UTC and would make these
// assertions a timezone lottery on any machine but this one).

test("firstUnspentDate: offers today while the time is still ahead", () => {
  const now = new Date(2026, 7, 18, 9, 0); // 18 Aug 2026, 09:00 local
  assert.equal(SF.firstUnspentDate("17:00", now), "2026-08-18");
});

test("firstUnspentDate: offers tomorrow once the time has gone", () => {
  const now = new Date(2026, 7, 18, 9, 0);
  assert.equal(SF.firstUnspentDate("07:30", now), "2026-08-19");
});

test("firstUnspentDate: the boundary is STRICTLY ahead, not 'not behind'", () => {
  // Exactly at the instant, the window is already open — offering today would hand back a
  // schedule the server calls complete the moment a run lands in it.
  const now = new Date(2026, 7, 18, 7, 30);
  assert.equal(SF.firstUnspentDate("07:30", now), "2026-08-19");
  assert.equal(SF.firstUnspentDate("07:31", now), "2026-08-18");
});

test("firstUnspentDate: rolls the month and the year over", () => {
  assert.equal(SF.firstUnspentDate("07:30", new Date(2026, 11, 31, 9, 0)), "2027-01-01");
  assert.equal(SF.firstUnspentDate("07:30", new Date(2026, 7, 31, 9, 0)), "2026-09-01");
  // A leap day is the case a naive +1 on the day number gets wrong.
  assert.equal(SF.firstUnspentDate("07:30", new Date(2028, 1, 28, 9, 0)), "2028-02-29");
});

test("firstUnspentDate: junk time is treated as midnight, never NaN", () => {
  const now = new Date(2026, 7, 18, 9, 0);
  // "" parses to 00:00, which is behind 09:00, so tomorrow — a real date either way. The
  // point is that it never reaches the form as "NaN-NaN-NaN".
  assert.equal(SF.firstUnspentDate("", now), "2026-08-19");
  assert.equal(SF.firstUnspentDate(undefined, now), "2026-08-19");
});

test("localDate: pads to YYYY-MM-DD and stays on the LOCAL day", () => {
  // 23:30 local on the 18th is already the 19th in UTC east of Greenwich and still the 18th
  // west of it — toISOString would disagree with the calendar the user is reading.
  assert.equal(SF.localDate(0, new Date(2026, 7, 18, 23, 30)), "2026-08-18");
  assert.equal(SF.localDate(0, new Date(2026, 0, 5, 0, 30)), "2026-01-05");
  assert.equal(SF.localDate(1, new Date(2026, 0, 5, 0, 30)), "2026-01-06");
});
