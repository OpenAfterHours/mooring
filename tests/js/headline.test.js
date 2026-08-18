"use strict";

// The hub headline's priority order IS the product decision — one sentence about the
// workspace, and the single action that answers it — so it is pinned here rather than
// left to a reading of app.js. Zero dependencies: Node's built-in test runner.
//   node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const Headline = require("../../src/mooring/hub/static/headline.js");

const repo = (extra) => Object.assign({ mode: "repo", loggedIn: true, files: [] }, extra || {});
const rows = (...states) => states.map((state, i) => ({ path: `n${i}.py`, state }));

test("a clean workspace says so, and offers a new notebook", () => {
  const h = Headline.derive(repo());
  assert.equal(h.text, "Everything here is in sync with your team.");
  assert.equal(h.primary.id, "new");
  assert.deepEqual(h.links.map((l) => l.id), ["search"]);
});

test("incoming work counts in words and offers Pull", () => {
  const h = Headline.derive(repo({ files: rows("remote changed", "new remote") }));
  assert.equal(h.text, "Two updates are waiting from your team.");
  assert.equal(h.primary.id, "pull");
});

test("'overnight' is claimed only when the work predates a MORNING session", () => {
  const files = rows("remote changed");
  const overnight = Headline.derive(repo({ files, pullWaitedAtStart: true, morning: true }));
  assert.equal(overnight.text, "One update came in from your team overnight.");
  // Arrived while you were working: it is waiting, not overnight.
  const during = Headline.derive(repo({ files, pullWaitedAtStart: false, morning: true }));
  assert.equal(during.text, "One update is waiting from your team.");
  // Already there, but you sat down after lunch: still not "overnight".
  const afternoon = Headline.derive(repo({ files, pullWaitedAtStart: true, morning: false }));
  assert.equal(afternoon.text, "One update is waiting from your team.");
});

test("outgoing work offers Push all, with Propose beside it", () => {
  const h = Headline.derive(repo({ files: rows("modified", "new local", "deleted locally") }));
  assert.equal(h.text, "Three of your changes are ready to push.");
  assert.equal(h.primary.id, "push");
  assert.deepEqual(h.links.map((l) => l.id), ["new", "propose", "search"]);
});

test("one change reads in the singular", () => {
  const h = Headline.derive(repo({ files: rows("modified") }));
  assert.equal(h.text, "One of your changes is ready to push.");
});

test("a conflict outranks everything else that is actionable", () => {
  // A workspace with a conflict AND both directions of work still leads with the
  // conflict: it is the one that blocks the others.
  const h = Headline.derive(repo({
    files: rows("conflict", "modified", "remote changed"),
    review: { branch: "review/x" },
  }));
  assert.equal(h.text, "One notebook needs you to resolve a conflict.");
  assert.equal(h.primary.id, "resolve");
  assert.ok(h.links.some((l) => l.id === "pull"));
});

test("conflicts pluralise", () => {
  const h = Headline.derive(repo({ files: rows("conflict", "conflict") }));
  assert.equal(h.text, "Two notebooks need you to resolve conflicts.");
});

test("offline outranks even a conflict, and offers NO network action", () => {
  const h = Headline.derive(repo({ offline: { reason: "network" }, files: rows("conflict") }));
  assert.equal(h.text, "GitHub is unreachable — this is your last synced view.");
  assert.equal(h.primary, null);
  // Only the two things that still work offline are offered.
  assert.deepEqual(h.links.map((l) => l.id), ["new", "search"]);
});

test("an open proposal is reported once nothing is pending", () => {
  const h = Headline.derive(repo({ review: { branch: "review/priya" } }));
  assert.equal(h.text, "Your proposal on review/priya is waiting for review.");
  assert.equal(h.primary.id, "review-pr");
});

test("work to push outranks an open proposal", () => {
  const h = Headline.derive(repo({ files: rows("modified"), review: { branch: "review/x" } }));
  assert.equal(h.primary.id, "push");
});

test("incoming work links to the outgoing pile only when there IS one", () => {
  const both = Headline.derive(repo({ files: rows("remote changed", "modified") }));
  assert.deepEqual(both.links.map((l) => l.label), ["new notebook", "push all · 1", "search"]);
  const pullOnly = Headline.derive(repo({ files: rows("remote changed") }));
  assert.deepEqual(pullOnly.links.map((l) => l.id), ["new", "search"]);
});

test("local mode never mentions a team, and the login wall offers nothing else", () => {
  const local = Headline.derive({ mode: "local", loggedIn: false, files: rows("local") });
  assert.equal(local.text, "This workspace is local — nothing here is shared yet.");
  assert.equal(local.primary.id, "new");
  const wall = Headline.derive({ mode: "repo", loggedIn: false, files: [] });
  assert.equal(wall.text, "Sign in to GitHub to sync with your team.");
  assert.equal(wall.primary, null);
  assert.deepEqual(wall.links, []);
});

test("derive copes with a missing/empty state object", () => {
  for (const input of [undefined, {}, { files: null }]) {
    const h = Headline.derive(input);
    assert.equal(typeof h.text, "string");
    assert.ok(h.text.length > 0);
    assert.ok(Array.isArray(h.links));
  }
});

test("counts beyond the word list fall back to digits", () => {
  assert.equal(Headline.count(3), "three");
  assert.equal(Headline.count(12), "twelve");
  assert.equal(Headline.count(13), "13");
  const h = Headline.derive(repo({ files: rows(...Array(13).fill("modified")) }));
  assert.equal(h.text, "13 of your changes are ready to push.");
});
