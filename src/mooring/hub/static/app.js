"use strict";

const $ = (id) => document.getElementById(id);

const STATE_BADGES = {
  "synced": "synced",
  "modified": "push",
  "new local": "push",
  "deleted locally": "push",
  "remote changed": "pull",
  "new remote": "pull",
  "deleted remotely": "pull",
  "conflict": "conflict",
  "mixed": "mixed",
  "in review": "review",
};

const PUSH_STATES = ["modified", "new local", "deleted locally"];

let busy = false;
let showAddRepo = false;
let lastFiles = [];

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
  if (message) {
    banner.textContent = message;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

function showLog(data) {
  if (!data || (!data.lines && !data.summary)) return;
  $("log-card").classList.remove("hidden");
  const lines = (data.lines || []).slice();
  if (data.summary) lines.push("", data.summary);
  $("log").textContent = lines.join("\n");
  // The <pre> is plain text; the compare link needs a real anchor.
  const linkBox = $("log-link");
  linkBox.classList.toggle("hidden", !data.compare_url);
  if (data.compare_url) {
    const a = linkBox.querySelector("a");
    a.href = data.compare_url;
    a.textContent = "Create / view the pull request on GitHub ↗";
  }
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll("button, select").forEach((b) => (b.disabled = value));
}

async function action(path, body, refreshAfter = true) {
  if (busy) return;
  setBusy(true);
  showError("");
  try {
    const data = await api(path, body || {});
    if (data.error) showError(data.error);
    showLog(data);
    if (data.url) window.open(data.url, "_blank");
    if (refreshAfter) await refresh();
  } finally {
    setBusy(false);
  }
}

// The contents API is throttled to ~1 file/s; tell the user a long push is alive.
function pushAction(paths, count) {
  if (count > 3) $("summary").textContent = `Pushing ${count} file(s)… (~${Math.ceil(count * 0.8)}s)`;
  return action("/api/push", paths ? { paths } : {});
}

function proposeAction(paths, count) {
  if (count > 3) $("summary").textContent = `Proposing ${count} file(s)… (~${Math.ceil(count * 0.8)}s)`;
  return action("/api/propose", paths ? { paths } : {});
}

function fileActions(file) {
  const actions = [];
  if (file.state === "conflict") {
    actions.push(["Use remote", () => action("/api/resolve", { path: file.path, strategy: "theirs" })]);
    actions.push(["Keep both", () => action("/api/resolve", { path: file.path, strategy: "keep-both" })]);
    actions.push(["Push as copy", () => action("/api/resolve", { path: file.path, strategy: "push-copy" })]);
  } else if (PUSH_STATES.includes(file.state)) {
    actions.push(["Push", () => action("/api/push", { paths: [file.path] })]);
    actions.push(["Propose", () => action("/api/propose", { paths: [file.path] })]);
  }
  const openable = file.path.endsWith(".py") || file.path.endsWith(".pbip");
  if (openable && !["new remote", "deleted locally"].includes(file.state)) {
    actions.push(["Open", () => action("/api/open", { path: file.path }, false)]);
  }
  return actions;
}

function buildRow(pathCell, state, actions) {
  const tr = document.createElement("tr");

  const pathTd = document.createElement("td");
  pathTd.className = "path";
  if (typeof pathCell === "string") {
    pathTd.textContent = pathCell;
  } else {
    pathTd.append(...pathCell);
  }

  const stateTd = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `badge ${STATE_BADGES[state] || ""}`;
  badge.textContent = state;
  stateTd.appendChild(badge);

  const actionsTd = document.createElement("td");
  for (const [label, handler] of actions) {
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = label;
    btn.addEventListener("click", handler);
    actionsTd.appendChild(btn);
    actionsTd.appendChild(document.createTextNode(" "));
  }

  tr.append(pathTd, stateTd, actionsTd);
  return tr;
}

function buildFileRow(file) {
  return buildRow(file.path, file.state, fileActions(file));
}

function buildArtifactRows(artifact, files) {
  const byPath = new Map(files.map((f) => [f.path, f]));
  const memberRows = artifact.members
    .map((path) => byPath.get(path))
    .filter(Boolean)
    .map((file) => {
      const row = buildFileRow(file);
      row.classList.add("member", "hidden");
      return row;
    });

  const caret = document.createElement("button");
  caret.className = "small caret";
  caret.textContent = "▸";
  caret.addEventListener("click", () => {
    const open = caret.textContent === "▾";
    caret.textContent = open ? "▸" : "▾";
    memberRows.forEach((row) => row.classList.toggle("hidden", open));
  });

  const name = document.createElement("b");
  name.textContent = ` ${artifact.name} `;
  const detail = document.createElement("span");
  detail.className = "muted";
  const counts = [];
  if (artifact.to_push) counts.push(`${artifact.to_push} to push`);
  if (artifact.to_pull) counts.push(`${artifact.to_pull} to pull`);
  if (artifact.conflicts) counts.push(`${artifact.conflicts} conflicted`);
  detail.textContent =
    `— Power BI project, ${artifact.members.length} files` +
    (counts.length ? ` (${counts.join(", ")})` : "");

  const actions = [];
  if (artifact.to_push) {
    const paths = artifact.members.filter((p) => {
      const f = byPath.get(p);
      return f && PUSH_STATES.includes(f.state);
    });
    actions.push(["Push", () => pushAction(paths, paths.length)]);
    actions.push(["Propose", () => proposeAction(paths, paths.length)]);
  }
  const pointer = byPath.get(artifact.pointer);
  if (pointer && !["new remote", "deleted locally"].includes(pointer.state)) {
    actions.push(["Open", () => action("/api/open", { path: artifact.pointer }, false)]);
  }

  const header = buildRow([caret, name, detail], artifact.state, actions);
  return [header, ...memberRows];
}

function renderFiles(files, artifacts) {
  const tbody = $("files-table").querySelector("tbody");
  tbody.innerHTML = "";
  $("empty-hint").classList.toggle("hidden", files.length > 0);
  $("files-table").classList.toggle("hidden", files.length === 0);
  for (const artifact of artifacts) {
    for (const row of buildArtifactRows(artifact, files)) tbody.appendChild(row);
  }
  for (const file of files) {
    if (!file.artifact) tbody.appendChild(buildFileRow(file));
  }
}

function renderReviewBanner(review) {
  const banner = $("review-banner");
  banner.innerHTML = "";
  banner.classList.toggle("hidden", !review);
  if (!review) return;
  banner.append(`Proposal open on ${review.branch} — `);
  const a = document.createElement("a");
  a.href = review.compare_url;
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = "create / view the pull request";
  banner.appendChild(a);
}

function renderRepoSelect(state) {
  const select = $("repo-select");
  select.innerHTML = "";
  const repos = state.repos || [];
  select.classList.toggle("hidden", repos.length === 0);
  for (const repo of repos) {
    const opt = document.createElement("option");
    opt.value = repo.alias;
    opt.textContent = `${repo.alias} — ${repo.slug}`;
    opt.selected = repo.active;
    select.appendChild(opt);
  }
  const add = document.createElement("option");
  add.value = "__add__";
  add.textContent = "+ Add repo…";
  select.appendChild(add);
}

async function refresh() {
  const state = await api("/api/state");
  showError(state.error || "");
  const hostSuffix = state.host && state.host !== "github.com" ? ` · ${state.host}` : "";
  $("repo-info").textContent = state.repo ? `${state.repo} @ ${state.branch}${hostSuffix}` : "";
  $("workspace-info").textContent = `Workspace: ${state.workspace}`;
  const hint = $("workspace-hint");
  hint.textContent = state.workspace_hint || "";
  hint.classList.toggle("hidden", !state.workspace_hint);
  $("packages").textContent = (state.packages || []).join(", ");

  renderRepoSelect(state);
  $("setup-card").classList.toggle("hidden", state.configured && !showAddRepo);
  $("setup-client-id-label").classList.toggle("hidden", state.configured);
  $("setup-host-label").classList.toggle("hidden", state.configured);
  $("setup-cancel").classList.toggle("hidden", !state.configured);
  $("setup-intro").classList.toggle("hidden", state.configured);
  $("login-card").classList.toggle("hidden", !state.configured || state.logged_in);
  $("files-card").classList.toggle("hidden", !state.logged_in);

  if (state.logged_in) {
    const userInfo = $("user-info");
    userInfo.innerHTML = "";
    userInfo.append(`@${state.user} `);
    const logoutBtn = document.createElement("button");
    logoutBtn.className = "small";
    logoutBtn.textContent = "Log out";
    logoutBtn.addEventListener("click", async () => {
      await api("/api/logout", {});
      location.reload();
    });
    userInfo.appendChild(logoutBtn);
    $("summary").textContent = state.summary || "";
    renderReviewBanner(state.review);
    lastFiles = state.files || [];
    renderFiles(lastFiles, state.artifacts || []);
  } else {
    $("user-info").textContent = "";
  }
}

async function startLogin() {
  showError("");
  const data = await api("/api/login/start", {});
  if (data.error) return showError(data.error);
  $("login-start").classList.add("hidden");
  $("login-code-box").classList.remove("hidden");
  $("login-code").textContent = data.user_code;
  $("login-link").href = data.verification_uri;
  $("login-link").textContent = data.verification_uri.replace(/^https:\/\//, "");
  window.open(data.verification_uri, "_blank");
  pollLogin();
}

async function pollLogin() {
  const data = await api("/api/login/poll");
  if (data.status === "ok") {
    $("login-code-box").classList.add("hidden");
    $("login-start").classList.remove("hidden");
    await refresh();
    return;
  }
  if (data.status === "error") {
    showError(data.message || "Login failed.");
    $("login-code-box").classList.add("hidden");
    $("login-start").classList.remove("hidden");
    return;
  }
  setTimeout(pollLogin, 2500);
}

$("login-start").addEventListener("click", startLogin);
$("btn-refresh").addEventListener("click", refresh);
$("btn-pull").addEventListener("click", () => action("/api/pull", {}));
$("btn-push").addEventListener("click", () => {
  const count = lastFiles.filter((f) => PUSH_STATES.includes(f.state)).length;
  return pushAction(null, count);
});
$("btn-propose").addEventListener("click", () => {
  const count = lastFiles.filter((f) => PUSH_STATES.includes(f.state)).length;
  return proposeAction(null, count);
});
$("btn-new").addEventListener("click", () => {
  const name = prompt("Notebook name (e.g. sales-analysis):");
  if (name) action("/api/new", { name });
});
$("repo-select").addEventListener("change", (event) => {
  const alias = event.target.value;
  if (alias === "__add__") {
    showAddRepo = true;
    $("setup-card").classList.remove("hidden");
    refresh();
    return;
  }
  action("/api/repo/switch", { alias });
});
$("setup-save").addEventListener("click", () => {
  showAddRepo = false;
  action("/api/setup", {
    client_id: $("setup-client-id").value,
    host: $("setup-host").value,
    owner: $("setup-owner").value,
    repo: $("setup-repo").value,
    branch: $("setup-branch").value,
    alias: $("setup-alias").value,
  });
});
$("setup-cancel").addEventListener("click", () => {
  showAddRepo = false;
  refresh();
});

refresh();
