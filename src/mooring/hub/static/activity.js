"use strict";

// The Activity page: the local ledger rendered as human sentences, plus the
// Trash panel with token-exact Restore. Read-only against /api/activity and
// /api/trash; restore POSTs /api/trash/restore and re-renders.

const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok && !data.error) data.error = `Request failed (${resp.status})`;
  return data;
}

function showError(message) {
  const banner = $("error-banner");
  banner.textContent = message || "";
  banner.classList.toggle("hidden", !message);
}

async function renderActivity() {
  const filter = $("activity-filter").value.trim();
  const query = filter ? `?path=${encodeURIComponent(filter)}` : "";
  const data = await api(`/api/activity${query}`);
  if (data.error) return showError(data.error);
  const entries = data.entries || [];
  const list = $("activity-list");
  list.innerHTML = "";
  $("activity-empty").classList.toggle("hidden", entries.length > 0);
  const now = Date.now();
  for (const entry of entries) {
    const li = document.createElement("li");
    const time = document.createElement("span");
    time.className = "muted activity-time";
    time.textContent = ActivityFmt.relTime(entry.ts, now);
    li.append(time, " — ", ActivityFmt.sentence(entry));
    list.appendChild(li);
  }
}

async function restoreEntry(token) {
  const data = await api("/api/trash/restore", { token });
  showError(data.error || "");
  await Promise.all([renderTrash(), renderActivity()]);
}

async function renderTrash() {
  const data = await api("/api/trash");
  if (data.error) return showError(data.error);
  const entries = data.entries || [];
  const table = $("trash-table");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  table.classList.toggle("hidden", entries.length === 0);
  $("trash-empty").classList.toggle("hidden", entries.length > 0);
  const now = Date.now();
  for (const entry of entries) {
    const tr = document.createElement("tr");
    const pathTd = document.createElement("td");
    pathTd.className = "path";
    pathTd.textContent = entry.path;
    const whenTd = document.createElement("td");
    whenTd.textContent = ActivityFmt.relTime(entry.ts, now);
    const whyTd = document.createElement("td");
    whyTd.textContent = entry.action;
    const actionTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = "Restore";
    btn.addEventListener("click", () => restoreEntry(entry.token));
    actionTd.appendChild(btn);
    tr.append(pathTd, whenTd, whyTd, actionTd);
    tbody.appendChild(tr);
  }
}

$("activity-refresh").addEventListener("click", () => {
  renderActivity();
  renderTrash();
});
$("activity-filter").addEventListener("change", renderActivity);

renderActivity();
renderTrash();
