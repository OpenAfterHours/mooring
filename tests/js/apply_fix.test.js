"use strict";

// Unit tests for the Apply repair loop's bound (ChatCore.applyFailureAction /
// applyFixPrompt).
//
// When an Apply fails, chat.js can hand the error back to the assistant for a
// corrective re-proposal. That used to be a single boolean — one attempt, ever — and
// weaker models routinely need the second. The bound is now explicit and small, and it
// lives here as a pure function precisely so the two cases that must NOT consume an
// attempt can be pinned: a 409 (the notebook moved under the proposal — re-READING is
// what's needed) and a refusal re-proposing cannot answer (an unreadable 428 gate hold).
// Spending the analyst's second attempt on either would leave nothing for the failure
// that actually IS the model's to fix.
//
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../src/mooring/hub/static/chat_core.js");

test("the bound is two attempts, not one", () => {
  assert.equal(C.MAX_FIX_ATTEMPTS, 2);
});

test("a write failure asks for a fix, twice, then reports", () => {
  const first = C.applyFailureAction(502, 0, false);
  assert.deepEqual(first, { action: "fix", tried: 1 });
  const second = C.applyFailureAction(502, first.tried, false);
  assert.deepEqual(second, { action: "fix", tried: 2 });
  // Two failed corrections is a model looping, not converging — and every attempt is a
  // billed turn, so it stops there.
  assert.deepEqual(C.applyFailureAction(502, second.tried, false), {
    action: "report",
    tried: 2,
  });
});

test("a staleness 409 neither fixes nor consumes an attempt", () => {
  assert.deepEqual(C.applyFailureAction(409, 0, false), { action: "conflict", tried: 0 });
  // ...and the two real attempts are still there afterwards.
  assert.equal(C.applyFailureAction(502, 0, false).action, "fix");
  assert.deepEqual(C.applyFailureAction(409, 1, false), { action: "conflict", tried: 1 });
  assert.equal(C.applyFailureAction(502, 1, false).action, "fix");
});

test("an unreadable gate hold reports without consuming an attempt", () => {
  // noFix: a 428 whose body we could not read. Re-proposing would not answer a gate,
  // so it must not be asked for — and the attempt budget is untouched.
  assert.deepEqual(C.applyFailureAction(428, 0, true), { action: "report", tried: 0 });
  assert.deepEqual(C.applyFailureAction(502, 0, true), { action: "report", tried: 0 });
});

test("a missing or nonsense attempt count is read as zero", () => {
  for (const bad of [undefined, null, NaN, -3, "1"]) {
    assert.deepEqual(C.applyFailureAction(502, bad, false), { action: "fix", tried: 1 });
  }
});

test("the second attempt says so, and says not to resend", () => {
  const first = C.applyFixPrompt("bad cell", 1);
  assert.match(first, /could not be applied: bad cell/);
  assert.doesNotMatch(first, /second attempt/);
  // Every attempt keeps the cell-shape reminder — it is the most common failure.
  assert.match(first, /BODY only/);

  const second = C.applyFixPrompt("bad cell", 2);
  assert.match(second, /second attempt/);
  assert.match(second, /change the approach rather than resending it/);
  assert.match(second, /BODY only/);
});
