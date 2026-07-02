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
// When the last /api/state landed (client clock) and whether it was logged in —
// the freshness banner's inputs. There is no server-side "last refreshed" time:
// /api/state recomputes live against GitHub, so freshness is a property of this
// open tab, not of the workspace.
let lastStateAt = null;
let lastLoggedIn = false;
const FOCUS_REFRESH_THROTTLE_MS = 60_000;

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
async function doOpen(path) {
  const summary = $("summary");
  const prev = summary.textContent;
  summary.textContent = "Starting the editor…";
  try {
    await action("/api/open", { path }, false);
  } finally {
    summary.textContent = prev;
  }
}

// Files the user chose to open stale this session ("Open my copy anyway"), mapped
// to the remote marker at the time (Freshness.dismissKey). The dialog re-arms only
// when the remote moves AGAIN — a user who decided to diverge isn't nagged per open.
const staleDismissed = new Map();

// Whether the branch head still matches the last-rendered /api/state. Timeboxed
// and advisory: any error, timeout, or offline answers "fresh" so Open is NEVER
// blocked by a slow or unreachable GitHub — the dialog is prevention, not a gate.
async function isStateFresh() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2000);
  try {
    const resp = await fetch("/api/freshness", { signal: ctrl.signal });
    const data = await resp.json();
    return data.fresh !== false;
  } catch {
    return true;
  } finally {
    clearTimeout(timer);
  }
}

// Per-state dialog copy. "pull" is the happy path (pull first, then open);
// a remote DELETION must not offer "Pull latest and open" (pull would remove the
// local copy); a conflict points at the row's resolve actions (pull skips it).
const STALE_COPY = {
  pull: (name) =>
    `A teammate updated ${name} after your last pull. Editing your copy now ` +
    "will end in a conflict at push time.",
  deleted: (name) =>
    `A teammate deleted ${name} from the team repo. Pulling would remove your ` +
    "local copy; opening keeps your version (Push it to restore it for the team).",
  conflict: (name) =>
    `${name} is conflicted — your copy and a teammate's version both changed. ` +
    "Resolve it from the row's actions (Use remote / Keep both / Push as copy); " +
    "you can still open your copy to look.",
};

function showStaleDialog(file, kind) {
  const dialog = $("stale-dialog");
  const name = file.path.split("/").pop();
  $("stale-message").textContent = STALE_COPY[kind](name);
  const pullBtn = $("stale-pull");
  pullBtn.classList.toggle("hidden", kind !== "pull");
  pullBtn.onclick = async () => {
    dialog.close();
    await action("/api/pull", {});
    doOpen(file.path);
  };
  $("stale-open").onclick = () => {
    dialog.close();
    staleDismissed.set(file.path, Freshness.dismissKey(file));
    doOpen(file.path);
  };
  $("stale-cancel").onclick = () => dialog.close();
  dialog.showModal();
  // Safe default focus: never "Open my copy anyway" (the actionsMenu lesson —
  // no control where a stray keypress fires the risky choice).
  (kind === "pull" ? pullBtn : $("stale-cancel")).focus();
}

// Open, guarded: warn at the moment of choice when the remote moved under this
// file (remote changed / deleted remotely / conflict) instead of letting the
// user discover it as a blocked push two hours later. The check is advisory and
// client-side only — /api/open itself gates nothing new.
async function openAction(path) {
  let file = lastFiles.find((f) => f.path === path);
  // The dialog decision is only as good as the cached rows: if the branch head
  // moved since the last /api/state, re-render first (timeboxed; see isStateFresh).
  if (file && !(await isStateFresh())) {
    await refresh();
    file = lastFiles.find((f) => f.path === path);
    if (!file) return; // the row vanished with the fresh state — nothing to open
  }
  const kind = Freshness.warnState(file, staleDismissed);
  if (kind) return showStaleDialog(file, kind);
  return doOpen(path);
}

// A plain helper module (a non-marimo .py) can't open in the marimo editor — that
// would rewrite it into notebook form. Reveal it in the OS file manager so the user
// edits it in their own editor; the change then syncs/pushes like any other file.
function revealAction(path) {
  return action("/api/reveal", { path });
}

// Open an external URL (e.g. GitHub) in a new tab, severing window.opener so the
// opened page can't navigate this hub tab (external-site hygiene).
function openExternal(url) {
  const win = window.open(url, "_blank");
  if (win) win.opener = null;
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
  // A .py is openable only when it's a real marimo notebook (server-sniffed
  // is_notebook): a plain helper module must NOT be opened in the editor, which
  // would rewrite it into notebook form on save (the server also refuses).
  const isNotebook = file.path.endsWith(".py") && file.is_notebook === true;
  const openable = isNotebook || file.path.endsWith(".pbip");
  if (openable && file.has_local) {
    actions.push(["Open", () => openAction(file.path)]);
  }
  // A plain helper module (non-marimo .py) can't open in marimo (it would be rewritten
  // into notebook form), so instead of Open it gets Reveal — open it in the file manager
  // to edit in your own editor. Edits still sync/push like any other file.
  if (file.is_module && file.has_local) {
    actions.push(["Reveal", () => revealAction(file.path)]);
  }
  // "View on GitHub" opens the file's blob page on the remote branch in a new tab. The
  // server sets github_url only for files that exist on the remote (any file type), so
  // this shows the REMOTE version — which can differ from unpushed local edits.
  if (file.github_url) {
    actions.push(["View on GitHub", () => openExternal(file.github_url)]);
  }
  // AI copilot pops out into its own window (not a tab) so it can sit beside the
  // notebook. One window per notebook; clicking again focuses the existing one.
  // A notebook can be opted out of AI (synced mooring.toml) — when it is, the open
  // button is hidden and the toggle offers to turn it back on. The toggle is the
  // off switch for "this notebook now handles PII; don't let AI touch it by mistake".
  // Modules (non-notebook .py) get no AI: the copilot operates on notebooks.
  if (aiChatEnabled && isNotebook && file.has_local) {
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

// Collapse a row's actions into ONE compact "Actions ▾" dropdown instead of a wall of
// small buttons (a busy row — a modified, remote-existing notebook with AI on — can
// offer up to ~8). Built as a <details> disclosure (the same idiom as the header
// Copilot menu), deliberately NOT a native <select>: a <select> used as an ACTION menu
// is a footgun — on Windows a focused, closed <select> fires 'change' on a single Arrow
// keypress, so merely browsing it would run actions[0] (Push, or a conflict "Use remote"
// that silently discards local edits) with no confirm. Here each action is a real
// <button> that fires ONLY on an explicit click/Enter, and setBusy() disables them all
// during a sync. The [text, handler] pairs are exactly what the buttons carried before.
function actionsMenu(actions, label) {
  const details = document.createElement("details");
  details.className = "row-menu";

  const summary = document.createElement("summary");
  summary.className = "row-menu-summary";
  summary.textContent = "Actions";
  summary.setAttribute("aria-label", label ? `Actions for ${label}` : "File actions");
  details.appendChild(summary);

  const panel = document.createElement("div");
  panel.className = "row-menu-panel";
  for (const [text, handler] of actions) {
    const btn = document.createElement("button");
    btn.className = "row-menu-item";
    btn.textContent = text;
    btn.addEventListener("click", () => {
      details.open = false; // close the menu first, then run the action
      handler();
    });
    panel.appendChild(btn);
  }
  details.appendChild(panel);
  return details;
}

function buildRow(pathCell, state, actions, label) {
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
  if (actions.length) actionsTd.appendChild(actionsMenu(actions, label));

  tr.append(pathTd, stateTd, actionsTd);
  return tr;
}

function shadowBadge(name) {
  const span = document.createElement("span");
  span.className = "badge warn";
  span.textContent = `shadows ${name}`;
  span.title =
    `“import ${name}” would load this notebook instead of the ${name} module — ` +
    "rename it; otherwise every notebook in this folder can fail to import.";
  return span;
}

function moduleBadge() {
  const span = document.createElement("span");
  span.className = "badge module";
  span.textContent = "module";
  span.title =
    "A Python module imported by notebooks — not a runnable marimo notebook, so it " +
    "isn't opened in the editor (the workspace root is on the notebook's import path). " +
    "Use Reveal to open it in your own editor.";
  return span;
}

function buildFileRow(file, opts) {
  opts = opts || {};
  // Inside a folder section the row shows its folder-relative path (`rel`); elsewhere
  // the full path. A notebook whose filename shadows an importable package (e.g.
  // polars.py) gets an amber badge so the sys.path[0] trap is visible before it
  // becomes a kernel traceback; a plain helper module gets a "module" badge.
  const display = opts.rel && file.rel != null ? file.rel : file.path;
  const extras = [];
  if (file.shadows) extras.push(" ", shadowBadge(file.shadows));
  if (file.is_module) extras.push(" ", moduleBadge());
  const pathCell = extras.length ? [display, ...extras] : display;
  return buildRow(pathCell, file.state, fileActions(file, opts), file.path);
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

  const header = buildRow([caret, name, detail], artifact.state, actions, artifact.name);
  return [header, ...memberRows];
}

// Create a notebook inside a specific folder (the section's "New here" button).
function newNotebookIn(folder) {
  const name = prompt(`Notebook name in ${folder}/\n(e.g. sales-analysis):`);
  if (name) action("/api/new", { name: `${folder}/${name}` });
}

// A collapsible folder section: a header row (caret + name + count) and the file rows
// under it. Reuses the PBIP caret/collapse pattern. An empty DECLARED folder still
// renders — "here's where notebooks go" — with a disabled caret and a New-here button.
function buildFolderSection(section) {
  const memberRows = section.files.map((file) => {
    const row = buildFileRow(file, { rel: true });
    row.classList.add("member");
    return row;
  });

  const caret = document.createElement("button");
  caret.className = "small caret";
  if (section.empty) {
    caret.textContent = "·";
    caret.disabled = true;
  } else {
    caret.textContent = "▾";
    caret.addEventListener("click", () => {
      const open = caret.textContent === "▾";
      caret.textContent = open ? "▸" : "▾";
      memberRows.forEach((row) => row.classList.toggle("hidden", open));
    });
  }

  const name = document.createElement("b");
  name.textContent = section.folder === "" ? " repo root " : ` ${section.folder}/ `;
  const detail = document.createElement("span");
  detail.className = "muted";
  detail.textContent = section.empty ? "— empty" : `— ${section.files.length} file(s)`;

  const tr = document.createElement("tr");
  tr.className = "folder-header";
  const headTd = document.createElement("td");
  headTd.className = "path";
  headTd.colSpan = 2;
  headTd.append(caret, name, detail);
  const actionsTd = document.createElement("td");
  if (section.folder !== "") {
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = "New here";
    btn.title = `Create a notebook in ${section.folder}/`;
    btn.addEventListener("click", () => newNotebookIn(section.folder));
    actionsTd.append(btn);
  }
  tr.append(headTd, actionsTd);
  return [tr, ...memberRows];
}

function renderFiles(files, artifacts, declaredFolders) {
  const tbody = $("files-table").querySelector("tbody");
  tbody.innerHTML = "";
  // PBIP artifacts keep their own collapsible grouping; the rest group by folder so the
  // structure (incl. an adopted/declared folder that is still empty) is visible.
  const nonArtifact = files.filter((f) => !f.artifact);
  const sections = FilesTree.group(nonArtifact, declaredFolders || []);
  const hasRows = files.length > 0 || sections.length > 0;
  $("files-table").classList.toggle("hidden", !hasRows);
  // The empty-hint and the table are mutually exclusive: declared folders seed empty
  // folder sections (each with its own "New here"), so once any row renders the hint
  // would just duplicate that nudge — show it only when there's truly nothing.
  $("empty-hint").classList.toggle("hidden", hasRows);
  for (const artifact of artifacts) {
    for (const row of buildArtifactRows(artifact, files)) tbody.appendChild(row);
  }
  for (const section of sections) {
    for (const row of buildFolderSection(section)) tbody.appendChild(row);
  }
}

// "Last checked 3 hours ago — 2 teammate update(s) waiting. Refresh". Quiet by
// design: hidden unless updates are waiting or the view is old enough (>= 30 min)
// that its numbers shouldn't be trusted. Re-rendered on an interval so the age
// stays honest while the tab sits open (text only — no network).
function renderFreshnessBanner() {
  const banner = $("freshness-banner");
  const waiting = Freshness.pullCount(lastFiles);
  const age = lastStateAt == null ? null : Date.now() - lastStateAt;
  const show = lastLoggedIn && age != null && (waiting > 0 || age >= 30 * 60_000);
  banner.classList.toggle("hidden", !show);
  if (!show) return;
  banner.innerHTML = "";
  banner.append(
    waiting > 0
      ? `Last checked ${Freshness.ageText(age)} — ${waiting} teammate update(s) waiting. `
      : `Last checked ${Freshness.ageText(age)}. `,
  );
  const btn = document.createElement("button");
  btn.className = "small";
  btn.textContent = "Refresh";
  btn.addEventListener("click", refresh);
  banner.appendChild(btn);
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

// The repo identity discovery last ran for. Discovery costs a full-tree fetch on
// the server, so we run it once per repo-session (on login / repo switch), NOT on
// every refresh() — and force a re-check (null) after an adopt so the banner clears.
let lastDiscoverRepo = null;

async function maybeDiscover(state) {
  const banner = $("adopt-banner");
  if (!state.logged_in) {
    banner.classList.add("hidden");
    lastDiscoverRepo = null;
    return;
  }
  if (state.repo === lastDiscoverRepo) return;  // already checked this repo this session
  lastDiscoverRepo = state.repo;
  try {
    const data = await api("/api/discover");
    renderAdoptBanner(data.candidates || []);
  } catch {
    banner.classList.add("hidden");  // discovery is a non-essential prompt; never block
  }
}

function renderAdoptBanner(candidates) {
  const banner = $("adopt-banner");
  banner.innerHTML = "";
  banner.classList.toggle("hidden", candidates.length === 0);
  if (!candidates.length) return;

  const names = candidates.map((c) => c.folder);
  const summary = document.createElement("div");
  // Discovery surfaces any folder with syncable files, which can include data-only
  // folders (0 .py) — so the copy says "files", not "Python files" (the per-folder
  // button shows each folder's .py count for the notebook signal).
  summary.append(
    `Found ${candidates.length} folder(s) with files outside your synced folders: `,
  );
  names.forEach((name, i) => {
    const b = document.createElement("b");
    b.textContent = name;
    summary.append(b, i < names.length - 1 ? ", " : ". ");
  });
  summary.append("Adopt them to sync their notebooks (and helper modules) for the team.");

  const actions = document.createElement("div");
  actions.className = "adopt-actions";
  for (const c of candidates) {
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = `Adopt ${c.folder} (${c.py_files} .py)`;
    btn.addEventListener("click", () => adoptFolders([c.folder]));
    actions.append(btn, " ");
  }
  if (candidates.length > 1) {
    const all = document.createElement("button");
    all.className = "small primary";
    all.textContent = "Adopt all";
    all.addEventListener("click", () => adoptFolders(names));
    actions.append(all);
  }
  banner.append(summary, actions);
}

function adoptFolders(folders) {
  // Force a re-check after the adopt (action() refreshes) so the banner reflects the
  // now-narrowed candidate set — the adopted folders drop out, leaving any others.
  lastDiscoverRepo = null;
  return action("/api/adopt", { folders });
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
  lastStateAt = Date.now();
  lastLoggedIn = !!state.logged_in;
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
    renderFiles(lastFiles, state.artifacts || [], state.folders || []);
  } else {
    lastFiles = [];  // no file surface (login wall) — don't leave stale push/propose targets
  }
  renderFreshnessBanner();
  // Prompt to adopt any notebook folders the repo keeps outside the synced folders.
  // Runs once per repo-session (see maybeDiscover), so it never rides the refresh loop.
  await maybeDiscover(state);
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
  const authzEl = $("copilot-authz");
  // "Signed in, but this account isn't authorized for Copilot" (e.g. a 403). Shown
  // whenever the provider reported it, with Switch account offered as the fix.
  authzEl.classList.toggle("hidden", !(s && s.authz_error));
  if (s && s.authz_error) {
    authzEl.textContent = s.authz_error;
    switchBtn.classList.remove("hidden");
  }
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
  $("copilot-code-box").classList.add("hidden"); // reset any prior code
  $("copilot-code").textContent = "";
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
    $("copilot-code-box").classList.add("hidden");
    note.textContent = data.account ? `Signed in as @${data.account}.` : "Signed in to Copilot.";
    $("copilot-connect").disabled = false;
    $("copilot-switch").disabled = false;
    await refreshCopilotStatus(false);
    return;
  }
  if (data.status === "error") {
    $("copilot-code-box").classList.add("hidden");
    $("copilot-connect").disabled = false;
    $("copilot-switch").disabled = false;
    note.textContent = "";
    showError(data.detail || "Copilot sign-in didn't complete.");
    return;
  }
  // Still pending: surface the device code `copilot login` printed. The CLI's own
  // clipboard copy often fails and a switch-account flow needs the code typed in,
  // so without this the device page is unusable (the original bug).
  const login = ChatCore.parseDeviceLogin(data.output);
  if (login.code) {
    note.textContent = "Waiting for you to authorize in the browser…";
    $("copilot-code").textContent = login.code;
    if (login.url) {
      $("copilot-link").href = login.url;
      $("copilot-link").textContent = login.url.replace(/^https?:\/\//, "");
    }
    $("copilot-code-box").classList.remove("hidden");
  }
  setTimeout(pollCopilotLogin, 2500); // still pending — keep polling
}

// A native <details> stays open until its summary is clicked again; close any open
// menu when the user clicks outside it, the way a menu should. This covers the header
// Copilot menu and every per-row actions menu — and, because clicking one row's summary
// runs here too, opening a menu closes any other row menu that was left open.
document.addEventListener("click", (e) => {
  const copilot = $("copilot-menu");
  if (copilot.open && !copilot.contains(e.target)) copilot.open = false;
  for (const menu of document.querySelectorAll("details.row-menu[open]")) {
    if (!menu.contains(e.target)) menu.open = false;
  }
});

$("copilot-connect").addEventListener("click", startCopilotLogin);
$("copilot-switch").addEventListener("click", startCopilotLogin);
$("copilot-copy").addEventListener("click", () => {
  const code = $("copilot-code").textContent;
  const btn = $("copilot-copy");
  if (code && navigator.clipboard) {
    navigator.clipboard.writeText(code).then(
      () => { btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy code"; }, 1500); },
      () => { /* clipboard blocked — the code is visible to copy by hand */ },
    );
  }
});
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
  const name = prompt(
    "Notebook name or path\n(e.g. sales-analysis, or packages/finance/notebooks/sales):",
  );
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

// An idle tab heals itself: refresh when the tab regains focus and the last
// check is older than the throttle, so the staleness dialog and banner decide
// from reasonably fresh rows without riding a polling loop or the rate limit.
function maybeFocusRefresh() {
  if (document.visibilityState !== "visible" || busy) return;
  if (Freshness.shouldAutoRefresh(lastStateAt, Date.now(), FOCUS_REFRESH_THROTTLE_MS)) refresh();
}
window.addEventListener("focus", maybeFocusRefresh);
document.addEventListener("visibilitychange", maybeFocusRefresh);
// Keep the banner's age text honest while the tab sits open (no network).
setInterval(renderFreshnessBanner, 60_000);

refresh();
