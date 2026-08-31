"use strict";

// The reasoning-effort picker decides what every request CARRIES — and therefore what
// it costs. It sits beside the model in the chat window and on the batch page. Batch
// remembers its last provider-level choice; interactive chat now treats a saved choice
// as a workspace/notebook override over Settings. These tests pin both decisions, the
// per-provider namespacing, and the one-time adoption of the old batch-era key.
//
// Zero dependencies: Node's built-in test runner + assert. Run with:  node --test tests/js/

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const C = require("../../src/mooring/hub/static/chat_core.js");

const STATIC = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");
// What the OpenAI provider advertises for a reasoning model. "default" is a SENTINEL,
// not an API value: picking it sends no reasoning_effort at all.
const OPENAI = ["default", "none", "low", "medium", "high"];
// The same list after the hub unions in a configured effort the provider never listed
// (see hub/routes/chat.py::_offer_configured_effort). "xhigh" and "minimal" are real
// OpenAI values; a gateway may take its own.
const OPENAI_XHIGH = [...OPENAI, "xhigh"];
// Copilot's list — note it INTERSECTS OpenAI's, which is why one shared storage key
// let a "high" picked under Copilot silently ride along on OpenAI.
const COPILOT = ["low", "medium", "high", "xhigh"];

function fakeStorage(init) {
  const map = new Map(Object.entries(init || {}));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    snapshot: () => Object.fromEntries(map),
  };
}

// -- chooseEffort: every row of the behaviour table ---------------------------
// "wire" is what reaches the API: the chosen word, except "default"/"" which mean the
// request carries no reasoning_effort at all.

test("(a) no stored pick and no configured default -> the send-nothing sentinel", () => {
  // A FRESH user must be byte-for-byte as before the picker existed: no param sent.
  assert.equal(C.chooseEffort(OPENAI, null, "", ""), "default");
});

test("(b) no stored pick, configured high -> high", () => {
  assert.equal(C.chooseEffort(OPENAI, null, "high", ""), "high");
});

test("(c) a stored pick for THIS provider wins over the configured default", () => {
  // Within a provider, the analyst's last explicit pick is the intent.
  assert.equal(C.chooseEffort(OPENAI, "low", "high", ""), "low");
});

test("(c') a pick made under ANOTHER provider does not select here", () => {
  // The bleed that silently raised spend: one shared key, two providers whose effort
  // words overlap. The pick is namespaced, so the OpenAI slot is empty and the
  // configured default stands — this is the pairing of chooseEffort with effortKey.
  const store = fakeStorage({ [C.effortKey("copilot")]: "low" });
  const saved = store.getItem(C.effortKey("openai"));
  assert.equal(saved, null);
  assert.equal(C.chooseEffort(OPENAI, saved, "high", ""), "high");
});

test("(d) a stored pick that is NOT in the list falls through to the configured default", () => {
  // A <select> resolves an unknown value to "", so an unselectable candidate must
  // never be chosen — that empty value is what used to reach the wire.
  assert.equal(C.chooseEffort(OPENAI, "xhigh", "high", ""), "high");
});

test("(e) a configured effort outside the provider's list is still selectable", () => {
  // The hub unions it in, so it stays both visible and selected instead of being
  // silently discarded down to the sentinel.
  assert.equal(C.chooseEffort(OPENAI_XHIGH, null, "xhigh", ""), "xhigh");
  // Without the union it is unselectable and the sentinel is all that's left — the
  // defect this pins the fix for.
  assert.equal(C.chooseEffort(OPENAI, null, "xhigh", ""), "default");
});

test("(f) explicitly picking 'default' beats a configured effort", () => {
  assert.equal(C.chooseEffort(OPENAI, "default", "high", ""), "default");
});

test("the model's own advertised default ranks below the configured one", () => {
  assert.equal(C.chooseEffort(COPILOT, null, "low", "high"), "low");
  assert.equal(C.chooseEffort(COPILOT, null, "", "high"), "high");
});

test("a model default the list doesn't contain is skipped, not selected", () => {
  // Copilot's listing has shipped a default_effort absent from its own efforts list;
  // assigning it would blank the <select> and send nothing.
  assert.equal(C.chooseEffort(COPILOT, null, "", "medium-high"), "low");
});

test("no efforts at all -> no pick (the caller hides the picker and sends none)", () => {
  for (const empty of [[], null, undefined]) {
    assert.equal(C.chooseEffort(empty, "high", "high", "high"), "");
  }
});

test("an empty-string candidate never selects", () => {
  assert.equal(C.chooseEffort(OPENAI, "", "", ""), "default");
});

// -- the stored pick is namespaced per provider -------------------------------

test("effortKey namespaces per provider and never collides", () => {
  assert.notEqual(C.effortKey("openai"), C.effortKey("copilot"));
  assert.match(C.effortKey("openai"), /openai$/);
  // A provider the page couldn't identify gets its OWN slot, not the shared one.
  assert.equal(C.effortKey(""), C.effortKey(null));
  assert.notEqual(C.effortKey(""), C.effortKey("copilot"));
  assert.notEqual(C.effortKey(""), "mooring.ai.effort");
});

test("adoptLegacyEffort hands the old shared key to Copilot only", () => {
  // Copilot is the only provider that could have written it: OpenAI advertised no
  // efforts, so its picker was hidden and no pick could be made.
  const store = fakeStorage({ "mooring.ai.effort": "high" });
  C.adoptLegacyEffort(store);
  assert.equal(store.getItem(C.effortKey("copilot")), "high");
  assert.equal(store.getItem(C.effortKey("openai")), null);
  assert.equal(store.getItem("mooring.ai.effort"), null); // consumed, so it can't bleed later
});

test("adoptLegacyEffort is idempotent and never overwrites a real pick", () => {
  const store = fakeStorage({
    "mooring.ai.effort": "high",
    [C.effortKey("copilot")]: "low",
  });
  C.adoptLegacyEffort(store);
  C.adoptLegacyEffort(store);
  assert.equal(store.getItem(C.effortKey("copilot")), "low");
  assert.deepEqual(store.snapshot(), { [C.effortKey("copilot")]: "low" });
});

test("adoptLegacyEffort with nothing stored does nothing", () => {
  const store = fakeStorage({});
  C.adoptLegacyEffort(store);
  assert.deepEqual(store.snapshot(), {});
  assert.doesNotThrow(() => C.adoptLegacyEffort(null));
});

// -- the pages must actually USE all of that ----------------------------------

const PAGES = ["chat.js", "batch.js"];

function read(name) {
  return fs.readFileSync(path.join(STATIC, name), "utf8");
}

test("neither page reads the un-namespaced effort key directly", () => {
  // The namespacing is only worth anything if both pages go through effortKey.
  for (const name of PAGES) {
    const src = read(name);
    assert.equal(
      /["']mooring\.ai\.effort["']/.test(src),
      false,
      `${name} must reach the stored effort through ChatCore.effortKey`,
    );
    assert.match(src, /effortKey\(/, `${name} must namespace the stored effort`);
  }
});

test("populateEfforts is never called with an argument", () => {
  // It used to take the configured default as a parameter, and three of its four call
  // sites (model switch, /model, the batch dropdown) passed nothing — so switching model
  // silently dropped the configured effort. The default is remembered instead; a call
  // with an argument means someone reintroduced the per-call parameter.
  for (const name of PAGES) {
    const calls = [...read(name).matchAll(/populateEfforts\(([^)]*)\)/g)].map((m) => m[1].trim());
    assert.ok(calls.length >= 2, `${name} should still call populateEfforts`);
    assert.deepEqual(
      calls.filter((a) => a !== ""),
      [],
      `${name} passes an argument to populateEfforts`,
    );
  }
});

test("batch uses chooseEffort while chat validates an explicit notebook override", () => {
  assert.match(read("batch.js"), /chooseEffort\(/);
  assert.match(read("chat.js"), /chooseNotebookOverride\(/);
  assert.match(read("chat.js"), /notebookPreferenceKey\(/);
});

test("the markup each page's effort path drives actually exists", () => {
  // The picker is the whole of "not silent": the batch page in particular now carries
  // an effort it never used to send, so the control that shows it must be on the page.
  // A missing/renamed id fails silently in the browser (getElementById -> null), so
  // pin the ids the effort path touches against the real HTML.
  const wanted = {
    "chat.html": ["chat-effort", "effort-wrap", "chat-model"],
    "batch.html": ["batch-effort", "batch-effort-wrap", "batch-model", "model-row", "caps"],
  };
  for (const [page, ids] of Object.entries(wanted)) {
    const html = read(page);
    for (const id of ids) {
      assert.match(html, new RegExp(`id="${id}"`), `${page} is missing #${id}`);
    }
  }
});

test("both effort wrappers start hidden, so an unpopulated picker never shows", () => {
  // populateEfforts unhides them; a model that takes no effort leaves them hidden, and
  // selectedEffort() reads exactly that class to decide whether an effort is sent.
  for (const [page, id] of [["chat.html", "effort-wrap"], ["batch.html", "batch-effort-wrap"]]) {
    const tag = new RegExp(`<[^>]*id="${id}"[^>]*>`).exec(read(page));
    assert.ok(tag, `${page} is missing #${id}`);
    assert.match(tag[0], /class="[^"]*\bhidden\b/, `${page}: #${id} must start hidden`);
  }
});
