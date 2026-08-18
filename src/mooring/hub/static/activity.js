"use strict";

// The Activity page: the local ledger rendered as human sentences, plus the
// Trash panel with token-exact Restore. Read-only against /api/activity and
// /api/trash; restore POSTs /api/trash/restore and re-renders.
//
// The chart-room chrome (the rail's page list, the section list and its scroll spy)
// comes from subpage.js; the headline is derived from the two counts below, so the
// page opens by saying whether there is anything here rather than describing itself.

const $ = (id) => document.getElementById(id);

// What the last render found, for the headline and the meta line.
let entryCount = 0;
let trashCount = 0;

// One sentence about this workspace's ledger. The trash leads when it has anything
// in it: an entry is a record of something that already happened, but a file in the
// trash is a decision still open to you.
function renderHead() {
  const n = Headline.count(entryCount);
  const t = Headline.count(trashCount);
  let text;
  if (!entryCount && !trashCount) {
    text = "Nothing has happened in this workspace yet.";
  } else if (trashCount) {
    text = `${t.charAt(0).toUpperCase() + t.slice(1)} file${trashCount === 1 ? "" : "s"} ` +
      `${trashCount === 1 ? "is" : "are"} in the trash, still recoverable.`;
  } else {
    text = `${n.charAt(0).toUpperCase() + n.slice(1)} action${entryCount === 1 ? "" : "s"} ` +
      `${entryCount === 1 ? "is" : "are"} recorded for this workspace.`;
  }
  $("headline").textContent = text;

  const parts = ["ACTIVITY", `${entryCount} RECORDED`];
  if (trashCount) parts.push(`${trashCount} IN TRASH`);
  else parts.push("THIS MACHINE");
  $("meta-line").textContent = parts.join("\u00a0 / \u00a0");

  // The trash only earns a rail entry once it holds something — an empty section
  // in the navigator is a promise of content that is not there.
  SubPage.sections(
    trashCount
      ? [{ id: "activity", label: "Recent activity" }, { id: "trash", label: "Trash" }]
      : [{ id: "activity", label: "Recent activity" }],
  );
  $("section-trash").classList.toggle("hidden", !trashCount);
}

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
  entryCount = entries.length;
  const list = $("activity-list");
  list.innerHTML = "";
  $("activity-empty").classList.toggle("hidden", entries.length > 0);
  const now = Date.now();
  for (const entry of entries) {
    const li = document.createElement("li");
    const time = document.createElement("span");
    time.className = "muted activity-time";
    time.textContent = ActivityFmt.relTime(entry.ts, now);
    li.append(time, ActivityFmt.sentence(entry));
    list.appendChild(li);
  }
  renderHead();
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
  trashCount = entries.length;
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
  renderHead();
}

$("activity-refresh").addEventListener("click", () => {
  renderActivity();
  renderTrash();
});
$("activity-filter").addEventListener("input", renderActivity);

renderActivity();
renderTrash();
