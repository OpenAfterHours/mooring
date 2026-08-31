"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../src/mooring/hub/static/chat_core.js");

const routing = {
  enabled: true,
  profile_label: "Firm Azure OpenAI",
  trusted_models: [
    { id: "approved-coder", name: "Approved coder" },
    { id: "approved-general", name: "Approved general" },
  ],
  default_trusted_model: "approved-coder",
  default_routing_preference: "auto",
};

function fakeStorage(initial = {}, throws = false) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      if (throws) throw new Error("storage blocked");
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      if (throws) throw new Error("storage blocked");
      values.set(key, String(value));
    },
    removeItem(key) {
      if (throws) throw new Error("storage blocked");
      values.delete(key);
    },
    snapshot() { return Object.fromEntries(values); },
  };
}

test("trusted routing options come only from valid server allowlist entries", () => {
  assert.deepEqual(
    C.trustedModelOptions({
      enabled: true,
      trusted_models: [
        { id: "approved-coder", name: "Coder" },
        { id: " approved-coder ", name: "duplicate" },
        { id: "second" },
        { name: "missing id" },
        "browser-invented",
      ],
    }),
    [
      { id: "approved-coder", name: "Coder" },
      { id: "second", name: "second" },
    ],
  );
  assert.deepEqual(C.trustedModelOptions({ enabled: false, trusted_models: routing.trusted_models }), []);
});

test("saved trusted model is accepted only while it remains allowlisted", () => {
  assert.equal(C.chooseTrustedModel(routing, "approved-general"), "approved-general");
  assert.equal(C.chooseTrustedModel(routing, "removed-or-tampered"), "approved-coder");
  assert.equal(C.chooseTrustedModel({ ...routing, default_trusted_model: "removed" }, "removed"), "approved-coder");
  assert.equal(C.chooseTrustedModel({ enabled: true, trusted_models: [] }, "approved-coder"), "");
});

test("routing preference permits automatic or upward-only approved routing", () => {
  assert.equal(C.chooseRoutingPreference(routing, "trusted"), "trusted");
  assert.equal(C.chooseRoutingPreference(routing, "auto"), "auto");
  assert.equal(C.chooseRoutingPreference(routing, "general"), "auto");
  assert.equal(C.chooseRoutingPreference({ enabled: false, trusted_models: routing.trusted_models }, "trusted"), "auto");
});

test("enabled routing is unavailable on an error or empty allowlist", () => {
  assert.equal(C.trustedRoutingAvailable(routing), true);
  assert.equal(C.trustedRoutingAvailable({ ...routing, error: "misconfigured" }), false);
  assert.equal(C.trustedRoutingAvailable({ enabled: true, trusted_models: [] }), false);
});

test("route notices include safe profile/model labels and handoff state", () => {
  assert.equal(
    C.routingNotice(
      {
        zone: "trusted",
        profile_label: "Firm Azure OpenAI",
        model: "approved-coder",
        conversation_carried: true,
      },
      true,
    ),
    "This conversation switched to your firm's approved customer-data model " +
      "(Firm Azure OpenAI · approved-coder). The earlier conversation was carried forward.",
  );
  assert.match(C.routingNotice({ zone: "general", model: "copilot-coder" }, false), /copilot-coder/);
  assert.equal(C.routingNotice({ zone: "general" }, true), "");
  assert.equal(C.routingNotice({ zone: "untrusted", model: "bad" }, false), "");
});

test("privacy chrome is truthful about approved routing and never exposes deployment details", () => {
  const copy = C.privacyChrome({
    enabled: true,
    trusted_models: [{ id: "approved-coder", name: "Approved coder" }],
    base_url: "https://must-not-appear.example",
    api_key: "must-not-appear",
    classifier_model: "must-not-appear",
  });
  assert.equal(copy.badge, "approved routing");
  assert.match(copy.body, /Customer information.*may be sent/);
  assert.match(copy.body, /does not automatically read raw dataset values or cell results/);
  assert.match(copy.body, /recognized credential patterns are blocked locally/);
  assert.doesNotMatch(Object.values(copy).join(" "), /must-not-appear/);
  assert.doesNotMatch(copy.footer, /Don't paste real values/);
});

test("chat markup offers only upward routing controls and starts them hidden", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const html = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "mooring", "hub", "static", "chat.html"),
    "utf8",
  );
  assert.match(html, /id="trusted-model-wrap" class="status-ctrl hidden"/);
  assert.match(html, /id="routing-preference-wrap" class="status-ctrl hidden"/);
  assert.match(html, /<option value="auto">Automatic<\/option>/);
  assert.match(html, /<option value="trusted">Always use approved<\/option>/);
  assert.doesNotMatch(html, /always use general/i);
});

test("privacy chrome retains schema-only guidance when routing is disabled", () => {
  const copy = C.privacyChrome({ enabled: false });
  assert.equal(copy.badge, "schema-only");
  assert.match(copy.body, /never the data itself/);
  assert.match(copy.footer, /Don't paste real values/);
});

test("routing misconfiguration is shown as unavailable, never green approved routing", () => {
  const copy = C.privacyChrome({ enabled: true, trusted_models: [], error: "secret detail" });
  assert.equal(copy.badge, "approved routing unavailable");
  assert.equal(copy.badgeClass, "danger");
  assert.match(copy.body, /cannot send messages/);
  assert.doesNotMatch(Object.values(copy).join(" "), /secret detail/);
});

test("routing changes are refused while a turn or replacement session is active", () => {
  assert.equal(C.routingChangeAllowed("idle"), true);
  assert.equal(C.routingChangeAllowed("error"), true);
  assert.equal(C.routingChangeAllowed("thinking"), false);
  assert.equal(C.routingChangeAllowed("streaming"), false);
  assert.equal(C.routingChangeAllowed("connecting"), false);
});

test("latest request gate rejects an out-of-order chat-open response", () => {
  const gate = C.latestRequestGate();
  const first = gate.begin();
  const second = gate.begin();
  assert.equal(gate.isCurrent(first), false);
  assert.equal(gate.isCurrent(second), true);
});

test("chat-open routing policy must match what the picker loaded in both directions", () => {
  assert.equal(C.routingExpectationMatches(routing, { zone: "general" }), true);
  assert.equal(C.routingExpectationMatches(routing, { zone: "trusted" }), true);
  assert.equal(C.routingExpectationMatches(routing, null), false);
  assert.equal(C.routingExpectationMatches(null, { zone: "general" }), false);
  assert.equal(C.routingExpectationMatches(null, null), true);
  assert.equal(C.routingExpectationMatches(routing, { zone: "unexpected" }), false);
});

test("general model controls are irrelevant only under forced approved routing", () => {
  assert.equal(C.generalModelRelevant(routing, "trusted"), false);
  assert.equal(C.generalModelRelevant(routing, "auto"), true);
  assert.equal(C.generalModelRelevant(null, "trusted"), true);
  assert.equal(
    C.generalModelRelevant({ enabled: true, trusted_models: [], error: "unavailable" }, "trusted"),
    true,
  );
});

test("notebook preference keys separate workspaces and normalise equivalent notebook paths", () => {
  const first = C.notebookPreferenceKey("workspace-a", ".\\folder\\\\report.py", "trusted_model");
  const same = C.notebookPreferenceKey("workspace-a", "folder/report.py", "trusted_model");
  const secondWorkspace = C.notebookPreferenceKey("workspace-b", "folder/report.py", "trusted_model");
  assert.equal(first, same);
  assert.notEqual(first, secondWorkspace);
  assert.notEqual(first, C.notebookPreferenceKey("workspace-a", "folder/report.py", "routing_preference"));
});

test("notebook overrides distinguish inheritance, valid choices, and revoked choices", () => {
  assert.equal(C.chooseNotebookOverride(["general-a"], ""), "");
  assert.equal(C.chooseNotebookOverride(["general-a"], "general-a"), "general-a");
  assert.equal(C.chooseNotebookOverride(["general-a"], "removed"), "");
  assert.equal(C.chooseNotebookTrustedOverride(routing, "approved-general"), "approved-general");
  assert.equal(C.chooseNotebookTrustedOverride(routing, "revoked"), "");
  assert.equal(
    C.chooseNotebookTrustedOverride({ ...routing, trusted_models: [routing.trusted_models[0]] }, "approved-coder"),
    "",
  );
  assert.equal(C.chooseNotebookRoutingOverride(routing, "general"), "");
  assert.equal(C.effectiveRoutingPreference(routing, ""), "auto");
  assert.equal(C.effectiveRoutingPreference(routing, "trusted"), "trusted");
});

test("stale notebook overrides self-heal and cannot resurrect after re-approval", () => {
  const key = C.notebookPreferenceKey("workspace-a", "report.py", "trusted_model");
  const storage = fakeStorage({ [key]: "approved-general" });
  const revoked = { ...routing, trusted_models: [routing.trusted_models[0]] };
  assert.equal(
    C.readValidNotebookOverride(storage, key, (saved) => C.chooseNotebookTrustedOverride(revoked, saved)),
    "",
  );
  assert.deepEqual(storage.snapshot(), {});
  assert.equal(
    C.readValidNotebookOverride(storage, key, (saved) => C.chooseNotebookTrustedOverride(routing, saved)),
    "",
  );
});

test("blocked browser storage safely degrades to Settings inheritance", () => {
  const storage = fakeStorage({}, true);
  assert.equal(C.safeStorageGet(storage, "key"), null);
  assert.equal(C.safeStorageSet(storage, "key", "approved-coder"), false);
  assert.equal(
    C.readValidNotebookOverride(storage, "key", (saved) => C.chooseNotebookTrustedOverride(routing, saved)),
    "",
  );
});

test("chat-open sends only concrete notebook overrides", () => {
  assert.deepEqual(C.notebookOverridePayload({
    model: "",
    reasoning_effort: "",
    trusted_model: "",
    routing_preference: "",
  }), {});
  assert.deepEqual(C.notebookOverridePayload({
    model: "general-a",
    reasoning_effort: "high",
    trusted_model: "approved-general",
    routing_preference: "trusted",
  }), {
    model: "general-a",
    reasoning_effort: "high",
    trusted_model: "approved-general",
    routing_preference: "trusted",
  });
});

test("resolved routing is allowlisted and must preserve explicit notebook overrides", () => {
  const resolved = { trusted_model: "approved-general", routing_preference: "trusted" };
  assert.equal(C.resolvedRoutingValuesValid(routing, resolved.trusted_model, resolved.routing_preference), true);
  assert.equal(C.resolvedRoutingValuesValid(routing, "revoked", "trusted"), false);
  assert.equal(C.resolvedRoutingValuesValid(routing, "approved-general", "general"), false);
  assert.equal(C.resolvedRoutingMatchesRequest(resolved, {}), true);
  assert.equal(C.resolvedRoutingMatchesRequest(resolved, { trusted_model: "approved-general" }), true);
  assert.equal(C.resolvedRoutingMatchesRequest(resolved, { trusted_model: "approved-coder" }), false);
  assert.equal(C.resolvedRoutingMatchesRequest(resolved, { routing_preference: "auto" }), false);
});

test("Settings trusted options use only server-provided enum metadata", () => {
  assert.deepEqual(C.trustedModelsFromEnumOptions([
    { value: " approved-coder ", label: "Approved coder" },
    { value: "approved-coder", label: "duplicate" },
    { value: "second", label: "" },
    { value: "", label: "invalid" },
    "browser-invented",
  ]), [
    { id: "approved-coder", name: "Approved coder" },
    { id: "second", name: "second" },
  ]);
});

test("Settings hydrates approved controls independently and chat uses safe scoped storage", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const staticDir = path.join(__dirname, "..", "..", "src", "mooring", "hub", "static");
  const settings = fs.readFileSync(path.join(staticDir, "settings.js"), "utf8");
  const settingsHtml = fs.readFileSync(path.join(staticDir, "settings.html"), "utf8");
  const chat = fs.readFileSync(path.join(staticDir, "chat.js"), "utf8");
  assert.ok(settingsHtml.indexOf("/static/chat_core.js") < settingsHtml.indexOf("/static/settings.js"));
  assert.match(settings, /payload\.routing\?\.enabled === true/);
  assert.match(settings, /trustedModelsFromEnumOptions\(spec\.enum_options\)/);
  assert.match(settings, /managed_default_trusted_model/);
  const showStart = settings.indexOf("async function show(payload)");
  const saveStart = settings.indexOf("async function save(spec, el)");
  const showSource = settings.slice(showStart, saveStart);
  assert.ok(showSource.indexOf("render(payload)") < showSource.indexOf("void loadModels()"));
  assert.doesNotMatch(settings, /await loadModels\(\)/);
  assert.match(chat, /notebookOverridePayload\(/);
  assert.match(chat, /readValidNotebookOverride\(/);
  assert.match(chat, /Use Settings default \(\$\{ROUTING\.default_trusted_model\}\)/);
  assert.doesNotMatch(chat, /localStorage\.getItem|localStorage\.setItem|localStorage\.removeItem/);
  assert.equal((chat.match(/window\.localStorage/g) || []).length, 1);
  assert.match(chat, /function browserStorage\(\) \{[\s\S]*?try \{[\s\S]*?window\.localStorage/);
});
