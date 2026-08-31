"use strict";

// Unit tests for the auto-apply loop's pure frontend helpers (ChatCore.receipt*,
// ChatCore.cancelledNotice / stopButtonState / stopOutcomeNotice).
//
// With `[ai] auto_apply` on there is no Apply button: the model writes the cell inside
// its own tool call, marimo runs it, and the analyst gets a RECEIPT afterwards plus a
// Stop beside the composer. Both of those are almost entirely WORDING — what changed,
// what came back, and whether the turn has actually stopped yet — which is why the
// strings are pinned here rather than left to a DOM review:
//
//   * the receipt is read by someone who does not read Python, so "Changed cell 3" is
//     the level and an op dict, a tool name or a status slug must never surface;
//   * a stop control that reads as done while the assistant is still replying is worse
//     than no stop at all, so every state it can be in is asserted.
//
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../src/mooring/hub/static/chat_core.js");

// -- receiptHeadline: what changed, in the analyst's language ----------------

test("receiptHeadline names each kind of change with the analyst's verb", () => {
  assert.equal(C.receiptHeadline({ edited: [3], appended: [8], deleted: [] }),
    "Changed cell 3 · Added cell 8");
  assert.equal(C.receiptHeadline({ appended: [8] }), "Added cell 8");
  assert.equal(C.receiptHeadline({ edited: [2] }), "Changed cell 2");
  assert.equal(C.receiptHeadline({ deleted: [5] }), "Removed cell 5");
});

test("receiptHeadline keeps a fixed verb order however the payload is ordered", () => {
  // deleted first on the wire still reads changed -> added -> removed, so a multi-op
  // write always reads the same way round.
  assert.equal(
    C.receiptHeadline({ deleted: [9], appended: [8], edited: [3] }),
    "Changed cell 3 · Added cell 8 · Removed cell 9",
  );
});

test("receiptHeadline pluralises cells the way a sentence does", () => {
  assert.equal(C.receiptHeadline({ edited: [3, 4] }), "Changed cells 3 and 4");
  assert.equal(C.receiptHeadline({ edited: [3, 4, 7] }), "Changed cells 3, 4 and 7");
  assert.equal(C.receiptHeadline({ edited: [1, 2, 3, 4] }), "Changed cells 1, 2, 3 and 4");
});

test("receiptHeadline counts instead of listing once the list stops being readable", () => {
  assert.equal(C.receiptHeadline({ edited: [1, 2, 3, 4, 5] }), "Changed 5 cells");
  assert.equal(C.receiptHeadline({ appended: [10, 11, 12, 13, 14, 15] }), "Added 6 cells");
});

test("receiptHeadline sorts and dedupes the cell numbers", () => {
  assert.equal(C.receiptHeadline({ edited: [7, 3, 3, 7, 4] }), "Changed cells 3, 4 and 7");
});

test("receiptHeadline drops anything that is not a cell number", () => {
  // A receipt that says "cell undefined" or "cell NaN" is worse than one that says less.
  assert.equal(C.receiptHeadline({ edited: [3, null, "x", undefined, 1.5, -2, {}] }),
    "Changed cell 3");
  assert.equal(C.receiptHeadline({ edited: ["4"] }), "Changed cell 4"); // numeric string is a number
});

test("receiptHeadline never renders empty — a write with no readable summary still happened", () => {
  for (const summary of [undefined, null, {}, [], "edited", 7, { edited: [], appended: [] }]) {
    assert.equal(C.receiptHeadline(summary), "Changed the notebook", JSON.stringify(summary));
  }
});

test("receiptHeadline never leaks op-dict or tool vocabulary", () => {
  const out = C.receiptHeadline({ edited: [3], appended: [8], deleted: [9] });
  for (const jargon of ["op", "append_cell", "edit_cell", "index", "status", "applied", "{"]) {
    assert.equal(out.includes(jargon), false, `"${jargon}" must not appear in "${out}"`);
  }
});

// -- receiptObservation: the value-free line that came back ------------------

test("receiptObservation passes the applier's line through, tidied", () => {
  assert.equal(
    C.receiptObservation("  cell 8 ran ·   sales_q3: 12 columns,\n 40331 rows  "),
    "cell 8 ran · sales_q3: 12 columns, 40,331 rows",
  );
});

test("receiptObservation groups a magnitude so it reads as a count", () => {
  assert.equal(C.receiptObservation("40331 rows"), "40,331 rows");
  assert.equal(C.receiptObservation("1234567 rows"), "1,234,567 rows");
  assert.equal(C.receiptObservation("there were 40331."), "there were 40,331.");
  assert.equal(C.receiptObservation("(99999)"), "(99,999)");
});

test("receiptObservation leaves small numbers and identifiers exactly as written", () => {
  // Four digits are not a count (a year is not a magnitude), and a run glued to a
  // letter, dot, dash or underscore is somebody's id, version or column name.
  assert.equal(C.receiptObservation("cell 8 · 12 columns · 2026 rows"), "cell 8 · 12 columns · 2026 rows");
  assert.equal(C.receiptObservation("v1.20250101 built"), "v1.20250101 built");
  assert.equal(C.receiptObservation("col_123456 kept"), "col_123456 kept");
  assert.equal(C.receiptObservation("id-987654 seen"), "id-987654 seen");
  assert.equal(C.receiptObservation("abc123456"), "abc123456");
  assert.equal(C.receiptObservation("3.14159265"), "3.14159265");
});

test("receiptObservation caps a runaway line rather than flooding the transcript", () => {
  const long = "x".repeat(400);
  const out = C.receiptObservation(long);
  assert.equal(out.length <= 240, true, `got ${out.length}`);
  assert.equal(out.endsWith("…"), true);
});

test("receiptObservation is empty for anything that is not text", () => {
  for (const value of [undefined, null, "", "   ", 7, {}, []]) {
    assert.equal(C.receiptObservation(value), "", JSON.stringify(value));
  }
});

// -- receiptSequence: several receipts in one turn read as a sequence --------

test("receiptSequence starts a group when there is nothing to join", () => {
  const s = C.receiptSequence(null, "t-abc123", false);
  assert.deepEqual(s, { reuse: false, turnId: "t-abc123", count: 1, numbered: false });
});

test("receiptSequence keeps one turn's receipts together and numbers them", () => {
  let prev = { turnId: "t-abc123", count: 1 };
  const second = C.receiptSequence(prev, "t-abc123", true);
  assert.equal(second.reuse, true);
  assert.equal(second.count, 2);
  // The first receipt is numbered retroactively — a lone "1" is noise, "1 2 3" is a
  // sequence.
  assert.equal(second.numbered, true);
  prev = { turnId: "t-abc123", count: second.count };
  const third = C.receiptSequence(prev, "t-abc123", true);
  assert.equal(third.count, 3);
  assert.equal(third.numbered, true);
});

test("receiptSequence starts a NEW group for a new turn", () => {
  const s = C.receiptSequence({ turnId: "t-old", count: 3 }, "t-new", true);
  assert.equal(s.reuse, false);
  assert.equal(s.count, 1);
  assert.equal(s.turnId, "t-new");
});

test("receiptSequence never reuses a group that is no longer on the page", () => {
  // /clear empties #messages. Appending into the detached group it leaves behind would
  // swallow every later receipt in silence — the same failure shape as a wholesale
  // innerHTML rebuild eating a live card.
  const s = C.receiptSequence({ turnId: "t-abc123", count: 2 }, "t-abc123", false);
  assert.equal(s.reuse, false);
  assert.equal(s.count, 1);
});

test("receiptSequence copes with a missing or malformed turn id / count", () => {
  // No turn id groups by "" — which is still one group per turn, because the caller
  // drops its group at the start of every turn.
  const first = C.receiptSequence(null, undefined, false);
  assert.equal(first.turnId, "");
  const second = C.receiptSequence({ turnId: "", count: "nope" }, undefined, true);
  assert.equal(second.reuse, true);
  assert.equal(second.count, 1); // a junk count restarts at 1 rather than yielding NaN
  assert.equal(C.receiptSequence({ turnId: "", count: 1 }, 7, true).reuse, true);
});

// -- cancelledNotice: the stop registered, the turn is winding down ----------

test("cancelledNotice says the analyst stopped it, and that it is still winding down", () => {
  const out = C.cancelledNotice("analyst");
  assert.equal(out.startsWith("You stopped this turn."), true);
  // The session broadcasts `cancelled` the moment it is asked; the model still finishes
  // the step it had started. Claiming the assistant has gone quiet here would be the
  // exact "looks stopped but is still streaming" failure.
  assert.match(out, /wrapping up/);
});

test("cancelledNotice only credits the ANALYST when the analyst is who stopped it", () => {
  // A session ending a turn for its own reasons is not the analyst being in control,
  // and saying "you stopped this" would be a lie about who did.
  assert.match(C.cancelledNotice("timeout"), /^This turn was stopped — it ran out of time\./);
  assert.match(C.cancelledNotice("provider"), /^This turn was stopped\./);
  assert.equal(C.cancelledNotice("provider").startsWith("You stopped"), false);
});

test("cancelledNotice drops the wind-down clause once the turn has already ended", () => {
  // Frames can arrive out of order (and the stream replays on reconnect). Promising a
  // wind-up that already happened is the small lie that makes the big one believable.
  const out = C.cancelledNotice("analyst", false);
  assert.equal(out, "You stopped this turn.");
  assert.equal(/wrapping up/.test(out), false);
  // Anything other than an explicit false is the ordinary live turn.
  assert.equal(C.cancelledNotice("analyst", true), C.cancelledNotice("analyst"));
  assert.equal(C.cancelledNotice("analyst", undefined), C.cancelledNotice("analyst"));
});

test("cancelledNotice treats a missing/odd reason as the analyst's stop", () => {
  // The stop button is the only thing that produces this event today, so an absent
  // reason is that button — but the wording still must not invent a different actor.
  for (const reason of [undefined, null, "", "  ", "ANALYST", "user", 7]) {
    const out = C.cancelledNotice(reason);
    assert.equal(typeof out, "string");
    assert.equal(out.length > 0, true);
  }
  assert.equal(C.cancelledNotice(undefined).startsWith("You stopped this turn."), true);
  assert.equal(C.cancelledNotice("ANALYST").startsWith("You stopped this turn."), true);
});

// -- stopButtonState: the control never overstates what it achieved ----------

test("stopButtonState hides the control when there is no live turn to stop", () => {
  for (const state of ["idle", "error", "connecting", "unavailable", "", undefined]) {
    const s = C.stopButtonState(state, false);
    assert.equal(s.visible, false, String(state));
    assert.equal(s.disabled, true, String(state)); // hidden AND inert, never just hidden
  }
});

test("stopButtonState offers a live Stop while a turn is in flight", () => {
  for (const state of ["thinking", "streaming"]) {
    const s = C.stopButtonState(state, false);
    assert.equal(s.visible, true, state);
    assert.equal(s.disabled, false, state);
    assert.equal(s.label, "Stop", state);
    assert.match(s.title, /Esc/); // the keyboard route is discoverable from the control
  }
});

test("stopButtonState reads 'Stopping…' — not 'Stopped' — while the ask is in flight", () => {
  const s = C.stopButtonState("streaming", true);
  assert.equal(s.visible, true);
  assert.equal(s.disabled, true); // the ask is already sent; a second press asks nothing
  assert.equal(s.label, "Stopping…");
  assert.equal(s.label.includes("Stopped"), false);
  assert.match(s.title, /finishing the step/);
});

test("stopButtonState drops the pending state the moment the turn is no longer live", () => {
  // A "Stopping…" left standing after the turn ended would make the NEXT turn open
  // looking like it was already being called off.
  const s = C.stopButtonState("idle", true);
  assert.equal(s.visible, false);
  assert.equal(s.label, "Stop");
});

// -- stopOutcomeNotice: the two ways a stop does not end in a stopped turn ---

test("stopOutcomeNotice says out loud when the turn finished before the stop landed", () => {
  const out = C.stopOutcomeNotice("finished");
  assert.match(out, /finished on its own/);
  assert.equal(out.includes("stopped it"), false);
});

test("stopOutcomeNotice says out loud when the stop did not take", () => {
  const out = C.stopOutcomeNotice("failed");
  assert.match(out, /still running/);
  // Anything this client does not recognise must fall to the CAUTIOUS reading: never
  // report a turn as stopped on the strength of an outcome we could not read.
  assert.equal(C.stopOutcomeNotice("who knows"), out);
  assert.equal(C.stopOutcomeNotice(undefined), out);
});

// -- canStopTurn: the button and the Esc key ask the same question -----------

test("canStopTurn allows a stop only during a live turn on a live session", () => {
  assert.equal(C.canStopTurn("thinking", false, true), true);
  assert.equal(C.canStopTurn("streaming", false, true), true);
});

test("canStopTurn refuses when there is no session to cancel on", () => {
  // openChat clears `sid` on a failure, on the per-notebook AI off-switch, and while
  // Copilot is signed out. POSTing a cancel for no session is noise, not a stop.
  assert.equal(C.canStopTurn("streaming", false, false), false);
});

test("canStopTurn refuses a second ask while one is already in flight", () => {
  assert.equal(C.canStopTurn("streaming", true, true), false);
});

test("canStopTurn refuses when there is no turn in flight", () => {
  for (const state of ["idle", "error", "connecting", "unavailable", ""]) {
    assert.equal(C.canStopTurn(state, false, true), false, state);
  }
});

// -- turnEndOutcome: how a turn's end is reported ---------------------------

test("turnEndOutcome reports a stop the session acknowledged as stopped", () => {
  assert.deepEqual(C.turnEndOutcome(true, true), { status: "stopped", notice: "" });
});

test("turnEndOutcome says so when the turn finished before the stop reached it", () => {
  // The click landed, the answer came back ok, and then the turn ended on its own —
  // a "stopping…" that silently becomes an ordinary finish is how the button stops
  // being believed.
  const out = C.turnEndOutcome(true, false);
  assert.equal(out.status, "");
  assert.equal(out.notice, C.stopOutcomeNotice("finished"));
});

test("turnEndOutcome says nothing about stopping for an ordinary turn", () => {
  assert.deepEqual(C.turnEndOutcome(false, false), { status: "", notice: "" });
});

test("turnEndOutcome trusts the acknowledgement even without a local ask", () => {
  // The session can broadcast `cancelled` without this page having clicked (another
  // window, or the session cancelling for its own reasons) — the turn still stopped.
  assert.deepEqual(C.turnEndOutcome(false, true), { status: "stopped", notice: "" });
});
