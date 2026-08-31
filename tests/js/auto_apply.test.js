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
const fs = require("node:fs");
const path = require("node:path");
const C = require("../../src/mooring/hub/static/chat_core.js");

const CHAT_JS = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "chat.js"),
  "utf8",
);
const STYLE_CSS = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "style.css"),
  "utf8",
);

// -- the DOM decisions, pinned at the source ---------------------------------
// These four are not wording, so no pure helper can hold them — but each was a real
// defect, and each is a one-line regression away.

test("a receipt is appended where it happened, not into a fixed container", () => {
  // The group used to be a <div> appended when receipt 1 arrived; receipts 2..n landed
  // INSIDE it, above every tool row and sentence that had appended to the end since —
  // and off the bottom of a view pinned to the bottom. In a five-write turn the analyst
  // could see receipt 1 and never see 2-5, for changes that had already run.
  assert.equal(CHAT_JS.includes('className = "receipt-group"'), false);
  assert.equal(CHAT_JS.includes("group.el"), false);
  assert.match(CHAT_JS, /\$\("messages"\)\.appendChild\(row\)/);
  // …and the numbering moved onto the rows with it.
  assert.equal(STYLE_CSS.includes(".receipt-group"), false);
  assert.match(STYLE_CSS, /\.receipt\.receipt-seq \.receipt-step/);
});

test("nothing suppresses an 'applied' frame that arrives during a manual Apply", () => {
  // /api/ai/chat/apply broadcasts nothing, so there was never a duplicate receipt to
  // suppress — the in-flight counter could only ever drop a GENUINE model receipt,
  // leaving a change in the notebook with no row here and no way back on screen.
  assert.equal(CHAT_JS.includes("manualApplyInFlight"), false);
});

test("the Revert button has a style of its own", () => {
  assert.match(STYLE_CSS, /\.chat-body \.receipt-revert\s*\{/);
});

test("stopTurn cannot leave the control wedged on a network failure", () => {
  // Without the catch, a failed POST left the button a disabled "Stopping…" and the
  // composer locked, with no way out but a reload — the one control that must never be
  // the thing that traps them.
  const body = CHAT_JS.slice(CHAT_JS.indexOf("async function stopTurn("));
  assert.match(body.slice(0, 1400), /try \{[\s\S]*chat\/cancel[\s\S]*\} catch/);
});

// -- receiptHeadline: what changed, in the analyst's language ----------------

test("receiptHeadline counts cells from the TOP, never from zero", () => {
  // The wire numbers are zero-based indices into the file — a number the analyst has no
  // way to see, since marimo never prints it and "cell 0" is not on their screen. They
  // are rendered as a position they can reach by counting down the notebook.
  assert.equal(C.receiptHeadline({ edited: [0] }), "Changed the 1st cell");
  assert.equal(C.receiptHeadline({ edited: [2] }), "Changed the 3rd cell");
  assert.equal(C.receiptHeadline({ deleted: [5] }), "Removed the 6th cell");
  assert.equal(C.receiptHeadline({ edited: [10] }), "Changed the 11th cell"); // not "11st"
  assert.equal(C.receiptHeadline({ edited: [20] }), "Changed the 21st cell");
  assert.equal(C.receiptHeadline({ edited: [111] }), "Changed the 112th cell");
});

test("receiptHeadline never prints 'cell 0'", () => {
  for (const summary of [{ edited: [0] }, { deleted: [0] }, { appended: [0] }]) {
    assert.equal(C.receiptHeadline(summary).includes("cell 0"), false, JSON.stringify(summary));
  }
});

test("receiptHeadline says where an appended cell went, not which number it is", () => {
  // Appends always land at the end, so their index is a fact about the file's length
  // rather than about the change — naming it invites a hunt for a number nothing agrees
  // with.
  assert.equal(C.receiptHeadline({ appended: [8] }), "Added a new cell at the end");
  assert.equal(C.receiptHeadline({ appended: [8, 9] }), "Added 2 new cells at the end");
  assert.equal(
    C.receiptHeadline({ edited: [3], appended: [8], deleted: [] }),
    "Changed the 4th cell · Added a new cell at the end",
  );
});

test("receiptHeadline keeps a fixed verb order however the payload is ordered", () => {
  // deleted first on the wire still reads changed -> added -> removed, so a multi-op
  // write always reads the same way round.
  assert.equal(
    C.receiptHeadline({ deleted: [9], appended: [8], edited: [3] }),
    "Changed the 4th cell · Added a new cell at the end · Removed the 10th cell",
  );
});

test("receiptHeadline pluralises cells the way a sentence does", () => {
  assert.equal(C.receiptHeadline({ edited: [3, 4] }), "Changed the 4th and 5th cells");
  assert.equal(C.receiptHeadline({ edited: [3, 4, 7] }), "Changed the 4th, 5th and 8th cells");
  assert.equal(
    C.receiptHeadline({ edited: [0, 1, 2, 3] }),
    "Changed the 1st, 2nd, 3rd and 4th cells",
  );
});

test("receiptHeadline counts instead of listing once the list stops being readable", () => {
  assert.equal(C.receiptHeadline({ edited: [1, 2, 3, 4, 5] }), "Changed 5 cells");
  assert.equal(
    C.receiptHeadline({ appended: [10, 11, 12, 13, 14, 15] }),
    "Added 6 new cells at the end",
  );
});

test("receiptHeadline sorts and dedupes the cell numbers", () => {
  assert.equal(C.receiptHeadline({ edited: [7, 3, 3, 7, 4] }), "Changed the 4th, 5th and 8th cells");
});

test("receiptHeadline drops anything that is not a cell number", () => {
  // A receipt that says "cell undefined" or "cell NaN" is worse than one that says less.
  assert.equal(C.receiptHeadline({ edited: [3, null, "x", undefined, 1.5, -2, {}] }),
    "Changed the 4th cell");
  assert.equal(C.receiptHeadline({ edited: ["4"] }), "Changed the 5th cell"); // numeric string
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

// -- revertScope: the button says what it will undo, BEFORE it is pressed ----
// The defect this replaces: mooring takes ONE undo checkpoint per TURN, so five writes
// in a turn share one snapshot and the Revert beside the fifth receipt put back all
// five — while its tooltip said "before this change" and the confirmation said
// "Reverted the last applied change". For a reader who does not read Python and cannot
// diff the notebook, that is the difference between knowing what happened and not.

test("revertScope reports the exact scope when the hub says how far the checkpoint reaches", () => {
  const s = C.revertScope(4, 4);
  assert.equal(s.covers, 4);
  assert.match(s.label, /4 changes/); // the count is ON the button, not only in a tooltip
  assert.match(s.title, /last 4 changes/);
  assert.match(s.title, /not just this one/);
  assert.equal(s.note.length > 0, true); // …and visible without hovering
});

test("revertScope says 'just before this change' only when that is true", () => {
  // checkpoint_writes = 1 is the hub saying so outright.
  const known = C.revertScope(1, 3);
  assert.equal(known.covers, 1);
  assert.equal(known.label, "Revert");
  assert.match(known.title, /just before this change/);
  assert.equal(known.note, "");
  // …and the first write of a turn always opens a fresh checkpoint, which is knowable
  // without the hub's help.
  const first = C.revertScope(undefined, 1);
  assert.equal(first.covers, 1);
  assert.match(first.title, /just before this change/);
  assert.equal(first.note, "");
});

test("revertScope never invents a number when the hub did not send one", () => {
  // undo_depth cannot tell an extended checkpoint from a pruned stack (bounded at 25),
  // so a confident count here would be the same lie in a new place. Uncertainty is said
  // out loud instead.
  const s = C.revertScope(undefined, 5);
  assert.equal(s.covers, null);
  assert.equal(/\d/.test(s.label), false, `no count on the button: "${s.label}"`);
  assert.match(s.title, /may go back with it/);
  assert.match(s.note, /earlier changes in this turn/);
  assert.equal(s.title.includes("just before this change"), false);
});

test("revertScope treats an unreadable checkpoint_writes as absent", () => {
  for (const bad of [null, "4", 0, -1, 2.5, NaN, Infinity, {}, true]) {
    const s = C.revertScope(bad, 3);
    assert.equal(s.covers, null, JSON.stringify(bad));
  }
  // …and an unreadable POSITION falls to the cautious first-write reading rather than
  // throwing or claiming a scope.
  assert.equal(C.revertScope(undefined, undefined).covers, 1);
  assert.equal(C.revertScope(undefined, 0).covers, 1);
});

// -- revertedNotice: what the transcript says AFTER the revert lands ---------

test("revertedNotice reports the scope the button promised", () => {
  assert.match(C.revertedNotice(4, 0), /last 4 changes/);
  assert.equal(C.revertedNotice(1, 0), "Reverted the last applied change.");
});

test("revertedNotice keeps saying 'may have' when the scope was never known", () => {
  const out = C.revertedNotice(null, 0);
  assert.match(out, /may have gone back with it/);
  assert.match(out, /Check the notebook/);
  // The old sentence claimed exactly one change every time; it must not come back.
  assert.equal(out.startsWith("Reverted the last applied change."), false);
});

test("revertedNotice still points at /undo for anything left underneath", () => {
  assert.match(C.revertedNotice(1, 3), /3 earlier changes still undoable with \/undo/);
  assert.match(C.revertedNotice(1, 1), /1 earlier change still undoable/);
  assert.equal(/still undoable/.test(C.revertedNotice(1, 0)), false);
  for (const junk of [undefined, null, "x", -2, NaN]) {
    assert.equal(/still undoable/.test(C.revertedNotice(1, junk)), false, String(junk));
  }
});

// -- receiptDisplacedNote: what happened to an older receipt's way back ------

test("receiptDisplacedNote never sends the analyst to /undo for a same-turn change", () => {
  // A later write in the SAME turn joins this one's checkpoint, so this change has no
  // undo step of its own and /undo will never stop on it. "superseded · /undo steps
  // further back" was simply false there.
  const same = C.receiptDisplacedNote(true);
  assert.equal(same.includes("/undo"), false, same);
  assert.match(same, /this turn/);
  const later = C.receiptDisplacedNote(false);
  assert.match(later, /\/undo/);
});

// -- receiptObservation: the failure clause is never truncated away ----------

test("receiptObservationFull keeps the whole line, however long", () => {
  const long = "sales: 12 columns, 40331 rows · " + "x".repeat(300) + " · not bound: total";
  const full = C.receiptObservationFull(long);
  assert.match(full, /not bound: total$/); // the half that says it did NOT work
  assert.match(full, /40,331 rows/); // still tidied and grouped
  assert.equal(full.length > 240, true);
  // …and the short form, which is what is shown, would have cut exactly that clause.
  assert.equal(C.receiptObservation(long).includes("not bound"), false);
});

test("receiptObservationTruncated says when a 'show more' is owed", () => {
  assert.equal(C.receiptObservationTruncated("x".repeat(400)), true);
  assert.equal(C.receiptObservationTruncated("cell 8 ran"), false);
  assert.equal(C.receiptObservationTruncated(""), false);
  assert.equal(C.receiptObservationTruncated(undefined), false);
});

test("receiptObservationFull is empty for anything that is not text", () => {
  for (const value of [undefined, null, "", "   ", 7, {}, []]) {
    assert.equal(C.receiptObservationFull(value), "", JSON.stringify(value));
  }
});

// -- cancelEventAction: one turn, one ending --------------------------------

test("cancelEventAction drops a stop that lost the race to the turn's own end", () => {
  // `request_cancel` broadcasts unconditionally. When `idle` arrived first the
  // transcript said "that turn finished on its own before the stop reached it" and this
  // frame then said "You stopped this turn" and parked the status on "stopped" for an
  // idle, ready session. Both cannot be true.
  assert.equal(C.cancelEventAction(true, false), "drop");
});

test("cancelEventAction reports a stop that reached a live turn", () => {
  assert.equal(C.cancelEventAction(false, false), "report");
});

test("cancelEventAction acknowledges a stop exactly once", () => {
  assert.equal(C.cancelEventAction(false, true), "drop");
  assert.equal(C.cancelEventAction(true, true), "drop");
});

// -- noticeMessageAction: mooring's asides are not the assistant's words -----

test("noticeMessageAction keeps ordinary assistant text as prose", () => {
  assert.equal(C.noticeMessageAction(false, false), "prose");
  assert.equal(C.noticeMessageAction(undefined, true), "prose");
});

test("noticeMessageAction renders mooring's own aside as an aside", () => {
  // The tool-ceiling line is mooring speaking, not the model; as assistant prose it put
  // mooring's words in the model's mouth.
  assert.equal(C.noticeMessageAction(true, false), "sys");
});

test("noticeMessageAction drops the stop's second announcement", () => {
  // _end_cancelled broadcasts "(Stopped at your request.)" as a notice AFTER the
  // `cancelled` frame the transcript has already reported in the analyst's own terms.
  assert.equal(C.noticeMessageAction(true, true), "drop");
});

// -- toolDoneMark: a stop is not the assistant failing -----------------------

test("toolDoneMark closes a stopped turn's remaining calls as stopped, not failed", () => {
  const stopped = C.toolDoneMark(false, true);
  assert.equal(stopped.cls, "stopped");
  assert.equal(stopped.glyph, "⏹");
  assert.equal(stopped.glyph === "✗", false); // never the ✗ that blames the model
});

test("toolDoneMark still marks a real failure and a real success", () => {
  assert.deepEqual(C.toolDoneMark(true, false), { cls: "ok", glyph: "⏺" });
  assert.deepEqual(C.toolDoneMark(false, false), { cls: "fail", glyph: "✗" });
  // A call that SUCCEEDED during a stop is still a success.
  assert.equal(C.toolDoneMark(true, true).cls, "ok");
});

// -- autoApplyBanner: the mode is stated, not inferred from a receipt --------

test("autoApplyBanner says what auto-apply will do to the notebook", () => {
  const on = C.autoApplyBanner(true);
  assert.match(on, /changes your notebook itself/);
  assert.match(on, /Revert/);
  assert.match(on, /stops and asks you first/); // the irreversible carve-out
  assert.match(on, /Stop/);
  assert.match(on, /Settings/); // …and how to turn it off
});

test("autoApplyBanner says the opposite for propose-then-Apply", () => {
  const off = C.autoApplyBanner(false);
  assert.match(off, /nothing touches your notebook until/);
  assert.match(off, /Apply/);
  assert.equal(off.includes("changes your notebook itself"), false);
});

// -- /help, in the mode the chat is actually in -----------------------------

test("helpRows describes /apply and /diff as acting on a HELD change in auto mode", () => {
  const auto = new Map(C.helpRows(true));
  assert.match(auto.get("/apply"), /holding for you/);
  assert.match(auto.get("/diff"), /holding for you/);
  const manual = new Map(C.helpRows(false));
  assert.match(manual.get("/apply"), /latest proposal/);
});

test("helpRows never advertises /apply as 'the latest proposal' in auto mode", () => {
  // In auto mode there is usually no proposal at all — the change has already landed.
  const auto = new Map(C.helpRows(true));
  assert.equal(auto.get("/apply").includes("latest proposal"), false);
});

test("helpKeys qualifies the a/s keys the same way", () => {
  assert.match(C.helpKeys(true), /holding for you/);
  assert.match(C.helpKeys(false), /skip a proposal/);
  for (const mode of [true, false]) {
    assert.match(C.helpKeys(mode), /Esc/); // the stop stays discoverable in both
  }
});

test("COMMANDS help is true in BOTH modes", () => {
  const help = new Map(C.COMMANDS.map((c) => [c.name, c.help]));
  // "propose" described only manual mode, and stopped being true for everyone the day
  // auto-apply became the default.
  for (const name of ["checks", "sql", "apply", "diff", "undo"]) {
    assert.equal(help.get(name).includes("propose"), false, `${name}: ${help.get(name)}`);
  }
});

// -- events this page did not write ----------------------------------------

test("eventNoteText finds the sentence whatever the sender called the field", () => {
  assert.equal(C.eventNoteText({ text: " hello " }), "hello");
  assert.equal(C.eventNoteText({ detail: "d" }), "d");
  assert.equal(C.eventNoteText({ message: "m" }), "m");
  assert.equal(C.eventNoteText({ summary: "s" }), "s");
  assert.equal(C.eventNoteText({ error: "e" }), "e");
  assert.equal(C.eventNoteText({ text: "first", detail: "second" }), "first");
});

test("eventNoteText is empty rather than wrong for a payload it cannot read", () => {
  for (const value of [undefined, null, "text", 7, [], {}, { text: "  " }, { text: 7 }]) {
    assert.equal(C.eventNoteText(value), "", JSON.stringify(value));
  }
});

test("runReportNote narrates a run that mooring started by itself", () => {
  const running = C.runReportNote({ state: "running" });
  assert.match(running.text, /can take a few minutes/);
  assert.equal(running.sent, "");
  assert.equal(C.runReportNote({ state: "started" }).text, running.text);
});

test("runReportNote shows exactly what was sent when the run failed", () => {
  const note = C.runReportNote({
    sent: "ValueError in cell 3",
    redactions: [{ line: 2, kind: "email" }],
  });
  assert.equal(note.sent, "ValueError in cell 3");
  assert.match(note.text, /exactly what was sent/);
  assert.equal(note.redactions.length, 1);
});

test("runReportNote says a clean run is a clean run", () => {
  assert.match(C.runReportNote({ ran_clean: true }).text, /ran clean/);
});

test("runReportNote still produces a row for a payload it does not recognise", () => {
  // Silence reads as "nothing happened", and by this point the notebook has been run.
  for (const value of [{}, undefined, null, [], "x", { odd: 1 }]) {
    const note = C.runReportNote(value);
    assert.equal(typeof note.text, "string");
    assert.equal(note.text.length > 0, true, JSON.stringify(value));
  }
  assert.equal(C.runReportNote({ text: "custom" }).text, "custom");
});

test("applyFailedNote distinguishes a write that failed from one that worked", () => {
  // Today neither is said at all, so a change that did not land and a change that landed
  // and broke something look identical from the transcript.
  assert.match(C.applyFailedNote({ status: "conflict" }), /had moved underneath it/);
  assert.match(C.applyFailedNote({ status: "disabled" }), /switched off/);
  assert.match(C.applyFailedNote({ status: "cancelled" }), /You stopped the turn/);
  for (const status of ["conflict", "disabled", "cancelled", "error", "", undefined]) {
    assert.match(C.applyFailedNote({ status }), /[Nn]othing was written/);
  }
});

test("applyFailedNote appends the server's own value-free detail", () => {
  const out = C.applyFailedNote({ status: "conflict", text: "the anchor no longer matches" });
  assert.match(out, /had moved underneath it/);
  assert.match(out, /the anchor no longer matches/);
});

test("applyFailedNote copes with a payload it cannot read", () => {
  for (const value of [undefined, null, {}, [], "x", 7]) {
    assert.match(C.applyFailedNote(value), /did not land/, JSON.stringify(value));
  }
});

test("streamResumedNotice warns that a drop can cost the transcript a change", () => {
  const out = C.streamResumedNotice();
  assert.match(out, /dropped and came back/);
  assert.match(out, /in your notebook but not in this transcript/);
  assert.match(out, /\/undo/);
});
