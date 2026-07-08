"use strict";

// Unit tests for the hub's pure folder-grouping helper (files_tree.js). Zero deps:
// Node's built-in runner + assert. Run with: node --test tests/js/
// Covers the grouping contract the hub relies on: declared folders always shown
// (incl. empty ones), longest-prefix matching, relative display paths, and root files.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const FT = require("../../src/mooring/hub/static/files_tree.js");

const f = (path, extra) => Object.assign({ path, state: "local" }, extra || {});

test("folderOf: longest declared prefix wins", () => {
  const decl = ["notebooks", "packages/finance/notebooks"];
  assert.equal(FT.folderOf("notebooks/sub/a.py", decl), "notebooks");
  assert.equal(FT.folderOf("packages/finance/notebooks/sales.py", decl), "packages/finance/notebooks");
});

test("folderOf: outside scope falls back to top-level segment, then root", () => {
  assert.equal(FT.folderOf("analysis/q1.py", []), "analysis");
  assert.equal(FT.folderOf("mooring.toml", []), "");
});

test("group: declared folders always appear, empty ones flagged", () => {
  const sections = FT.group([f("notebooks/a.py")], ["notebooks", "data", "reports"]);
  const byFolder = Object.fromEntries(sections.map((s) => [s.folder, s]));
  assert.deepEqual(Object.keys(byFolder).sort(), ["data", "notebooks", "reports"]);
  assert.equal(byFolder["notebooks"].empty, false);
  assert.equal(byFolder["data"].empty, true);
  assert.equal(byFolder["reports"].empty, true);
});

test("group: files carry a folder-relative `rel` for compact display", () => {
  const sections = FT.group([f("notebooks/sub/deep.py")], ["notebooks"]);
  assert.equal(sections[0].files[0].rel, "sub/deep.py");
  assert.equal(sections[0].files[0].path, "notebooks/sub/deep.py"); // original preserved
});

test("group: a folder outside the declared scope still gets its own section", () => {
  const sections = FT.group([f("analysis/q1.py")], ["notebooks"]);
  const folders = sections.map((s) => s.folder);
  assert.ok(folders.includes("analysis"));
  assert.ok(folders.includes("notebooks")); // declared, empty
});

test("group: loose root-level files go in a trailing root section", () => {
  const sections = FT.group([f("mooring.toml"), f("notebooks/a.py")], ["notebooks"]);
  const last = sections[sections.length - 1];
  assert.equal(last.folder, "");
  assert.equal(last.files[0].rel, "mooring.toml");
});

test("group: sections are sorted, root always last", () => {
  const sections = FT.group(
    [f("reports/r.py"), f("data/d.csv"), f("zeta.txt")],
    ["notebooks", "reports", "data"],
  );
  const folders = sections.map((s) => s.folder);
  assert.deepEqual(folders, ["data", "notebooks", "reports", ""]);
});

test("group: a file named exactly like a declared folder keeps a visible name", () => {
  // A loose file literally named "notebooks" (no extension) must not slice to "".
  const sections = FT.group([f("notebooks")], ["notebooks"]);
  const all = sections.flatMap((s) => s.files);
  assert.equal(all[0].rel, "notebooks");
});

test("group: does not mutate the input files", () => {
  const input = [f("notebooks/a.py")];
  FT.group(input, ["notebooks"]);
  assert.equal("rel" in input[0], false);
});

// -- matches(): the catalog search filter -----------------------------------

test("matches: empty query matches everything", () => {
  assert.equal(FT.matches({ path: "notebooks/a.py" }, ""), true);
  assert.equal(FT.matches({ path: "notebooks/a.py" }, "   "), true);
});

test("matches: searches path, title, and tags, case-insensitively", () => {
  const f = { path: "notebooks/q3_recon_v2.py", title: "Quarterly Reconciliation", tags: ["finance"] };
  assert.equal(FT.matches(f, "recon"), true); // path
  assert.equal(FT.matches(f, "quarterly"), true); // title
  assert.equal(FT.matches(f, "FINANCE"), true); // tag, case-insensitive
  assert.equal(FT.matches(f, "sales"), false);
});

test("matches: space-separated terms are ANDed", () => {
  const f = { path: "notebooks/sales.py", title: "Monthly Sales Report" };
  assert.equal(FT.matches(f, "sales report"), true);
  assert.equal(FT.matches(f, "sales quarterly"), false); // one term misses
});

test("matches: missing title/tags don't throw", () => {
  assert.equal(FT.matches({ path: "a.py" }, "a"), true);
  assert.equal(FT.matches({ path: "a.py", tags: null }, "a"), true);
});

// -- scope(): the "focus one folder" filter ---------------------------------

test("scope: empty focus returns every file (a copy, not the input)", () => {
  const input = [f("a.py"), f("b/c.py")];
  const out = FT.scope(input, "");
  assert.deepEqual(out.map((x) => x.path), ["a.py", "b/c.py"]);
  assert.notEqual(out, input); // a fresh array
});

test("scope: keeps only files at or under the focus", () => {
  const files = [f("reports/2026/q3/x.py"), f("reports/y.py"), f("analysis/z.py")];
  assert.deepEqual(FT.scope(files, "reports").map((x) => x.path),
    ["reports/2026/q3/x.py", "reports/y.py"]);
});

test("scope: is slash-bounded — 'report' never captures 'reports/…'", () => {
  const files = [f("reports/a.py"), f("report/b.py"), f("report")];
  assert.deepEqual(FT.scope(files, "report").map((x) => x.path), ["report/b.py", "report"]);
});

// -- crumbs(): the breadcrumb trail -----------------------------------------

test("crumbs: builds a cumulative-prefix trail, empty for the root", () => {
  assert.deepEqual(FT.crumbs(""), []);
  assert.deepEqual(FT.crumbs("reports/2026"), [
    { label: "reports", prefix: "reports" },
    { label: "2026", prefix: "reports/2026" },
  ]);
});

// -- subsections(): the re-rooted, one-level-below-focus view ----------------

test("subsections: focus '' delegates to group() unchanged", () => {
  const files = [f("notebooks/a.py")];
  assert.deepEqual(FT.subsections(files, ["notebooks"], ""), FT.group(files, ["notebooks"]));
});

test("subsections: deep paths group by the segment BELOW the focus, not flattened", () => {
  const files = [f("reports/2026/q3/x.py"), f("reports/2026/q1/y.py"), f("reports/2025/z.py")];
  const secs = FT.subsections(files, [], "reports");
  const byFolder = Object.fromEntries(secs.map((s) => [s.folder, s]));
  assert.deepEqual(Object.keys(byFolder).sort(), ["reports/2025", "reports/2026"]);
  assert.equal(byFolder["reports/2026"].label, "2026");
  assert.equal(byFolder["reports/2026"].files.length, 2);
  // rel is relative to the sub-section, so the row stays compact.
  assert.deepEqual(byFolder["reports/2026"].files.map((x) => x.rel).sort(), ["q1/y.py", "q3/x.py"]);
});

test("subsections: files DIRECTLY in the focus lead in a `here` section", () => {
  const files = [f("reports/summary.py"), f("reports/2026/x.py")];
  const secs = FT.subsections(files, [], "reports");
  assert.equal(secs[0].here, true);
  assert.equal(secs[0].folder, "reports");
  assert.deepEqual(secs[0].files.map((x) => x.rel), ["summary.py"]);
  // A distinct, collision-proof expand key: it shares `folder` with the aggregate
  // "reports" section, so its remembered open/closed bit must not be the same key.
  assert.equal(secs[0].expandKey, "reports/");
});

test("subsections: a declared child folder under the focus seeds an empty section", () => {
  const secs = FT.subsections([], ["reports/2026"], "reports");
  const empty = secs.find((s) => s.folder === "reports/2026");
  assert.ok(empty);
  assert.equal(empty.empty, true);
});

test("subsections: does not mutate the input files", () => {
  const input = [f("reports/2026/x.py")];
  FT.subsections(input, [], "reports");
  assert.equal("rel" in input[0], false);
});

// -- crowded(): the default-collapse heuristic ------------------------------

test("crowded: a small/flat repo is not crowded (stays expanded)", () => {
  const secs = FT.group([f("a/x.py"), f("b/y.py")], []);
  assert.equal(FT.crowded(secs), false);
});

test("crowded: a lone folder never auto-collapses, even with many files", () => {
  const many = Array.from({ length: 40 }, (_, i) => f(`only/n${i}.py`));
  assert.equal(FT.crowded(FT.group(many, [])), false);
});

test("crowded: many folders or a long listing collapse by default", () => {
  const manyFolders = FT.group(
    [f("a/x.py"), f("b/x.py"), f("c/x.py"), f("d/x.py")], [],
  );
  assert.equal(FT.crowded(manyFolders), true); // >= 4 folders
  const manyFiles = FT.group(
    Array.from({ length: 25 }, (_, i) => f(`a/x${i}.py`)).concat(f("b/y.py")), [],
  );
  assert.equal(FT.crowded(manyFiles), true); // > 20 files
});

// -- focusLive(): self-heal a stale focus -----------------------------------

test("focusLive: root focus is always live", () => {
  assert.equal(FT.focusLive([], [], ""), true);
});

test("focusLive: live when a file lives under the focus", () => {
  assert.equal(FT.focusLive([f("reports/a.py")], [], "reports"), true);
});

test("focusLive: live when a declared folder is at/under the focus", () => {
  assert.equal(FT.focusLive([], ["reports/2026"], "reports"), true);
});

test("focusLive: dead when the folder is gone (no files, no declaration)", () => {
  assert.equal(FT.focusLive([f("analysis/a.py")], ["analysis"], "reports"), false);
});
