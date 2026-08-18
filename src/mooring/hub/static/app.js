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
// The accounts/setup/sign-in view was opened deliberately from the rail (rather than
// forced by a login wall), so refresh() knows not to close it under the user.
let showAccountsView = false;
let lastFiles = [];
let lastArtifacts = [];
let lastFolders = [];
let lastReview = null;
// The catalog search box's current text — filters the file listing client-side. Kept
// across /api/state re-renders so a poll doesn't clear an in-progress filter.
let fileQuery = "";
// The "focus one folder" view. `focusPrefix` narrows the whole listing to one folder
// subtree ("" = every notebook); `moreOpen` is the folder summary line's personal
// "show the rest" fold. Display-only, and persisted PER WORKSPACE in localStorage on
// the stable hub origin (see loadFolderView) alongside the selection.
let focusPrefix = "";
let folderViewKey = null;
// The one row the detail panel is about. Persisted per workspace beside the focus,
// and NEVER set automatically — see setSelected.
let selectedPath = "";
// Which pane the centre shows: the notebook list, or one of the panels that used to
// be a stacked card below it. See CENTRE_VIEWS / setCentreView.
let centreView = "list";
// Whether the notebook surface is usable at all (logged in, or local mode) — the
// rail and the header read it, so it is mirrored here from refresh().
let filesVisible = false;
let lastMode = "local";
let canRecall = false;
let aiBatchEnabled = false;
// Repo-curated "featured folders" (synced mooring.toml [hub]): the starred folders show
// first and the rest fold under a "More folders" disclosure. `lastFeatured` mirrors
// /api/state; `canFeature` gates the star control to repo mode (it curates for the team);
// `moreOpen` is the personal open/closed state of that disclosure (persisted like focus).
let lastFeatured = [];
let canFeature = false;
let moreOpen = false;
// Team-offered AI context folders (synced mooring.toml [ai] context_folders): the
// value-free menu whose folders the copilot may read. `lastContextFolders` mirrors
// /api/state; `canCurateContext` gates the per-folder "AI context" toggle to repo mode
// with AI enabled (curating what the model reads is a team governance act).
let lastContextFolders = [];
let canCurateContext = false;
// The per-user subscription checklist: `lastAiContext` is this machine's [ai] context
// consent bool and `lastSelectedContext` the offered folders THIS copilot actually reads.
let lastAiContext = false;
let lastSelectedContext = [];
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

// The result of the last operation. It used to be a card permanently parked below
// the file table; now the one-line outcome rides the header's status line and the
// full text is a centre view. The view OPENS BY ITSELF only when the operation
// produced something you must act on — a pull-request link, or a warning — because
// yanking the list away after every routine push would be its own kind of noise.
function showLog(data) {
  if (!data || (!data.lines && !data.summary && !data.warning)) return;
  const lines = (data.lines || []).slice();
  if (data.warning) lines.push("⚠ " + data.warning);
  if (data.summary) lines.push("", data.summary);
  $("log").textContent = lines.join("\n");
  // The <pre> is plain text; the PR link needs a real anchor. When mooring opened the
  // PR (Slice 2), link straight to it; otherwise fall back to the compare page.
  const linkBox = $("log-link");
  const link = data.pull_url || data.compare_url;
  linkBox.classList.toggle("hidden", !link);
  if (link || data.warning) setCentreView("log");
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
  // #btn-sweep-cancel is exempt: a sweep runs for minutes OUTSIDE the busy lock, and any
  // unrelated in-flight action (a Pull) would otherwise grey out the only way to stop it.
  document.querySelectorAll("button, select").forEach((b) => {
    if (b.id !== "btn-sweep-cancel") b.disabled = value;
  });
  // A visible "working" cue for the whole surface. The toolbar is disabled while
  // an action is in flight, but that greying is faint — .busy adds a progress
  // cursor (+ a subtle dim, in CSS) so even a fast op looks like it did something.
  document.body.classList.toggle("busy", value);
}

// The quiet outcome line under the headline: what the last operation did, with a way
// into its full text. Written AFTER any refresh, since refresh() resets #summary to
// the workspace caption and would otherwise wipe the result the instant it appeared.
// Returns whether it had anything to say.
function showOutcome(data) {
  const text = data && (data.warning ? "⚠ " + data.warning : data.summary);
  if (!text) return false;
  const status = $("summary");
  status.textContent = "";
  status.classList.toggle("status-warn", !!data.warning);
  status.append(text + " ");
  const more = document.createElement("button");
  more.className = "mono-link";
  more.textContent = "details";
  more.addEventListener("click", () => setCentreView("log"));
  status.appendChild(more);
  return true;
}

async function action(path, body, refreshAfter = true, status = "") {
  if (busy) return;
  setBusy(true);
  showError("");
  // Optional in-flight status on the files-summary line (mirrors doOpen's
  // "Starting the editor…"). Deliver/Verify re-run the WHOLE notebook, so the
  // disabled toolbar alone reads as "did anything happen?"; a "Rendering…" line
  // explains the wait. When refreshAfter runs, its re-render resets #summary for
  // us (see the render path, ~line 1660), so only restore the prior text when it
  // won't — otherwise we'd clobber the fresh summary with the stale one.
  const summaryEl = status ? $("summary") : null;
  const prevSummary = summaryEl ? summaryEl.textContent : "";
  if (summaryEl) summaryEl.textContent = status;
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
    // The outcome goes on AFTER the refresh, or the refresh's caption would erase it.
    if (!showOutcome(data) && summaryEl && !refreshAfter) summaryEl.textContent = prevSummary;
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
  const depsRows = GuardFmt.depsRows(data);
  const policyRows = GuardFmt.policyRows(data);
  const files = findings.length + policyRows.length;
  const verb = apiPath.includes("propose") ? "proposed" : "pushed";
  // Three guards can fire on one push, and they ask three different questions.
  // Lead with whichever is actually present rather than blurring them: "this lock
  // change breaks 3 notebooks" and "this file looks sensitive" are not the same
  // warning, and running them together wastes both.
  $("guard-message").textContent = findings.length
    ? `${files} file(s) were NOT ${verb} — they contain something that looks sensitive:`
    : policyRows.length
    ? `${files} file(s) were NOT ${verb} — your team's policy doesn't allow a direct push:`
    : `A dependency change was NOT ${verb}:`;
  const list = $("guard-findings");
  list.innerHTML = "";
  for (const row of GuardFmt.rows(findings).concat(depsRows, policyRows)) {
    const li = document.createElement("li");
    li.textContent = row;
    list.appendChild(li);
  }
  const override = GuardFmt.canOverride(data);
  // Each guard has its own remedy, so each contributes its own sentence — never drop
  // one because another fired. Block mode replaces the CONTENT sentence only.
  const contentHint = override
    ? "Remove the flagged content, or add a “mooring: push-ok” comment on a " +
      "reviewed false-positive line. Pushing anyway publishes it to everyone " +
      "with access to the repo."
    : "Your team's policy blocks pushing flagged files ([guard] push = \"block\"). " +
      "Remove the flagged content, or add a “mooring: push-ok” comment on a " +
      "reviewed false-positive line, then push again.";
  const depsHint =
    "Use “Check all notebooks run” to see what these dependencies do to the " +
    "repo. Pushing anyway changes the environment for everyone on the team." +
    // In block mode with a content finding there is no button at all, and the deps
    // warning would otherwise look unactionable. Say what to do about it.
    (override ? "" : " (Fix the flagged content first — then this can be pushed anyway.)");
  const policyHint = findings.length
    ? "Files listed as propose-only can’t be pushed directly at all — send them " +
      "for review with Propose."
    : "Your team's policy allows these paths to change only through review — use Propose.";
  const hints = [];
  if (findings.length) hints.push(contentHint);
  if (depsRows.length) hints.push(depsHint);
  if (policyRows.length) hints.push(policyHint);
  $("guard-hint").textContent = hints.join(" ");
  const anyway = $("guard-anyway");
  anyway.classList.toggle("hidden", !override);
  anyway.onclick = () => {
    dialog.close();
    const confirmed = Object.assign({}, body, {
      confirm_tokens: GuardFmt.allTokens(data),
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
        closeCentreView("review");
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

// "Open with AI" launches the Workbench: a hub page that puts the REAL marimo
// editor (a cross-origin iframe — mooring can't script into it, so the copilot
// stays value-blind) beside the copilot in a resizable, collapsible panel. Opens
// in a NEW TAB, named per notebook. We keep the tab reference so a PLAIN re-click
// (no command) just FOCUSES the open workbench instead of reloading its live editor.
// Explain/Review DO re-navigate (that runs the command — the point of the button);
// they pass &explain=1/&review=1 through to the embedded chat, which auto-runs them.
// The notebook opens in app view by default (results, not code).
const workbenchWindows = {};
function openWorkbench(path, opts) {
  const hasCmd = !!(opts && (opts.explain || opts.review));
  const existing = workbenchWindows[path];
  if (existing && !existing.closed && !hasCmd) {
    existing.focus(); // already open — focus it; don't reload the live editor
    return;
  }
  let url = `/workbench?notebook=${encodeURIComponent(path)}`;
  if (opts && opts.explain) url += "&explain=1";
  if (opts && opts.review) url += "&review=1";
  const name = "mooringWB_" + path.replace(/[^a-z0-9]/gi, "_");
  const win = window.open(url, name);
  if (!win) {
    // window.open runs after an awaited freshness fetch (see guardedOpen), so a
    // strict pop-up blocker can null it — say so rather than silently no-op.
    showError("Couldn't open the workbench — allow pop-ups for this site, then retry.");
    return;
  }
  workbenchWindows[path] = win;
  win.focus();
  checklistSet("opened"); // opening a notebook (with AI) ticks the onboarding step
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

function showStaleDialog(file, kind, opener) {
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
    if (still) return showStaleDialog(fresh, still, opener);
    opener();
  };
  $("stale-open").onclick = () => {
    dialog.close();
    staleDismissed.set(file.path, Freshness.dismissKey(file));
    opener();
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
// The stale guard, shared by plain Open and "Open with AI" (both now open the
// notebook): warn at the moment of choice when the remote moved under this file,
// then run `opener` to actually open it — a plain marimo tab (doOpen) or the
// Workbench (openWorkbench).
async function guardedOpen(path, opener) {
  let file = lastFiles.find((f) => f.path === path);
  // The dialog decision is only as good as the cached rows: if the branch head
  // moved since the last /api/state, re-render first (timeboxed; see isStateFresh).
  if (file && !(await isStateFresh())) {
    await refresh();
    file = lastFiles.find((f) => f.path === path);
    if (!file) return; // the row vanished with the fresh state — nothing to open
  }
  const kind = Freshness.warnState(file, staleDismissed);
  if (kind) return showStaleDialog(file, kind, opener);
  return opener();
}

function openAction(path) {
  return guardedOpen(path, () => doOpen(path));
}

// "Open with AI" (and Explain / Review): same stale guard, but the opener launches
// the Workbench instead of a plain marimo tab.
function openWorkbenchAction(path, opts) {
  return guardedOpen(path, () => openWorkbench(path, opts));
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
  return action("/api/deliver", { path }, false,
    "Rendering… this re-runs the whole notebook (can take a minute).");
}

// Deliver as Excel: the same last mile for a stakeholder who works in Excel rather
// than reading a chart. Runs the notebook and collects the tables it named with
// `import mooring_deliver` into one .xlsx in the same never-synced outbox. Not opened
// for preview — Excel locks an open workbook, which would block the next delivery.
function deliverExcelAction(path) {
  return action("/api/deliver/excel", { path }, false,
    "Building the workbook… this re-runs the whole notebook (can take a minute).");
}

// Verify: smoke-run this notebook once on your machine and record whether it ran clean
// (the trust badge). Runs in the real environment; nothing is committed and no value
// leaves the machine — the receipt is a boolean keyed to the file's content, so the
// badge auto-clears the moment you edit the notebook. Refresh after so the badge shows.
function verifyAction(path) {
  return action("/api/verify", { path }, true,
    "Verifying… this re-runs the whole notebook (can take a minute).");
}

// -- the catalog-wide sweep --------------------------------------------------
// "Check all notebooks run" = Verify, for every notebook in the workspace, one at a time. It
// EXECUTES each notebook, so the cost is stated before anything starts, progress is
// visible while it runs, and Cancel is reachable throughout — which is why this does
// NOT go through action() (that disables the whole surface for the duration of one
// request). The start POST returns immediately and the client polls.
let sweepPoll = null;

async function sweepAction() {
  if (busy || sweepPoll) return;
  let plan;
  try {
    plan = await (await fetch("/api/sweep/plan")).json();
  } catch {
    showError("Couldn't work out how many notebooks to check.");
    return;
  }
  const total = plan.total || 0;
  if (!total) {
    showError("No notebooks to check.");
    return;
  }
  // The server words the cost (and prices it from the last check's median run time), so
  // the hub and the CLI can never quote a different number for the same work.
  const ok = window.confirm(
    `${plan.cost}\n\n` +
      "It records the same “ran clean” badge as Verify — and proves each notebook RUNS, " +
      "not that its numbers are right.\n\nStart?"
  );
  if (!ok) return;
  const started = await api("/api/sweep", {});
  if (started.error) {
    showError(started.error);
    return;
  }
  sweepTotal = total;
  watchSweep({ running: true, done: 0, total });
}

// A sweep outlives the page: it runs on a server thread for minutes, so a reload (or a
// tab reopened from the checklist) must find it again rather than leaving the progress
// box hidden and Cancel unreachable for the rest of the run.
let sweepTotal = 0;
async function resumeSweepWatch() {
  let state;
  try {
    state = await (await fetch("/api/sweep")).json();
  } catch {
    return;
  }
  if (state.running) watchSweep(state);
}

function watchSweep(state) {
  showSweepProgress(state);
  if (!sweepPoll) sweepPoll = setInterval(pollSweep, 1500);
}

function showSweepProgress(state) {
  const box = $("sweep-progress");
  const running = !!state.running;
  box.classList.toggle("hidden", !running);
  if (running) {
    // `total` is 0 until the worker's first progress tick; fall back to the count this
    // tab already fetched so the line never reads "0 of 0".
    const total = state.total || sweepTotal || 0;
    $("sweep-progress-text").textContent =
      `Checking notebooks… ${state.done || 0} of ${total} run. ` +
      "You can keep working; this runs one notebook at a time.";
  }
}

async function pollSweep() {
  let state;
  try {
    state = await (await fetch("/api/sweep")).json();
  } catch {
    return; // a transient poll failure must not kill a running sweep
  }
  showSweepProgress(state);
  if (state.running) return;
  clearInterval(sweepPoll);
  sweepPoll = null;
  $("sweep-progress").classList.add("hidden");
  if (state.error) {
    showError(state.error);
    return;
  }
  if (!state.finished) return;
  showLog(state); // lines + summary + the "it ran, not that it's right" warning
  await refresh(); // the swept receipts badge the rows exactly like a hand Verify
}

function cancelSweep() {
  $("sweep-progress-text").textContent = "Stopping — the notebook running now is stopped too…";
  api("/api/sweep/cancel", {}).catch(() => {});
}

// -- parameterised runs ------------------------------------------------------
// "Run this notebook once per region / entity / month." Attended: the analyst watches it
// go, value by value, and can stop it. Values run ONE AT A TIME, so the card shows exactly
// one "running…" row and everything else is queued or reported.
//
// Progress is polled rather than streamed. A fan-out changes state at most once per
// notebook run (tens of seconds), so a 1s poll on loopback carries the same information an
// SSE stream would, with no broadcaster to keep correct.
let paramsPoll = null;
let paramsPath = "";

function paramsAction(path) {
  paramsPath = path;
  $("params-path").textContent = path;
  $("params-for").value = "";
  $("params-deliver").checked = true;
  $("params-table").classList.add("hidden");
  $("params-banner").classList.add("hidden");
  setCentreView("params");
  renderParamsPreview();
  $("params-for").focus();
}

// "Runs 3 times: EMEA, APAC, AMER" — the last cheap moment to notice a typo before
// committing the machine to N notebook runs. The SERVER re-validates everything.
function renderParamsPreview() {
  const spec = ParamsFmt.previewValues($("params-for").value);
  const el = $("params-preview");
  if (!spec.name || !spec.values.length) {
    el.textContent = "Give a parameter and its values, e.g. region=EMEA,APAC,AMER or month=2026-01..2026-06.";
    return;
  }
  const shown = spec.values.slice(0, 8).join(", ");
  const more = spec.values.length > 8 ? `, … (${spec.values.length} in total)` : "";
  el.textContent = `Runs ${spec.values.length} time(s), one at a time — ${spec.name} = ${shown}${more}.`;
}

async function startParamsRun() {
  if (!paramsPath) return;
  showError("");
  const data = await api("/api/run/start", {
    path: paramsPath,
    for: $("params-for").value,
    deliver: $("params-deliver").checked,
  });
  if (data.error) {
    showError(data.error);
    return;
  }
  renderParamsRun(data.run);
  startParamsPolling();
}

function startParamsPolling() {
  stopParamsPolling();
  paramsPoll = setInterval(async () => {
    const data = await api("/api/run/state");
    const run = data && data.run;
    if (!run) return stopParamsPolling();
    renderParamsRun(run);
    // The run keeps its handle after finishing (so a reload can still read the report),
    // so polling stops on `done` rather than on the handle disappearing.
    if (run.done) {
      stopParamsPolling();
      refresh(); // artifacts landed in .mooring/outbox; re-read the file list
    }
  }, 1000);
}

function stopParamsPolling() {
  if (paramsPoll) clearInterval(paramsPoll);
  paramsPoll = null;
}

async function cancelParamsRun() {
  const data = await api("/api/run/cancel", {});
  if (data.error) showError(data.error);
  else if (data.run) renderParamsRun(data.run);
}

function renderParamsRun(snap) {
  if (!snap) return;
  $("params-path").textContent = snap.notebook || paramsPath;
  const banner = $("params-banner");
  banner.textContent = ParamsFmt.summary(snap);
  banner.classList.remove("hidden");
  // The banner turns red/amber for a partial pack: "2 of 3 ran clean" must never look
  // like the calm end of a successful run.
  const tone = ParamsFmt.tone(snap);
  banner.classList.toggle("notice-error", tone === "bad" || tone === "warn");
  // Start is unavailable while a run is in flight; Cancel appears only then.
  $("params-start").disabled = !snap.done;
  $("params-cancel").classList.toggle("hidden", !!snap.done);
  $("params-cancel").disabled = !!snap.cancelling;

  const table = $("params-table");
  table.classList.remove("hidden");
  const body = table.querySelector("tbody");
  body.textContent = "";
  for (const row of ParamsFmt.rows(snap)) {
    const tr = document.createElement("tr");
    const value = document.createElement("td");
    value.className = "path";
    value.textContent = row.value;
    const state = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${row.state.tone}`;
    badge.textContent = row.state.text;
    state.appendChild(badge);
    const detail = document.createElement("td");
    detail.className = "muted";
    detail.textContent = row.detail;
    tr.append(value, state, detail);
    body.appendChild(tr);
  }
}

function closeParamsCard() {
  stopParamsPolling();
  paramsPath = "";
  closeCentreView("params");
}

// -- scheduled refresh -------------------------------------------------------
// The board that makes a stale refresh impossible to miss. lastSchedules is the last
// /api/schedules payload, kept so a background poll can re-render without refetching and
// so the form can prefill from an existing schedule.
let lastSchedules = { schedules: [], overdue: 0, due: 0 };
let schedulingPath = "";

// Open the inline editor for one notebook, prefilled from its existing schedule if it has
// one. Deliberately part of the schedules card rather than a modal: the card is where the
// consequence shows up, so the user edits it in the place they will read it.
function scheduleAction(path) {
  schedulingPath = path;
  const existing = (lastSchedules.schedules || []).find((r) => r.notebook === path);
  $("schedule-form-path").textContent = path;
  $("schedule-cadence").value = existing ? existing.cadence : "daily";
  const at = existing ? existing.at : "07:30";
  $("schedule-at").value = at;
  $("schedule-day").value = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][
    existing ? existing.day : 0
  ];
  // A one-shot needs a calendar date; anything else ignores it, so an existing schedule's
  // date only prefills when it has one, and firstUnspentDate stands in otherwise — the
  // time it is paired with decides which date that is, hence passing `at` rather than
  // reading the field back. Remembering what we offered is what lets reofferDate revise it
  // if the time moves; an existing date is the user's, so nothing is remembered for it.
  offeredDate = (existing && existing.date) ? "" : ScheduleFmt.firstUnspentDate(at);
  $("schedule-date").value = (existing && existing.date) || offeredDate;
  $("schedule-deliver").checked = existing ? !!existing.deliver : true;
  $("schedule-pull").checked = existing ? !!existing.pull : true;
  syncScheduleCadenceFields();
  setCentreView("schedules");
  $("schedule-form").classList.remove("hidden");
}

// The offered one-shot date, tracked so a later change to the TIME can revise it — but only
// while the user has not picked a date of their own. "" once they have.
let offeredDate = "";

// Re-offer the date when the time it is paired with changes. ScheduleFmt.firstUnspentDate
// answers "today or tomorrow?" from `at`, so a form opened at 09:00 offering today-at-17:00
// would otherwise keep offering today after the user moved the time back to 08:00 — handing
// them the spent instant the helper exists to avoid. Silent while the field still holds what
// we offered; the moment they type a date themselves it is theirs, and we stop touching it.
function reofferDate() {
  const field = $("schedule-date");
  if (!offeredDate || field.value !== offeredDate) return;
  offeredDate = ScheduleFmt.firstUnspentDate($("schedule-at").value);
  field.value = offeredDate;
}

// Each cadence asks for one extra thing, or nothing: "on <weekday>" is meaningful for
// weekly only, "on <date>" for the one-shot only. Showing both at once would invite a
// user to fill in a field the cadence then ignores.
function syncScheduleCadenceFields() {
  const cadence = $("schedule-cadence").value;
  $("schedule-day-label").classList.toggle("hidden", cadence !== "weekly");
  $("schedule-date-label").classList.toggle("hidden", cadence !== "once");
}

function closeScheduleForm() {
  schedulingPath = "";
  $("schedule-form").classList.add("hidden");
}

async function saveSchedule() {
  if (!schedulingPath) return;
  const body = {
    path: schedulingPath,
    cadence: $("schedule-cadence").value,
    at: $("schedule-at").value || "07:30",
    day: $("schedule-day").value,
    // Sent whatever the cadence — the server keeps it only for "once" and validates it
    // there, so the client never has to decide which fields a cadence cares about.
    date: $("schedule-date").value,
    deliver: $("schedule-deliver").checked,
    pull: $("schedule-pull").checked,
  };
  const data = await action("/api/schedule/add", body, false);
  // A 409 here is the preflight gate ("verify it first"), which action() has already
  // surfaced as an error banner — keep the form open so the user can act on it.
  if (data && !data.error) {
    closeScheduleForm();
    applySchedules(data);
  }
}

// Scheduling is gated on a clean verify (the server answers 409 otherwise), so an
// unverified notebook does the two steps as one action: verify, and open the form only if
// it passed. The form stays shut when it didn't, because a form asking when to re-run a
// notebook that cannot run is a promise mooring can't keep.
//
// The refusal is worded HERE, and that is load-bearing — do not delete it on the theory
// that the ordinary verify feedback covers it. It does not: a failing verify is a 200
// carrying {path, ok: false, lines}, with no `error`, `warning` or `summary` on it, so
// every automatic channel stays quiet. showError is never reached (there is no error);
// showLog fills the hidden log card but only OPENS it for a link or a warning; showOutcome
// finds neither warning nor summary and returns false. The whole chain therefore ends in
// silence — a user who waited a minute for a notebook to run sees the form simply not
// appear, which reads as a broken button rather than a refusal.
//
// So we say it in the hub's own outcome idiom rather than a new one: the same showLog +
// showOutcome pair action() uses, on a copy of the payload carrying the warning the server
// had no way to know it needed. The warning opens the log card (where the run's own line
// explains WHAT failed, right above ours explaining what that cost), and leaves the
// one-line outcome + "details" on the status line for when the user goes back to the list.
// A route failure (a 502 from a run that could not start, a 400 for a path that is no longer
// a notebook) needs saying here too, and for a subtler reason: action() DOES set the error
// banner for it — and then clears it again three lines later, because verify refreshes
// afterwards and refresh() re-renders the banner from /api/state. So that branch ends in the
// same silence as ok:false and gets the same treatment; only the wording differs, since one
// is "it ran and failed" and the other is "it never got to run".
async function verifyAndSchedule(path) {
  const data = await verifyAction(path);
  if (!data) return;
  if (data.ok && !data.error) return scheduleAction(path);
  const refused = Object.assign({}, data, {
    warning: data.error
      ? `Not scheduled — couldn’t verify ${path}: ${data.error}`
      : `Not scheduled — ${path} didn’t run clean. Fix what stopped it, ` +
        "then choose “Verify & schedule…” again.",
  });
  // Dropped so showLog/showOutcome read this as the warning it now is rather than an error
  // with no channel left to reach the user through.
  delete refused.error;
  showLog(refused);
  showOutcome(refused);
}

// Run one schedule now (or everything due, when path is ""). This EXECUTES the notebook,
// so it gets the same in-flight status line Deliver/Verify use.
function runRefresh(path) {
  const status = path
    ? "Refreshing… this pulls and re-runs the whole notebook (can take a minute)."
    : "Refreshing everything due… this re-runs each notebook (can take a while).";
  return action("/api/refresh", path ? { path } : {}, true, status).then((data) => {
    if (data && !data.error) applySchedules(data);
    return data;
  });
}

// Adopt a board payload returned by any of the schedule endpoints (they all echo it) and
// re-render, so the card is never a poll behind the action the user just took.
function applySchedules(data) {
  if (!data || !Array.isArray(data.schedules)) return;
  lastSchedules = {
    schedules: data.schedules,
    overdue: data.overdue || 0,
    due: data.due || 0,
    background: data.background || null,
  };
  renderSchedules();
  renderRailNav();  // the rail carries the board's overdue/where-to-find-it count
}

// Turn background refresh (the OS task / sign-in agent) on or off. The response says which
// tier was actually registered — enabling can legitimately land on the sign-in agent when
// Task Scheduler is blocked by policy — so the log lines are the real feedback here.
function setBackground(enabled) {
  return action("/api/schedule/background", { enabled }, false).then((data) => {
    if (data && !data.error) applySchedules(data);
    return data;
  });
}

// The "which clock is running" line under the board, plus the button that upgrades it.
function renderBackground() {
  const bg = lastSchedules.background;
  const line = $("schedules-tier");
  const reason = $("schedules-tier-reason");
  const on = $("btn-background-on");
  const off = $("btn-background-off");
  if (!bg) {
    line.textContent = "";
    reason.classList.add("hidden");
    on.classList.add("hidden");
    off.classList.add("hidden");
    return;
  }
  line.textContent = `Refreshes run ${bg.tier_text}.`;
  // Offer the upgrade only when this machine can actually deliver it, and explain the
  // absence when it can't — an unexplained missing button reads as a bug.
  on.classList.toggle("hidden", !bg.offer);
  off.classList.toggle("hidden", bg.tier < 2);
  reason.textContent = bg.reason || "";
  reason.classList.toggle("hidden", !bg.reason);
}

function renderSchedules() {
  const rows = lastSchedules.schedules || [];
  const formOpen = !$("schedule-form").classList.contains("hidden");
  // The board POPULATES here but does not decide whether it is on screen — the rail owns
  // that, and it offers this destination whether or not anything is scheduled. So an empty
  // board must render AS an empty board: it used to bounce the view back to the file list,
  // which would now throw a user out of the page they had just navigated to on the very
  // next poll. The empty hint stands down while the form is open — the user is already
  // being told what this place is for, by filling it in.
  const empty = !rows.length;
  $("schedules-empty").classList.toggle("hidden", !empty || formOpen);
  $("schedules-table").classList.toggle("hidden", empty);
  // The foot states which clock is running and offers to upgrade it; with nothing
  // scheduled it would be answering a question nobody has asked yet.
  $("schedules-foot").classList.toggle("hidden", empty);
  renderBackground();

  const banner = $("schedules-banner");
  const text = ScheduleFmt.banner(lastSchedules);
  banner.textContent = text;
  banner.classList.toggle("hidden", !text);
  banner.classList.toggle("notice-error", lastSchedules.overdue > 0);
  $("btn-run-due").classList.toggle("hidden", !lastSchedules.due);

  const body = $("schedules-table").querySelector("tbody");
  body.textContent = "";
  for (const row of rows) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.className = "schedule-name";
    name.textContent = row.notebook;
    tr.appendChild(name);

    const cadence = document.createElement("td");
    cadence.textContent = row.cadence_text;
    const next = document.createElement("div");
    next.className = "muted schedule-next";
    next.textContent = ScheduleFmt.nextDue(row);
    cadence.appendChild(next);
    tr.appendChild(cadence);

    const status = document.createElement("td");
    const state = ScheduleFmt.state(row);
    const badge = document.createElement("span");
    badge.className = `schedule-badge schedule-${state.tone}`;
    badge.textContent = state.text;
    status.appendChild(badge);
    const last = (row.last_run || {}).at;
    if (last) {
      const stamp = document.createElement("div");
      stamp.className = "muted schedule-next";
      stamp.textContent = ScheduleFmt.when(last);
      status.appendChild(stamp);
    }
    for (const line of [ScheduleFmt.detail(row), ScheduleFmt.autoHint(row)]) {
      if (!line) continue;
      const note = document.createElement("div");
      note.className = "muted schedule-detail";
      note.textContent = line;
      status.appendChild(note);
    }
    tr.appendChild(status);

    const actionsCell = document.createElement("td");
    const actions = [
      ["Run now", () => runRefresh(row.notebook)],
      ["Edit schedule", () => scheduleAction(row.notebook)],
      [row.paused ? "Resume" : "Pause", () =>
        action("/api/schedule/pause", { path: row.notebook, paused: !row.paused }, false)
          .then(applySchedules)],
      ["Unschedule", () =>
        action("/api/schedule/remove", { path: row.notebook }, false).then(applySchedules)],
    ];
    if (row.last_run && row.last_run.artifact) {
      actions.splice(1, 0, ["Open last snapshot", () =>
        action("/api/reveal", { path: row.last_run.artifact }, false)]);
    }
    actionsCell.appendChild(actionsMenu(actions, row.notebook));
    tr.appendChild(actionsCell);
    body.appendChild(tr);
  }
}

async function loadSchedules() {
  const data = await api("/api/schedules");
  if (data && !data.error) applySchedules(data);
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

// The recorded-lineage clause for a row that is about to be destroyed, looked up from the
// last /api/state rows so every destructive confirm can carry it without a new argument.
// "" when nothing is recorded — a dialog must never gain a reassuring "nothing depends on
// this", which is a claim lineage cannot make.
function impactClause(path) {
  const file = lastFiles.find((f) => f.path === path);
  return file ? LineageFmt.impactWarning(file.lineage) : "";
}

function deleteAction(path, kind) {
  const name = path.split("/").pop();
  const what = kind === "project" ? `the Power BI project ${name}` : name;
  // Delete is strictly more destructive than Pull — it removes the local file and, once
  // pushed, removes it for the whole team — so the row that badges "3 notebooks read
  // this" must not offer a bare confirm that says nothing about them.
  const ok = confirm(
    `Delete ${what} from your workspace?\n\n` +
    "This removes the local file(s). Push or Propose afterwards to remove it from the " +
    "team repo." + impactClause(path)
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
      : "\n\nThis cannot be undone.") +
    impactClause(path)
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
      "not run under the repo's current packages." + impactClause(path)
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
  setCentreView("history");
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
  closeCentreView("history");
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
  setCentreView("review");
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
      closeCentreView("review");
      reviewPath = null;
    }
  });
}

$("review-push").addEventListener("click", () => reviewSend("/api/push"));
$("review-propose").addEventListener("click", () => reviewSend("/api/propose"));
$("review-close").addEventListener("click", () => {
  closeCentreView("review");
  reviewPath = null;
});

// -- merge cell by cell (the per-cell conflict resolution) -------------------
// The fourth resolution for a conflicted NOTEBOOK, offered beside the three
// whole-file ones (which stay exactly as they were, and are the only option
// when this refuses). Two calls: a read-only plan, then the write. The plan's
// three SHAs ride back with the write so the server can refuse (409) if your
// copy or the team's moved while you were deciding.

let mergePath = null;
let mergePlan = null;
let mergeChoices = {};

async function mergeAction(path) {
  const data = await api("/api/resolve/cells", { path });
  if (data.error) {
    // "unavailable" is a designed answer, not a failure: this conflict can't be
    // merged per cell (not a notebook, restructured, no shared version), so say
    // why and leave the row's three whole-file resolutions to do the job.
    return showError(
      data.unavailable
        ? `${data.error} Use one of the other resolutions on ${path}.`
        : data.error,
    );
  }
  mergePath = path;
  mergePlan = data;
  mergeChoices = {};
  renderMerge();
}

function renderMerge() {
  setCentreView("merge");
  $("merge-title").textContent = `Merge cell by cell — ${mergePath}`;
  $("merge-summary").textContent = MergeFmt.summary(mergePlan);
  const frame = MergeFmt.frameNote(mergePlan);
  $("merge-frame").textContent = frame;
  $("merge-frame").classList.toggle("hidden", !frame);
  const box = $("merge-cells");
  box.textContent = ""; // clear children — cell diffs are untrusted, plain text only
  for (const block of MergeFmt.buildBlocks(mergePlan)) {
    const cell = document.createElement("div");
    cell.className = block.status === "choice" ? "merge-cell merge-choice" : "merge-cell";
    const label = document.createElement("div");
    label.className = "merge-cell-label";
    label.textContent = block.label;
    cell.appendChild(label);
    if (block.options.length) cell.appendChild(mergeOptions(block));
    if (block.diff) {
      const pre = document.createElement("pre");
      pre.className = "merge-cell-diff";
      pre.textContent = block.diff;
      cell.appendChild(pre);
    }
    box.appendChild(cell);
  }
  updateMergeReady();
  card.scrollIntoView({ block: "nearest" });
}

// One radio group per contested cell, named by the cell id so the browser
// enforces "at most one side wins" for us. Nothing is checked initially.
function mergeOptions(block) {
  const row = document.createElement("div");
  row.className = "merge-options";
  for (const option of block.options) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `merge-${block.id}`;
    input.value = option.value;
    input.checked = mergeChoices[block.id] === option.value;
    input.addEventListener("change", () => {
      mergeChoices[block.id] = option.value;
      updateMergeReady();
    });
    const text = document.createElement("span");
    text.textContent = option.label;
    label.append(input, text);
    row.appendChild(label);
  }
  return row;
}

// The write button stays disabled until every contested cell has a side. The
// count of what's left is the whole instruction, so it lives on the button.
function updateMergeReady() {
  const left = MergeFmt.unresolved(mergePlan, mergeChoices).length;
  const button = $("merge-apply");
  button.disabled = left > 0;
  button.textContent = left
    ? `Choose ${left} more cell${left === 1 ? "" : "s"}`
    : "Write the merged notebook";
}

function mergeApply() {
  if (!mergePath || !MergeFmt.ready(mergePlan, mergeChoices)) return;
  const body = {
    path: mergePath,
    choices: mergeChoices,
    base_sha: mergePlan.base_sha,
    local_sha: mergePlan.local_sha,
    remote_sha: mergePlan.remote_sha,
  };
  // Through action() so the busy cue, the log card, and the trash Undo toast
  // (the merge's safety net) all work exactly as they do for pull/resolve.
  action("/api/resolve/cells/apply", body, true, "Merging cells…").then((data) => {
    if (!data || data.error) return;
    closeMerge();
  });
}

function closeMerge() {
  closeCentreView("merge");
  mergePath = null;
  mergePlan = null;
  mergeChoices = {};
}

$("merge-apply").addEventListener("click", mergeApply);
$("merge-close").addEventListener("click", closeMerge);

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
  if (lastWhatsnew && centreView === "whatsnew") {
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
  setCentreView("whatsnew");
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

$("whatsnew-close").addEventListener("click", () => {
  closeCentreView("whatsnew");
});

function fileActions(file, opts) {
  opts = opts || {};
  const actions = [];
  // Opening the notebook is by far the most common thing to do from a row, so the
  // open actions LEAD the menu — plain "Open" first, then the AI Workbench variants —
  // ahead of the sync/manage actions below, whatever the file's sync state. (The rarer
  // "Enable/Disable AI" toggle is a config action, so it's kept out of this cluster and
  // added further down.)
  //
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
  // "Open with AI" launches the Workbench — the notebook and the copilot side by
  // side in one tab (see openWorkbench). One workbench per notebook; clicking again
  // focuses the existing one. Explain / Review logic open the same Workbench and
  // auto-run /explain or /review once the copilot is ready (both are model turns, so
  // they share the ai_disabled opt-out). A notebook can be opted out of AI (synced
  // mooring.toml) — when it is, these are hidden and the "Enable AI" toggle below
  // offers to turn it back on. Modules (non-notebook .py) get no AI: the copilot
  // operates on notebooks. Plain "Open" (a raw marimo tab, no AI) is added above.
  if (aiChatEnabled && isNotebook && file.has_local && !file.ai_disabled) {
    actions.push(["Open with AI", () => openWorkbenchAction(file.path)]);
    actions.push(["Explain", () => openWorkbenchAction(file.path, { explain: true })]);
    actions.push(["Review logic", () => openWorkbenchAction(file.path, { review: true })]);
  }
  // --- Sync + manage actions follow the open cluster above. ---
  // Offline every NETWORK action is skipped — the banner explains why. The
  // conflict resolves, Push/Propose, "Review changes…" (fetches the base blob),
  // "Discard my changes" (ditto), and "History…" all need the team repo. A
  // conflicted row keeps its badge: the cached remote still classifies it.
  if (file.state === "conflict" && !offlineMode) {
    // "Merge cell by cell…" leads: on a notebook it is the only resolution that
    // keeps BOTH sides' work in one file, and it usually asks nothing (a cell
    // only one of you touched merges itself). Offered on any notebook row —
    // whether this particular conflict is mergeable is a server answer, and it
    // says why and points back here when it isn't. The three whole-file
    // resolutions below are unchanged and remain the fallback.
    if (isNotebook && file.has_local) {
      actions.push(["Merge cell by cell…", () => mergeAction(file.path)]);
    }
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
    // The Excel variant sits right beside it: same step, different last mile. Always
    // offered — whether the notebook named any tables is only knowable by running it,
    // and the server explains what to add when it named none.
    actions.push(["Deliver as Excel", () => deliverExcelAction(file.path)]);
  }
  // Verify: smoke-run the notebook on this machine and badge the row with whether it
  // ran clean (a value-free trust receipt). The "does this still run before I share it?"
  // step. Notebooks only; the badge auto-clears when the file is edited.
  if (isNotebook && file.has_local) {
    actions.push(["Verify runs", () => verifyAction(file.path)]);
  }
  // Run this notebook once per region / entity / month, with one artifact per value —
  // the month-end "same pack, six times" loop. Attended: you watch it and can stop it.
  // The server refuses a notebook that never reads the parameter, since that would write
  // differently-named artifacts holding identical numbers.
  if (isNotebook && file.has_local) {
    actions.push(["Run for each…", () => paramsAction(file.path)]);
  }
  // Schedule a refresh (pull → run → report). ALWAYS offered on a local notebook. It used
  // to appear only once the notebook had verified clean, which quietly made the feature
  // undiscoverable: the server's 409 politely explains "Verify this notebook first", and
  // nobody could ever reach it. So an unverified notebook gets "Verify & schedule…", which
  // does the required step and then opens the form — the prerequisite becomes one click
  // instead of a missing menu item.
  if (isNotebook && file.has_local) {
    const scheduled = (lastSchedules.schedules || []).some((r) => r.notebook === file.path);
    const verified = !!(file.verified && file.verified.passed);
    const label = scheduled ? "Edit refresh schedule" : "Schedule refresh…";
    actions.push(verified
      ? [label, () => scheduleAction(file.path)]
      : ["Verify & schedule…", () => verifyAndSchedule(file.path)]);
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
  // The AI opt-out toggle: the off switch for "this notebook now handles PII; don't let
  // AI touch it by mistake" (and the way back on). A rare config action, so it sits down
  // here with the manage actions rather than in the open cluster at the top. Shown
  // whenever AI is available for this notebook, in either opt state.
  if (aiChatEnabled && isNotebook && file.has_local) {
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
function actionsMenu(actions, label, summaryText) {
  const details = document.createElement("details");
  details.className = "row-menu";

  const summary = document.createElement("summary");
  summary.className = "row-menu-summary";
  summary.textContent = summaryText || "Actions";
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

// One row of the notebook list. Title-first: the notebook's own harvested title is
// the thing you read, its filename the mono line beneath. Two cells — what it is,
// and what state it is in — with hairlines instead of card chrome.
//
// Still a real <table>: every row is the same two columns, which is what a table is
// for, and it gives assistive tech the row/column semantics the old badge soup never
// had. The row is a `role=row` in a `role=grid` list (see renderFiles) so arrow keys
// move a roving selection.
//
// The row carries NO actions. Everything a file can do now lives in the detail panel
// for the SELECTED row, so merely browsing the list can never put a Push — or a
// conflict's "Use remote", which discards local edits — one stray click away.
function buildRow(mainNodes, state, opts) {
  opts = opts || {};
  const tr = document.createElement("tr");
  tr.className = "nb-row";

  const mainTd = document.createElement("td");
  mainTd.className = "cell-main";
  mainTd.append(...mainNodes);

  const stateTd = document.createElement("td");
  stateTd.className = "cell-state";
  // The state word is carried VERBATIM (synced / modified / conflict / …) and coloured
  // by its family. Meaning is in the word as well as the colour — never colour alone.
  const word = document.createElement("div");
  word.className = `state-word state-${STATE_BADGES[state] || "local"}`;
  word.textContent = state;
  stateTd.appendChild(word);
  if (opts.qualifier) {
    const q = document.createElement("div");
    q.className = "state-qualifier";
    q.textContent = opts.qualifier;
    stateTd.appendChild(q);
  }

  tr.append(mainTd, stateTd);
  return tr;
}

// The one fact a row earns beside its filename — at most one, or the subtitle becomes
// the badge cluster it replaced. Ordered by how much it changes what you'd do with the
// file: it isn't a notebook at all > other notebooks depend on it > you asked to watch it.
function rowFact(file) {
  if (file.is_module) return "module";
  if (file.lineage && file.lineage.readers) {
    const n = file.lineage.readers;
    return `${n} notebook${n === 1 ? "" : "s"} read${n === 1 ? "s" : ""} this`;
  }
  if (watchedPaths.has(file.path)) return "watched";
  return "";
}

// The state cell's second line: a short qualifier that says something the state word
// alone doesn't. Only states with an honest, free answer get one — a "modified" row
// would need a cell-level diff to say "3 cells", and inventing a number here would be
// exactly the overclaiming the receipts are careful to avoid.
const STATE_QUALIFIERS = {
  "new local": "not shared",
  "new remote": "not pulled yet",
  "deleted locally": "deleted here",
  "deleted remotely": "deleted by the team",
  "conflict": "both changed",
  "mixed": "both ways",
  "in review": "on your review branch",
};
function rowQualifier(file) {
  return STATE_QUALIFIERS[file.state] || "";
}

function shadowBadge(name, lead) {
  const span = document.createElement("span");
  span.className = "row-shadow";
  span.textContent = `${lead ? " · " : ""}shadows ${name}`;
  span.title =
    `“import ${name}” would load this notebook instead of the ${name} module — ` +
    "rename it; otherwise every notebook in this folder can fail to import.";
  return span;
}

function buildFileRow(file, opts) {
  opts = opts || {};
  // Inside a focused folder the row shows its folder-relative path (`rel`); elsewhere
  // the full path. The title leads — a repo of q3_recon_v2.py files is unreadable by
  // filename — falling back to the filename for a module or an untitled notebook.
  const display = opts.rel && file.rel != null ? file.rel : file.path;
  const base = display.split("/").pop();
  const title = document.createElement("div");
  title.className = "row-title";
  title.textContent = file.title || base;

  // The subtitle never repeats the title. With a harvested title the filename goes
  // beneath it (that is the whole point of leading with the title); without one the
  // title IS the filename, so the line beneath carries where it lives and its one
  // fact instead — "module · 4 notebooks read this" — rather than saying it twice.
  const sub = document.createElement("div");
  sub.className = "row-sub";
  const bits = [];
  if (file.title) bits.push(display);
  else if (display.length > base.length) bits.push(display.slice(0, -base.length));
  const fact = rowFact(file);
  if (fact) bits.push(fact);
  sub.append(bits.join(" · "));
  // A notebook whose filename shadows an importable package (polars.py) is a HAZARD,
  // not a receipt: every other badge moved to the panel, but this one stays on the row
  // in amber, because it is the one that bites before you ever select the file.
  if (file.shadows) sub.appendChild(shadowBadge(file.shadows, bits.length > 0));

  const main = bits.length || file.shadows ? [title, sub] : [title];
  const row = buildRow(main, file.state, { qualifier: rowQualifier(file) });
  row.dataset.path = file.path;
  if (opts.member) row.classList.add("member");
  return row;
}

function buildArtifactRows(artifact, files) {
  const byPath = new Map(files.map((f) => [f.path, f]));
  const memberRows = artifact.members
    .map((path) => byPath.get(path))
    .filter(Boolean)
    .map((file) => {
      const row = buildFileRow(file, { member: true, rel: false });
      row.classList.add("hidden");
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

  const head = document.createElement("div");
  head.className = "row-title row-artifact";
  head.append(caret, name);
  const menu = document.createElement("div");
  menu.className = "row-sub";
  menu.append(detail);
  // The artifact header is a GROUP, not a file, so it has no detail panel of its own to
  // move its actions into — it keeps the <details> menu (never a <select>: the Windows
  // single-ArrowDown footgun) that every row used to carry.
  const header = buildRow(
    actions.length ? [head, menu, actionsMenu(actions, artifact.name)] : [head, menu],
    artifact.state,
    {},
  );
  header.classList.add("artifact-header");
  return [header, ...memberRows];
}

// -- folder view: focus + selection (persisted per workspace) ----------------
// Display-only, client-side. Keyed by the repo slug when connected, else the
// workspace path (local mode), so two workspaces sharing the stable hub origin's
// localStorage don't collide on one key. Best-effort like the theme/watch mirrors:
// private mode just forgets the view between launches.

function folderViewId(state) {
  // A non-lossy encoding, so two distinct workspaces never slug to the same key (a
  // character-class replacement collapses "my proj" and "my_proj" to one string).
  const raw = (state && (state.repo || state.workspace)) || "default";
  return encodeURIComponent(raw);
}

function loadFolderView(id) {
  folderViewKey = id ? `mooring.folderview.${id}` : null;
  let raw;
  try {
    raw = folderViewKey ? localStorage.getItem(folderViewKey) : null;
  } catch {
    // Storage is blocked (not just empty): keep the in-memory view rather than wiping a
    // focus/selection the user set this session — private mode only forgets BETWEEN launches.
    return;
  }
  focusPrefix = "";
  moreOpen = false;
  selectedPath = "";
  try {
    if (raw) {
      const data = JSON.parse(raw) || {};
      focusPrefix = FilesTree.norm(data.focus || "");
      moreOpen = !!data.more;
      selectedPath = typeof data.sel === "string" ? data.sel : "";
    }
  } catch {
    // corrupt JSON — start at All folders with nothing selected.
  }
}

function saveFolderView() {
  try {
    if (folderViewKey) {
      localStorage.setItem(
        folderViewKey,
        JSON.stringify({ focus: focusPrefix, more: moreOpen, sel: selectedPath }),
      );
    }
  } catch {
    // best-effort; the in-memory view still drives this session.
  }
}

// Narrow the whole listing to one folder subtree (or "" to reset to all notebooks).
function setFocus(prefix) {
  focusPrefix = FilesTree.norm(prefix || "");
  saveFolderView();
  setCentreView("list");
  renderWorkspace();
}

// -- selection: the one row the detail panel is about ------------------------
// New state, and deliberately never automatic: an auto-selected row would make the
// panel's Push / "Use remote" ambient — a thing that is simply there, aimed at a file
// you did not choose. Selecting is always an act.

function selectedFile() {
  return selectedPath ? lastFiles.find((f) => f.path === selectedPath) || null : null;
}

function setSelected(path, opts) {
  selectedPath = path || "";
  saveFolderView();
  renderFileSelection();
  renderPanel();
  // Below the panel breakpoint the panel is an overlay drawer: selecting opens it.
  if ((opts || {}).open !== false && selectedPath) openDrawer();
}

function clearSelection() {
  setSelected("");
  closeDrawer();
}

// -- the notebook list -------------------------------------------------------

// Files at or under the current focus, with `rel` relative to it. The focus narrows
// RECURSIVELY (not just to the folder's own files): the folder summary line and the
// rail let you drill deeper for convenience, but no file is ever unreachable from
// where you are — a "focus" that hid files in sub-folders would be a trap.
function scopedFiles(files, focus) {
  const f = FilesTree.norm(focus || "");
  return FilesTree.scope(files, f)
    .map((file) => Object.assign({}, file, {
      rel: f && file.path.length > f.length ? file.path.slice(f.length + 1) : file.path,
    }))
    .sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
}

function renderFiles(files, artifacts, declaredFolders) {
  const table = $("files-table");
  const tbody = table.querySelector("tbody");
  tbody.textContent = "";
  const q = fileQuery.trim();
  const declared = declaredFolders || [];
  // PBIP artifacts keep their own collapsible grouping; everything else is one list.
  const nonArtifact = files.filter((f) => !f.artifact);
  // Catalog presence (UNFILTERED): declared-but-empty folders still count, so the
  // table/empty-hint toggles keep the original "structure is visible" behaviour.
  const hasCatalog = FilesTree.group(nonArtifact, declared).length > 0 || artifacts.length > 0;
  table.classList.toggle("hidden", !hasCatalog);
  $("empty-hint").classList.toggle("hidden", hasCatalog);
  $("files-foot").classList.toggle("hidden", !hasCatalog);

  const searching = !!q;
  const focus = searching ? "" : focusPrefix;
  // While searching, matches come from the WHOLE repo with their full paths, so a hit
  // under a folder you aren't focused on is never hidden by the focus.
  const rows = searching
    ? nonArtifact.filter((f) => FilesTree.matches(f, q))
    : scopedFiles(nonArtifact, focus);
  const shownArtifacts = searching
    ? artifacts.filter((a) => FilesTree.matches({ path: a.pointer || a.name || a.key }, q))
    : (focus
      ? artifacts.filter((a) =>
        FilesTree.scope([{ path: a.pointer || a.name || a.key }], focus).length > 0)
      : artifacts.slice());

  renderFilesHead({ searching, query: q, focus, count: rows.length + shownArtifacts.length,
    total: nonArtifact.length });

  for (const artifact of shownArtifacts) {
    for (const row of buildArtifactRows(artifact, files)) tbody.appendChild(row);
  }
  for (const file of rows) tbody.appendChild(buildFileRow(file, { rel: !searching }));

  if (!rows.length && !shownArtifacts.length && hasCatalog) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.className = "muted";
    td.textContent = searching
      ? `No notebooks match “${q}”.`
      : "Nothing in this folder yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  renderFolderSummary(nonArtifact, declared, searching ? "" : focus, searching);
  wireFileRows();
  renderFileSelection();
}

// The list's column header. Its left cell carries the breadcrumb that replaced the
// old #folder-controls bar: where you are, and every level you can climb back to.
function renderFilesHead(o) {
  const head = $("files-head");
  head.textContent = "";
  if (o.searching) {
    head.append(`MATCHING “${o.query}” · ${o.count} OF ${o.total}`);
    return;
  }
  const crumb = (label, prefix) => {
    const b = document.createElement("button");
    b.className = "crumb";
    b.textContent = label;
    b.title = prefix ? `Focus ${prefix}/` : "Show every notebook";
    b.addEventListener("click", () => setFocus(prefix));
    return b;
  };
  if (!o.focus) {
    head.append(`ALL NOTEBOOKS · ${o.count}`);
    return;
  }
  head.appendChild(crumb("ALL", ""));
  const parts = FilesTree.crumbs(o.focus);
  parts.forEach((c, i) => {
    const sep = document.createElement("span");
    sep.className = "crumb-sep";
    sep.textContent = "/";
    head.appendChild(sep);
    if (i === parts.length - 1) {
      const cur = document.createElement("span");
      cur.className = "crumb current";
      cur.textContent = c.label.toUpperCase() + "/";
      head.appendChild(cur);
    } else {
      head.appendChild(crumb(c.label.toUpperCase(), c.prefix));
    }
  });
  head.append(` · ${o.count}`);
}

// The quiet last line of the list: the folders you are NOT looking at, with their
// counts — "data/ 26 · reports/ 6 · more 3". Clicking one focuses it. Featured
// folders lead; the rest fold behind "more N" (the same personal, persisted fold the
// old "More folders" disclosure used).
const SUMMARY_VISIBLE = 6;
function renderFolderSummary(files, declared, focus, searching) {
  const foot = $("files-foot");
  foot.textContent = "";
  if (searching) return;
  const t = FilesTree.tree(files, declared, focus);
  const part = !focus && lastFeatured.length
    ? FilesTree.partitionFeatured(t.folders, lastFeatured)
    : { featured: [], rest: t.folders };
  const ordered = part.featured.concat(part.rest);
  if (!ordered.length) return;
  const shown = moreOpen ? ordered : ordered.slice(0, SUMMARY_VISIBLE);
  shown.forEach((node, i) => {
    if (i) foot.append(" · ");
    const b = document.createElement("button");
    b.className = "folder-jump";
    b.textContent = `${node.name}/ ${node.count}`;
    b.title = `Focus ${node.path}/`;
    b.addEventListener("click", () => setFocus(node.path));
    foot.appendChild(b);
  });
  if (ordered.length > SUMMARY_VISIBLE) {
    foot.append(" · ");
    const more = document.createElement("button");
    more.className = "folder-jump";
    more.textContent = moreOpen ? "fewer" : `more ${ordered.length - SUMMARY_VISIBLE}`;
    more.addEventListener("click", () => {
      moreOpen = !moreOpen;
      saveFolderView();
      renderWorkspace();
    });
    foot.appendChild(more);
  }
}

// Click / keyboard on the list. A roving-tabindex grid: ↑/↓ move the selection,
// Enter opens, Space selects without opening. Real buttons inside a row (the PBIP
// caret, its actions menu) keep their own behaviour — the row handler ignores them.
function wireFileRows() {
  const rows = Array.from($("files-table").querySelectorAll("tr.nb-row[data-path]"));
  rows.forEach((row) => {
    row.tabIndex = -1;
    row.setAttribute("role", "row");
    row.addEventListener("click", (e) => {
      if (e.target.closest("button, a, summary, input")) return;
      setSelected(row.dataset.path);
    });
    row.addEventListener("dblclick", (e) => {
      if (e.target.closest("button, a, summary, input")) return;
      openSelected();
    });
    row.addEventListener("keydown", (e) => {
      const i = rows.indexOf(row);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = rows[i + (e.key === "ArrowDown" ? 1 : -1)];
        if (next) {
          next.focus();
          setSelected(next.dataset.path, { open: false });
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        setSelected(row.dataset.path);
        openSelected();
      } else if (e.key === " ") {
        e.preventDefault();
        setSelected(row.dataset.path);
      }
    });
  });
}

// Paint the selection onto the rows. Separate from building them so a selection
// change costs a class flip rather than a re-render of the whole list.
function renderFileSelection() {
  const rows = $("files-table").querySelectorAll("tr.nb-row[data-path]");
  let first = null;
  rows.forEach((row) => {
    const on = row.dataset.path === selectedPath;
    row.classList.toggle("selected", on);
    row.setAttribute("aria-selected", on ? "true" : "false");
    row.tabIndex = on ? 0 : -1;
    if (!first) first = row;
  });
  // Keep exactly one row in the tab order even with nothing selected, so Tab reaches
  // the list and the arrow keys take over from there.
  if (!selectedPath && first) first.tabIndex = 0;
}

// Select (and scroll to) the first conflicted row — the "Resolve" primary action.
function selectFirstConflict() {
  const hit = lastFiles.find((f) => f.state === "conflict");
  if (!hit) return;
  setFocus("");
  setSelected(hit.path);
  const row = $("files-table").querySelector(`tr.nb-row[data-path="${cssAttr(hit.path)}"]`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function cssAttr(value) {
  return window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/["\\]/g, "\\$&");
}

// -- the rail: destinations, folders, and what is offered to the copilot -----

// One rail nav item. `count` is rendered right-aligned and coloured by severity, so
// "1 review waiting" and "1 refresh overdue" read differently at a glance. Each item
// also carries a glyph, which is all that survives when the rail collapses to a 56px
// icon strip on a narrow window — the glyphs are the ones these destinations already
// used on their old header links (✎ Reviews, ⏱ Activity, ⚙ Settings).
function railItem(o) {
  const el = document.createElement(o.href ? "a" : "button");
  el.className = "rail-item" + (o.active ? " active" : "");
  if (o.href) {
    el.href = o.href;
  } else {
    el.type = "button";
    el.addEventListener("click", o.onClick);
  }
  const caret = document.createElement("span");
  caret.className = "rail-caret";
  caret.textContent = o.active ? "▶" : "";
  caret.setAttribute("aria-hidden", "true");
  const glyph = document.createElement("span");
  glyph.className = "rail-icon";
  glyph.textContent = o.glyph || "·";
  glyph.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "rail-item-label";
  label.textContent = o.label;
  el.append(caret, glyph, label);
  if (o.count) {
    const n = document.createElement("span");
    n.className = `rail-count rail-${o.tone || "faint"}`;
    n.textContent = String(o.count);
    el.appendChild(n);
    // The collapsed strip has no room for the number, so severity travels as a dot.
    // Only a count that MEANS something (a waiting review, an overdue refresh) gets
    // one — a plain file count would just be decoration.
    if (o.tone && o.tone !== "faint") {
      const dot = document.createElement("span");
      dot.className = `rail-dot rail-${o.tone}`;
      dot.textContent = "●";
      dot.setAttribute("aria-hidden", "true");
      el.appendChild(dot);
    }
  }
  // The label is hidden in the collapsed strip, so the title carries it there.
  el.title = o.title || o.label;
  return el;
}

function renderRailNav() {
  const nav = $("rail-nav");
  nav.textContent = "";
  const items = [];
  // Notebook destinations need a notebook surface; Activity (machine-local) and
  // Settings (per-machine config) are added below whatever the login state — they are
  // exactly where you go when the login itself is what's wrong.
  if (filesVisible) {
    items.push({ id: "list", label: "notebooks", glyph: "▤",
      count: lastFiles.filter((f) => !f.artifact).length,
      onClick: () => { setCentreView("list"); } });
  }
  // The first-run checklist is a rail destination while it is unfinished — it used to
  // be a card everyone scrolled past, including people who had finished it.
  if (checklistKey) {
    const stored = checklistStored();
    const items4 = Checklist.derive(lastFiles, lastReview, stored);
    const done = items4.filter((i) => i.done).length;
    if (!stored.dismissed && !Checklist.isDone(items4)) {
      items.push({ id: "checklist", label: "getting started", glyph: "◎",
        count: `${done}/${items4.length}`, tone: "accent",
        onClick: () => setCentreView("checklist") });
    }
  }
  if (lastLoggedIn) {
    items.push({ id: "reviews", label: "reviews", href: "/reviews", glyph: "✎",
      count: lastReview ? 1 : 0, tone: "review",
      title: "Review teammates' proposed changes" });
  }
  // The board is a DESTINATION, not a notification: it is offered wherever there are
  // notebooks, including before the first schedule exists — otherwise the one place that
  // explains scheduled refreshes is reachable only by people who already use them. A zero
  // count renders no badge at all (railItem skips a falsy count), so an empty board is
  // quiet rather than nagging.
  if (filesVisible) {
    const sched = (lastSchedules.schedules || []).length;
    items.push({ id: "schedules", label: "schedules", glyph: "↻",
      count: lastSchedules.overdue || sched,
      tone: lastSchedules.overdue ? "bad" : "faint",
      title: "Scheduled refreshes: what re-runs by itself, and how the last run went",
      onClick: () => setCentreView("schedules") });
  }
  items.push({ id: "activity", label: "activity", href: "/activity", glyph: "⏱",
    title: "Recent activity & trash (local to this machine)" });
  items.push({ id: "settings", label: "settings", href: "/settings", glyph: "⚙",
    title: "Settings & preferences (including appearance)" });
  for (const item of items) {
    nav.appendChild(railItem(Object.assign({ active: centreView === item.id }, item)));
  }
}

// The folder navigator. Each row focuses a folder; the ☆/★ feature star (which
// curates the team's synced display order) and the ◆/◇ AI-context toggle (which
// governs what the copilot may READ) moved here from the old per-folder table rows.
// Both keep their endpoints and their meaning; they are shown when set and revealed
// on hover/focus otherwise, so the narrow rail stays legible without hiding state.
function renderRailFolders(declared) {
  const box = $("rail-folders");
  box.textContent = "";
  if (!filesVisible) return;
  const nonArtifact = lastFiles.filter((f) => !f.artifact);
  const t = FilesTree.tree(nonArtifact, declared || [], "");
  const part = lastFeatured.length
    ? FilesTree.partitionFeatured(t.folders, lastFeatured)
    : { featured: [], rest: t.folders };
  const nodes = part.featured.concat(part.rest);
  if (!nodes.length && !lastFeatured.length && !lastContextFolders.length) return;

  const label = document.createElement("div");
  label.className = "rail-label";
  label.textContent = "FOLDERS";
  box.appendChild(label);

  const list = document.createElement("div");
  list.className = "rail-folder-list";
  const featuredSet = new Set(lastFeatured.map(FilesTree.norm));
  const contextSet = new Set(lastContextFolders.map(FilesTree.norm));
  for (const node of nodes) {
    list.appendChild(railFolderRow(node.path, node.name, node.count, featuredSet, contextSet));
  }
  // A featured / offered folder that no longer exists in the repo has no node of its
  // own, so without a row here the dead entry would linger in the synced mooring.toml
  // forever with no control left to clear it.
  const live = new Set(FilesTree.allFolderPaths(t));
  const stale = new Set();
  for (const raw of lastFeatured.concat(lastContextFolders)) {
    const p = FilesTree.norm(raw);
    if (!p || live.has(p) || stale.has(p)) continue;
    if (FilesTree.focusLive(nonArtifact, declared || [], p)) continue;
    stale.add(p);
    list.appendChild(railFolderRow(p, p, 0, featuredSet, contextSet, true));
  }
  box.appendChild(list);
  renderAdopt(box);
}

function railFolderRow(path, name, count, featuredSet, contextSet, gone) {
  const row = document.createElement("div");
  row.className = "rail-folder" + (focusPrefix === path ? " active" : "") + (gone ? " gone" : "");

  const jump = document.createElement("button");
  jump.className = "rail-folder-name";
  jump.textContent = `${name}/`;
  jump.title = gone ? `${path}/ is no longer in the repo` : `Focus ${path}/`;
  jump.disabled = !!gone;
  jump.addEventListener("click", () => setFocus(path));
  row.appendChild(jump);

  // ☆/★ curates the team's display order. Repo mode only — starring is a team act,
  // and a local-only user has nobody to share an order with.
  if (canFeature) {
    const featured = featuredSet.has(path);
    const star = document.createElement("button");
    star.className = "rail-glyph feature-star" + (featured ? " on" : "");
    star.textContent = featured ? "★" : "☆";
    star.title = gone
      ? "This folder no longer exists — click to un-star it for the team"
      : featured
        ? "Featured for the team — click to un-star"
        : "Feature this folder for the team (pins it to the top)";
    star.setAttribute("aria-pressed", featured ? "true" : "false");
    star.setAttribute("aria-label", `Feature ${path} for the team`);
    star.addEventListener("click", () =>
      action("/api/hub/feature", { folder: path, featured: !featured }));
    row.appendChild(star);
  }
  // ◆/◇ offers the folder as team copilot context. Independent of the star: this
  // governs what the model reads, not display order.
  if (canCurateContext) {
    const offered = contextSet.has(path);
    const b = document.createElement("button");
    b.className = "rail-glyph ctx-folder" + (offered ? " on" : "");
    b.textContent = offered ? "◆" : "◇";
    b.title = gone
      ? "This folder no longer exists — click to withdraw it from the team AI context"
      : offered
        ? "Offered as team AI context — click to withdraw"
        : "Offer this folder as team AI context (the copilot reads it; reading needs [ai] context on)";
    b.setAttribute("aria-pressed", offered ? "true" : "false");
    b.setAttribute("aria-label", `Offer ${path} as team AI context`);
    b.addEventListener("click", () =>
      action("/api/hub/context-folder", { folder: path, offered: !offered }));
    row.appendChild(b);
  }
  if (!gone) {
    const n = document.createElement("span");
    n.className = "rail-count rail-faint";
    n.textContent = String(count);
    row.appendChild(n);
  }
  return row;
}

// The adopt prompt: notebook folders the repo keeps OUTSIDE the synced set. It used
// to be a fourth notice box at the top of the page; it is the same offer with the
// same per-folder labels and the same adoptFolders() call, in the rail under the
// folders it is about.
let adoptCandidates = [];
function renderAdopt(box) {
  if (!adoptCandidates.length) return;
  const wrap = document.createElement("details");
  wrap.className = "rail-adopt";
  const sum = document.createElement("summary");
  sum.textContent = `adopt ${adoptCandidates.length} folder${adoptCandidates.length === 1 ? "" : "s"}`;
  sum.title = "Folders with files outside your synced folders — adopt them to sync " +
    "their notebooks (and helper modules) for the team.";
  wrap.appendChild(sum);
  const panel = document.createElement("div");
  panel.className = "rail-adopt-panel";
  for (const c of adoptCandidates) {
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = `Adopt ${c.folder} (${c.py_files} .py)`;
    btn.addEventListener("click", () => adoptFolders([c.folder]));
    panel.append(btn);
  }
  if (adoptCandidates.length > 1) {
    const all = document.createElement("button");
    all.className = "small primary";
    all.textContent = "Adopt all";
    all.addEventListener("click", () => adoptFolders(adoptCandidates.map((c) => c.folder)));
    panel.append(all);
  }
  wrap.appendChild(panel);
  box.appendChild(wrap);
}

// The per-user AI context subscription: which of the repo's OFFERED context folders
// THIS machine's copilot reads (the synced offer stays the ceiling). Shown only in
// repo mode with AI + [ai] context on and a non-empty offer. Toggling a box POSTs a
// per-user subscription change — it changes READ scope only.
function renderContextSubscription() {
  const panel = $("context-sub");
  if (!panel) return;
  const offer = lastContextFolders;
  const show = canCurateContext && lastAiContext && offer.length > 0;
  panel.classList.toggle("hidden", !show);
  panel.textContent = "";
  if (!show) return;
  const reading = new Set(lastSelectedContext);
  const wrap = document.createElement("details");
  const head = document.createElement("summary");
  head.className = "context-sub-head";
  head.textContent = `copilot context · ${reading.size} of ${offer.length}`;
  head.title = `The copilot reads ${reading.size} of the ${offer.length} folder(s) this ` +
    "repo offers as context.";
  wrap.appendChild(head);
  const list = document.createElement("div");
  list.className = "context-sub-list";
  for (const folder of offer) {
    const label = document.createElement("label");
    label.className = "context-sub-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = reading.has(folder);
    box.addEventListener("change", () =>
      action("/api/ai/context/subscribe", { folder, on: box.checked }));
    const span = document.createElement("span");
    span.textContent = " " + folder;
    label.append(box, span);
    list.appendChild(label);
  }
  wrap.appendChild(list);
  panel.appendChild(wrap);
}

// -- the header block: one sentence, one action ------------------------------

// When this tab started, and whether it started in the morning — the two inputs
// Headline.derive needs to decide whether incoming work "came in overnight" or is
// simply "waiting". A claim about WHEN work arrived has to be earned.
const sessionMorning = new Date().getHours() < 12;
let pullWaitedAtStart = null;

function renderHeaderBlock() {
  // Meta line: three segments, so it never wraps.
  const meta = $("meta-line");
  const now = new Date();
  const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const parts = [`${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()}`];
  const total = lastFiles.filter((f) => !f.artifact).length;
  if (filesVisible) parts.push(`${total} FILE${total === 1 ? "" : "S"}`);
  if (lastStateAt != null) {
    // Reuse Freshness.ageText — the age wording is unit-tested there and must not be
    // re-derived here, where it would quietly drift.
    parts.push(`CHECKED ${Freshness.ageText(Date.now() - lastStateAt).toUpperCase()}`);
  }
  meta.textContent = parts.join("\u00a0 / \u00a0");

  if (pullWaitedAtStart === null && lastLoggedIn) {
    pullWaitedAtStart = Freshness.pullCount(lastFiles) > 0;
  }
  const block = Headline.derive({
    mode: lastMode,
    loggedIn: lastLoggedIn,
    offline: offlineMode,
    files: lastFiles,
    review: lastReview,
    pullWaitedAtStart: !!pullWaitedAtStart,
    morning: sessionMorning,
  });
  $("headline").textContent = block.text;

  const row = $("action-row");
  row.textContent = "";
  if (block.primary) {
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = block.primary.label;
    btn.addEventListener("click", HEADLINE_ACTIONS[block.primary.id]);
    row.appendChild(btn);
  }
  for (const link of block.links) {
    const a = document.createElement("button");
    a.className = "mono-link";
    a.textContent = link.label;
    a.addEventListener("click", HEADLINE_ACTIONS[link.id]);
    row.appendChild(a);
  }
  // Everything else the workspace can do, in one <details> menu — the nine-button
  // toolbar's contents, with their existing labels and confirm flows.
  row.appendChild(actionsMenu(workspaceActions(), "workspace", "more"));
}

// Every workspace-level action, whatever the headline chose to promote. Kept
// unconditional in shape (rather than appearing and disappearing) so the menu is a
// place you can learn; only genuinely unavailable actions are omitted.
function workspaceActions() {
  const acts = [["Refresh", refresh]];
  if (lastLoggedIn && !offlineMode) {
    acts.push(
      ["Pull", pullAll],
      ["What’s new", whatsnewAction],
      ["Push all", pushAll],
      ["Propose", proposeAll],
    );
    if (canRecall) acts.push(["Recall push", recallAction]);
  }
  if (filesVisible) {
    acts.push(["New notebook", newNotebook], ["Check all notebooks run…", sweepAction]);
    if (aiBatchEnabled) {
      acts.push(["Batch build…", () => window.open("/ai/batch", "mooringBatch")]);
    }
  }
  acts.push(["Last operation", () => setCentreView("log")]);
  acts.push(["Workspace, packages & health", () => setCentreView("workspace")]);
  return acts;
}

const HEADLINE_ACTIONS = {
  pull: () => pullAll(),
  push: () => pushAll(),
  propose: () => proposeAll(),
  new: () => newNotebook(),
  search: () => toggleSearch(),
  resolve: () => selectFirstConflict(),
  "review-pr": () => lastReview && openExternal(lastReview.compare_url),
};

// The search box reveals itself on demand (and stays open while a query is live) so
// the header keeps its rhythm on the ordinary "I know which notebook I want" morning.
function toggleSearch() {
  const box = $("file-search");
  const open = box.classList.contains("hidden");
  box.classList.toggle("hidden", !open);
  if (open) return box.focus();
  // Closing the box CLEARS the filter. A hidden box with a live query would leave the
  // list quietly filtered with nothing on screen saying so — the one failure mode a
  // reveal-on-demand search box can have.
  box.value = "";
  fileQuery = "";
  renderWorkspace();
}

// Create a notebook. A focused folder becomes the default location — the "New here"
// button each folder row used to carry, without the row.
function newNotebook() {
  const where = focusPrefix ? ` in ${focusPrefix}/` : "";
  const name = prompt(
    `Notebook name or path${where}\n(e.g. sales-analysis, or packages/finance/notebooks/sales):`,
  );
  if (!name) return;
  const full = focusPrefix && !name.includes("/") ? `${focusPrefix}/${name}` : name;
  return action("/api/new", { name: full });
}

// -- the detail panel: everything about the selected notebook ----------------

// Panel action labels that are NOT part of the "more" menu, because they belong to a
// block of their own: the STATE block's sync verbs, the SCHEDULE block's, and HISTORY's.
// Every label here MUST be rendered by the block that claims it — a label taken out of the
// list and then never appended vanishes from the UI altogether.
const STATE_ACTIONS = {
  "Review changes…": "review",
  "Push": "push",
  "Propose": "propose",
  "Merge cell by cell…": "merge cell by cell",
  "Use remote": "use remote",
  "Keep both": "keep both",
  "Push as copy": "push as copy",
};
// Scheduling, in whichever of its three shapes fileActions offered (see there: the verify
// prerequisite becomes part of the action rather than hiding it).
const SCHEDULE_ACTIONS = {
  "Schedule refresh…": "schedule refresh",
  "Edit refresh schedule": "edit schedule",
  "Verify & schedule…": "verify & schedule",
};
// History is its own block: "what changed last week" is a different question from "will
// this still be fresh tomorrow", and the two used to share a heading that announced
// scheduling and then offered a parameter sweep. "Run for each…" is a RUN action, so it
// went back to the "more" menu beside Deliver and Verify runs where the other ones live.
const HISTORY_ACTIONS = {
  "History…": "history",
};

// ScheduleFmt's tone names -> the panel's colour tokens.
const SCHEDULE_TONES = { good: "ok", warn: "warn", bad: "bad", idle: "muted" };

function panelLabel(text) {
  const el = document.createElement("div");
  el.className = "panel-label";
  el.textContent = text;
  return el;
}

function panelLinks(pairs) {
  const row = document.createElement("div");
  row.className = "panel-links";
  for (const [label, handler] of pairs) {
    const b = document.createElement("button");
    b.className = "mono-link";
    b.textContent = label;
    b.addEventListener("click", handler);
    row.appendChild(b);
  }
  return row;
}

function openSelected() {
  const file = selectedFile();
  if (!file) return;
  const pair = fileActions(file, {}).find(([t]) => t === "Open" || t === "Reveal");
  if (pair) pair[1]();
}

function renderPanel() {
  const panel = $("panel");
  panel.textContent = "";
  const file = selectedFile();

  const head = document.createElement("div");
  head.appendChild(panelLabel("SELECTED"));
  if (!file) {
    // No disabled buttons: an empty panel says what to do, it doesn't mime the full
    // one with everything greyed out.
    const hint = document.createElement("div");
    hint.className = "panel-empty";
    hint.textContent = "select a notebook";
    head.appendChild(hint);
    panel.appendChild(head);
    return;
  }
  const title = document.createElement("div");
  title.className = "panel-title";
  title.textContent = file.title || file.path.split("/").pop();
  const path = document.createElement("div");
  path.className = "panel-path";
  path.textContent = file.path;
  head.append(title, path);
  panel.appendChild(head);

  // --- actions: the open cluster leads, exactly as fileActions orders it -----
  const all = fileActions(file, {});
  const take = (label) => {
    const i = all.findIndex(([t]) => t === label);
    return i === -1 ? null : all.splice(i, 1)[0];
  };
  const open = take("Open") || take("Reveal");
  const withAi = take("Open with AI");
  const stateLinks = [];
  for (const label of Object.keys(STATE_ACTIONS)) {
    const pair = take(label);
    if (pair) stateLinks.push([STATE_ACTIONS[label], pair[1]]);
  }
  const scheduleLinks = [];
  for (const label of Object.keys(SCHEDULE_ACTIONS)) {
    const pair = take(label);
    if (pair) scheduleLinks.push([SCHEDULE_ACTIONS[label], pair[1]]);
  }
  const historyLinks = [];
  for (const label of Object.keys(HISTORY_ACTIONS)) {
    const pair = take(label);
    if (pair) historyLinks.push([HISTORY_ACTIONS[label], pair[1]]);
  }

  const actions = document.createElement("div");
  actions.className = "panel-actions";
  if (open) {
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = open[0];
    btn.addEventListener("click", open[1]);
    actions.appendChild(btn);
  }
  if (withAi) {
    const b = document.createElement("button");
    b.className = "mono-link";
    b.textContent = "open with AI";
    b.addEventListener("click", withAi[1]);
    actions.appendChild(b);
  }
  if (all.length) actions.appendChild(actionsMenu(all, file.path, "more"));
  if (actions.childNodes.length) panel.appendChild(actions);

  // --- STATE ----------------------------------------------------------------
  const stateBlock = document.createElement("div");
  stateBlock.className = "panel-block panel-block-strong";
  stateBlock.appendChild(panelLabel("STATE"));
  const line = document.createElement("div");
  line.className = "panel-state";
  const word = document.createElement("span");
  word.className = `state-word state-${STATE_BADGES[file.state] || "local"}`;
  word.textContent = file.state;
  line.appendChild(word);
  const qual = rowQualifier(file);
  if (qual) {
    const q = document.createElement("span");
    q.className = "state-qualifier";
    q.textContent = " " + qual;
    line.appendChild(q);
  }
  stateBlock.appendChild(line);
  if (stateLinks.length) stateBlock.appendChild(panelLinks(stateLinks));
  panel.appendChild(stateBlock);

  // --- RECEIPTS: the value-free ledger -------------------------------------
  // The same claims the row badges made, in the same words, with the same caveats
  // on their title text. A receipt with no payload contributes no line at all —
  // never a placeholder, never a reassuring "nothing to report".
  const receipts = ReceiptsFmt.lines(file, LineageFmt);
  if (receipts.length) {
    const block = document.createElement("div");
    block.className = "panel-block";
    block.appendChild(panelLabel("RECEIPTS"));
    for (const r of receipts) {
      const row = document.createElement("div");
      row.className = "receipt";
      row.title = r.title;
      const code = document.createElement("span");
      code.className = `receipt-code tone-${r.tone}`;
      code.textContent = r.code;
      row.append(code, r.text);
      block.appendChild(row);
    }
    panel.appendChild(block);
  }

  // --- SCHEDULE -------------------------------------------------------------
  // Whether this notebook re-runs by itself, and how to change that. A block of its own:
  // it is the only part of the panel that talks about the FUTURE, and the answer is worth
  // reading even when it is "no schedule yet" — which is why the block appears as soon as
  // there is a scheduling action to offer, not only once a schedule exists.
  const sched = (lastSchedules.schedules || []).find((r) => r.notebook === file.path);
  if (sched && !ScheduleFmt.isDone(sched)) {
    // Run now belongs with the schedule, not in the "more" menu: it is the manual half of
    // the same idea — the button you reach for when the board says this one is due. Gated
    // on isDone for that reason: offering it under a badge reading "done" would advertise a
    // refresh this schedule no longer owes. Re-running a finished one-off is still possible,
    // via Verify runs or by re-dating it — this link just stops claiming to be the schedule.
    scheduleLinks.push(["run now", () => runRefresh(file.path)]);
  }
  // Either half earns the block on its own: a finished one-shot may contribute no link at
  // all, and its badge is still the answer to "does this refresh itself?".
  if (sched || scheduleLinks.length) {
    const block = document.createElement("div");
    block.className = "panel-block";
    block.appendChild(panelLabel("SCHEDULE"));
    if (sched) {
      // ScheduleFmt already decides the tone, and its ordering is the load-bearing
      // part (paused and overdue outrank "it ran clean on Monday", and a finished
      // one-shot outranks both) — so the wording and the severity both come from
      // there, only the token names are mapped.
      const state = ScheduleFmt.state(sched);
      const row = document.createElement("div");
      row.append(`${sched.cadence_text} · `);
      const tone = document.createElement("span");
      tone.className = `tone-${SCHEDULE_TONES[state.tone] || "muted"}`;
      tone.textContent = state.text;
      row.appendChild(tone);
      block.appendChild(row);
    }
    if (scheduleLinks.length) block.appendChild(panelLinks(scheduleLinks));
    panel.appendChild(block);
  }

  // --- HISTORY --------------------------------------------------------------
  // "in your last push" is what /api/state can honestly say about this file's
  // history without a round trip: the manifest records WHICH files the last push
  // wrote, not when or by whom. History… has the dated detail.
  const inLastPush = canRecall && recallPaths.includes(file.path);
  if (inLastPush || historyLinks.length) {
    const block = document.createElement("div");
    block.className = "panel-block";
    block.appendChild(panelLabel("HISTORY"));
    if (inLastPush) {
      const row = document.createElement("div");
      row.className = "panel-faint";
      row.textContent = "in your last push · recall available";
      block.appendChild(row);
    }
    if (historyLinks.length) block.appendChild(panelLinks(historyLinks));
    panel.appendChild(block);
  }
}

// -- centre views ------------------------------------------------------------
// Every panel the hub had as a stacked card now OPENS IN THE CENTRE, replacing the
// list, with one "← back to notebooks" link at the top. The detail panel stays put,
// so the context of what you were looking at survives the trip.

const CENTRE_VIEWS = {
  list: "files-card",
  accounts: "accounts-view",
  checklist: "checklist-card",
  schedules: "schedules-card",
  params: "params-card",
  log: "log-card",
  history: "history-card",
  review: "review-card",
  merge: "merge-card",
  whatsnew: "whatsnew-card",
  workspace: "workspace-card",
};

function setCentreView(name) {
  centreView = CENTRE_VIEWS[name] ? name : "list";
  syncCentre();
  renderRailNav();
}

// Close ONE view if it happens to be the one showing (used when its data goes away —
// e.g. going offline closes Review and History). Never yanks an unrelated view.
function closeCentreView(name) {
  if (centreView === name) setCentreView("list");
}

function syncCentre() {
  for (const [view, id] of Object.entries(CENTRE_VIEWS)) {
    const el = $(id);
    if (!el) continue;
    const show = view === centreView && (view !== "list" || filesVisible);
    el.classList.toggle("hidden", !show);
  }
  $("centre-back").classList.toggle("hidden", centreView === "list");
  // The header block is about the WORKSPACE; on a detail view it would be answering a
  // question you have stopped asking. The error banner and the sweep's progress line
  // live outside it and are never hidden by a view change.
  $("centre-head").classList.toggle("hidden", centreView !== "list");
}

// -- the responsive panel drawer --------------------------------------------
// Below the panel breakpoint the panel becomes a right-anchored overlay: selecting a
// row opens it, Esc and the close link dismiss it.

function openDrawer() {
  document.body.classList.add("drawer-open");
}
function closeDrawer() {
  document.body.classList.remove("drawer-open");
}

// Re-render everything that derives from the last /api/state rows, without re-fetching.
function renderWorkspace() {
  renderFiles(lastFiles, lastArtifacts, lastFolders);
  renderRailNav();
  renderRailFolders(lastFolders);
  renderHeaderBlock();
  renderPanel();
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
  // The checklist is a rail destination now (renderRailNav offers it only while it is
  // unfinished and undismissed), so this populates it and steps aside if the
  // destination has just disappeared under the user.
  const stored = checklistStored();
  const items = checklistKey ? Checklist.derive(lastFiles, lastReview, stored) : [];
  const gone = !checklistKey || !!stored.dismissed || Checklist.isDone(items);
  if (gone) {
    if (centreView === "checklist") setCentreView("list");
    return;
  }
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

$("checklist-dismiss").addEventListener("click", () => {
  checklistSet("dismissed");
  setCentreView("list");
});

// Catalog search: filter the file listing as you type (client-side, no network). Re-render
// from the last /api/state rows so the filter is instant and survives background polls.
$("file-search").addEventListener("input", (event) => {
  fileQuery = event.target.value || "";
  renderWorkspace();
});

// The repo identity discovery last ran for. Discovery costs a full-tree fetch on
// the server, so we run it once per repo-session (on login / repo switch), NOT on
// every refresh() — and force a re-check (null) after an adopt so the banner clears.
let lastDiscoverRepo = null;

async function maybeDiscover(state) {
  const clear = () => {
    adoptCandidates = [];
    lastDiscoverRepo = null;
    renderRailFolders(lastFolders);
  };
  if (!state.logged_in) return clear();
  if (offlineMode) {
    // Offline mode keeps logged_in true, so without this the one-per-repo-session
    // shot would be burnt on a discovery that cannot succeed — and the adopt
    // prompt would then never appear after connectivity returns. Re-arm instead.
    return clear();
  }
  if (state.repo === lastDiscoverRepo) return;  // already checked this repo this session
  lastDiscoverRepo = state.repo;
  try {
    const data = await api("/api/discover");
    adoptCandidates = data.candidates || [];
    renderRailFolders(lastFolders);
  } catch {
    clear();  // discovery is a non-essential prompt; never block
  }
}

function adoptFolders(folders) {
  // Force a re-check after the adopt (action() refreshes) so the rail reflects the
  // now-narrowed candidate set — the adopted folders drop out, leaving any others.
  lastDiscoverRepo = null;
  return action("/api/adopt", { folders });
}

function renderRepoSelect(state) {
  const select = $("repo-select");
  select.innerHTML = "";
  const repos = state.repos || [];
  select.classList.toggle("hidden", repos.length === 0);
  const many = (state.accounts || []).length > 1;
  for (const repo of repos) {
    const opt = document.createElement("option");
    opt.value = repo.alias;
    // Show the account only when there's more than one to tell apart; with a
    // single identity the suffix is noise on every row.
    const showWho = many && repo.account_label;
    opt.textContent = showWho
      ? `${repo.alias} — ${repo.slug} (${repo.account_label})`
      : `${repo.alias} — ${repo.slug}`;
    opt.selected = repo.active;
    select.appendChild(opt);
  }
  const add = document.createElement("option");
  add.value = "__add__";
  add.textContent = "+ Add repo…";
  select.appendChild(add);
}

// -- accounts -----------------------------------------------------------------

// Device codes for account sign-ins, keyed by alias. The flows are alias-keyed
// server-side so several can be pending at once, and every refresh() rebuilds the
// account rows — so the code lives here and the row is rendered from it, rather
// than being written into the DOM once and lost on the next render.
const pendingLogins = new Map();

function renderAccounts(state) {
  const accounts = state.accounts || [];
  // The card is for managing MULTIPLE identities; with none registered the setup
  // form still covers the single-account path, so don't add a step to first run.
  $("accounts-card").classList.toggle("hidden", accounts.length === 0);
  const list = $("accounts-list");
  list.textContent = "";
  for (const account of accounts) {
    const li = document.createElement("li");
    li.dataset.account = account.alias;
    const label = document.createElement("span");
    label.textContent = account.label;
    if (account.active) label.textContent += " (default for new repos)";
    li.appendChild(label);

    const state_ = document.createElement("span");
    state_.className = account.signed_in ? "muted" : "warn";
    const repos = account.repos.length ? account.repos.join(", ") : "no repos";
    state_.textContent = account.signed_in
      ? ` — signed in · ${repos}`
      : ` — not signed in · ${repos}`;
    li.appendChild(state_);

    // While a code is outstanding the row shows it instead of the button, so a
    // second click can't discard a flow the user is halfway through.
    if (!account.signed_in && !pendingLogins.has(account.alias)) {
      const signIn = accountButton("Sign in", () => startLogin(account.alias));
      signIn.dataset.act = "signin"; // renderPendingLogin pulls it once a code lands
      li.appendChild(signIn);
    }
    if (!account.active) {
      li.appendChild(
        accountButton("Make default", async () => {
          await action("/api/accounts/use", { alias: account.alias });
        })
      );
    }
    li.appendChild(
      accountButton("Remove", async () => {
        const used = account.repos.length
          ? `\n\nThese repos will lose their account: ${account.repos.join(", ")}.`
          : "";
        if (!confirm(`Sign out and forget ${account.label}?${used}`)) return;
        await action("/api/accounts/remove", { alias: account.alias });
      })
    );
    const pending = pendingLogins.get(account.alias);
    if (pending) li.appendChild(loginCodeBox(pending));
    list.appendChild(li);
  }
  renderAccountOptions(state);
}

function accountButton(text, onClick) {
  const btn = document.createElement("button");
  btn.className = "small";
  btn.textContent = text;
  btn.addEventListener("click", onClick);
  return btn;
}

// The device code for ONE account, shown on that account's own row. The shared
// #login-code-box can't serve here: it lives inside #login-card, which refresh()
// hides whenever the ACTIVE repo is signed in — exactly the case where you sign
// in to a second account — and it can only ever show one flow at a time.
function loginCodeBox(pending) {
  const box = document.createElement("div");
  box.className = "login-pending";
  const intro = document.createElement("p");
  intro.append("Enter this code at ");
  const link = document.createElement("a");
  link.href = pending.verification_uri;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = pending.verification_uri.replace(/^https:\/\//, "");
  intro.append(link, ":");
  const code = document.createElement("div");
  code.className = "code";
  code.textContent = pending.user_code;
  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "Waiting for you to authorize…";
  box.append(intro, code, note);
  return box;
}

// Patch one row in place so the code appears on click, without waiting for the
// round-trip a full refresh() would cost. A later refresh() re-renders it from
// pendingLogins, so the two paths agree.
function renderPendingLogin(alias) {
  const li = $("accounts-list").querySelector(`li[data-account="${cssAttr(alias)}"]`);
  if (!li) return;
  li.querySelector(".login-pending")?.remove();
  const pending = pendingLogins.get(alias);
  if (!pending) return;
  li.querySelector('button[data-act="signin"]')?.remove();
  li.appendChild(loginCodeBox(pending));
}

function renderAccountOptions(state) {
  const select = $("setup-account");
  const previous = select.value;
  select.textContent = "";
  for (const account of state.accounts || []) {
    const opt = document.createElement("option");
    opt.value = account.alias;
    opt.textContent = account.label + (account.signed_in ? "" : " (not signed in)");
    select.appendChild(opt);
  }
  const wanted = previous || state.active_account;
  if (wanted && [...select.options].some((o) => o.value === wanted)) {
    select.value = wanted;
  }
}

// The owner/repo pickers ask ONE account what it can reach — never a merged view
// across accounts, which would leak one identity's reach into another's picker.
async function loadOwners() {
  const alias = $("setup-account").value;
  const ownerSelect = $("setup-owner-select");
  if (!alias) return;
  ownerSelect.textContent = "";
  try {
    const body = await api(`/api/accounts/${encodeURIComponent(alias)}/owners`);
    for (const owner of body.owners || []) {
      const opt = document.createElement("option");
      opt.value = owner;
      opt.textContent = owner;
      ownerSelect.appendChild(opt);
    }
    await loadRepoChoices();
  } catch (err) {
    showError(err.message || String(err));
  }
}

async function loadRepoChoices() {
  const alias = $("setup-account").value;
  const owner = $("setup-owner-select").value;
  const repoSelect = $("setup-repo-select");
  repoSelect.textContent = "";
  if (!alias || !owner) return;
  try {
    const q = `?owner=${encodeURIComponent(owner)}`;
    const body = await api(`/api/accounts/${encodeURIComponent(alias)}/repos${q}`);
    for (const repo of body.repos || []) {
      const opt = document.createElement("option");
      opt.value = repo.name;
      opt.textContent = repo.private ? `${repo.name} (private)` : repo.name;
      opt.dataset.branch = repo.default_branch;
      repoSelect.appendChild(opt);
    }
    if (body.truncated) {
      const opt = document.createElement("option");
      opt.disabled = true;
      opt.textContent = "— showing the first page only —";
      repoSelect.appendChild(opt);
    }
  } catch (err) {
    showError(err.message || String(err));
  }
}

async function refresh() {
  const state = await api("/api/state");
  lastStateAt = Date.now();
  lastLoggedIn = !!state.logged_in;
  offlineMode = !!state.offline;
  showError(state.error || "");
  // Appearance is set on /settings now; the hub only follows what it (or another
  // tab, via theme.js) chose. The server stays the source of truth.
  if (state.ui_theme) applyTheme(state.ui_theme);
  // Local mode (no repo configured): the notebook surface is usable without a
  // login; only sync needs a repo. The server reports state.mode === "local".
  const localMode = state.mode === "local";
  const showFiles = state.logged_in || localMode;
  lastMode = state.mode || "local";
  filesVisible = showFiles;

  // Identity is per-repo, so the badge names the ACCOUNT this repo syncs as, not a
  // single installation-wide host. The host is only worth showing when it isn't
  // github.com; the account is worth showing whenever there is more than one.
  const activeRow = (state.repos || []).find((r) => r.active) || {};
  const hostSuffix =
    activeRow.host && activeRow.host !== "github.com" ? ` · ${activeRow.host}` : "";
  const manyAccounts = (state.accounts || []).length > 1;
  const whoSuffix = manyAccounts && activeRow.account_label ? ` · ${activeRow.account_label}` : "";
  // Two mono lines in the rail: the repo slug, then branch · account (the design's
  // "acme-analytics/notebooks / main · @priya"). The full string stays on the title
  // attribute, since both lines ellipsis-truncate in a 13.5rem rail.
  const repoInfoText = state.repo
    ? `${state.repo} @ ${state.branch}${hostSuffix}${whoSuffix}`
    : (localMode ? "Local workspace — not connected to a repo" : "Not connected");
  const repoInfoEl = $("repo-info");
  repoInfoEl.textContent = "";
  repoInfoEl.title = repoInfoText;
  const slugLine = document.createElement("span");
  slugLine.className = "repo-slug";
  slugLine.textContent = state.repo || (localMode ? "local workspace" : "not connected");
  const whoLine = document.createElement("span");
  whoLine.className = "repo-who";
  whoLine.textContent = state.repo
    ? `${state.branch}${hostSuffix}${state.user ? ` · @${state.user}` : ""}`
    : (localMode ? "not connected to a repo" : "");
  repoInfoEl.append(slugLine, whoLine);

  $("rail-workspace").textContent = state.workspace;
  $("rail-workspace").title = `Workspace: ${state.workspace}`;
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
  renderAccounts(state);
  // A repo bound to a missing account is BROKEN, not unconfigured. Say so, or the
  // falsy client_id silently drops the page into local mode and the repo looks gone.
  if (state.account_error) {
    showError(state.account_error);
  }
  // The connect-repo form opens on demand — the rail menu's "Connect a repo" in
  // local mode, or the switcher's "+ Add repo…" when configured — so it's never
  // forced on a local-only user who has no intention of connecting a repo.
  $("setup-card").classList.toggle("hidden", !showAddRepo);
  // With accounts registered, the host and client id come from the chosen account,
  // so the raw fields give way to an account picker plus browse-or-create.
  const hasAccounts = (state.accounts || []).length > 0;
  $("setup-account-label").classList.toggle("hidden", !hasAccounts);
  $("setup-client-id-label").classList.toggle("hidden", state.configured || hasAccounts);
  $("setup-host-label").classList.toggle("hidden", state.configured || hasAccounts);
  $("setup-browse").classList.toggle("hidden", !hasAccounts);
  $("setup-manual").classList.toggle("hidden", hasAccounts);
  $("setup-cancel").classList.toggle("hidden", !showAddRepo);
  $("setup-intro").classList.toggle("hidden", state.configured);
  // The header button is the local-mode entry to the form; when a repo is configured
  // the switcher's "+ Add repo…" handles it, and while the form is open it's redundant.
  $("connect-repo").classList.toggle("hidden", state.configured || showAddRepo);
  const needsLogin = state.configured && !state.logged_in;
  $("login-card").classList.toggle("hidden", !needsLogin);
  // Sign-in and repo setup take over the centre while they are the thing to do, and
  // give it back the moment they aren't — so nobody is stranded on a blank list
  // behind a login wall, and nobody has a setup form parked under their notebooks.
  if (needsLogin || showAddRepo) {
    setCentreView("accounts");
  } else if (centreView === "accounts" && !showAccountsView) {
    setCentreView("list");
  }
  // Fire-and-forget: the borrowed-credential offer shells out to git, so it must
  // never hold up the render. It reveals itself if and when the probe finds one.
  if (needsLogin) {
    offerGitLogin();
  } else {
    $("login-git-box")?.classList.add("hidden");
  }
  syncCentre();
  // The schedules board reads purely local state (no network), so it works offline and in
  // local mode — which matters, since a refresh degrades to "ran against your copy" rather
  // than failing when GitHub is unreachable. Fire-and-forget: a board that can't load must
  // never hold up the file list.
  if (showFiles) {
    loadSchedules();
  } else {
    $("schedules-card").classList.add("hidden");
  }
  // Copilot sign-in menu: shown wherever the notebook surface is usable (local mode
  // or logged in) and AI is enabled. Copilot's sign-in is independent of the GitHub
  // login, so it lives in its own header dropdown rather than taking up a card the
  // user has to scroll past. Status is fetched cached (no CLI spawn).
  const showCopilot = aiChatEnabled && showFiles;
  const copilotMenu = $("copilot-menu");
  $("copilot-summary").textContent = "copilot";
  copilotMenu.classList.toggle("hidden", !showCopilot);
  if (showCopilot) {
    refreshCopilotStatus(false);
  } else {
    copilotMenu.open = false; // don't leave the dropdown open when it's hidden
  }

  // Pull / Push all / Propose (and the pull digest) only make sense against a
  // connected, logged-in repo that is REACHABLE. In local mode the notebooks are
  // usable but there's nothing to sync to; offline the network actions drop out of
  // the header block and its menu (workspaceActions), and the panel omits them for
  // the same reason (see fileActions) — the headline says why.
  if (!state.logged_in || offlineMode) {
    closeCentreView("whatsnew");
    // An already-open Review/History panel keeps live "Push this file"/"Restore"
    // buttons, each of which needs GitHub — close them too, like the rows that
    // stop offering Review/History while the amber banner shows.
    closeCentreView("review");
    reviewPath = null;
    closeCentreView("history");
    historyPath = null;
  }
  // The per-file watch set is keyed by repo; local mode has no digest to watch.
  loadWatched(state.mode === "repo" && state.logged_in ? state.repo : null);
  // Recall shows only while the manifest holds a recallable last push; the
  // confirm names exactly which files it would revert (a stale record is the
  // trap — this is how the user catches one).
  recallPaths = state.recall_paths || [];
  canRecall = !!(state.logged_in && state.can_recall && !offlineMode);
  // Workspace-level "Batch build" — only when the opt-in orchestrator is enabled.
  aiBatchEnabled = !!state.ai_batch;
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
  // Settings (per-machine config) stay reachable everywhere — renderRailNav gates it.
  if (state.logged_in) {
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
    $("summary").classList.remove("status-warn");
  } else {
    $("user-info").textContent = "";
    $("summary").textContent = "";
  }
  if (showFiles) {
    lastFiles = state.files || [];
    lastArtifacts = state.artifacts || [];
    lastFolders = state.folders || [];
    // Repo-curated featured folders (synced): honoured everywhere; only repo mode gets
    // the star control (curating is a team act — local-only users have nobody to share with).
    lastFeatured = state.featured_folders || [];
    canFeature = state.mode === "repo";
    // Team AI-context offer (synced): the toggle only shows in repo mode with AI on —
    // curating what the copilot reads is a team act, and pointless with AI off.
    lastContextFolders = state.context_folders || [];
    canCurateContext = state.mode === "repo" && !!state.ai_chat;
    lastAiContext = !!state.ai_context;
    lastSelectedContext = state.selected_context_folders || [];
    // Folder view (focus + collapse memory) persists per workspace on the stable hub
    // origin. Re-read each poll — localStorage is the source of truth — then self-heal
    // a focus whose folder a teammate has since renamed or deleted (a blank card
    // otherwise), resetting it to All folders.
    loadFolderView(folderViewId(state));
    if (focusPrefix &&
        !FilesTree.focusLive(lastFiles.filter((f) => !f.artifact), lastFolders, focusPrefix)) {
      focusPrefix = "";
      saveFolderView();
    }
  } else {
    lastFiles = [];  // no file surface (login wall) — don't leave stale push/propose targets
    lastArtifacts = [];
    lastFolders = [];
    lastFeatured = [];
    canFeature = false;
    lastContextFolders = [];
    canCurateContext = false;
    lastAiContext = false;
    lastSelectedContext = [];
  }
  renderChecklist();  // after lastFiles lands: two of the items derive from the rows
  renderContextSubscription();
  renderWorkspace();  // list + rail + headline + panel, all from the rows above
  // Prompt to adopt any notebook folders the repo keeps outside the synced folders.
  // Runs once per repo-session (see maybeDiscover), so it never rides the refresh loop.
  await maybeDiscover(state);
}

// Flows are keyed by account alias server-side, so several can be pending at once
// (add a second account while the first is still waiting for its code). "" means
// the active repo's own account.
async function startLogin(alias = "") {
  showError("");
  const q = alias ? `?account=${encodeURIComponent(alias)}` : "";
  const data = await api(`/api/login/start${q}`, {});
  if (data.error) return showError(data.error);
  // The server echoes back which alias the flow is for ("" = the active repo's
  // own account, on the pre-accounts path). An aliased flow renders on the
  // account's row; only the bare one uses the login card.
  const account = data.account || "";
  if (account) {
    pendingLogins.set(account, {
      user_code: data.user_code,
      verification_uri: data.verification_uri,
    });
    renderPendingLogin(account);
  } else {
    $("login-start").classList.add("hidden");
    $("login-code-box").classList.remove("hidden");
    $("login-code").textContent = data.user_code;
    $("login-link").href = data.verification_uri;
    $("login-link").textContent = data.verification_uri.replace(/^https:\/\//, "");
  }
  window.open(data.verification_uri, "_blank");
  pollLogin(account);
}

// -- Signing in with the credential git already holds --------------------------
// For organisations that restrict third-party OAuth apps AND cap personal access
// token lifetimes: the credential behind the user's daily `git clone` is neither,
// so it is the one route that needs nobody's approval. Offered only when the probe
// finds one — advertising a method that cannot work here would be worse than
// silence. The probe is value-free: it reports the token's TYPE, never the token.
async function offerGitLogin(alias = "") {
  const box = $("login-git-box");
  if (!box) return;
  box.classList.add("hidden");
  const q = alias ? `?account=${encodeURIComponent(alias)}` : "";
  const probe = await api(`/api/login/git/probe${q}`).catch(() => null);
  if (!probe || !probe.found) return;
  const note = $("login-git-note");
  note.textContent = probe.refreshable
    ? `Can't approve an OAuth app? Sign in with the credential git already uses for ${probe.host} — nothing to register, and mooring stores no copy of it.`
    : `Can't approve an OAuth app? Sign in with the credential git already uses for ${probe.host}. Note it looks like a personal access token, so it will expire whenever your organisation's policy says.`;
  box.classList.remove("hidden");
}

async function loginWithGit(alias = "") {
  showError("");
  const btn = $("login-git");
  btn.disabled = true;
  try {
    const data = await api("/api/login/git", { account: alias });
    if (data.error) return showError(data.error);
    $("login-git-box").classList.add("hidden");
    await refresh();
  } finally {
    btn.disabled = false;
  }
}

// Clear a finished flow BEFORE the refresh() that follows, so renderAccounts
// doesn't paint the spent code back onto the row.
function endLogin(alias) {
  if (alias) {
    pendingLogins.delete(alias);
    renderPendingLogin(alias);
    return;
  }
  $("login-code-box").classList.add("hidden");
  $("login-start").classList.remove("hidden");
}

async function pollLogin(alias = "") {
  const q = alias ? `?account=${encodeURIComponent(alias)}` : "";
  const data = await api(`/api/login/poll${q}`);
  if (data.status === "ok") {
    endLogin(alias);
    await refresh();
    return;
  }
  if (data.status === "error") {
    // `resumable` means GitHub authorised us but naming the account failed. The
    // token is parked, so retrying does NOT need a new code.
    showError(data.message || "Login failed.");
    endLogin(alias);
    if (alias) await refresh(); // the row goes back to offering "Sign in"
    return;
  }
  setTimeout(() => pollLogin(alias), 2500);
}

async function addAccount() {
  showError("");
  const alias = $("account-alias").value.trim();
  const body = {
    alias,
    host: $("account-host").value.trim(),
    client_id: $("account-client-id").value.trim(),
  };
  // api() reports failures in `error` rather than throwing, so a refusal has to be
  // read off the body — otherwise a rejected add (no client id, bad alias) falls
  // through to startLogin and the real reason is replaced by "Unknown account".
  const data = await api("/api/accounts/add", body);
  if (data.error) return showError(data.error);
  $("account-alias").value = "";
  $("account-client-id").value = "";
  await refresh();
  await startLogin(alias); // registering and signing in are one gesture for the user
}

// -- Adding an account on a host mooring doesn't know yet ----------------------
// Borrowing used to be reachable only for a host already set up, because the host
// came from the active repo or an existing account. These two do the other half:
// discover() asks the machine which hosts it can reach, and addAccountFromGit()
// registers one WITHOUT an OAuth client id — there is no device flow to run, so
// requiring one would have gated this behind the very thing borrowing exists to
// avoid. No startLogin() afterwards: the server signs in as it registers.
async function addAccountFromGit(host, alias) {
  showError("");
  const data = await api("/api/accounts/add", { alias, host, from_git: true });
  if (data.error) return showError(data.error);
  $("account-alias").value = "";
  $("account-client-id").value = "";
  await refresh();
  await discoverGitHosts(); // the row it came from is now "already set up"
}

async function discoverGitHosts() {
  const box = $("account-discovered");
  const list = $("account-discovered-list");
  if (!box || !list) return;
  const data = await api("/api/login/git/discover").catch(() => null);
  const hosts = (data && data.hosts) || [];
  // Only hosts that still need signing in are worth a button; a host already set
  // up would just be a row that does nothing.
  const offers = hosts.filter((h) => !h.signed_in);
  list.textContent = "";
  if (!offers.length) return box.classList.add("hidden");
  for (const h of offers) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    // The type prefix is the honest bit: a ghp_ credential inherits whatever PAT
    // lifetime cap the org sets, and the user should know that before relying on it.
    label.textContent = h.refreshable
      ? `${h.host} — git can refresh this one`
      : h.kind
        ? `${h.host} — looks like a ${h.kind} token, so it expires on your org's schedule`
        : h.host;
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = h.known ? `Sign in as ${h.alias}` : `Add as ${h.alias}`;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await addAccountFromGit(h.host, h.alias);
      } finally {
        btn.disabled = false;
      }
    });
    li.append(label, " ", btn);
    list.append(li);
  }
  box.classList.remove("hidden");
}

// -- Copilot sign-in (separate from the GitHub login) -----------------------
// GitHub Copilot signs in independently of mooring's GitHub login (different
// OAuth flow, different credential store, possibly a different account). This
// card surfaces that sign-in + which account is connected, so the user never has
// to drop to `mooring ai login` in a terminal.

let aiProvider = "copilot";

// The rail footer's one-line copilot state — signed in and schema-only, or the
// reason it isn't. "schema-only" is the promise the copilot is built on (it sees
// column names and authored code, never a value), so the rail states it rather
// than leaving the user to remember it.
function copilotSummary(text, tone) {
  const el = $("copilot-summary");
  el.textContent = text;
  el.className = `rail-ai tone-${tone}`;
}

function renderCopilotStatus(s) {
  if (s && s.provider) aiProvider = s.provider;
  const isOpenai = aiProvider === "openai";
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
  const connectLabel = isOpenai ? "Set API key" : "Sign in to Copilot";
  if (!s || s.available === false) {
    statusEl.textContent =
      s?.detail ||
      (isOpenai
        ? "The OpenAI SDK isn't installed (install the mooring[openai] extra)."
        : "GitHub Copilot isn't available in this build (install the mooring[copilot] extra).");
    connectBtn.classList.add("hidden");
    switchBtn.classList.add("hidden");
    copilotSummary("copilot · unavailable", "muted");
    return;
  }
  if (!s.checked) {
    statusEl.textContent = "Sign-in status not checked yet.";
    copilotSummary("copilot · not checked", "muted");
    connectBtn.textContent = connectLabel;
    connectBtn.classList.remove("hidden");
    switchBtn.classList.add("hidden");
    return;
  }
  if (s.connected) {
    statusEl.textContent = isOpenai
      ? s.detail || "OpenAI-compatible endpoint connected."
      : s.account
        ? `Signed in as @${s.account}.`
        : "Signed in.";
    connectBtn.classList.add("hidden");
    switchBtn.textContent = isOpenai ? "Change API key" : "Switch account";
    switchBtn.classList.remove("hidden");
    copilotSummary("copilot · schema-only", "ok");
  } else {
    statusEl.textContent = isOpenai
      ? s.detail || "No OpenAI API key configured."
      : "Not signed in to Copilot.";
    connectBtn.textContent = connectLabel;
    connectBtn.classList.remove("hidden");
    switchBtn.classList.add("hidden");
    copilotSummary("copilot · sign in", "muted");
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
// menu when the user clicks outside it, the way a menu should. This covers the rail
// menus (repo + Copilot) and every actions menu — the row ones, the header's "more",
// and the panel's — and, because clicking one menu's summary runs here too, opening a
// menu closes any other menu left open.
document.addEventListener("click", (e) => {
  for (const menu of document.querySelectorAll("details.rail-menu[open], details.row-menu[open]")) {
    if (!menu.contains(e.target)) menu.open = false;
  }
});

async function setOpenAiKey() {
  const key = window.prompt(
    "Paste the API key for your OpenAI-compatible endpoint. It is stored in your OS " +
      "credential store on this machine only — never synced. (Local endpoints may need none.)"
  );
  if (!key || !key.trim()) return;
  const note = $("copilot-note");
  const connectBtn = $("copilot-connect");
  const switchBtn = $("copilot-switch");
  connectBtn.disabled = true;
  switchBtn.disabled = true;
  note.textContent = "Validating the API key…";
  try {
    const data = await api("/api/ai/key", { key: key.trim() });
    note.textContent = "";
    if (data.error) showError(data.error);
    else renderCopilotStatus(data.status);
  } catch (e) {
    note.textContent = "";
    showError((e && e.message) || "Couldn't store the API key.");
  } finally {
    connectBtn.disabled = false;
    switchBtn.disabled = false;
  }
}

// Copilot signs in via a browser device flow; OpenAI has no such flow, so its
// "connect" asks for an API key instead. One handler dispatches on the provider.
function connectAi() {
  return aiProvider === "openai" ? setOpenAiKey() : startCopilotLogin();
}

$("copilot-connect").addEventListener("click", connectAi);
$("copilot-switch").addEventListener("click", connectAi);
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

// Wrapped, not passed straight through: startLogin's first parameter is an account
// alias, and a bare listener would hand it the click event instead.
$("login-start").addEventListener("click", () => startLogin());
$("login-git").addEventListener("click", () => loginWithGit());
$("btn-run-due").addEventListener("click", () => runRefresh(""));
$("btn-background-on").addEventListener("click", () => setBackground(true));
$("btn-background-off").addEventListener("click", () => setBackground(false));
$("schedule-save").addEventListener("click", saveSchedule);
$("schedule-cancel").addEventListener("click", () => {
  closeScheduleForm();
  renderSchedules(); // re-reveals the empty hint the open form was standing in for
});
$("schedule-cadence").addEventListener("change", syncScheduleCadenceFields);
// The offered date is derived from the time, so it follows the time until the user takes
// ownership of it. "input" on the date field is what marks that handover.
$("schedule-at").addEventListener("change", reofferDate);
$("schedule-date").addEventListener("input", () => { offeredDate = ""; });
$("params-start").addEventListener("click", startParamsRun);
$("params-cancel").addEventListener("click", cancelParamsRun);
$("params-close").addEventListener("click", closeParamsCard);
$("params-for").addEventListener("input", renderParamsPreview);
// A pull REPLACES local files. When one of them is a dataset other notebooks are
// recorded as reading (or generating), say so BEFORE it lands, instead of leaving the
// analyst to discover it a week later in a number that moved. Advisory, never blocking:
// pulling the team's work is right almost every time, and lineage under-reports by
// construction — so this informs the choice, claims only what is recorded, and DATES
// each claim so a six-month-old one cannot masquerade as current. Scoped to the toolbar
// Pull, the unaimed bulk action; the per-row stale dialog already names the one file it
// is about, and a second modal there is noise.
function confirmPullImpact() {
  const hits = LineageFmt.pullImpact(lastFiles);
  if (!hits.length) return true;
  return confirm(
    "This pull changes files other notebooks depend on:\n\n" +
    hits.map(LineageFmt.impactLine).join("\n") +
    "\n\nRe-run those notebooks afterwards to see what moved. (Lineage only sees " +
    "notebooks that record their inputs, so there may be more.)\n\n" +
    "OK pulls anyway; Cancel leaves your copies alone."
  );
}

async function pullAll() {
  if (!confirmPullImpact()) return;
  const data = await action("/api/pull", {});
  // The pull response carries the digest of what just landed, computed against
  // the PRE-pull horizon — so a pull is never a black box. States shown are the
  // pre-pull ones ("remote changed" = what the pull just applied).
  if (data && !data.error && data.whatsnew && (data.whatsnew.entries || []).length) {
    renderWhatsnew(data.whatsnew, "What just landed");
  }
}

function pushAll() {
  const candidates = lastFiles.filter((f) => PUSH_STATES.has(f.state));
  const sel = draftShareSelection(candidates);
  if (!sel.count) {
    // Everything pending was a draft and the user excluded them — say so
    // rather than silently doing nothing.
    $("summary").textContent = "Nothing pushed — only drafts were pending.";
    return;
  }
  return pushAction(sel.paths, sel.count);
}

function proposeAll() {
  const candidates = lastFiles.filter((f) => PUSH_STATES.has(f.state));
  const sel = draftShareSelection(candidates);
  if (!sel.count) {
    $("summary").textContent = "Nothing proposed — only drafts were pending.";
    return;
  }
  return proposeAction(sel.paths, sel.count);
}

let recallPaths = [];

function recallAction() {
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
}

$("btn-sweep-cancel").addEventListener("click", cancelSweep);
$("connect-repo").addEventListener("click", () => {
  showAddRepo = true;
  // reveals #setup-card (and Cancel), then fills the pickers
  refresh().then(openRepoForm);
});
// The rail's way into sign-in / repo setup, so managing identities is reachable at
// any time and not only behind a login wall.
$("btn-accounts").addEventListener("click", () => {
  showAccountsView = true;
  $("repo-menu").open = false;
  setCentreView("accounts");
});
$("centre-back").addEventListener("click", () => {
  showAccountsView = false;
  setCentreView("list");
});
$("rail-workspace").addEventListener("click", () => setCentreView("workspace"));

// Populating owner/repo costs GitHub round trips, so it happens when the form is
// actually opened rather than on every /api/state poll.
function openRepoForm() {
  if (!$("setup-browse").classList.contains("hidden")) loadOwners();
}
$("repo-select").addEventListener("change", (event) => {
  const alias = event.target.value;
  if (alias === "__add__") {
    showAddRepo = true;
    refresh().then(openRepoForm);
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
    const account = $("setup-account").value;
    const browsing = !$("setup-browse").classList.contains("hidden");
    let data;
    if (browsing && $("setup-mode-new").checked) {
      // Create it on GitHub first; the route registers it bound to this account.
      data = await api(`/api/accounts/${encodeURIComponent(account)}/repos`, {
        owner: $("setup-owner-select").value,
        repo: $("setup-new-name").value.trim(),
        private: $("setup-private").checked,
        seed: $("setup-seed").checked,
        alias: $("setup-alias").value,
      });
    } else {
      const picked = $("setup-repo-select").selectedOptions[0];
      data = await api("/api/setup", {
        client_id: $("setup-client-id").value,
        host: $("setup-host").value,
        account,
        owner: browsing ? $("setup-owner-select").value : $("setup-owner").value,
        repo: browsing ? $("setup-repo-select").value : $("setup-repo").value,
        branch:
          browsing && picked && picked.dataset.branch
            ? picked.dataset.branch
            : $("setup-branch").value,
        alias: $("setup-alias").value,
      });
    }
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
$("account-save").addEventListener("click", addAccount);
// Discovery shells out to git once per candidate host, so it runs when the panel is
// actually opened rather than on every refresh().
$("account-add").addEventListener("toggle", (e) => {
  if (e.target.open) discoverGitHosts();
});
// The manual escape hatch: a host with no entry in git's config never shows up in
// discovery (the credential protocol cannot list what a helper holds), but naming it
// here still works.
$("account-save-git").addEventListener("click", () => {
  const alias = $("account-alias").value.trim();
  const host = $("account-host").value.trim();
  if (!alias) return showError("Give the account a short name first.");
  if (!host) return showError("Give the GitHub URL of the host to borrow a credential for.");
  return addAccountFromGit(host, alias);
});
$("setup-account").addEventListener("change", loadOwners);
$("setup-owner-select").addEventListener("change", loadRepoChoices);
for (const id of ["setup-mode-existing", "setup-mode-new"]) {
  $(id).addEventListener("change", () => {
    const creating = $("setup-mode-new").checked;
    $("setup-new-fields").classList.toggle("hidden", !creating);
    $("setup-existing-label").classList.toggle("hidden", creating);
  });
}
$("setup-cancel").addEventListener("click", () => {
  showAddRepo = false;
  refresh();
});

// Appearance is set on /settings; theme.js applies a cross-tab change to <html>
// for us, and refresh() reconciles with the server value. Nothing to do here.

// -- health check (mooring doctor, in the workspace view) --------------------
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
// Keep the meta line's "CHECKED 14 MIN AGO" honest while the tab sits open (no
// network) — the job the freshness banner used to do, in the line that replaced it.
setInterval(() => { if (lastStateAt != null) renderHeaderBlock(); }, 60_000);

// Esc, innermost thing first: close the search box, then the detail drawer on a
// narrow window, then let go of the selection. The panel's actions are always aimed
// at something you chose, so putting it down has to be one keystroke. A native
// <dialog> handles its own Esc (and closes on the safe choice), so this stands aside
// whenever one is open rather than acting behind it.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.querySelector("dialog[open]")) return;
  if (document.activeElement === $("file-search")) toggleSearch();
  else if (document.body.classList.contains("drawer-open")) closeDrawer();
  else if (selectedPath) clearSelection();
});

refresh();
// Re-attach to a sweep already running on the server (a reload mid-check).
resumeSweepWatch();
