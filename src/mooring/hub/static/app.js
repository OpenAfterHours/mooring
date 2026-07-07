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
const PULL_STATES = new Set(["remote changed", "new remote", "deleted remotely"]);

// Appearance lives in the shared theme.js module (loaded before this file): it
// owns applyTheme + the localStorage key and installs the cross-tab `storage`
// follower used by every mooring page. Alias it so the hub's call sites read as
// before; the server (/api/state) stays the source of truth.
const applyTheme = window.MooringTheme.applyTheme;

let busy = false;
let showAddRepo = false;
let lastFiles = [];
let lastArtifacts = [];
let lastFolders = [];
let lastReview = null;
// The catalog search box's current text — filters the file listing client-side. Kept
// across /api/state re-renders so a poll doesn't clear an in-progress filter.
let fileQuery = "";
let aiChatEnabled = false;
// When the last /api/state landed (client clock) and whether it was logged in —
// the freshness banner's inputs. There is no server-side "last refreshed" time:
// /api/state recomputes live against GitHub, so freshness is a property of this
// open tab, not of the workspace.
let lastStateAt = null;
let lastLoggedIn = false;
// GitHub is unreachable: /api/state carried an `offline` payload and the rows
// are the last OBSERVED sync state. Network actions (pull/push/propose/resolve/
// review/history/discard/recall/what's-new) hide behind the amber banner;
// local work (Open/Reveal/Undo/Delete/Duplicate/AI) stays live.
let offlineMode = false;
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
  // The <pre> is plain text; the PR link needs a real anchor. When mooring opened the
  // PR (Slice 2), link straight to it; otherwise fall back to the compare page.
  const linkBox = $("log-link");
  const link = data.pull_url || data.compare_url;
  linkBox.classList.toggle("hidden", !link);
  if (link) {
    const a = linkBox.querySelector("a");
    a.href = link;
    a.textContent = data.pull_url
      ? `View pull request #${data.pull_number} on GitHub ↗ (opened for you)`
      : "Create / view the pull request on GitHub ↗";
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
  let guardData = null;
  try {
    const data = await api(path, body || {});
    // The push guard withheld file(s): not an error — the confirm dialog is
    // the real UI (api() synthesized data.error from the 409). Opened AFTER
    // setBusy(false) below, or its own buttons would be disabled.
    if (GuardFmt.needsDialog(data)) {
      delete data.error;
      guardData = data;
      showLog(data);
    } else {
      if (data.error) showError(data.error);
      showLog(data);
    }
    if (data.url) window.open(data.url, "_blank");
    if (data.trashed && data.trashed.length) showUndoToast(data.trashed);
    if (refreshAfter) await refresh();
    return data;
  } finally {
    setBusy(false);
    if (guardData) showGuardDialog(guardData, path, body || {});
  }
}

// The push guard found something that looks like a secret / structured PII /
// a bulk data export in files about to publish. Flagged files were WITHHELD
// (clean files already went). Warn mode offers "Push anyway" carrying per-file
// confirm tokens — each binds the exact findings to the exact bytes, so a
// changed file or a new finding is never covered by an old confirm. Block mode
// ([guard] push = "block" in the synced mooring.toml) offers no override.
function showGuardDialog(data, apiPath, body) {
  const dialog = $("guard-dialog");
  const findings = data.guard_findings || [];
  const files = findings.length;
  $("guard-message").textContent =
    `${files} file(s) were NOT ${apiPath.includes("propose") ? "proposed" : "pushed"} — ` +
    "they contain something that looks sensitive:";
  const list = $("guard-findings");
  list.innerHTML = "";
  for (const row of GuardFmt.rows(findings)) {
    const li = document.createElement("li");
    li.textContent = row;
    list.appendChild(li);
  }
  const override = GuardFmt.canOverride(data);
  $("guard-hint").textContent = override
    ? "Remove the flagged content, or add a “mooring: push-ok” comment on a " +
      "reviewed false-positive line. Pushing anyway publishes it to everyone " +
      "with access to the repo."
    : "Your team's policy blocks pushing flagged files ([guard] push = \"block\"). " +
      "Remove the flagged content, or add a “mooring: push-ok” comment on a " +
      "reviewed false-positive line, then push again.";
  const anyway = $("guard-anyway");
  anyway.classList.toggle("hidden", !override);
  anyway.onclick = () => {
    dialog.close();
    const confirmed = Object.assign({}, body, {
      confirm_tokens: GuardFmt.allTokens(findings),
    });
    action(apiPath, confirmed).then((data) => {
      if (!data || data.error || data.needs_confirm) return;
      // The confirmed re-POST bypasses the ORIGINAL caller's .then continuation
      // (pushAction/proposeAction/reviewSend attached theirs to the first,
      // 409'd request) — re-run the success effects here, or a push completed
      // via "Push anyway" never ticks the checklist and leaves the Review panel
      // open showing a stale diff with a live "Push this file" button.
      if (apiPath === "/api/push" || apiPath === "/api/propose") checklistSet("pushed");
      if (reviewPath && (body.paths || []).includes(reviewPath)) {
        $("review-card").classList.add("hidden");
        reviewPath = null;
      }
    });
  };
  $("guard-cancel").onclick = () => dialog.close();
  dialog.showModal();
  $("guard-cancel").focus(); // the safe choice is the default
}

// "Local copy replaced — Undo": a transient toast for every pre-image the last
// operation banked in the local trash (a conflict's "Use remote", pull
// updates/removals, delete, a data-file revert). Undo restores via the
// token-exact /api/trash/restore, which refuses (409) if the file has since
// changed again — so a stale toast can never clobber newer work. The full list
// lives on the Activity page after the toast is gone.
function showUndoToast(trashed) {
  let box = $("undo-toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "undo-toasts";
    document.body.appendChild(box);
  }
  // A big pull can bank dozens of pre-images; don't flood the viewport —
  // show a few, then one summary pointing at the Trash panel (which has all).
  if (trashed.length > 4) {
    const summary = document.createElement("div");
    summary.className = "undo-toast";
    const label = document.createElement("span");
    label.textContent = `${trashed.length} local copies replaced.`;
    const link = document.createElement("a");
    link.href = "/activity";
    link.textContent = "Open Trash";
    summary.append(label, link);
    box.appendChild(summary);
    setTimeout(() => summary.remove(), 15000);
    trashed = trashed.slice(0, 3);
  }
  for (const entry of trashed) {
    const toast = document.createElement("div");
    toast.className = "undo-toast";
    const name = entry.path.split("/").pop();
    const label = document.createElement("span");
    label.textContent = `${name} — local copy replaced.`;
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = "Undo";
    btn.addEventListener("click", async () => {
      toast.remove();
      const data = await api("/api/trash/restore", { token: entry.token });
      if (data.error) showError(data.error);
      await refresh();
    });
    const close = document.createElement("button");
    close.className = "small undo-toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => toast.remove());
    toast.append(label, btn, close);
    box.appendChild(toast);
    setTimeout(() => toast.remove(), 15000);
  }
}

// Pop the copilot out into its own window (not a tab) so it sits beside the
// notebook. The window features are what make the browser open a window rather
// than a tab; a per-notebook name reuses/focuses an already-open chat window —
// keyed on the path ALONE, so "Explain" (opts.explain adds &explain=1, which
// auto-runs /explain once the session is ready) targets the same window as "AI"
// instead of spawning a second chat for the notebook.
function openChatWindow(path, opts) {
  let url = `/ai/chat?notebook=${encodeURIComponent(path)}`;
  if (opts && opts.explain) url += "&explain=1";
  if (opts && opts.review) url += "&review=1";
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
    const data = await action("/api/open", { path }, false);
    if (data && !data.error) checklistSet("opened");
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
    const pulled = await action("/api/pull", {});
    if (!pulled || pulled.error) return; // the pull failed — don't open a stale copy
    // Re-evaluate against the refreshed rows: the pull may have skipped this
    // file (it became conflicted meanwhile) — never open pretending it's fresh.
    const fresh = lastFiles.find((f) => f.path === file.path);
    if (!fresh || !fresh.has_local) return; // gone with the pull (deleted remotely)
    const still = Freshness.warnState(fresh, staleDismissed);
    if (still) return showStaleDialog(fresh, still);
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

// Deliver: render this notebook to a self-contained HTML snapshot (code hidden) in
// the local .mooring/outbox and reveal/open it — a thing you can email a stakeholder
// who won't open marimo. Executes locally; the artifact embeds values but lives in
// .mooring, which sync excludes, so it is never pushed. The server also opens it for
// preview, so refresh is unnecessary.
function deliverAction(path) {
  return action("/api/deliver", { path }, false);
}

// Verify: smoke-run this notebook once on your machine and record whether it ran clean
// (the trust badge). Runs in the real environment; nothing is committed and no value
// leaves the machine — the receipt is a boolean keyed to the file's content, so the
// badge auto-clears the moment you edit the notebook. Refresh after so the badge shows.
function verifyAction(path) {
  return action("/api/verify", { path });
}

// A safe playground: byte-copy this notebook to a personal {stem}-{login}-draft.py
// sibling. To the three-way engine the draft is just a new local file — it can never
// conflict with the team file and is only shared by an explicit push. The response's
// url auto-opens the copy in the editor (action() handles it).
function duplicateAction(path) {
  return action("/api/duplicate", { path }).then((data) => {
    if (data && !data.error) checklistSet("duplicated");
    return data;
  });
}

// Open an external URL (e.g. GitHub) in a new tab, severing window.opener so the
// opened page can't navigate this hub tab (external-site hygiene).
function openExternal(url) {
  const win = window.open(url, "_blank");
  if (win) win.opener = null;
}

// The contents API is throttled to ~1 file/s; tell the user a long push is alive.
// A push guard 409 (needs_confirm) means nothing sensitive went yet, so it never
// ticks the checklist's push item — only a clean success does.
function pushAction(paths, count) {
  if (count > 3) $("summary").textContent = `Pushing ${count} file(s)… (~${Math.ceil(count * 0.8)}s)`;
  return action("/api/push", paths ? { paths } : {}).then((data) => {
    if (data && !data.error && !data.needs_confirm) checklistSet("pushed");
    return data;
  });
}

function proposeAction(paths, count) {
  if (count > 3) $("summary").textContent = `Proposing ${count} file(s)… (~${Math.ceil(count * 0.8)}s)`;
  return action("/api/propose", paths ? { paths } : {}).then((data) => {
    if (data && !data.error && !data.needs_confirm) checklistSet("pushed");
    return data;
  });
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

// -- version history (the git-free time machine) ----------------------------

let historyPath = null;
let historyPage = 1;

async function historyAction(path, page) {
  const target = page || 1;
  const data = await api(
    `/api/history?path=${encodeURIComponent(path)}&page=${target}`,
  );
  if (data.error) return showError(data.error);
  // Commit the panel state only on success, so a failed "Show older" retries
  // the SAME page instead of silently skipping one.
  historyPath = path;
  historyPage = target;
  renderHistory(path, data.versions || [], target);
}

async function viewVersion(path, sha, mode) {
  const data = await api(
    `/api/history/file?path=${encodeURIComponent(path)}&at=${encodeURIComponent(sha)}`,
  );
  if (data.error) return showError(data.error);
  const view = $("history-view");
  view.textContent = mode === "diff"
    ? (data.diff || "(no differences against your current copy)")
    : data.source;
  view.classList.remove("hidden");
}

async function restoreVersion(path, sha, asCopy) {
  if (!asCopy) {
    const ok = confirm(
      `Replace your current ${path.split("/").pop()} with the version from ` +
      `${sha.slice(0, 7)}?\n\n` +
      "Your current bytes are saved first, so this is undoable. The restored " +
      "file stays LOCAL until you push it — and pushing a version older than " +
      "your last pull replaces newer team work on purpose. Old code may also " +
      "not run under the repo's current packages."
    );
    if (!ok) return;
  }
  const data = await action("/api/restore", { path, at: sha, copy: !!asCopy });
  if (data && !data.error && data.undo_token) {
    recentlyReverted.set(path, data.undo_token);
    refresh();
  }
}

function renderHistory(path, versions, page) {
  const card = $("history-card");
  card.classList.remove("hidden");
  $("history-title").textContent = `History — ${path}`;
  $("history-view").classList.add("hidden");
  const tbody = $("history-table").querySelector("tbody");
  if (page === 1) tbody.innerHTML = "";
  for (const v of versions) {
    const tr = document.createElement("tr");
    const label = document.createElement("td");
    label.className = "path";
    label.textContent = HistoryFmt.versionLabel(v);
    const actionsTd = document.createElement("td");
    const acts = [
      ["View", () => viewVersion(path, v.sha)],
      ["Diff", () => viewVersion(path, v.sha, "diff")],
      ["Restore as copy", () => restoreVersion(path, v.sha, true)],
    ];
    if (HistoryFmt.canRestoreOver(path)) {
      acts.push(["Restore over current", () => restoreVersion(path, v.sha, false)]);
    }
    for (const [text, handler] of acts) {
      const btn = document.createElement("button");
      btn.className = "small";
      btn.textContent = text;
      btn.addEventListener("click", handler);
      actionsTd.append(btn, " ");
    }
    tr.append(label, actionsTd);
    tbody.appendChild(tr);
  }
  if (!versions.length && page === 1) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.className = "muted";
    td.textContent = "No pushed versions found for this file.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  // No more pages when a page comes back short (the API pages by 30).
  $("history-older").classList.toggle("hidden", versions.length < 30);
  card.scrollIntoView({ block: "nearest" });
}

$("history-older").addEventListener("click", () => {
  if (historyPath) historyAction(historyPath, historyPage + 1);
});
$("history-close").addEventListener("click", () => {
  $("history-card").classList.add("hidden");
  historyPath = null;
});

// -- review changes (the cell-aware pre-push diff + the optional push note) --
// Read-only by design: the only inputs are the note field and the footer's
// per-file Push/Propose — resolving hunks in place would be a merge tool.

let reviewPath = null;

async function reviewAction(path) {
  const data = await api("/api/diff", { path });
  if (data.error) return showError(data.error);
  reviewPath = path;
  renderReview(path, data);
}

function renderReview(path, result) {
  const card = $("review-card");
  card.classList.remove("hidden");
  $("review-title").textContent = `Review changes — ${path}`;
  $("review-summary").textContent = DiffFmt.summary(result);
  const cellsBox = $("review-cells");
  cellsBox.textContent = ""; // clear children — diff text is untrusted, plain text only
  const view = $("review-view");
  view.textContent = "";
  view.classList.add("hidden");
  if (result.kind === "cells") {
    for (const block of DiffFmt.buildBlocks(result.cells)) {
      const cell = document.createElement("div");
      cell.className = "review-cell";
      const label = document.createElement("div");
      label.className = `review-cell-label review-${block.status}`;
      label.textContent = block.label;
      cell.appendChild(label);
      if (block.diff) {
        const pre = document.createElement("pre");
        pre.className = "review-cell-diff";
        pre.textContent = block.diff;
        cell.appendChild(pre);
      }
      cellsBox.appendChild(cell);
    }
  } else if (result.kind === "lines") {
    view.textContent = result.line_diff || "(no differences against the last-synced version)";
    view.classList.remove("hidden");
  }
  // kind "binary": the summary line (sizes only) is the whole story.
  $("review-note").value = "";
  card.scrollIntoView({ block: "nearest" });
}

// Per-file Push/Propose with the optional note as the commit message. Through
// the shared action() helper so the push-guard 409 dialog (whose confirm
// re-POST re-sends this body, note included), busy state, and undo toasts all
// keep working. Ticks the checklist exactly like pushAction: only a clean
// success (a guard 409 means nothing sensitive went yet). The panel stays open
// on a 409 so the note survives the user's "Push anyway" decision visibly.
function reviewSend(apiPath) {
  if (!reviewPath) return;
  const body = { paths: [reviewPath] };
  const note = $("review-note").value.trim();
  if (note) body.message = note;
  action(apiPath, body).then((data) => {
    if (data && !data.error && !data.needs_confirm) {
      checklistSet("pushed");
      $("review-card").classList.add("hidden");
      reviewPath = null;
    }
  });
}

$("review-push").addEventListener("click", () => reviewSend("/api/push"));
$("review-propose").addEventListener("click", () => reviewSend("/api/propose"));
$("review-close").addEventListener("click", () => {
  $("review-card").classList.add("hidden");
  reviewPath = null;
});

// -- what's new (the pull digest) + the per-file watch set -------------------
// The digest answers "who changed what since MY last sync" (server-computed
// against the manifest horizon); watching a file promotes it — a badge on its
// row when teammate changes wait, and its digest entry sorts first. The watch
// set is client-side only (localStorage per repo, the theme-mirror posture).

let watchKey = null;
let watchedPaths = new Set();
let lastWhatsnew = null;
let lastWhatsnewTitle = "What's new";

function loadWatched(repo) {
  watchKey = repo ? WhatsnewFmt.watchKey(repo) : null;
  let raw = null;
  try {
    raw = watchKey ? localStorage.getItem(watchKey) : null;
  } catch {
    // localStorage unavailable (private mode) — watching quietly degrades.
  }
  watchedPaths = WhatsnewFmt.watchSet(raw);
}

function toggleWatch(path) {
  if (watchedPaths.has(path)) watchedPaths.delete(path);
  else watchedPaths.add(path);
  try {
    if (watchKey) localStorage.setItem(watchKey, WhatsnewFmt.watchSerialize(watchedPaths));
  } catch {
    // best-effort persistence; the in-memory set still drives this session
  }
  renderFiles(lastFiles, lastArtifacts, lastFolders); // re-badge + relabel the menus
  if (lastWhatsnew && !$("whatsnew-card").classList.contains("hidden")) {
    renderWhatsnew(lastWhatsnew, lastWhatsnewTitle); // re-sort watched-first
  }
}

function watchBadge() {
  const span = document.createElement("span");
  span.className = "badge watched";
  span.textContent = "watched";
  span.title = "You watch this file — a teammate's change is waiting to pull.";
  return span;
}

async function whatsnewAction() {
  const data = await api("/api/whatsnew");
  if (data.error) return showError(data.error);
  renderWhatsnew(data, "What's new since your last sync");
}

// Expand one entry to a compact "what actually changed" summary (cell counts
// for notebooks, line counts otherwise). BOTH shas ride from the digest entry:
// remote_sha so the summary matches the panel even if the branch moved, and
// base_sha because after a pull the manifest already points at the remote sha —
// a server-derived base would diff the pulled blob against itself and report
// "no cell changes" for the very change the panel is describing.
async function whatsnewDetail(entry, slot, btn) {
  btn.disabled = true;
  btn.textContent = "…";
  const data = await api("/api/whatsnew/detail", {
    path: entry.path,
    remote_sha: entry.remote_sha || "",
    base_sha: entry.base_sha || "",
  });
  if (data.error) {
    btn.disabled = false;
    btn.textContent = "Details";
    return showError(data.error);
  }
  btn.remove();
  slot.textContent = WhatsnewFmt.detailSummary(data);
}

function renderWhatsnew(digest, title) {
  lastWhatsnew = digest;
  lastWhatsnewTitle = title || lastWhatsnewTitle;
  const card = $("whatsnew-card");
  card.classList.remove("hidden");
  $("whatsnew-title").textContent = lastWhatsnewTitle;
  const now = Date.now();
  const note = $("whatsnew-note");
  if (digest.attributed === false) {
    note.textContent = "Couldn't read the commit history — showing sync states only.";
  } else if (digest.truncated) {
    note.textContent =
      "A long time away — GitHub truncated the commit window, so attribution may be partial.";
  } else {
    note.textContent = "Read-only: Pull applies these; a conflict is resolved from its file row.";
  }
  const groupsBox = $("whatsnew-groups");
  groupsBox.textContent = "";
  for (const g of (digest.groups || []).slice(0, 5)) {
    const div = document.createElement("div");
    div.textContent = WhatsnewFmt.groupLabel(g, now);
    groupsBox.appendChild(div);
  }
  const tbody = $("whatsnew-table").querySelector("tbody");
  tbody.innerHTML = "";
  const entries = WhatsnewFmt.sortEntries(digest.entries || [], watchedPaths);
  if (!entries.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "muted";
    td.textContent = "Nothing new — you're up to date.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  for (const entry of entries) {
    const tr = document.createElement("tr");
    const pathTd = document.createElement("td");
    pathTd.className = "path";
    pathTd.textContent = entry.path;
    if (watchedPaths.has(entry.path)) pathTd.append(" ", watchBadge());
    const stateTd = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${STATE_BADGES[entry.state] || ""}`;
    badge.textContent = entry.state;
    stateTd.appendChild(badge);
    const whoTd = document.createElement("td");
    const label = document.createElement("span");
    label.textContent = WhatsnewFmt.entryLabel(entry, now) || "—";
    whoTd.appendChild(label);
    // Details needs at least one blob to diff; the endpoint 404s otherwise.
    if (entry.remote_sha || entry.base_sha) {
      const slot = document.createElement("span");
      slot.className = "muted";
      const btn = document.createElement("button");
      btn.className = "small";
      btn.textContent = "Details";
      btn.addEventListener("click", () => whatsnewDetail(entry, slot, btn));
      whoTd.append(" ", btn, " ", slot);
    }
    tr.append(pathTd, stateTd, whoTd);
    tbody.appendChild(tr);
  }
  card.scrollIntoView({ block: "nearest" });
}

$("btn-whatsnew").addEventListener("click", whatsnewAction);
$("whatsnew-close").addEventListener("click", () => {
  $("whatsnew-card").classList.add("hidden");
});

function fileActions(file, opts) {
  opts = opts || {};
  const actions = [];
  // Offline every NETWORK action is skipped — the banner explains why. The
  // conflict resolves, Push/Propose, "Review changes…" (fetches the base blob),
  // "Discard my changes" (ditto), and "History…" all need the team repo. A
  // conflicted row keeps its badge: the cached remote still classifies it.
  if (file.state === "conflict" && !offlineMode) {
    actions.push(
      ["Use remote", () => action("/api/resolve", { path: file.path, strategy: "theirs" })],
      ["Keep both", () => action("/api/resolve", { path: file.path, strategy: "keep-both" })],
      ["Push as copy", () => action("/api/resolve", { path: file.path, strategy: "push-copy" })],
    );
  } else if (PUSH_STATES.has(file.state) && !offlineMode) {
    actions.push(
      // First, above Push: see what a push would publish before publishing it.
      ["Review changes…", () => reviewAction(file.path)],
      ["Push", () => pushAction([file.path], 1)],
      ["Propose", () => proposeAction([file.path], 1)],
    );
    // "Discard my changes" (né Revert) restores the last synced version.
    // Notebook-only: data files and Power BI members aren't snapshotted (so an
    // Undo would be a dead promise) and a lone PBIP member can't be reverted
    // without breaking the artifact — use the CLI for those. "new local" has no
    // checkpoint to go back to (that's Delete). Relabelled so it can't blur
    // with History's "Restore" (the time machine vs the one-click discard).
    if (file.path.endsWith(".py") && (file.state === "modified" || file.state === "deleted locally")) {
      actions.push(["Discard my changes", () => revertAction(file.path, file.state)]);
    }
  }
  // History: every pushed version of this file (the git-free time machine).
  // Never-synced files have no history; PBIP members restore only whole.
  if (HistoryFmt.hasHistory(file) && !opts.member && !offlineMode) {
    actions.push(["History…", () => historyAction(file.path)]);
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
  // A fearless personal copy: {stem}-{login}-draft.py in the same folder, opened
  // at once. Notebooks only (a PBIP member never satisfies isNotebook) — a draft
  // never flows back into the original automatically; fold work back by hand.
  if (isNotebook && file.has_local) {
    actions.push(["Duplicate as draft", () => duplicateAction(file.path)]);
  }
  // Deliver: render a shareable HTML snapshot (code hidden) into the local outbox —
  // the "hand it to a stakeholder" step. Notebooks only; the output never syncs.
  if (isNotebook && file.has_local) {
    actions.push(["Deliver", () => deliverAction(file.path)]);
  }
  // Verify: smoke-run the notebook on this machine and badge the row with whether it
  // ran clean (a value-free trust receipt). The "does this still run before I share it?"
  // step. Notebooks only; the badge auto-clears when the file is edited.
  if (isNotebook && file.has_local) {
    actions.push(["Verify runs", () => verifyAction(file.path)]);
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
    if (!file.ai_disabled) {
      actions.push(["AI", () => openChatWindow(file.path)]);
      // Explain: the same chat window, but it auto-runs /explain once ready — a
      // cell-anchored walkthrough for picking up a teammate's notebook. Same gate
      // as AI (and it IS a model turn, so the ai_disabled opt-out applies).
      actions.push(["Explain", () => openChatWindow(file.path, { explain: true })]);
      // Review logic: the same window, auto-runs /review once ready — a value-blind
      // pass over source + schema that flags structural correctness risks (fan-out
      // joins, hardcoded periods, un-run cells). Same gate; it is a model turn too.
      actions.push(["Review logic", () => openChatWindow(file.path, { review: true })]);
    }
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
  // Watch: promote this file — its row badges when a teammate change waits and
  // its What's-new entry sorts first. Per-repo and client-side only; a plain
  // menu button like every other action, never auto-run (the actionsMenu rule).
  if (watchKey && file.state !== "local") {
    actions.push([
      watchedPaths.has(file.path) ? "Unwatch" : "Watch",
      () => toggleWatch(file.path),
    ]);
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

// A green/red tie-out badge from the value-free .mooring/checks receipts a notebook
// wrote via `import mooring_checks`. Counts only — never a data value; local, never
// synced.
function checksBadge(checks) {
  const span = document.createElement("span");
  const failed = checks.failed || 0;
  const total = checks.total || 0;
  if (failed > 0) {
    span.className = "badge checks-fail";
    span.textContent = `✗ ${failed} failing`;
    span.title = `${failed} of ${total} tie-out check(s) are failing — open the notebook to see which.`;
  } else {
    span.className = "badge checks-ok";
    span.textContent = `✓ ${total} check${total === 1 ? "" : "s"}`;
    span.title = `${total} tie-out check(s) passing (mooring_checks). Value-free and never pushed.`;
  }
  return span;
}

// A green/red trust badge from the value-free .mooring/verify receipt a Verify run
// wrote: did the notebook run clean end-to-end on this machine. The server only sends
// `verified` when the receipt still matches the file's current content SHA, so this is
// gone the moment the notebook is edited — a stale "verified" never rides edited code.
function verifiedBadge(v) {
  const span = document.createElement("span");
  const when = v.ran_at ? ` (${v.ran_at.slice(0, 10)})` : "";
  if (v.passed) {
    span.className = "badge verify-ok";
    span.textContent = "✓ ran clean";
    span.title = `This notebook ran clean end-to-end when last verified${when}. ` +
      "Value-free and local; clears when you edit it.";
  } else {
    span.className = "badge verify-fail";
    const cells = v.cells_failed
      ? `${v.cells_failed} cell${v.cells_failed === 1 ? "" : "s"} failed`
      : "failed to run";
    span.textContent = `⚠ ${cells}`;
    span.title = `This notebook did not run clean when last verified${when} — open it to ` +
      "see which cell failed.";
  }
  return span;
}

// A reproducibility badge from the value-free .mooring/inputs receipts a notebook wrote
// via `import mooring_inputs`. Green when every pinned input matches the previous run,
// amber when one changed under you (content hash / row count / schema). Counts only —
// never a data value; local, never synced.
function inputsBadge(inp) {
  const span = document.createElement("span");
  const changed = inp.changed || 0;
  const total = inp.total || 0;
  if (changed > 0) {
    span.className = "badge inputs-changed";
    span.textContent = `⚠ ${changed} input${changed === 1 ? "" : "s"} changed`;
    span.title = `${changed} of ${total} pinned input(s) changed since the last run ` +
      "(content, row count, or schema) — check the numbers still hold. Value-free and local.";
  } else {
    span.className = "badge inputs-ok";
    span.textContent = `⛓ ${total} input${total === 1 ? "" : "s"} pinned`;
    span.title = `${total} input(s) fingerprinted (content hash + shape + schema), unchanged ` +
      "since the last run. Value-free and never pushed.";
  }
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
  if (file.checks && file.checks.total) extras.push(" ", checksBadge(file.checks));
  // The trust badge from a Verify run (present only while it matches the file's SHA).
  if (file.verified) extras.push(" ", verifiedBadge(file.verified));
  // The input-fingerprint badge: N inputs pinned, amber if one changed since last run.
  if (file.inputs && file.inputs.total) extras.push(" ", inputsBadge(file.inputs));
  // A watched file with a teammate change waiting gets its promotion badge —
  // quiet otherwise (watching an in-sync file must not add row noise).
  if (watchedPaths.has(file.path) && PULL_STATES.has(file.state)) {
    extras.push(" ", watchBadge());
  }
  // The notebook's own title (harvested value-free from its first markdown cell) as a
  // muted subtitle under the filename, so a repo of q3_recon_v2.py files is legible.
  if (file.title) extras.push(titleHint(file.title));
  const pathCell = extras.length ? [display, ...extras] : display;
  return buildRow(pathCell, file.state, fileActions(file, opts), file.path);
}

// A muted, block-level subtitle showing a notebook's harvested title beneath its path.
function titleHint(title) {
  const span = document.createElement("span");
  span.className = "file-title";
  span.textContent = title;
  return span;
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
  // The semantic-model summary (server-side, mtime-cached): what the copilot
  // could read of this project — plus the synced per-model opt-out state.
  const modelBits = artifact.model
    ? ` · model: ${artifact.model.tables} tables, ${artifact.model.measures} measures` +
      (artifact.ai_model_disabled ? " (AI off)" : "")
    : "";
  detail.textContent =
    `— Power BI project, ${artifact.members.length} files` +
    (counts.length ? ` (${counts.join(", ")})` : "") +
    modelBits;

  const actions = [];
  // Offline the header's Push/Propose hide exactly like a file row's (see
  // fileActions) — the cached report still computes to_push, but the network
  // actions live behind the amber banner.
  if (artifact.to_push && !offlineMode) {
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
    // Through openAction (not a bare /api/open) so the staleness guard covers
    // the artifact header's Open exactly like every file row's.
    actions.push(["Open", () => openAction(artifact.pointer)]);
    actions.push(["Delete", () => deleteAction(artifact.pointer, "project")]);
  }
  // Per-model AI opt-out (synced mooring.toml): shown whenever the project has a
  // readable semantic model and the copilot is on. A plain menu button like every
  // other action (the actionsMenu rule — never auto-run). Disabling applies to
  // chats opened AFTER the toggle; tools are bound when a chat opens.
  if (aiChatEnabled && artifact.model) {
    const label = artifact.ai_model_disabled ? "Enable AI on model" : "Disable AI on model";
    actions.push([label, () =>
      action("/api/ai/model/toggle", { model: artifact.key, disabled: !artifact.ai_model_disabled })]);
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
  const q = fileQuery.trim();
  // PBIP artifacts keep their own collapsible grouping; the rest group by folder so the
  // structure (incl. an adopted/declared folder that is still empty) is visible.
  const nonArtifact = files.filter((f) => !f.artifact);
  // Catalog presence (UNFILTERED): declared-but-empty folders still count, so the
  // table/empty-hint/search toggles keep the original "structure is visible" behaviour.
  const baseSections = FilesTree.group(nonArtifact, declaredFolders || []);
  const hasCatalog = baseSections.length > 0 || artifacts.length > 0;
  $("files-table").classList.toggle("hidden", !hasCatalog);
  // The empty-hint and the table are mutually exclusive: declared folders seed empty
  // folder sections (each with its own "New here"), so once any row renders the hint
  // would just duplicate that nudge — show it only when there's truly nothing.
  $("empty-hint").classList.toggle("hidden", hasCatalog);
  // The search box appears once there's a catalog to filter (find a notebook by name/title).
  $("file-search").classList.toggle("hidden", !hasCatalog);

  // Filtered view for rendering: match path/title/tags; while filtering, drop empty
  // declared-folder sections (they're noise) and any artifact that doesn't match.
  const shownArtifacts = artifacts.filter((a) =>
    FilesTree.matches({ path: a.pointer || a.name || a.key }, q),
  );
  const sections = q
    ? FilesTree.group(nonArtifact.filter((f) => FilesTree.matches(f, q)), declaredFolders || []).filter(
        (s) => s.files.length,
      )
    : baseSections;

  for (const artifact of shownArtifacts) {
    for (const row of buildArtifactRows(artifact, files)) tbody.appendChild(row);
  }
  for (const section of sections) {
    for (const row of buildFolderSection(section)) tbody.appendChild(row);
  }
  const shown = shownArtifacts.length + sections.reduce((n, s) => n + s.files.length, 0);
  if (q && shown === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "muted";
    td.textContent = `No notebooks match “${q}”.`;
    tr.appendChild(td);
    tbody.appendChild(tr);
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

// "GitHub unreachable — showing sync state as of N min ago." Loud and amber:
// the rows below are the last OBSERVED remote view (server-cached), not live.
// `offline` is /api/state's payload: { reason: "tls"|"network", as_of: ISO|"" }.
function renderOfflineBanner(offline) {
  const banner = $("offline-banner");
  banner.classList.toggle("hidden", !offline);
  if (!offline) return;
  const what = offline.reason === "tls"
    ? "Couldn't make a secure connection to GitHub (a proxy may be interfering)"
    : "GitHub is unreachable";
  const asOf = offline.as_of ? Date.parse(offline.as_of) : NaN;
  const shown = Number.isNaN(asOf)
    ? "no cached sync state yet"
    // Math.max: clock skew must not blank the age ("just now" is honest enough).
    : `showing sync state as of ${Freshness.ageText(Math.max(0, Date.now() - asOf))}`;
  banner.textContent = `${what} — ${shown}. ` +
    "Editing works normally; push and pull resume when you're back online.";
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

// -- first-run checklist (the self-ticking ramp; pure derivation in checklist.js) --
// Progress lives in localStorage under a per-repo key (the same best-effort
// posture as the theme key: private mode just means the checklist re-derives from
// the /api/state rows). Repo mode only — it teaches the pull→push rhythm, which
// needs a connected repo. Null key = no checklist surface (local mode/login wall).

let checklistKey = null;

function checklistStored() {
  if (!checklistKey) return {};
  try {
    return JSON.parse(localStorage.getItem(checklistKey)) || {};
  } catch {
    return {};
  }
}

function checklistSet(flag) {
  if (!checklistKey) return;
  try {
    const stored = checklistStored();
    if (!stored[flag]) {
      stored[flag] = true;
      localStorage.setItem(checklistKey, JSON.stringify(stored));
    }
  } catch {
    // localStorage unavailable — the derivable items still tick from the rows.
  }
  renderChecklist(); // tick immediately; the next refresh() re-derives anyway
}

function renderChecklist() {
  const card = $("checklist-card");
  if (!checklistKey) {
    card.classList.add("hidden");
    return;
  }
  const stored = checklistStored();
  const items = Checklist.derive(lastFiles, lastReview, stored);
  const hide = !!stored.dismissed || Checklist.isDone(items);
  card.classList.toggle("hidden", hide);
  if (hide) return;
  const list = $("checklist-items");
  list.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    if (item.done) li.classList.add("done");
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.textContent = item.done ? "✓" : "○";
    li.append(tick, item.label);
    list.appendChild(li);
  }
}

$("checklist-dismiss").addEventListener("click", () => checklistSet("dismissed"));

// Catalog search: filter the file listing as you type (client-side, no network). Re-render
// from the last /api/state rows so the filter is instant and survives background polls.
$("file-search").addEventListener("input", (event) => {
  fileQuery = event.target.value || "";
  renderFiles(lastFiles, lastArtifacts, lastFolders);
});

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
  if (offlineMode) {
    // Offline mode keeps logged_in true, so without this the one-per-repo-session
    // shot would be burnt on a discovery that cannot succeed — and the adopt
    // banner would then never appear after connectivity returns. Re-arm instead.
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
  // Wrap in .notice-content so the summary + action buttons stack (block) beside
  // the leading icon rather than becoming inline flex items next to it.
  const content = document.createElement("div");
  content.className = "notice-content";
  content.append(summary, actions);
  banner.append(content);
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
  offlineMode = !!state.offline;
  renderOfflineBanner(state.offline);
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
  const repoInfoText = state.repo
    ? `${state.repo} @ ${state.branch}${hostSuffix}`
    : (localMode ? "Local workspace — not connected to a repo" : "");
  const repoInfoEl = $("repo-info");
  repoInfoEl.textContent = repoInfoText;
  repoInfoEl.title = repoInfoText; // the line ellipsis-truncates when the bar is tight

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

  // Pull / Push all / Propose (and the pull digest) only make sense against a
  // connected, logged-in repo that is REACHABLE. In local mode the notebooks are
  // usable but there's nothing to sync to; offline the network controls hide
  // behind the amber banner (the per-file rows already omit their network
  // actions — see fileActions).
  for (const id of ["btn-pull", "btn-whatsnew", "btn-push", "btn-propose"]) {
    $(id).classList.toggle("hidden", !state.logged_in || offlineMode);
  }
  if (!state.logged_in || offlineMode) {
    $("whatsnew-card").classList.add("hidden");
    // An already-open Review/History panel keeps live "Push this file"/"Restore"
    // buttons, each of which needs GitHub — close them too, like the rows that
    // stop offering Review/History while the amber banner shows.
    $("review-card").classList.add("hidden");
    reviewPath = null;
    $("history-card").classList.add("hidden");
    historyPath = null;
  }
  // The per-file watch set is keyed by repo; local mode has no digest to watch.
  loadWatched(state.mode === "repo" && state.logged_in ? state.repo : null);
  // Recall shows only while the manifest holds a recallable last push; the
  // confirm names exactly which files it would revert (a stale record is the
  // trap — this is how the user catches one).
  recallPaths = state.recall_paths || [];
  $("btn-recall").classList.toggle(
    "hidden", !(state.logged_in && state.can_recall && !offlineMode),
  );
  // Workspace-level "Batch build" — only when the opt-in orchestrator is enabled.
  $("btn-batch").classList.toggle("hidden", !state.ai_batch);
  // No team Pull in local mode, so don't dangle it in the empty-state hint.
  $("empty-hint").innerHTML = localMode
    ? "No notebooks yet &mdash; click <b>New notebook</b> to create one."
    : "No notebooks yet &mdash; click <b>New notebook</b> to create one, or <b>Pull</b> to " +
      "fetch your team's notebooks.";

  // First-run checklist: keyed per repo so a second repo ramps afresh. The key
  // gates every checklist surface, so local mode and the login wall show nothing.
  checklistKey = state.mode === "repo" && state.logged_in
    ? Checklist.storageKey(state.repo)
    : null;
  lastReview = (state.logged_in && state.review) || null;

  // Reviews only makes sense against a logged-in repo; Activity (machine-local) and
  // Settings (per-machine config) stay reachable everywhere, so only Reviews is gated.
  $("reviews-link").classList.toggle("hidden", !state.logged_in);
  const accountSummary = $("account-summary");
  if (state.logged_in) {
    // The @handle labels the account menu; the panel repeats it with the Log out it
    // acts on. (This replaced a second, redundant @username → /settings link.)
    accountSummary.textContent = `@${state.user}`;
    const userInfo = $("user-info");
    userInfo.innerHTML = "";
    const who = document.createElement("div");
    who.className = "muted";
    who.append("Signed in as ");
    const handle = document.createElement("b");
    handle.textContent = `@${state.user}`;
    who.appendChild(handle);
    const logoutBtn = document.createElement("button");
    logoutBtn.className = "small";
    logoutBtn.textContent = "Log out";
    logoutBtn.addEventListener("click", async () => {
      await api("/api/logout", {});
      location.reload();
    });
    userInfo.append(who, logoutBtn);
    $("summary").textContent = state.summary || "";
    renderReviewBanner(state.review);
  } else {
    accountSummary.textContent = "☰ Menu";
    $("user-info").textContent = "";
    $("summary").textContent = "";
    renderReviewBanner(null);
  }
  if (showFiles) {
    lastFiles = state.files || [];
    lastArtifacts = state.artifacts || [];
    lastFolders = state.folders || [];
    renderFiles(lastFiles, lastArtifacts, lastFolders);
  } else {
    lastFiles = [];  // no file surface (login wall) — don't leave stale push/propose targets
    lastArtifacts = [];
    lastFolders = [];
  }
  renderChecklist();  // after lastFiles lands: two of the items derive from the rows
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
// menus (Copilot + account) and every per-row actions menu — and, because clicking one
// menu's summary runs here too, opening a menu closes any other menu left open.
document.addEventListener("click", (e) => {
  for (const menu of document.querySelectorAll("details.header-menu[open], details.row-menu[open]")) {
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

// Bulk Push/Propose sweeps up personal -draft.py copies with everything else; ask
// first, so a draft is only ever shared on purpose. The question is about the
// DRAFTS, so Cancel answers it: the drafts are EXCLUDED and everything else still
// goes (never a silent abort of the whole push — the old behaviour, where Cancel
// quietly sent nothing, read as "5 team files pushed" to the analyst). Returns
// { paths, count }: paths null = push everything, [] = nothing left to send.
// A filename-shape check only — the push guard's server-side content scan still
// runs and its dialog fires independently afterwards. Pushing a draft from its
// own row stays unprompted: that click is already explicit.
function draftShareSelection(candidates) {
  const isDraft = (f) => Checklist.DRAFT_RE.test(f.path);
  const drafts = candidates.filter(isDraft);
  if (!drafts.length) return { paths: null, count: candidates.length };
  const names = drafts.map((f) => "  " + f.path).join("\n");
  const include = confirm(
    `Include your ${drafts.length} draft(s)?\n\n${names}\n\n` +
    "OK sends everything; Cancel sends everything EXCEPT the draft(s)."
  );
  if (include) return { paths: null, count: candidates.length };
  const rest = candidates.filter((f) => !isDraft(f));
  return { paths: rest.map((f) => f.path), count: rest.length };
}

$("login-start").addEventListener("click", startLogin);
$("btn-refresh").addEventListener("click", refresh);
$("btn-pull").addEventListener("click", async () => {
  const data = await action("/api/pull", {});
  // The pull response carries the digest of what just landed, computed against
  // the PRE-pull horizon — so a pull is never a black box. States shown are the
  // pre-pull ones ("remote changed" = what the pull just applied).
  if (data && !data.error && data.whatsnew && (data.whatsnew.entries || []).length) {
    renderWhatsnew(data.whatsnew, "What just landed");
  }
});
$("btn-push").addEventListener("click", () => {
  const candidates = lastFiles.filter((f) => PUSH_STATES.has(f.state));
  const sel = draftShareSelection(candidates);
  if (!sel.count) {
    // Everything pending was a draft and the user excluded them — say so
    // rather than silently doing nothing.
    $("summary").textContent = "Nothing pushed — only drafts were pending.";
    return;
  }
  return pushAction(sel.paths, sel.count);
});
$("btn-propose").addEventListener("click", () => {
  const candidates = lastFiles.filter((f) => PUSH_STATES.has(f.state));
  const sel = draftShareSelection(candidates);
  if (!sel.count) {
    $("summary").textContent = "Nothing proposed — only drafts were pending.";
    return;
  }
  return proposeAction(sel.paths, sel.count);
});
let recallPaths = [];

$("btn-recall").addEventListener("click", () => {
  const shown = recallPaths.slice(0, 8).join("\n  ");
  const more = recallPaths.length > 8 ? `\n  …and ${recallPaths.length - 8} more` : "";
  const ok = confirm(
    "Undo your last push on GitHub?\n\n" +
    (shown ? `This reverts:\n  ${shown}${more}\n\n` : "") +
    "The previous version of each file is written back to the team branch. " +
    "The pushed version stays in the repo's history — if you pushed a secret, you " +
    "still need to rotate it. If a teammate has pushed since, the recall stops with " +
    "a conflict instead of overwriting their work."
  );
  if (ok) action("/api/recall", {});
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

// theme.js applies a cross-tab appearance change to <html>; the hub just keeps
// its Appearance select in sync with it.
window.addEventListener("mooring:theme", (event) => {
  $("theme-select").value = event.detail;
});

// Match the toggle to the theme the pre-paint script already applied, so it's
// never momentarily out of sync; refresh() reconciles with the server value.
$("theme-select").value = document.documentElement.dataset.theme || "system";

// -- health check (mooring doctor in the footer) -----------------------------
// On-demand only: nothing probes at startup or on refresh. The Copy report is
// the server's redacted, paste-safe text (no tokens/hostnames/usernames).

let healthReport = "";

$("health-run").addEventListener("click", async () => {
  const btn = $("health-run");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const data = await api("/api/doctor", {});
    if (data.error) return showError(data.error);
    healthReport = data.report || "";
    const list = $("health-results");
    list.innerHTML = "";
    for (const r of data.results || []) {
      const li = document.createElement("li");
      li.className = `health-${r.status}`;
      let text = `${r.title}: ${r.detail}`;
      if (r.fix && r.status !== "pass") text += ` Fix: ${r.fix}`;
      li.textContent = text;
      list.appendChild(li);
    }
    $("health-copy").classList.toggle("hidden", !healthReport);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run health check";
  }
});
$("health-copy").addEventListener("click", () => {
  const btn = $("health-copy");
  if (healthReport && navigator.clipboard) {
    navigator.clipboard.writeText(healthReport).then(
      () => { btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy report"; }, 1500); },
      () => { /* clipboard blocked — nothing sensible to do */ },
    );
  }
});

// An idle tab heals itself: refresh when the tab regains focus and the last
// check is older than the throttle, so the staleness dialog and banner decide
// from reasonably fresh rows without riding a polling loop or the rate limit.
function maybeFocusRefresh() {
  if (document.visibilityState !== "visible" || busy) return;
  if (Freshness.shouldAutoRefresh(lastStateAt, Date.now(), FOCUS_REFRESH_THROTTLE_MS)) {
    // Stamp BEFORE the fetch: returning to the tab fires both `focus` and
    // `visibilitychange`, and without this both would start a refresh.
    lastStateAt = Date.now();
    refresh();
  }
}
window.addEventListener("focus", maybeFocusRefresh);
document.addEventListener("visibilitychange", maybeFocusRefresh);
// Keep the banner's age text honest while the tab sits open (no network).
setInterval(renderFreshnessBanner, 60_000);

refresh();
