"use strict";

// The chat transcript's TWO streaming channels. Some OpenAI-compatible gateways
// (LiteLLM, DeepSeek, Qwen, vLLM) stream a reasoning model's THINKING ahead of its
// answer; mooring forwards it as a `delta` frame flagged `reasoning: true` so the chat
// window is not dead for the whole think. Everything about that is display-only, and
// the one decision that keeps it from being rendered as the model's answer is
// `deltaChannel` — so it is pinned here, DOM-free, with the label helper beside it.
//
// Run with:  node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const C = require("../../src/mooring/hub/static/chat_core.js");

test("a frame flagged reasoning goes to the thinking channel", () => {
  assert.equal(C.deltaChannel({ text: "hmm", reasoning: true }), "reasoning");
});

test("an ordinary delta is prose — the answer, exactly as before", () => {
  assert.equal(C.deltaChannel({ text: "the answer" }), "prose");
});

test("anything that is not literally `true` falls back to prose", () => {
  // Fail-SAFE direction: the cost of mis-reading a frame as prose is a think shown as
  // an answer once; the cost of mis-reading the answer as a think is the reply
  // disappearing into a folded block. So only an exact `true` diverts, and every
  // unreadable/degraded/legacy shape renders the way it always has.
  for (const frame of [
    {},
    null,
    undefined,
    "reasoning",
    42,
    { text: "x", reasoning: false },
    { text: "x", reasoning: "true" },
    { text: "x", reasoning: 1 },
    { text: "x", reasoning: null },
  ]) {
    assert.equal(C.deltaChannel(frame), "prose", JSON.stringify(frame) + " must be prose");
  }
});

test("the fold label says ACTIVITY while live and a noun once it is over", () => {
  const live = C.reasoningSummary(true);
  const done = C.reasoningSummary(false);
  assert.notEqual(live, done);
  assert.match(live, /thinking/i); // the whole point: the window is not dead
  // Neither label may read as the assistant's reply.
  for (const label of [live, done]) assert.ok(label.length > 0);
  // Only an exact `true` is "live" — the default for any other shape is the resting
  // label, so a block can never be left claiming to think.
  for (const v of [false, undefined, null, "true", 1]) {
    assert.equal(C.reasoningSummary(v), done);
  }
});

test("chat.js keeps the thinking text out of the assistant row's accumulator", () => {
  // The load-bearing invariant is enforced in the SERVER (reasoning never joins
  // `text_parts`), but the browser has its own copy of it: the answer row accumulates
  // into `asstRaw` and the think into `reasonRaw`, and `finalizeAssistant` renders
  // `asstRaw` alone. A refactor that pointed `appendReasoning` at `asstRaw` would put
  // the model's scratch thinking in the transcript as its reply, and no DOM-free test
  // can catch that — so pin the two accumulators as distinct by source.
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "chat.js"),
    "utf8",
  );
  const appendReasoning = src.slice(src.indexOf("function appendReasoning("));
  const body = appendReasoning.slice(0, appendReasoning.indexOf("\n}"));
  assert.ok(body.includes("reasonRaw"), "appendReasoning must accumulate into reasonRaw");
  assert.ok(!body.includes("asstRaw"), "appendReasoning must never touch asstRaw");
  assert.ok(!body.includes("streamingRow"), "appendReasoning must never open the answer row");
});
