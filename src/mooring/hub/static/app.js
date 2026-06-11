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
};

let busy = false;

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
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll("button").forEach((b) => (b.disabled = value));
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

function fileActions(file) {
  const actions = [];
  if (file.state === "conflict") {
    actions.push(["Use remote", () => action("/api/resolve", { path: file.path, strategy: "theirs" })]);
    actions.push(["Keep both", () => action("/api/resolve", { path: file.path, strategy: "keep-both" })]);
    actions.push(["Push as copy", () => action("/api/resolve", { path: file.path, strategy: "push-copy" })]);
  } else if (["modified", "new local", "deleted locally"].includes(file.state)) {
    actions.push(["Push", () => action("/api/push", { paths: [file.path] })]);
  }
  if (file.path.endsWith(".py") && !["new remote", "deleted locally"].includes(file.state)) {
    actions.push(["Open", () => action("/api/open", { path: file.path }, false)]);
  }
  return actions;
}

function renderFiles(files) {
  const tbody = $("files-table").querySelector("tbody");
  tbody.innerHTML = "";
  $("empty-hint").classList.toggle("hidden", files.length > 0);
  $("files-table").classList.toggle("hidden", files.length === 0);
  for (const file of files) {
    const tr = document.createElement("tr");

    const pathTd = document.createElement("td");
    pathTd.className = "path";
    pathTd.textContent = file.path;

    const stateTd = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${STATE_BADGES[file.state] || ""}`;
    badge.textContent = file.state;
    stateTd.appendChild(badge);

    const actionsTd = document.createElement("td");
    for (const [label, handler] of fileActions(file)) {
      const btn = document.createElement("button");
      btn.className = "small";
      btn.textContent = label;
      btn.addEventListener("click", handler);
      actionsTd.appendChild(btn);
      actionsTd.appendChild(document.createTextNode(" "));
    }

    tr.append(pathTd, stateTd, actionsTd);
    tbody.appendChild(tr);
  }
}

async function refresh() {
  const state = await api("/api/state");
  showError(state.error || "");
  $("repo-info").textContent = state.repo ? `${state.repo} @ ${state.branch}` : "";
  $("workspace-info").textContent = `Workspace: ${state.workspace}`;
  $("packages").textContent = (state.packages || []).join(", ");

  $("setup-card").classList.toggle("hidden", state.configured);
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
    renderFiles(state.files || []);
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
$("btn-push").addEventListener("click", () => action("/api/push", {}));
$("btn-new").addEventListener("click", () => {
  const name = prompt("Notebook name (e.g. sales-analysis):");
  if (name) action("/api/new", { name });
});
$("setup-save").addEventListener("click", () =>
  action("/api/setup", {
    client_id: $("setup-client-id").value,
    owner: $("setup-owner").value,
    repo: $("setup-repo").value,
    branch: $("setup-branch").value,
  })
);

refresh();
