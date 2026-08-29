"use strict";

// Unit tests for the apply gate's pure frontend helpers (ChatCore.gate*).
//
// The gate holds an Apply that Undo could not put right (HTTP 428 + a `gate`
// payload). These helpers decide EVERY analyst-facing string on that hold card, so
// the wording is pinned here rather than left to a DOM review: the difference
// between "floor" and "ask" is wording and emphasis, and the sentence that breaks
// the Undo belief is the whole point of the feature.
//
// Zero deps: Node's built-in runner + assert. Run with: node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../src/mooring/hub/static/chat_core.js");

const FLOOR = {
  band: "floor",
  token: "a1b2c3d4e5f60718",
  findings: [
    { line: 4, kind: "deletes_files", label: "Deletes files or folders" },
    { line: 9, kind: "destroys_rows", label: "Deletes every row from a table" },
  ],
};
const ASK = {
  band: "ask",
  token: "0011223344556677",
  findings: [{ line: 2, kind: "overwrites_file", label: "Overwrites a file that already exists" }],
};

// -- gateFromResponse: normalise a 428 body, fail closed ---------------------

test("gateFromResponse reads a well-formed gate payload", () => {
  const g = C.gateFromResponse({ gate: FLOOR });
  assert.equal(g.band, "floor");
  assert.equal(g.token, "a1b2c3d4e5f60718");
  assert.equal(g.findings.length, 2);
  assert.equal(C.gateFromResponse({ gate: ASK }).band, "ask");
});

test("gateFromResponse returns null when there is no gate to read", () => {
  assert.equal(C.gateFromResponse({}), null);
  assert.equal(C.gateFromResponse(null), null);
  assert.equal(C.gateFromResponse(undefined), null);
  assert.equal(C.gateFromResponse({ gate: null }), null);
  assert.equal(C.gateFromResponse({ gate: "floor" }), null);
  assert.equal(C.gateFromResponse({ error: "Request failed (428)" }), null);
});

test("gateFromResponse fails CLOSED: only an explicit 'ask' is treated as ask", () => {
  // A typo, a missing band, or a band this client doesn't know must over-warn,
  // never under-warn — "clean" never reaches the wire, so seeing it is a bug.
  for (const band of [undefined, null, "", "clean", "Ask", "ASK", "warn", 7, {}]) {
    assert.equal(C.gateFromResponse({ gate: { band } }).band, "floor", String(band));
  }
});

test("gateFromResponse coerces a malformed token/findings without throwing", () => {
  const g = C.gateFromResponse({ gate: { band: "ask", token: 42, findings: "nope" } });
  assert.equal(g.token, "");
  assert.deepEqual(g.findings, []);
});

// -- gateIsFloor -------------------------------------------------------------

test("gateIsFloor is true for everything except an explicit ask", () => {
  assert.equal(C.gateIsFloor(FLOOR), true);
  assert.equal(C.gateIsFloor(ASK), false);
  assert.equal(C.gateIsFloor({}), true);
  assert.equal(C.gateIsFloor(null), true);
  assert.equal(C.gateIsFloor(undefined), true);
});

// -- gateFindingRows: the analyst's language, never the slug -----------------

test("gateFindingRows renders one line per finding, label only", () => {
  assert.deepEqual(C.gateFindingRows(FLOOR), [
    "line 4: Deletes files or folders",
    "line 9: Deletes every row from a table",
  ]);
});

test("gateFindingRows handles a single finding", () => {
  assert.deepEqual(C.gateFindingRows(ASK), [
    "line 2: Overwrites a file that already exists",
  ]);
});

test("gateFindingRows NEVER shows the kind slug or any code", () => {
  const rows = C.gateFindingRows(FLOOR).concat(C.gateFindingRows(ASK));
  for (const row of rows) {
    for (const slug of ["deletes_files", "destroys_rows", "overwrites_file"]) {
      assert.equal(row.includes(slug), false, row);
    }
    assert.equal(row.includes("_"), false, row); // no python_case leaks at all
  }
});

test("gateFindingRows drops a labelless finding rather than falling back to the slug", () => {
  const gate = {
    band: "floor",
    findings: [
      { line: 1, kind: "runs_program" },
      { line: 1, kind: "runs_program", label: "   " },
      { line: 3, kind: "dynamic_code", label: "Runs code built while it runs" },
    ],
  };
  assert.deepEqual(C.gateFindingRows(gate), ["line 3: Runs code built while it runs"]);
});

test("gateFindingRows omits a missing or nonsensical line number", () => {
  const gate = {
    band: "ask",
    findings: [
      { kind: "sends_data", label: "Sends data to another system" },
      { line: 0, kind: "installs_package", label: "Installs a package" },
      { line: -2, kind: "replaces_notebook", label: "Replaces the whole notebook" },
      { line: "4", kind: "unparseable", label: "This cell isn't valid Python" },
    ],
  };
  assert.deepEqual(C.gateFindingRows(gate), [
    "Sends data to another system",
    "Installs a package",
    "Replaces the whole notebook",
    "line 4: This cell isn't valid Python", // a numeric string is still a line
  ]);
});

test("gateFindingRows de-duplicates identical rows", () => {
  const dup = { line: 4, kind: "deletes_files", label: "Deletes files or folders" };
  assert.deepEqual(C.gateFindingRows({ band: "floor", findings: [dup, dup, dup] }), [
    "line 4: Deletes files or folders",
  ]);
});

test("gateFindingRows survives an empty or malformed payload", () => {
  assert.deepEqual(C.gateFindingRows({ band: "floor", findings: [] }), []);
  assert.deepEqual(C.gateFindingRows({ band: "floor" }), []);
  assert.deepEqual(C.gateFindingRows({ band: "floor", findings: "nope" }), []);
  assert.deepEqual(C.gateFindingRows(null), []);
  assert.deepEqual(C.gateFindingRows(undefined), []);
  assert.deepEqual(C.gateFindingRows({ findings: [null, 3, "x", {}] }), []);
});

// -- gateFindingItems: the optional per-finding band -------------------------

test("gateFindingItems marks the irreversible lines in a MIXED verdict", () => {
  const items = C.gateFindingItems({
    band: "floor",
    findings: [
      { line: 2, kind: "overwrites_file", label: "Overwrites a file", band: "ask" },
      { line: 6, kind: "deletes_files", label: "Deletes files or folders", band: "floor" },
    ],
  });
  assert.deepEqual(items, [
    { text: "line 2: Overwrites a file", floor: false, mark: "" },
    { text: "line 6: Deletes files or folders", floor: true, mark: "can't be undone" },
  ]);
});

test("gateFindingItems does NOT mark when every finding is the same band", () => {
  // The card's header already says the verdict; repeating it per row is noise.
  const allFloor = C.gateFindingItems({
    band: "floor",
    findings: [
      { line: 1, label: "Deletes files or folders", band: "floor" },
      { line: 4, label: "Runs another program on your machine", band: "floor" },
    ],
  });
  assert.deepEqual(allFloor.map((i) => i.mark), ["", ""]);
  assert.deepEqual(allFloor.map((i) => i.floor), [true, true]);
  const allAsk = C.gateFindingItems({
    band: "ask",
    findings: [{ line: 1, label: "Overwrites a file", band: "ask" }],
  });
  assert.deepEqual(allAsk.map((i) => i.mark), [""]);
});

test("gateFindingItems degrades gracefully when the wire omits per-finding band", () => {
  // The two changes land concurrently; an older server sends no per-finding band, and
  // an absent mark is the right failure (a wrong mark would mislead).
  const items = C.gateFindingItems(FLOOR);
  assert.deepEqual(items.map((i) => i.mark), ["", ""]);
  assert.deepEqual(items.map((i) => i.floor), [false, false]);
  assert.deepEqual(C.gateFindingItems(null), []);
  assert.deepEqual(C.gateFindingItems({ findings: "nope" }), []);
});

test("gateFindingItems ignores a per-finding band it doesn't recognise", () => {
  const items = C.gateFindingItems({
    band: "floor",
    findings: [
      { line: 1, label: "A", band: "clean" },
      { line: 2, label: "B", band: "FLOOR" },
      { line: 3, label: "C", band: 7 },
    ],
  });
  assert.deepEqual(items.map((i) => i.mark), ["", "", ""]);
});

test("gateFindingRows stays the plain-text view of gateFindingItems", () => {
  for (const gate of [FLOOR, ASK, null, { band: "floor", findings: [] }]) {
    assert.deepEqual(C.gateFindingRows(gate), C.gateFindingItems(gate).map((i) => i.text));
  }
});

test("gateHoldWording carries items and rows in step", () => {
  const w = C.gateHoldWording({
    band: "floor",
    findings: [
      { line: 2, label: "Overwrites a file", band: "ask" },
      { line: 6, label: "Deletes files or folders", band: "floor" },
    ],
  });
  assert.deepEqual(w.rows, w.items.map((i) => i.text));
  assert.deepEqual(w.items.map((i) => i.mark), ["", "can't be undone"]);
});

// -- gateHoldSummary ---------------------------------------------------------

test("gateHoldSummary says nothing was applied, in the band's words", () => {
  assert.equal(
    C.gateHoldSummary(FLOOR),
    "Held before applying — this change can't be taken back.",
  );
  assert.equal(
    C.gateHoldSummary(ASK),
    "Held before applying — this change does more than work out an answer.",
  );
  // a malformed payload gets the heavier wording
  assert.equal(C.gateHoldSummary(null), C.gateHoldSummary(FLOOR));
});

// -- gateHoldWording: the whole card ----------------------------------------

test("gateHoldWording (floor) breaks the Undo belief in plain English", () => {
  const w = C.gateHoldWording(FLOOR);
  assert.equal(w.band, "floor");
  assert.equal(w.floor, true);
  assert.equal(w.summary, "Held before applying — this change can't be taken back.");
  assert.equal(
    w.mechanism,
    "Nothing has changed yet. Applying writes the change into the notebook, and " +
      "marimo runs it straight away.",
  );
  assert.equal(w.lead, "What it would do, permanently:");
  assert.deepEqual(w.rows, [
    "line 4: Deletes files or folders",
    "line 9: Deletes every row from a table",
  ]);
  assert.equal(
    w.undoNote,
    "Undo puts the notebook back. It can't put back a deleted file or a dropped " +
      "table. Once this runs, that part is permanent.",
  );
  assert.equal(w.confirmLabel, "Run it anyway");
  assert.equal(w.cancelLabel, "Don't apply");
});

test("gateHoldWording (ask) asks once, with lighter emphasis", () => {
  const w = C.gateHoldWording(ASK);
  assert.equal(w.band, "ask");
  assert.equal(w.floor, false);
  assert.equal(
    w.summary,
    "Held before applying — this change does more than work out an answer.",
  );
  assert.equal(w.lead, "What it would do:");
  assert.deepEqual(w.rows, ["line 2: Overwrites a file that already exists"]);
  assert.equal(
    w.undoNote,
    "Undo puts the notebook back. It can't take back anything this writes or " +
      "sends elsewhere.",
  );
  assert.equal(w.confirmLabel, "Apply anyway");
  assert.equal(w.cancelLabel, "Don't apply");
});

test("both bands are HELD: each carries a confirm label and an Undo sentence", () => {
  for (const gate of [FLOOR, ASK]) {
    const w = C.gateHoldWording(gate);
    assert.ok(w.confirmLabel.length, "a confirm exists in both bands");
    assert.ok(w.cancelLabel.length, "a decline exists in both bands");
    assert.ok(w.undoNote.startsWith("Undo puts the notebook back."), w.undoNote);
    assert.ok(w.mechanism.includes("Nothing has changed yet"), w.mechanism);
  }
});

test("floor and ask differ in wording, not in whether a confirm exists", () => {
  const f = C.gateHoldWording(FLOOR);
  const a = C.gateHoldWording(ASK);
  assert.notEqual(f.summary, a.summary);
  assert.notEqual(f.undoNote, a.undoNote);
  assert.notEqual(f.confirmLabel, a.confirmLabel);
  assert.equal(f.cancelLabel, a.cancelLabel);
  assert.equal(f.mechanism, a.mechanism);
});

test("gateHoldWording drops the lead-in when there is nothing to list", () => {
  for (const gate of [{ band: "floor", findings: [] }, { band: "ask" }, null, {}]) {
    const w = C.gateHoldWording(gate);
    assert.equal(w.lead, "", "no dangling colon above an empty list");
    assert.deepEqual(w.rows, []);
    assert.ok(w.summary.length && w.undoNote.length && w.confirmLabel.length);
  }
});

test("gateHoldWording fails closed on a malformed payload", () => {
  const w = C.gateHoldWording({ band: "clean", findings: "nope", token: null });
  assert.equal(w.band, "floor");
  assert.equal(w.floor, true);
  assert.equal(w.confirmLabel, "Run it anyway");
  assert.deepEqual(w.rows, []);
});

// -- the reviewer's list (hub reviews page) ----------------------------------
// Same wire shape, same rows — but informational, not a hold. The reviewer is the
// one person in the loop who reads Python, so they see BOTH bands.

const REVIEW_CODE = {
  band: "floor",
  findings: [
    { line: 2, kind: "overwrites_file", label: "Overwrites a file that already exists", band: "ask" },
    { line: 6, kind: "deletes_files", label: "Deletes files or folders", band: "floor" },
  ],
};

test("codeFindingRows shows BOTH bands, each carrying its own", () => {
  assert.deepEqual(C.codeFindingRows(REVIEW_CODE), [
    { text: "line 2: Overwrites a file that already exists", band: "ask", floor: false },
    { text: "line 6: Deletes files or folders", band: "floor", floor: true },
  ]);
});

test("codeFindingRows drops the hold's mixed-verdict mark", () => {
  // The hold marks the irreversible rows because it styles them all alike; this list
  // colours every row by band already, so a mark would only repeat itself.
  const rows = C.codeFindingRows(REVIEW_CODE);
  for (const row of rows) {
    assert.equal("mark" in row, false, "no mark field reaches the reviewer's row");
    assert.equal(row.text.includes("can't be undone"), false, row.text);
  }
});

test("codeFindingRows reads a bandless finding as the quieter band", () => {
  // Informational, not a gate: nothing is being stopped, so crying wolf on a row the
  // reviewer can read for themselves costs more than it buys.
  assert.deepEqual(C.codeFindingRows({ band: "floor", findings: [{ line: 1, label: "A" }] }), [
    { text: "line 1: A", band: "ask", floor: false },
  ]);
});

test("codeFindingRows inherits the value-free rules from the gate derivation", () => {
  const rows = C.codeFindingRows({
    band: "ask",
    findings: [
      { line: 3, kind: "sends_data" }, // no label — dropped, never shown as its slug
      { line: 4, kind: "sends_data", label: "Sends data to another system", band: "ask" },
      { line: 4, kind: "sends_data", label: "Sends data to another system", band: "ask" },
    ],
  });
  assert.deepEqual(rows.map((r) => r.text), ["line 4: Sends data to another system"]);
  assert.equal(rows[0].text.includes("sends_data"), false);
});

test("codeFindingRows survives an absent or malformed code block", () => {
  assert.deepEqual(C.codeFindingRows(undefined), []);
  assert.deepEqual(C.codeFindingRows(null), []);
  assert.deepEqual(C.codeFindingRows({}), []);
  assert.deepEqual(C.codeFindingRows({ findings: "nope" }), []);
  assert.deepEqual(C.codeFindingRows({ findings: [null, 1, "x"] }), []);
});

test("a file entry with NO `code` key renders nothing at all", () => {
  // The scan landed after the reviews page shipped, so `{path, status, diff}` with no
  // `code` key is a real payload — an older server, or a cached response. The renderer
  // skips on an empty row list, so both halves must come back empty: no block, no
  // empty box, no lead line with nothing under it.
  const file = { path: "notebooks/old.py", status: "modified", diff: { kind: "line" } };
  assert.equal(file.code, undefined, "the shape under test really has no code key");
  assert.deepEqual(C.codeFindingRows(file.code), []);
  assert.equal(C.codeFindingLead(file.code), "");
  // and the same for a present-but-empty scan (a clean file)
  assert.deepEqual(C.codeFindingRows({ band: "clean", findings: [] }), []);
  assert.equal(C.codeFindingLead({ band: "clean", findings: [] }), "");
});

test("codeFindingLead names the scan and counts the findings", () => {
  assert.equal(C.codeFindingLead(REVIEW_CODE), "Destructive-code scan — 2 findings:");
  assert.equal(
    C.codeFindingLead({ band: "ask", findings: [{ line: 1, label: "A", band: "ask" }] }),
    "Destructive-code scan — 1 finding:",
  );
  // nothing found: no lead, so the caller renders no block at all
  assert.equal(C.codeFindingLead({ band: "clean", findings: [] }), "");
  assert.equal(C.codeFindingLead(null), "");
});

test("codeFindingTag names the band, never the code", () => {
  const [ask, floor] = C.codeFindingRows(REVIEW_CODE);
  assert.equal(C.codeFindingTag(floor), "can't be undone");
  assert.equal(C.codeFindingTag(ask), "side effect");
  assert.equal(C.codeFindingTag(null), "side effect"); // quieter default, as above
});

test("the reviewer's list carries NO hold wording — it is not a gate", () => {
  const text = [C.codeFindingLead(REVIEW_CODE)]
    .concat(C.codeFindingRows(REVIEW_CODE).map((r) => C.codeFindingTag(r) + " " + r.text))
    .join(" ");
  for (const held of ["Held before applying", "Undo puts the notebook back", "anyway", "Don't apply"]) {
    assert.equal(text.includes(held), false, held);
  }
});

test("no reviewer string leaks a kind slug, a path, or Python", () => {
  const text = [C.codeFindingLead(REVIEW_CODE)]
    .concat(C.codeFindingRows(REVIEW_CODE).map((r) => C.codeFindingTag(r) + " " + r.text))
    .join(" ");
  for (const bad of ["overwrites_file", "deletes_files", "shutil", "os.remove", ".py"]) {
    assert.equal(text.includes(bad), false, bad);
  }
});

test("no card string leaks a kind slug, a path, or Python", () => {
  for (const gate of [FLOOR, ASK, null, {}]) {
    const w = C.gateHoldWording(gate);
    const all = [w.summary, w.mechanism, w.lead, w.undoNote, w.confirmLabel, w.cancelLabel]
      .concat(w.rows)
      .join(" ");
    for (const bad of ["shutil", "os.remove", "DROP TABLE", "subprocess", "_files", ".py"]) {
      assert.equal(all.includes(bad), false, bad);
    }
  }
});
