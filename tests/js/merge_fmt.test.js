"use strict";

// Unit tests for the cell-level conflict merge panel's pure helpers
// (merge_fmt.js), plus the pins that matter for a destructive-looking panel:
// no side is ever preselected, the write stays gated until every contested
// cell has an answer, and untrusted cell diffs reach the DOM via textContent.
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");
const MF = require(path.join(STATIC, "merge_fmt.js"));

const auto = (over) =>
  Object.assign(
    { id: "b0", origin: "base", status: "auto", side: "local", index_base: 0, dropped: false },
    over,
  );
const choice = (over) =>
  Object.assign(
    {
      id: "b1",
      origin: "base",
      status: "choice",
      side: "",
      index_base: 1,
      dropped: false,
      has_local: true,
      has_remote: true,
      diff: "-mine\n+theirs",
    },
    over,
  );

test("cellLabel names the winning side for every auto outcome", () => {
  assert.equal(MF.cellLabel(auto({ side: "unchanged" })), "Cell 1 — unchanged");
  assert.equal(MF.cellLabel(auto({ side: "local" })), "Cell 1 — your change, merged");
  assert.equal(MF.cellLabel(auto({ side: "remote" })), "Cell 1 — the team's change, merged");
  assert.equal(
    MF.cellLabel(auto({ side: "both" })),
    "Cell 1 — the same change on both sides, merged",
  );
});

test("cellLabel says plainly when a cell is dropped, and by whom", () => {
  assert.equal(MF.cellLabel(auto({ side: "local", dropped: true })), "Cell 1 — you deleted it");
  assert.equal(
    MF.cellLabel(auto({ side: "remote", dropped: true })),
    "Cell 1 — the team deleted it",
  );
  assert.equal(
    MF.cellLabel(auto({ side: "both", dropped: true })),
    "Cell 1 — you both deleted it",
  );
});

test("a cell neither side inherited is numbered separately from the shared cells", () => {
  const added = { id: "l3", origin: "local", status: "auto", side: "local", index_base: null };
  assert.equal(MF.cellLabel(added, 2), "New cell 2 — added by you, merged");
  assert.equal(
    MF.cellLabel({ ...added, origin: "remote", side: "remote" }, 1),
    "New cell 1 — added by the team, merged",
  );
  assert.equal(
    MF.cellLabel({ ...added, origin: "both", side: "both" }, 1),
    "New cell 1 — you both added it, merged",
  );
});

test("a contested cell always asks — base and added alike", () => {
  assert.equal(MF.cellLabel(choice()), "Cell 2 — you both changed it · choose one");
  assert.equal(
    MF.cellLabel(choice({ origin: "both", index_base: null }), 1),
    "New cell 1 — you both added a cell here · choose one",
  );
});

test("a side that deleted the cell offers Drop, not an empty version", () => {
  assert.deepEqual(MF.choiceOptions(choice()), [
    { value: "local", label: "Keep my version" },
    { value: "remote", label: "Take the team's version" },
  ]);
  assert.equal(
    MF.choiceOptions(choice({ has_local: false }))[0].label,
    "Drop the cell (you deleted it)",
  );
  assert.equal(
    MF.choiceOptions(choice({ has_remote: false }))[1].label,
    "Drop the cell (the team deleted it)",
  );
});

test("buildBlocks collapses settled cells and carries the diff + options for the rest", () => {
  const blocks = MF.buildBlocks({ cells: [auto({ side: "unchanged" }), choice()] });
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].collapsed, true);
  assert.deepEqual(blocks[0].options, []);
  assert.equal(blocks[0].diff, "");
  assert.equal(blocks[1].collapsed, false);
  assert.equal(blocks[1].diff, "-mine\n+theirs");
  assert.equal(blocks[1].options.length, 2);
  assert.deepEqual(MF.buildBlocks(undefined), []);
});

test("new-cell numbering runs independently of the shared cells' positions", () => {
  const blocks = MF.buildBlocks({
    cells: [
      auto({ id: "b0", side: "unchanged" }),
      { id: "l1", origin: "local", status: "auto", side: "local", index_base: null },
      auto({ id: "b1", index_base: 1, side: "remote" }),
      { id: "r4", origin: "remote", status: "auto", side: "remote", index_base: null },
    ],
  });
  assert.deepEqual(
    blocks.map((b) => b.label),
    [
      "Cell 1 — unchanged",
      "New cell 1 — added by you, merged",
      "Cell 2 — the team's change, merged",
      "New cell 2 — added by the team, merged",
    ],
  );
});

test("no side is preselected: the write is gated until every choice is made", () => {
  const plan = { cells: [auto({ side: "unchanged" }), choice({ id: "b1" }), choice({ id: "b2" })] };
  assert.deepEqual(MF.conflictIds(plan), ["b1", "b2"]);
  assert.deepEqual(MF.unresolved(plan, {}), ["b1", "b2"]);
  assert.equal(MF.ready(plan, {}), false);
  assert.deepEqual(MF.unresolved(plan, { b1: "local" }), ["b2"]);
  assert.equal(MF.ready(plan, { b1: "local" }), false);
  assert.equal(MF.ready(plan, { b1: "local", b2: "remote" }), true);
  // A bogus value is not a decision.
  assert.deepEqual(MF.unresolved(plan, { b1: "local", b2: "whatever" }), ["b2"]);
  assert.equal(MF.ready(null, {}), false);
});

test("a plan with nothing contested is ready immediately", () => {
  const plan = { cells: [auto({ side: "local" }), auto({ id: "b1", side: "remote" })] };
  assert.deepEqual(MF.unresolved(plan, {}), []);
  assert.equal(MF.ready(plan, {}), true);
});

test("summary leads with the work the analyst did not have to do", () => {
  const plan = {
    auto_merged: 4,
    auto_local: 3,
    auto_remote: 1,
    auto_both: 0,
    unchanged: 10,
    cells: [choice({ id: "b1" }), choice({ id: "b2" })],
  };
  assert.equal(
    MF.summary(plan),
    "4 cells merged automatically (3 yours · 1 the team's) · 10 unchanged · 2 need your choice",
  );
  assert.equal(
    MF.summary({ auto_merged: 1, auto_local: 1, unchanged: 0, cells: [choice()] }),
    "1 cell merged automatically (1 yours) · 1 needs your choice",
  );
  assert.equal(
    MF.summary({ auto_merged: 2, auto_both: 2, unchanged: 3, cells: [] }),
    "2 cells merged automatically (2 identical on both sides) · 3 unchanged · nothing left to choose",
  );
  assert.equal(MF.summary(null), "");
});

test("merge_fmt.js emits strings only — no innerHTML, no editable regions", () => {
  const src = fs.readFileSync(path.join(STATIC, "merge_fmt.js"), "utf8");
  assert.ok(!src.includes("innerHTML"), "merge_fmt must never touch innerHTML");
  assert.ok(!/contenteditable/i.test(src), "merge_fmt must never emit editable regions");
});

test("app.js renderMerge writes untrusted cell diffs via textContent only", () => {
  const appSrc = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  const start = appSrc.indexOf("function renderMerge");
  assert.ok(start > -1, "renderMerge must exist in app.js");
  const end = appSrc.indexOf("\nfunction ", start + 1);
  const body = appSrc.slice(start, end === -1 ? undefined : end);
  assert.ok(!body.includes("innerHTML"), "the renderer must use textContent, never innerHTML");
  assert.ok(!/contenteditable/i.test(body), "cell source is never editable here");
});

test("the merge write posts the plan's three shas so a moved side is caught", () => {
  // The staleness key is the whole reason apply() can trust decisions alone —
  // dropping it client-side would silently merge against a file nobody saw.
  const appSrc = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  const start = appSrc.indexOf("function mergeApply");
  assert.ok(start > -1, "mergeApply must exist in app.js");
  const body = appSrc.slice(start, appSrc.indexOf("\nfunction ", start + 1));
  for (const key of ["base_sha", "local_sha", "remote_sha"]) {
    assert.ok(body.includes(key), `mergeApply must send ${key}`);
  }
});

test("merge_fmt.js exposes both the bare global and window.MergeFmt (browser + Node)", () => {
  const src = fs.readFileSync(path.join(STATIC, "merge_fmt.js"), "utf8");
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(src + "\n;window.__bare = MergeFmt;", sandbox);
  assert.equal(typeof sandbox.window.MergeFmt, "object");
  assert.equal(sandbox.window.__bare, sandbox.window.MergeFmt);
  for (const fn of ["cellLabel", "buildBlocks", "summary", "ready", "unresolved"]) {
    assert.equal(typeof sandbox.window.MergeFmt[fn], "function", `window.MergeFmt.${fn}`);
  }
});

test("index.html loads merge_fmt.js before app.js (the bare-global contract)", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const fmt = html.indexOf("/static/merge_fmt.js");
  const app = html.indexOf("/static/app.js");
  assert.ok(fmt > -1, "index.html must load merge_fmt.js");
  assert.ok(fmt < app, "merge_fmt.js must load before app.js");
});
