"use strict";

// The Settings page: a generic renderer over the registry the server sends from
// GET /api/settings. Each editable control POSTs one {key,value} to /api/settings
// (the theme control reuses /api/ui/theme so an open hub/chat re-themes live).
// Privacy-weakening flips come back as 409 needs_confirm and must be confirmed.

const $ = (id) => document.getElementById(id);

// Appearance is owned by the shared theme.js module (loaded before this file):
// it writes the localStorage key and follows a cross-tab change live. The theme
// control below calls applyTheme when the user changes it here; alias it.
const applyTheme = window.MooringTheme.applyTheme;

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch { /* empty body */ }
  return { ok: resp.ok, status: resp.status, data };
}

function showError(msg) {
  const el = $("error-banner");
  el.textContent = msg;
  el.classList.toggle("hidden", !msg);
}

let MODELS = []; // [{id, name, multiplier}] from /api/ai/models (empty if AI off)
let ROUTING = null; // safe approved-model metadata only; never endpoint/key/classifier
let modelsLoaded = false; // has the best-effort general-model load completed?
let modelsLoading = false;
let lastPayload = null;
let modelsError = ""; // why the list is empty (e.g. a 403 "not authorized") — shown on the row

let ROUTING_SOURCE = "off"; // "off" | "managed" | "local" — which profile is live
let ROUTING_LOCAL_ALLOWED = true; // false when the launcher or a team policy forbids it
let ROUTING_KEY_STORED = false; // whether a self-configured credential exists (never it)

function isRoutingSetting(spec) {
  return spec.key === "ai.trusted_model" || spec.key === "ai.routing_preference";
}

// The six rows that DEFINE a self-configured profile, as opposed to the two that
// pick within whichever profile is live.
function isLocalRoutingSetting(spec) {
  return spec.key.startsWith("ai.routing.");
}

function trustedModelsForSpec(spec) {
  if (spec.key !== "ai.trusted_model" || !Array.isArray(spec.enum_options)) {
    return ChatCore.trustedModelOptions(ROUTING);
  }
  return ChatCore.trustedModelsFromEnumOptions(spec.enum_options);
}

function routingForSpec(spec) {
  if (!ROUTING || spec.key !== "ai.trusted_model") return ROUTING;
  return { ...ROUTING, trusted_models: trustedModelsForSpec(spec) };
}

function routingSettingUnavailable(spec) {
  // The server explains an on-but-unusable profile itself; trust that over the
  // client's own read of the metadata, which cannot see an endpoint or credential.
  if (spec.unavailable_note) return true;
  if (isLocalRoutingSetting(spec)) return !ROUTING_LOCAL_ALLOWED;
  return isRoutingSetting(spec) && !ChatCore.trustedRoutingAvailable(routingForSpec(spec));
}

// Build the value to send for a control, reading its DOM element. An emptied
// number field yields null, which the caller treats as a no-op (not a bad write).
function readControl(spec, el) {
  if (spec.control === "toggle") return el.checked;
  if (spec.type === "int") return el.value === "" ? null : parseInt(el.value, 10);
  if (spec.type === "float") return el.value === "" ? null : parseFloat(el.value);
  if (spec.control === "tags") {
    return el.value.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return el.value;
}

function makeControl(spec) {
  let el;
  if (spec.control === "toggle") {
    el = document.createElement("input");
    el.type = "checkbox";
    el.checked = !!spec.value;
  } else if (spec.control === "select") {
    el = document.createElement("select");
    if (spec.key === "ai.model") {
      el.add(new Option("(provider default)", ""));
      for (const m of MODELS) {
        const mult = m.multiplier && m.multiplier > 1 ? ` · ${m.multiplier}×` : "";
        el.add(new Option((m.name || m.id) + mult, m.id));
      }
      // Keep the current value selectable even if the provider didn't list it.
      if (spec.value && !MODELS.some((m) => m.id === spec.value)) {
        el.add(new Option(spec.value, spec.value));
      }
      el.value = spec.value || "";
    } else if (spec.key === "ai.trusted_model") {
      const safeRouting = routingForSpec(spec);
      const options = ChatCore.trustedModelOptions(safeRouting);
      // Settings' empty value resets to the managed profile default. The chat
      // page intentionally uses default_trusted_model instead: there it means
      // the effective user-level Settings choice.
      const approvedDefault = safeRouting?.managed_default_trusted_model ||
        safeRouting?.default_trusted_model ||
        "approved model";
      el.add(new Option(`Use approved default (${approvedDefault})`, ""));
      for (const model of options) el.add(new Option(model.name, model.id));
      // A revoked model is never preserved as a selectable custom value.
      el.value = ChatCore.routingSettingValueAllowed(safeRouting, spec.key, spec.value || "")
        ? (spec.value || "")
        : "";
    } else if (spec.key === "ai.routing_preference") {
      el.add(new Option("Automatic", "auto"));
      el.add(new Option("Always use approved", "trusted"));
      el.value = ChatCore.routingSettingValueAllowed(ROUTING, spec.key, spec.value)
        ? spec.value
        : "auto";
    } else {
      for (const opt of spec.enum_options || []) el.add(new Option(opt.label, opt.value));
      el.value = spec.value;
    }
  } else if (spec.control === "number") {
    el = document.createElement("input");
    el.type = "number";
    if (spec.min !== null) el.min = spec.min;
    if (spec.max !== null) el.max = spec.max;
    if (spec.type === "float") el.step = "0.05";
    el.value = spec.value;
  } else if (spec.control === "tags") {
    el = document.createElement("input");
    el.type = "text";
    el.value = (spec.value || []).join(", ");
  } else {
    el = document.createElement("input");
    el.type = "text";
    el.value = spec.value == null ? "" : spec.value;
  }
  el.id = `ctrl:${spec.key}`;
  // A policy-locked control is disabled as well as annotated: the server refuses
  // the write with a 409 either way, but a click that appears to work and then
  // snaps back is exactly the "silently ignored" experience the lock must avoid.
  if (spec.env_overridden || spec.locked || routingSettingUnavailable(spec)) el.disabled = true;
  el.addEventListener("change", () => save(spec, el));
  if (spec.control === "toggle") {
    // Wrap the checkbox as a sliding on/off switch (the input keeps its id, so the
    // name label, focus restore, and the change handler all still target it).
    const sw = document.createElement("label");
    sw.className = "switch";
    const slider = document.createElement("span");
    slider.className = "slider";
    sw.append(el, slider);
    return sw;
  }
  return el;
}

function badge(spec) {
  // The lock wins the badge slot: "who decided this" outranks "how risky is it".
  if (spec.locked) return { cls: "warn", text: "Set by your team" };
  if (spec.sensitivity === "weakens") return { cls: "danger", text: "Weakens privacy" };
  if (spec.sensitivity === "needs_care") return { cls: "warn", text: "Heads up" };
  return null;
}

function renderRow(spec) {
  const row = document.createElement("div");
  row.className = "settings-row";

  const left = document.createElement("div");
  left.className = "settings-label";
  const name = document.createElement("label");
  name.htmlFor = `ctrl:${spec.key}`;
  name.textContent = spec.label;
  left.appendChild(name);
  const b = badge(spec);
  if (b) {
    const tag = document.createElement("span");
    tag.className = `badge ${b.cls}`;
    tag.textContent = b.text;
    left.appendChild(tag);
  }
  const help = document.createElement("div");
  help.className = "settings-help muted";
  help.textContent = spec.help;
  left.appendChild(help);
  // Why the model picker is empty (e.g. a 403 "not authorized") — so the row isn't a
  // silent dead end. Only the model row carries it; only when the list failed.
  if (spec.key === "ai.model" && modelsError) {
    const note = document.createElement("div");
    note.className = "settings-help env-note";
    note.textContent = "Couldn’t load models — " + modelsError;
    left.appendChild(note);
  }
  if (isLocalRoutingSetting(spec)) {
    const note = document.createElement("div");
    note.className = "settings-help env-note";
    // env_overridden already renders its own "managed centrally" note, so only
    // explain the OTHER reason a local profile can be forbidden: a team policy.
    if (!ROUTING_LOCAL_ALLOWED && !spec.env_overridden) {
      note.textContent =
        "Your team's policy switches self-configured customer-data routing off, so " +
        "it can't be set up here.";
    } else if (spec.key === "ai.routing.enabled" && ROUTING_SOURCE === "local") {
      note.textContent =
        "In use. Chats say “Self-configured”, never “approved”: nobody but you has " +
        "vetted this endpoint.";
    } else if (spec.key === "ai.routing.enabled") {
      note.textContent =
        "Not in use yet. Fill in every field, store an API key, then switch it on.";
    }
    // The status line belongs to the master switch; the five fields under it say
    // nothing extra unless they are dead, in which case every one of them says why.
    if (note.textContent) left.appendChild(note);
  }
  if (isRoutingSetting(spec)) {
    const note = document.createElement("div");
    note.className = "settings-help env-note";
    if (spec.unavailable_note) {
      note.textContent = spec.unavailable_note;
    } else if (routingSettingUnavailable(spec)) {
      note.textContent = "Customer-data routing is unavailable. Check its profile, or ask your administrator.";
    } else if (spec.key === "ai.trusted_model") {
      const profile = typeof ROUTING?.profile_label === "string" ? ROUTING.profile_label.trim() : "";
      note.textContent = profile
        ? `Approved service: ${profile}`
        : "Choose only from administrator-approved models.";
    } else {
      note.textContent = "This default applies to newly opened interactive notebook chats.";
    }
    left.appendChild(note);
  }
  if (spec.locked) {
    // Honesty: say WHERE the lock came from, not just that the control is dead.
    const note = document.createElement("div");
    note.className = "settings-help env-note";
    note.textContent = spec.locked_note;
    left.appendChild(note);
  }
  if (spec.env_overridden) {
    const note = document.createElement("div");
    note.className = "settings-help muted env-note";
    note.textContent = "Overridden by an environment variable (managed centrally) — the value shown is the active override, not your saved choice.";
    left.appendChild(note);
  }

  const right = document.createElement("div");
  right.className = "settings-control";
  right.appendChild(makeControl(spec));
  const reset = document.createElement("button");
  reset.className = "small ghost";
  reset.textContent = "Reset";
  reset.title = spec.locked ? "Set by your team's policy" : "Reset to the default";
  reset.disabled = spec.env_overridden || spec.locked || routingSettingUnavailable(spec);
  reset.addEventListener("click", () => resetKey(spec));
  right.appendChild(reset);

  row.append(left, right);
  return row;
}

// The credential for a SELF-CONFIGURED endpoint. Not a SettingSpec, because it is
// not config: it goes to the OS credential store, never config.toml, and is never
// read back — the page only ever learns whether one exists.
function renderTrustedKeyRow() {
  const row = document.createElement("div");
  row.className = "settings-row";

  const left = document.createElement("div");
  left.className = "settings-label";
  const name = document.createElement("label");
  name.htmlFor = "ctrl:ai.routing.api_key";
  name.textContent = "Customer-data API key";
  left.appendChild(name);
  const tag = document.createElement("span");
  tag.className = `badge ${ROUTING_KEY_STORED ? "synced" : "warn"}`;
  tag.textContent = ROUTING_KEY_STORED ? "Stored" : "Not set";
  left.appendChild(tag);
  const help = document.createElement("div");
  help.className = "settings-help muted";
  help.textContent =
    "The key for the endpoint above, kept in this machine's credential store — never " +
    "in config.toml and never synced. It is deliberately separate from your general " +
    "OpenAI key: the customer-data route will not fall back to it.";
  left.appendChild(help);

  const right = document.createElement("div");
  right.className = "settings-control";
  const input = document.createElement("input");
  input.type = "password";
  input.id = "ctrl:ai.routing.api_key";
  input.placeholder = ROUTING_KEY_STORED ? "Replace the stored key…" : "Paste a key…";
  input.autocomplete = "off";
  input.disabled = !ROUTING_LOCAL_ALLOWED;
  const store = document.createElement("button");
  store.className = "small";
  store.textContent = "Store";
  store.disabled = !ROUTING_LOCAL_ALLOWED;
  store.addEventListener("click", () => saveTrustedKey(input));
  const clear = document.createElement("button");
  clear.className = "small ghost";
  clear.textContent = "Clear";
  clear.disabled = !ROUTING_LOCAL_ALLOWED || !ROUTING_KEY_STORED;
  clear.addEventListener("click", () => clearTrustedKey());
  right.append(input, store, clear);

  row.append(left, right);
  return row;
}

async function postTrustedKey(body, failure) {
  showError("");
  try {
    const resp = await fetch("/api/ai/trusted-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showError(data.error || failure);
      return;
    }
  } catch {
    showError(failure);
    return;
  }
  await reload();
}

function saveTrustedKey(input) {
  const key = input.value.trim();
  if (!key) {
    showError("Paste a key first.");
    return;
  }
  input.value = "";
  return postTrustedKey({ key }, "Could not store the key.");
}

function clearTrustedKey() {
  return postTrustedKey({ clear: true }, "Could not clear the key.");
}

function render(payload) {
  // Preserve keyboard focus (and caret) across the full-form rebuild, so a toggle
  // or select the user just changed doesn't drop focus to <body>.
  const active = document.activeElement;
  const activeId = active && active.id ? active.id : null;
  const selStart = active && "selectionStart" in active ? active.selectionStart : null;
  const selEnd = active && "selectionEnd" in active ? active.selectionEnd : null;

  showError("");
  const root = $("settings-root");
  root.innerHTML = "";
  const byGroup = {};
  for (const spec of payload.editable) (byGroup[spec.group] ||= []).push(spec);

  const sections = [];  // {id, label} for the rail, in page order
  for (const group of payload.groups) {
    const specs = byGroup[group.id] || [];
    if (!specs.length) continue;
    const card = SubPage.section(group.id, group.label);
    sections.push({ id: group.id, label: group.label });
    // A live, value-free status line for the PII guard.
    if (group.id === "pii" && payload.pii) {
      const s = payload.pii;
      const line = document.createElement("div");
      line.className = "muted settings-help";
      if (!s.enabled) line.textContent = "Guard status: scan off.";
      else if (s.names && s.names_active) line.textContent = `Guard status: scan on · name detection active (${s.backend}).`;
      else if (s.names) line.textContent = "Guard status: scan on · name detection requested but the model/extra isn't ready (install mooring[pii] or mooring[pii-spacy]).";
      else line.textContent = "Guard status: scan on.";
      card.appendChild(line);
    }
    for (const spec of specs) {
      card.appendChild(renderRow(spec));
      // The credential belongs with the endpoint it unlocks, not in a card of its own.
      if (spec.key === "ai.routing.base_url") card.appendChild(renderTrustedKeyRow());
    }
    root.appendChild(card);
  }

  // What the TEAM enforces, above the per-machine admin block: the synced
  // [policy] rules this client actually applies (and any it had to ignore).
  const pol = payload.policy;
  if (pol && (pol.in_force || pol.unreadable)) {
    const card = SubPage.section("policy", "Set by your team");
    card.classList.add("read-only");
    sections.push({ id: "policy", label: "Set by your team" });
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent =
      "From the [policy] block in this repo's synced mooring.toml. Policy can only make " +
      "things stricter than your own settings, never weaker. Change it with `mooring policy` " +
      "and push, or ask whoever maintains the repo.";
    card.appendChild(p);
    const ul = document.createElement("ul");
    ul.className = "settings-help";
    for (const line of pol.lines || []) {
      const li = document.createElement("li");
      li.textContent = line.trim();
      ul.appendChild(li);
    }
    card.appendChild(ul);
    root.appendChild(card);
  }

  if (payload.admin && payload.admin.length) {
    const card = SubPage.section("admin", "Managed by your admin");
    card.classList.add("read-only");
    sections.push({ id: "admin", label: "Managed by your admin" });
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "Set when your app was built, by your team, or via environment variables. Change these with `mooring config` or ask your admin.";
    card.appendChild(p);
    const table = document.createElement("table");
    table.className = "admin-table";
    const tbody = document.createElement("tbody");
    for (const row of payload.admin) {
      const tr = document.createElement("tr");
      const k = document.createElement("td");
      k.textContent = row.label;
      const v = document.createElement("td");
      v.className = "admin-value";
      v.textContent = row.value;
      tr.append(k, v);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    card.appendChild(table);
    root.appendChild(card);
  }

  SubPage.sections(sections);
  renderMeta(payload);

  if (activeId) {
    const el = document.getElementById(activeId);
    if (el) {
      el.focus();
      if (selStart != null && el.setSelectionRange) {
        try { el.setSelectionRange(selStart, selEnd); } catch { /* not a text field */ }
      }
    }
  }
}

// The meta line: what this page is, how much of it there is, and where it saves.
// Three segments, like the hub's, so it never wraps.
function renderMeta(payload) {
  const n = (payload.editable || []).length;
  $("meta-line").textContent =
    ["SETTINGS", `${n} PREFERENCE${n === 1 ? "" : "S"}`, "THIS MACHINE"].join("\u00a0 / \u00a0");
}

// Apply a fresh server payload: pull the model list if AI just became available,
// then re-render.
//
// Reconcile the appearance first. The pre-paint script in <head> paints from
// localStorage ("last used"), but the SERVER is the source of truth — the hub does
// this in refresh(), and without the same step here the page that OWNS the theme
// control could sit there dark with its own select reading "Light".
async function show(payload) {
  lastPayload = payload;
  const theme = (payload.editable || []).find((s) => s.key === "ui.theme");
  if (theme && theme.value) applyTheme(theme.value);
  // Approved routing metadata is part of the Settings payload so these controls
  // remain usable even when the unrelated general provider cannot list models.
  // Never retain earlier allowlist metadata across a failed/disabled refresh.
  if (Object.prototype.hasOwnProperty.call(payload, "routing")) {
    ROUTING = payload.routing?.enabled === true ? payload.routing : null;
  }
  ROUTING_SOURCE = typeof payload.routing_source === "string" ? payload.routing_source : "off";
  ROUTING_LOCAL_ALLOWED = payload.routing_local_allowed !== false;
  ROUTING_KEY_STORED = payload.routing_key_stored === true;
  render(payload);
  if (payload.ai_enabled && !modelsLoaded && !modelsLoading) {
    modelsLoading = true;
    // General-model discovery is deliberately out of the approved controls'
    // critical path. Re-render this same server snapshot when it completes.
    void loadModels().finally(() => {
      modelsLoading = false;
      if (lastPayload) render(lastPayload);
    });
  }
}

async function save(spec, el) {
  const value = readControl(spec, el);
  if (
    isRoutingSetting(spec) &&
    !ChatCore.routingSettingValueAllowed(routingForSpec(spec), spec.key, value)
  ) {
    await reload();
    showError("That approved AI choice is no longer available. Reload and choose again.");
    return;
  }
  // An emptied number field is a no-op, not a bad write — restore the prior value.
  if ((spec.type === "int" || spec.type === "float") && value === null) return reload();
  // The theme reuses the proven hub endpoint so editors + an open hub/chat re-theme.
  if (spec.key === "ui.theme") {
    applyTheme(value);
    const r = await api("/api/ui/theme", { theme: value });
    if (!r.ok) { await reload(); showError(r.data.error || "Could not save the theme."); }
    return;
  }
  let res = await api("/api/settings", { key: spec.key, value });
  // A policy lock has no confirm path — restore the server's truth and explain.
  if (res.status === 409 && res.data.locked) {
    await reload();
    showError(res.data.message || res.data.error || "Your team's policy sets this.");
    return;
  }
  if (res.status === 409 && res.data.needs_confirm) {
    if (window.confirm(res.data.message || "Are you sure?")) {
      res = await api("/api/settings", { key: spec.key, value, confirm: true });
    } else {
      return reload(); // declined — revert the control to the server's truth
    }
  }
  if (!res.ok) {
    await reload(); // revert first (render clears the banner), then show the error
    showError(res.data.error || "Could not save this setting.");
    return;
  }
  await show(res.data); // the response is a fresh full payload
}

async function resetKey(spec) {
  let res = await api("/api/settings/reset", { key: spec.key });
  if (res.status === 409 && res.data.locked) {
    await reload();
    showError(res.data.message || res.data.error || "Your team's policy sets this.");
    return;
  }
  if (res.status === 409 && res.data.needs_confirm) {
    if (!window.confirm(res.data.message || "Are you sure?")) return; // nothing changed
    res = await api("/api/settings/reset", { key: spec.key, confirm: true });
  }
  if (!res.ok) {
    await reload();
    showError(res.data.error || "Could not reset this setting.");
    return;
  }
  if (spec.key === "ui.theme") {
    const t = (res.data.editable || []).find((s) => s.key === "ui.theme");
    if (t) applyTheme(t.value);
  }
  await show(res.data);
}

async function reload() {
  const { ok, data } = await api("/api/settings");
  if (ok) await show(data);
}

async function loadModels() {
  try {
    const { ok, data } = await api("/api/ai/models");
    MODELS = ok && data.models ? data.models : [];
    modelsError = ok && data.error ? data.error : (ok ? "" : "Could not load models.");
  } catch {
    MODELS = [];
    modelsError = "Could not load models.";
  }
  modelsLoaded = true;
}

// Cross-tab theme sync (following the hub / another tab) is handled by the
// shared theme.js module.

(async function init() {
  await reload(); // approved controls render before general models hydrate
})();
