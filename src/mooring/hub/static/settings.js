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
  if (msg) el.scrollIntoView({ block: "nearest" });
}

let MODELS = []; // [{id, name, multiplier}] from /api/ai/models (empty if AI off)
let modelsLoaded = false; // did the last /api/ai/models call succeed (AI on)?
let modelsError = ""; // why the list is empty (e.g. a 403 "not authorized") — shown on the row

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
  if (spec.env_overridden) el.disabled = true;
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
  reset.title = "Reset to the default";
  reset.disabled = spec.env_overridden;
  reset.addEventListener("click", () => resetKey(spec));
  right.appendChild(reset);

  row.append(left, right);
  return row;
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

  for (const group of payload.groups) {
    const specs = byGroup[group.id] || [];
    if (!specs.length) continue;
    const card = document.createElement("section");
    card.className = "card";
    const h = document.createElement("h2");
    h.textContent = group.label;
    card.appendChild(h);
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
    for (const spec of specs) card.appendChild(renderRow(spec));
    root.appendChild(card);
  }

  if (payload.admin && payload.admin.length) {
    const card = document.createElement("section");
    card.className = "card admin-card";
    const h = document.createElement("h2");
    h.textContent = "Managed by your admin";
    card.appendChild(h);
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

// Apply a fresh server payload: pull the model list if AI just became available,
// then re-render.
async function show(payload) {
  if (payload.ai_enabled && !modelsLoaded) await loadModels();
  render(payload);
}

async function save(spec, el) {
  const value = readControl(spec, el);
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
  const { ok, data } = await api("/api/ai/models");
  MODELS = ok && data.models ? data.models : [];
  modelsError = ok && data.error ? data.error : "";
  modelsLoaded = ok;
}

// Cross-tab theme sync (following the hub / another tab) is handled by the
// shared theme.js module.

(async function init() {
  await loadModels(); // best-effort; empty when AI is off (the model row falls back)
  await reload();
})();
