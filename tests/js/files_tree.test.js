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

// -- tree(): the recursive, nesting folder view ------------------------------

test("tree: repo root splits into top-level folder nodes + loose root files", () => {
  const t = FT.tree([f("reports/a.py"), f("data/b.csv"), f("mooring.toml")], [], "");
  assert.deepEqual(t.folders.map((n) => n.name).sort(), ["data", "reports"]);
  assert.deepEqual(t.files.map((x) => x.rel), ["mooring.toml"]);
});

test("tree: deep sub-folders NEST as their own nodes instead of flattening", () => {
  const t = FT.tree([f("reports/2026/q3/x.py"), f("reports/2026/q1/y.py"), f("reports/2025/z.py")], [], "");
  const reports = t.folders.find((n) => n.name === "reports");
  assert.equal(reports.count, 3); // subtree total, shown on the collapsed header
  assert.equal(reports.files.length, 0); // no files directly in reports/
  assert.deepEqual(reports.children.map((n) => n.name), ["2025", "2026"]); // sorted
  const y2026 = reports.children.find((n) => n.name === "2026");
  assert.deepEqual(y2026.children.map((n) => n.name), ["q1", "q3"]);
  assert.equal(y2026.children.find((n) => n.name === "q3").files[0].rel, "x.py");
});

test("tree: files directly in a folder attach to that node (rel = basename)", () => {
  const t = FT.tree([f("reports/overview.py"), f("reports/2026/x.py")], [], "");
  const reports = t.folders.find((n) => n.name === "reports");
  assert.deepEqual(reports.files.map((x) => x.rel), ["overview.py"]);
  assert.equal(reports.count, 2);
});

test("tree: focus re-roots — returns the subtree below the focused folder", () => {
  const t = FT.tree([f("reports/2026/q1/x.py"), f("analysis/z.py")], [], "reports/2026");
  assert.deepEqual(t.folders.map((n) => n.name), ["q1"]);
  assert.equal(t.folders[0].path, "reports/2026/q1"); // absolute path preserved
});

test("tree: an empty declared LEAF is `empty`; an intermediate on the chain is not", () => {
  const t = FT.tree([], ["packages/finance/notebooks"], "");
  const packages = t.folders.find((n) => n.name === "packages");
  assert.equal(packages.empty, false); // has children — keep a live caret to drill in
  const leaf = packages.children[0].children[0];
  assert.equal(leaf.name, "notebooks");
  assert.equal(leaf.empty, true); // nothing beneath — the "here's where notebooks go" nub
});

test("tree: does not mutate the input files", () => {
  const input = [f("reports/2026/x.py")];
  FT.tree(input, [], "");
  assert.equal("rel" in input[0], false);
});

// -- allFolderPaths(): the Expand/Collapse-all target set --------------------

test("allFolderPaths: every node path, depth-first", () => {
  const t = FT.tree([f("reports/2026/x.py"), f("data/y.csv")], [], "");
  assert.deepEqual(FT.allFolderPaths(t).sort(),
    ["data", "reports", "reports/2026"]);
});

test("expandableCount: counts only steerable nodes, not empty declared leaves", () => {
  // Two declared-but-empty siblings — nothing to expand, so the toggles shouldn't show.
  assert.equal(FT.expandableCount(FT.tree([], ["notebooks", "reports"], "")), 0);
  // A real folder + an empty declared sibling → only the real one is steerable.
  assert.equal(FT.expandableCount(FT.tree([f("data/a.csv")], ["notebooks"], "")), 1);
  // Nested folders each count once.
  assert.equal(FT.expandableCount(FT.tree([f("reports/2026/x.py"), f("data/y.csv")], [], "")), 3);
});

// -- partitionFeatured(): repo-curated pin-to-top order ----------------------

test("partitionFeatured: featured folders come first in the curator's order", () => {
  const t = FT.tree([f("a/x.py"), f("b/y.py"), f("c/z.py")], [], "");
  const { featured, rest } = FT.partitionFeatured(t.folders, ["c", "a"]);
  assert.deepEqual(featured.map((n) => n.path), ["c", "a"]); // declared order, not sorted
  assert.deepEqual(rest.map((n) => n.path), ["b"]);
});

test("partitionFeatured: empty featured leaves everything in rest", () => {
  const t = FT.tree([f("a/x.py"), f("b/y.py")], [], "");
  const { featured, rest } = FT.partitionFeatured(t.folders, []);
  assert.equal(featured.length, 0);
  assert.deepEqual(rest.map((n) => n.path), ["a", "b"]);
});

test("partitionFeatured: unknown / duplicate featured paths are skipped", () => {
  const t = FT.tree([f("a/x.py"), f("b/y.py")], [], "");
  const { featured, rest } = FT.partitionFeatured(t.folders, ["a", "gone", "a"]);
  assert.deepEqual(featured.map((n) => n.path), ["a"]); // "gone" dropped, "a" not doubled
  assert.deepEqual(rest.map((n) => n.path), ["b"]);
});

// -- crowdedCount(): the default-collapse heuristic --------------------------

test("crowdedCount: a small/flat repo is not crowded (stays expanded)", () => {
  assert.equal(FT.crowdedCount(2, 5), false);
});

test("crowdedCount: a lone folder never auto-collapses, even with many files", () => {
  assert.equal(FT.crowdedCount(1, 40), false);
});

test("crowdedCount: many folders or a long listing collapse by default", () => {
  assert.equal(FT.crowdedCount(4, 8), true); // >= 4 folders
  assert.equal(FT.crowdedCount(2, 25), true); // > 20 files
  assert.equal(FT.crowdedCount(3, 12), false); // neither threshold
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
