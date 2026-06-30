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
