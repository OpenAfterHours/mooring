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
  "local": "local",
};

const PUSH_STATES = new Set(["modified", "new local", "deleted locally"]);

// Appearance, shared with the chat window (same origin) via this localStorage
// key; a `storage` event lets an open chat re-theme live. The server is the
// source of truth — /api/state carries the value and we mirror it here.
const LS_THEME = "mooring.ui.theme";

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    // Only rewrite on a real change so we don't fire redundant storage events.
    if (localStorage.getItem(LS_THEME) !== theme) localStorage.setItem(LS_THEME, theme);
  } catch {
    // localStorage may be unavailable (private mode / blocked); theming is best-effort.
  }
}

let busy = false;
let showAddRepo = false;
let lastFiles = [];
let aiChatEnabled = false;

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
  if (!data || (!data.lines && !data.summary && !data.warning)) return;
  $("log-card").classList.remove("hidden");
  const lines = (data.lines || []).slice();
  if (data.warning) lines.push("⚠ " + data.warning);
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
    return data;
  } finally {
    setBusy(false);
  }
}

// Pop the copilot out into its own window (not a tab) so it sits beside the
// notebook. The window features are what make the browser open a window rather
// than a tab; a per-notebook name reuses/focuses an already-open chat window.
function openChatWindow(path) {
  const url = `/ai/chat?notebook=${encodeURIComponent(path)}`;
  const name = "mooringAI_" + path.replace(/[^a-z0-9]/gi, "_");
  const height = Math.min(960, window.screen?.availHeight || 900);
  const win = window.open(url, name, `popup,width=560,height=${height},left=80,top=60`);
  if (win) win.focus();
  else window.open(url, "_blank"); // popup blocked → fall back to a tab
}

// Opening a notebook may need to start the marimo editor subprocess (cold the
// first time per workspace). The hub pre-warms it in the background at startup, so
// this is usually instant — but show a progress hint in case it isn't, since the
// whole toolbar is disabled while the open POST is in flight.
async function openAction(path) {
  const summary = $("summary");
  const prev = summary.textContent;
  summary.textContent = "Starting the editor…";
  try {
    await action("/api/open", { path }, false);
  } finally {
    summary.textContent = prev;
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

function deleteAction(path, kind) {
  const name = path.split("/").pop();
  const what = kind === "project" ? `the Power BI project ${name}` : name;
  const ok = confirm(
    `Delete ${what} from your workspace?\n\n` +
    "This removes the local file(s). Push or Propose afterwards to remove it from the team repo."
  );
  if (ok) action("/api/delete", { path });
}

// Notebooks reverted this session, mapped to the undo-snapshot token /api/rollback
// returned. The token lets /api/undo refuse if a later write (e.g. an AI Apply from
// the chat window) has since landed on top of the shared undo stack, rather than
// restoring the wrong version. A row's one-shot Undo button reads this map; kept
// client-side so /api/state needn't carry per-row undo state.
const recentlyReverted = new Map();

function revertAction(path, state) {
  const name = path.split("/").pop();
  // Only a modified .py is snapshotted, hence undoable. A deleted-locally restore has
  // no prior bytes to keep, and Revert isn't offered for non-.py rows at all.
  const undoable = state === "modified" && path.endsWith(".py");
  const ok = confirm(
    `Discard your changes to ${name} and restore the last synced version?` +
    (undoable
      ? "\n\nYour current version is saved locally, so you can Undo this."
      : "\n\nThis cannot be undone.")
  );
  if (!ok) return;
  // Register the Undo affordance only once the revert succeeds AND the server returns
  // a snapshot token — so a failed revert never leaves a dead Undo button. action()'s
  // own refresh already ran by now, so re-render to surface the new button.
  action("/api/rollback", { path }).then((data) => {
    if (data && !data.error && data.undo_token) {
      recentlyReverted.set(path, data.undo_token);
      refresh();
    }
  });
}

function undoAction(path) {
  const token = recentlyReverted.get(path);
  action("/api/undo", { path, token }).then((data) => {
    // Drop the affordance and re-render only on a RESOLVED outcome — restored (ok:true)
    // or the token is dead (superseded / nothing-to-undo, both carry `ok:false`). A
    // transient failure (502, e.g. a momentarily locked file) keeps the snapshot on
    // disk for retry, so the response has no `ok` and we leave the button in place
    // (with its still-valid token, so a retry never falls back to a blind restore).
    if (data && "ok" in data) {
      recentlyReverted.delete(path);
      refresh();
    }
  });
}

function fileActions(file, opts) {
  opts = opts || {};
  const actions = [];
  if (file.state === "conflict") {
    actions.push(
      ["Use remote", () => action("/api/resolve", { path: file.path, strategy: "theirs" })],
      ["Keep both", () => action("/api/resolve", { path: file.path, strategy: "keep-both" })],
      ["Push as copy", () => action("/api/resolve", { path: file.path, strategy: "push-copy" })],
    );
  } else if (PUSH_STATES.has(file.state)) {
    actions.push(
      ["Push", () => action("/api/push", { paths: [file.path] })],
      ["Propose", () => action("/api/propose", { paths: [file.path] })],
    );
    // Revert restores the last synced version. Notebook-only: data files and Power BI
    // members aren't snapshotted (so an Undo would be a dead promise) and a lone PBIP
    // member can't be reverted without breaking the artifact — use the CLI for those.
    // "new local" has no checkpoint to go back to (that's Delete).
    if (file.path.endsWith(".py") && (file.state === "modified" || file.state === "deleted locally")) {
      actions.push(["Revert", () => revertAction(file.path, file.state)]);
    }
  }
  // A one-shot Undo for a file just reverted this session (snapshot kept server-side).
  if (recentlyReverted.has(file.path)) {
    actions.push(["Undo", () => undoAction(file.path)]);
  }
  // has_local is server truth (the file exists on disk); some states such as a
  // remote-deleted conflict have no local file, so Open/Delete must not appear.
  const openable = file.path.endsWith(".py") || file.path.endsWith(".pbip");
  if (openable && file.has_local) {
    actions.push(["Open", () => openAction(file.path)]);
  }
  // AI copilot pops out into its own window (not a tab) so it can sit beside the
  // notebook. One window per notebook; clicking again focuses the existing one.
  // A notebook can be opted out of AI (synced mooring.toml) — when it is, the open
  // button is hidden and the toggle offers to turn it back on. The toggle is the
  // off switch for "this notebook now handles PII; don't let AI touch it by mistake".
  if (aiChatEnabled && file.path.endsWith(".py") && file.has_local) {
    if (!file.ai_disabled) actions.push(["AI", () => openChatWindow(file.path)]);
    const label = file.ai_disabled ? "Enable AI" : "Disable AI";
    actions.push([label, () =>
      action("/api/ai/notebook/toggle", { notebook: file.path, disabled: !file.ai_disabled })]);
  }
  // Delete is suppressed on PBIP member rows (opts.member): a project is only
  // deleted whole, via its header, since removing one member would leave a
  // structurally broken artifact.
  if (file.has_local && !opts.member) {
    actions.push(["Delete", () => deleteAction(file.path)]);
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
    actionsTd.append(btn, " ");
  }

  tr.append(pathTd, stateTd, actionsTd);
  return tr;
}

function buildFileRow(file, opts) {
  return buildRow(file.path, file.state, fileActions(file, opts));
}

function buildArtifactRows(artifact, files) {
  const byPath = new Map(files.map((f) => [f.path, f]));
  const memberRows = artifact.members
    .map((path) => byPath.get(path))
    .filter(Boolean)
    .map((file) => {
      const row = buildFileRow(file, { member: true });
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
      return f && PUSH_STATES.has(f.state);
    });
    actions.push(
      ["Push", () => pushAction(paths, paths.length)],
      ["Propose", () => proposeAction(paths, paths.length)],
    );
  }
  const pointer = byPath.get(artifact.pointer);
  if (pointer?.has_local) {
    actions.push(["Open", () => action("/api/open", { path: artifact.pointer }, false)]);
    actions.push(["Delete", () => deleteAction(artifact.pointer, "project")]);
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
  if (state.ui_theme) {
    applyTheme(state.ui_theme);
    $("theme-select").value = state.ui_theme;
  }
  // Local mode (no repo configured): the notebook surface is usable without a
  // login; only sync needs a repo. The server reports state.mode === "local".
  const localMode = state.mode === "local";
  const showFiles = state.logged_in || localMode;

  const hostSuffix = state.host && state.host !== "github.com" ? ` · ${state.host}` : "";
  $("repo-info").textContent = state.repo
    ? `${state.repo} @ ${state.branch}${hostSuffix}`
    : (localMode ? "Local workspace — not connected to a repo" : "");
  $("workspace-info").textContent = `Workspace: ${state.workspace}`;
  const hint = $("workspace-hint");
  hint.textContent = state.workspace_hint || "";
  hint.classList.toggle("hidden", !state.workspace_hint);
  // Notebook packages: the actively-selected deps (the repo's pyproject list, or the
  // env's top-level packages when there's no project) + how to add more.
  const env = state.env || {};
  $("env-summary").textContent = env.summary || "";
  const pkgs = env.packages || [];
  $("packages").textContent = pkgs.length
    ? pkgs.join("\n")
    : "(no packages selected yet)";
  $("env-add-hint").textContent = env.add_hint || "";
  aiChatEnabled = !!state.ai_chat;

  renderRepoSelect(state);
  // The connect-repo form opens on demand — the header "Connect a repo" button in
  // local mode, or the switcher's "+ Add repo…" when configured — so it's never
  // forced on a local-only user who has no intention of connecting a repo.
  $("setup-card").classList.toggle("hidden", !showAddRepo);
  $("setup-client-id-label").classList.toggle("hidden", state.configured);
  $("setup-host-label").classList.toggle("hidden", state.configured);
  $("setup-cancel").classList.toggle("hidden", !showAddRepo);
  $("setup-intro").classList.toggle("hidden", state.configured);
  // The header button is the local-mode entry to the form; when a repo is configured
  // the switcher's "+ Add repo…" handles it, and while the form is open it's redundant.
  $("connect-repo").classList.toggle("hidden", state.configured || showAddRepo);
  $("login-card").classList.toggle("hidden", !state.configured || state.logged_in);
  $("files-card").classList.toggle("hidden", !showFiles);
  // Copilot sign-in menu: shown wherever the notebook surface is usable (local mode
  // or logged in) and AI is enabled. Copilot's sign-in is independent of the GitHub
  // login, so it lives in its own header dropdown rather than taking up a card the
  // user has to scroll past. Status is fetched cached (no CLI spawn).
  const showCopilot = aiChatEnabled && showFiles;
  const copilotMenu = $("copilot-menu");
  copilotMenu.classList.toggle("hidden", !showCopilot);
  if (showCopilot) {
    refreshCopilotStatus(false);
  } else {
    copilotMenu.open = false; // don't leave the dropdown open when it's hidden
  }

  // Pull / Push all / Propose only make sense against a connected, logged-in repo.
  // In local mode the notebooks are usable but there's nothing to sync to, so hide
  // those controls (the per-file rows already omit Push/Propose for "local" files).
  for (const id of ["btn-pull", "btn-push", "btn-propose"]) {
    $(id).classList.toggle("hidden", !state.logged_in);
  }
  // Workspace-level "Batch build" — only when the opt-in orchestrator is enabled.
  $("btn-batch").classList.toggle("hidden", !state.ai_batch);
  // No team Pull in local mode, so don't dangle it in the empty-state hint.
  $("empty-hint").innerHTML = localMode
    ? "No notebooks yet &mdash; click <b>New notebook</b> to create one."
    : "No notebooks yet &mdash; click <b>New notebook</b> to create one, or <b>Pull</b> to " +
      "fetch your team's notebooks.";

  if (state.logged_in) {
    const userInfo = $("user-info");
    userInfo.innerHTML = "";
    const profile = document.createElement("a");
    profile.href = "/settings";
    profile.className = "profile-link";
    profile.title = "Settings & preferences";
    profile.textContent = `@${state.user}`;
    userInfo.append(profile, " ");
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
  } else {
    $("user-info").textContent = "";
    $("summary").textContent = "";
    renderReviewBanner(null);
  }
  if (showFiles) {
    lastFiles = state.files || [];
    renderFiles(lastFiles, state.artifacts || []);
  } else {
    lastFiles = [];  // no file surface (login wall) — don't leave stale push/propose targets
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

// -- Copilot sign-in (separate from the GitHub login) -----------------------
// GitHub Copilot signs in independently of mooring's GitHub login (different
// OAuth flow, different credential store, possibly a different account). This
// card surfaces that sign-in + which account is connected, so the user never has
// to drop to `mooring ai login` in a terminal.

function renderCopilotStatus(s) {
  const statusEl = $("copilot-status");
  const connectBtn = $("copilot-connect");
  const switchBtn = $("copilot-switch");
  if (!s || s.available === false) {
    statusEl.textContent =
      s?.detail ||
      "GitHub Copilot isn't available in this build (install the mooring[copilot] extra).";
    connectBtn.classList.add("hidden");
    switchBtn.classList.add("hidden");
    return;
  }
  if (!s.checked) {
    statusEl.textContent = "Sign-in status not checked yet.";
    connectBtn.textContent = "Sign in to Copilot";
    connectBtn.classList.remove("hidden");
    switchBtn.classList.add("hidden");
    return;
  }
  if (s.connected) {
    statusEl.textContent = s.account ? `Signed in as @${s.account}.` : "Signed in.";
    connectBtn.classList.add("hidden");
    switchBtn.classList.remove("hidden");
  } else {
    statusEl.textContent = "Not signed in to Copilot.";
    connectBtn.textContent = "Sign in to Copilot";
    connectBtn.classList.remove("hidden");
    switchBtn.classList.add("hidden");
  }
}

// probe=false uses the cached status (no CLI spawn — safe on every refresh);
// probe=true forces a real check (spawns the Copilot CLI, ~tens of seconds).
async function refreshCopilotStatus(probe) {
  try {
    const data = await api("/api/ai/status" + (probe ? "?probe=1" : ""));
    if (data.enabled === false) return; // AI disabled — the card stays hidden
    renderCopilotStatus(data);
  } catch {
    // A cached-status probe failing is non-fatal; leave the card as-is.
  }
}

async function startCopilotLogin() {
  const note = $("copilot-note");
  $("copilot-connect").disabled = true;
  $("copilot-switch").disabled = true;
  note.textContent = "Opening a browser to sign in to Copilot…";
  const data = await api("/api/ai/login/start", {});
  if (data.error) {
    $("copilot-connect").disabled = false;
    $("copilot-switch").disabled = false;
    note.textContent = "";
    showError(data.error);
    return;
  }
  note.textContent = "Waiting for you to authorize in the browser…";
  pollCopilotLogin();
}

async function pollCopilotLogin() {
  const note = $("copilot-note");
  const data = await api("/api/ai/login/poll");
  if (data.status === "ok") {
    note.textContent = data.account ? `Signed in as @${data.account}.` : "Signed in to Copilot.";
    $("copilot-connect").disabled = false;
    $("copilot-switch").disabled = false;
    await refreshCopilotStatus(false);
    return;
  }
  if (data.status === "error") {
    $("copilot-connect").disabled = false;
    $("copilot-switch").disabled = false;
    note.textContent = "";
    showError(data.detail || "Copilot sign-in didn't complete.");
    return;
  }
  setTimeout(pollCopilotLogin, 2500); // still pending — keep polling
}

// Native <details> stays open until its summary is clicked again; close it when
// the user clicks anywhere outside the dropdown, the way a header menu should.
document.addEventListener("click", (e) => {
  const menu = $("copilot-menu");
  if (menu.open && !menu.contains(e.target)) menu.open = false;
});

$("copilot-connect").addEventListener("click", startCopilotLogin);
$("copilot-switch").addEventListener("click", startCopilotLogin);
$("copilot-check").addEventListener("click", () => {
  $("copilot-note").textContent = "Checking…";
  refreshCopilotStatus(true).then(() => {
    $("copilot-note").textContent = "";
  });
});

$("login-start").addEventListener("click", startLogin);
$("btn-refresh").addEventListener("click", refresh);
$("btn-pull").addEventListener("click", () => action("/api/pull", {}));
$("btn-push").addEventListener("click", () => {
  const count = lastFiles.filter((f) => PUSH_STATES.has(f.state)).length;
  return pushAction(null, count);
});
$("btn-propose").addEventListener("click", () => {
  const count = lastFiles.filter((f) => PUSH_STATES.has(f.state)).length;
  return proposeAction(null, count);
});
$("btn-new").addEventListener("click", () => {
  const name = prompt("Notebook name (e.g. sales-analysis):");
  if (name) action("/api/new", { name });
});
// Batch build opens the workspace-level page in its own (reused) tab, beside the hub.
$("btn-batch").addEventListener("click", () => {
  window.open("/ai/batch", "mooringBatch");
});
$("connect-repo").addEventListener("click", () => {
  showAddRepo = true;
  refresh();  // reveals #setup-card (and Cancel) and hides the button
});
$("repo-select").addEventListener("change", (event) => {
  const alias = event.target.value;
  if (alias === "__add__") {
    showAddRepo = true;
    refresh();
    return;
  }
  action("/api/repo/switch", { alias });
});
$("setup-save").addEventListener("click", async () => {
  // Close the form only on success: the card is now gated solely on showAddRepo, so
  // resetting it before the request would hide the form (and the user's input) on a
  // validation error (e.g. a bad host). Mirrors action()'s busy/refresh handling.
  if (busy) return;
  setBusy(true);
  showError("");
  try {
    const data = await api("/api/setup", {
      client_id: $("setup-client-id").value,
      host: $("setup-host").value,
      owner: $("setup-owner").value,
      repo: $("setup-repo").value,
      branch: $("setup-branch").value,
      alias: $("setup-alias").value,
    });
    if (data.error) {
      showError(data.error);  // leave the form open so the values can be corrected
      return;
    }
    showAddRepo = false;
    await refresh();
  } finally {
    setBusy(false);
  }
});
$("setup-cancel").addEventListener("click", () => {
  showAddRepo = false;
  refresh();
});

// Appearance toggle: apply locally at once, then persist server-side (which
// also re-themes the notebooks' .marimo.toml). Deliberately not routed through
// action(): an appearance change shouldn't disable the toolbar or refresh.
$("theme-select").addEventListener("change", async (event) => {
  const theme = event.target.value;
  applyTheme(theme);
  const data = await api("/api/ui/theme", { theme });
  if (data.error) showError(data.error);
});

// Another same-origin window (the open chat) changed the theme — follow it.
window.addEventListener("storage", (event) => {
  if (event.key === LS_THEME && event.newValue) {
    applyTheme(event.newValue);
    $("theme-select").value = event.newValue;
  }
});

// Match the toggle to the theme the pre-paint script already applied, so it's
// never momentarily out of sync; refresh() reconciles with the server value.
$("theme-select").value = document.documentElement.dataset.theme || "system";

refresh();
