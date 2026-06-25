"use strict";

// The batch builder page. Drives the unattended fan-out: parse the briefs, POST
// /api/ai/batch/open, stream per-job progress over SSE, then render the review tray
// where the analyst Applies each notebook's proposed cells through the SAME
// single-notebook write path the copilot uses. Pure parsing/diff/highlight logic
// lives in chat_core.js (unit-tested); this file is the DOM/SSE wiring. mooring talks
// to the model over the hub only — no data value ever reaches this page.

(function () {
  const C = window.ChatCore;
  const $ = (id) => document.getElementById(id);

  // chat.js's escapeHtml, kept byte-for-byte so highlightCode's XSS contract holds
  // (it must be called with already-escaped text).
  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function fetchJSON(url, opts) {
    let r;
    try {
      r = await fetch(url, opts);
    } catch (e) {
      // A rejected fetch (network drop / hub gone) must route through callers' !ok
      // recovery so a disabled button is re-enabled, not stuck forever.
      return { ok: false, status: 0, body: { error: "Network error — check your connection." } };
    }
    let body = {};
    try {
      body = await r.json();
    } catch (e) {}
    return { ok: r.ok, status: r.status, body };
  }
  function postJSON(url, payload) {
    return fetchJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  let state = { enabled: false, max_jobs: 20, max_concurrency: 3, datasets: [] };
  let batchId = null; // null until the first "Add to queue" opens the run
  let stream = null;
  let queue = { pending: 0, total: 0 };
  let trayTimer = null;
  const applying = new Set(); // "job:proposal" keys with an Apply in flight
  // The model/effort the analyst picks apply to every job submitted while selected; the
  // queue is appendable so they can change the model between submits. Preference is
  // shared with the chat window via the same localStorage keys.
  let MODELS = [];
  const LS_MODEL = "mooring.ai.model";
  const LS_EFFORT = "mooring.ai.effort";

  function showError(msg) {
    const b = $("error-banner");
    b.textContent = msg;
    b.classList.remove("hidden");
  }
  function clearError() {
    $("error-banner").classList.add("hidden");
  }

  const PILL_TEXT = {
    queued: "queued",
    running: "building…",
    building: "building…",
    built: "ready to review",
    empty: "nothing proposed",
    failed: "failed",
    pii_blocked: "PII blocked",
    skipped_disabled: "AI off",
    not_run: "not run",
    applied: "applied ✓",
    refining: "revising…",
  };
  const PILL_CLS = {
    built: "ok",
    applied: "ok",
    failed: "bad",
    pii_blocked: "warn",
    empty: "warn",
  };
  function pill(status) {
    const cls = PILL_CLS[status] || "";
    return `<span class="pill ${cls}">${escapeHtml(PILL_TEXT[status] || status)}</span>`;
  }

  function showDisabled(msg) {
    const n = $("disabled-notice");
    n.textContent = msg;
    n.classList.remove("hidden");
    $("build-btn").disabled = true;
    $("add-job").disabled = true;
    $("batch-model").disabled = true;
    $("batch-effort").disabled = true;
    $("jobs-form")
      .querySelectorAll("input, select, textarea, button")
      .forEach((el) => (el.disabled = true));
  }

  async function loadState() {
    const { ok, status, body } = await fetchJSON("/api/ai/batch/state");
    if (!ok) {
      showDisabled(
        status === 404
          ? "The AI copilot is turned off for this workspace."
          : "Could not load batch settings."
      );
      return;
    }
    state = body;
    $("caps").textContent = `up to ${state.max_jobs} per queue · ${state.max_concurrency} built at a time`;
    if (!$("jobs-form").querySelector(".job-form-card")) addJobCard(); // start with one card
    if (!state.enabled) {
      showDisabled(
        "Batch building is turned off. An admin can enable it with [ai.batch] enabled = true."
      );
    }
  }

  // -- model / effort (mirrors the chat window) ----------------------------

  function populateEfforts(preferDefault) {
    const model = MODELS.find((m) => m.id === $("batch-model").value);
    const sel = $("batch-effort");
    sel.innerHTML = "";
    const efforts = (model && model.efforts) || [];
    if (!efforts.length) {
      $("batch-effort-wrap").classList.add("hidden");
      return;
    }
    $("batch-effort-wrap").classList.remove("hidden");
    for (const e of efforts) {
      const o = document.createElement("option");
      o.value = e;
      o.textContent = e;
      sel.appendChild(o);
    }
    const saved = localStorage.getItem(LS_EFFORT);
    sel.value = efforts.includes(saved)
      ? saved
      : efforts.includes(preferDefault)
        ? preferDefault
        : (model && model.default_effort) || efforts[0];
  }

  function selectedEffort() {
    return $("batch-effort-wrap").classList.contains("hidden") ? "" : $("batch-effort").value;
  }

  async function loadModels() {
    const { ok, body } = await fetchJSON("/api/ai/models");
    MODELS = (ok && body.models) || [];
    const sel = $("batch-model");
    sel.innerHTML = "";
    // Hide the picker when there are no models (not signed in / provider unavailable —
    // the batch then uses the server's configured default model) OR when batch is off.
    // loadModels runs AFTER loadState (see DOMContentLoaded), so state.enabled is known.
    if (!MODELS.length || !state.enabled) {
      $("model-row").classList.add("hidden");
      return;
    }
    $("model-row").classList.remove("hidden");
    for (const m of MODELS) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.name + (m.multiplier && m.multiplier > 1 ? ` · ${m.multiplier}×` : "");
      sel.appendChild(o);
    }
    const saved = localStorage.getItem(LS_MODEL);
    sel.value = [saved, body.default_model, MODELS[0].id].find((id) =>
      MODELS.some((m) => m.id === id)
    );
    populateEfforts(body.default_effort);
  }

  // -- the per-notebook job form -------------------------------------------

  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, "&quot;");
  }
  function datasetOptions(selected) {
    const opts = ['<option value="">— no dataset —</option>'];
    (state.datasets || []).forEach((d) => {
      const sel = d === selected ? " selected" : "";
      opts.push(`<option value="${escapeAttr(d)}"${sel}>${escapeHtml(d)}</option>`);
    });
    return opts.join("");
  }
  function addJobCard(prefill) {
    prefill = prefill || {};
    const card = document.createElement("div");
    card.className = "job-form-card";
    card.innerHTML = `
      <div class="jf-head">
        <input class="jf-name" placeholder="notebook name (optional)" value="${escapeAttr(prefill.name || "")}">
        <select class="jf-dataset" title="Optional: give the AI this dataset's column schema">${datasetOptions(prefill.dataset || "")}</select>
        <button class="jf-remove small" title="Remove this notebook">&times;</button>
      </div>
      <textarea class="jf-brief" rows="4" spellcheck="true"
        placeholder="Describe this notebook in detail — what to load, the analysis to run, the charts to draw. Multiple lines and bullet points are fine.">${escapeHtml(prefill.brief || "")}</textarea>`;
    card.querySelector(".jf-remove").addEventListener("click", () => removeJobCard(card));
    $("jobs-form").appendChild(card);
    return card;
  }
  function removeJobCard(card) {
    card.remove();
    if (!$("jobs-form").querySelector(".job-form-card")) addJobCard(); // always keep one
  }
  function currentJobRows() {
    return Array.from($("jobs-form").querySelectorAll(".job-form-card")).map((c) => ({
      name: c.querySelector(".jf-name").value,
      brief: c.querySelector(".jf-brief").value,
      dataset: c.querySelector(".jf-dataset").value,
    }));
  }

  // Queue the current form's cards onto the run, OPENING it on the first submit and
  // APPENDING on every submit after — so you can kick off a job, then keep writing the
  // next and add it while the first builds. The form resets to a fresh card each time.
  async function submitJobs() {
    clearError();
    const model = $("batch-model").value;
    const reasoning_effort = selectedEffort();
    const jobs = C.cleanJobs(currentJobRows()).map((j) => ({ ...j, model, reasoning_effort }));
    if (!jobs.length) {
      showError("Add at least one notebook with a brief.");
      return;
    }
    const projected = queue.total + jobs.length;
    if (state.max_jobs && projected > state.max_jobs) {
      showError(`That would be ${projected} notebooks; the limit is ${state.max_jobs} per queue.`);
      return;
    }
    $("build-btn").disabled = true;
    let res;
    if (!batchId) {
      res = await postJSON("/api/ai/batch/open", { jobs });
      if (res.ok) {
        batchId = res.body.batch_id;
        openStream(batchId);
      }
    } else {
      res = await postJSON("/api/ai/batch/add", { batch_id: batchId, jobs });
    }
    $("build-btn").disabled = false;
    if (!res.ok) {
      showError(res.body.error || "Could not queue these notebooks.");
      return;
    }
    queue.total = projected; // optimistic; the next tray refresh reconciles it
    resetForm();
    scheduleTrayRefresh();
  }

  function resetForm() {
    $("jobs-form").innerHTML = "";
    addJobCard();
    const ta = $("jobs-form").querySelector(".jf-brief");
    if (ta) ta.focus(); // ready to type the next brief immediately
  }

  function openStream(id) {
    stream = new EventSource(`/api/ai/batch/stream/${id}`);
    // The run streams 'job' events for its whole life; each just signals "something
    // changed" — we (debounced) re-pull the authoritative tray rather than tracking
    // state from the events. 'closed' = the run was reaped / the repo switched.
    stream.addEventListener("job", scheduleTrayRefresh);
    stream.addEventListener("closed", closeStream);
    stream.onerror = () => {
      // EventSource auto-reconnects while CONNECTING; only a CLOSED state is permanent
      // (the hub went away / unknown id) — stop and tell the user. The tray still loads.
      if (stream && stream.readyState === EventSource.CLOSED) {
        closeStream();
        showError("Lost the live connection — reload the page to reconnect.");
      }
    };
  }
  function closeStream() {
    if (stream) {
      stream.close();
      stream = null;
    }
  }

  function scheduleTrayRefresh() {
    if (trayTimer) return; // coalesce a burst of job events into one tray pull
    trayTimer = setTimeout(() => {
      trayTimer = null;
      refreshTray();
    }, 150);
  }

  async function refreshTray() {
    if (!batchId) return;
    const { ok, body } = await fetchJSON(`/api/ai/batch/tray/${batchId}`);
    if (!ok) return;
    queue = { pending: body.pending || 0, total: (body.jobs || []).length };
    renderTray(body);
  }

  function renderTray(tray) {
    const jobs = tray.jobs || [];
    if (!jobs.length) {
      $("jobs").innerHTML = "";
      return;
    }
    const pending = tray.pending || 0;
    const done = jobs.length - pending;
    const meta =
      pending > 0
        ? `${pending} building/queued · ${done} ready — keep adding above while these run.`
        : `All caught up — ${jobs.length} notebook(s). Review and Apply below; nothing is written until you do. Add more above anytime.`;
    $("jobs").innerHTML =
      `<div class="batch-meta">${escapeHtml(meta)}</div>` + jobs.map(renderJob).join("");
    $("jobs")
      .querySelectorAll("[data-apply]")
      .forEach((b) => b.addEventListener("click", onApply));
    $("jobs")
      .querySelectorAll("[data-open]")
      .forEach((b) => b.addEventListener("click", onOpen));
    $("jobs")
      .querySelectorAll("[data-refine]")
      .forEach((b) => b.addEventListener("click", onRefine));
    $("jobs")
      .querySelectorAll("[data-force]")
      .forEach((b) => b.addEventListener("click", onForce));
  }

  function allApplied(j) {
    const ps = j.proposals || [];
    return ps.length > 0 && ps.every((p) => p.applied);
  }

  function renderJob(j) {
    let body = "";
    if (j.error) body += `<div class="job-err">${escapeHtml(j.error)}</div>`;
    if (j.pii && j.pii.length) {
      const kinds = j.pii.map((f) => escapeHtml(f.kind)).join(", ");
      if (j.status === "pii_blocked") {
        // A flagged brief held the build. The analyst reviewing the tray can override it
        // (parity with the chat's "Send anyway") — the brief is then forwarded verbatim.
        body += `<div class="job-err">Blocked: ${kinds} detected in the brief.${
          j.forcing ? "" : ` <button class="small" data-force="${j.index}">Build anyway</button>`
        }</div>`;
      } else {
        // Built after the analyst chose Build anyway — keep the override visible.
        body += `<div class="job-warn">⚠ Built despite flagged data (${kinds}) — you chose Build anyway.</div>`;
      }
    }
    (j.proposals || []).forEach((p) => (body += renderProposal(j, p)));
    // Revise a built notebook's proposal BEFORE applying — fold a note into its brief
    // and re-build. While it revises, the box becomes a "revising…" line; the updated
    // proposal arrives live. The notebook file is never touched until you Apply.
    if (j.notebook && j.status === "built") {
      body += j.refining
        ? `<div class="refine-row muted">revising…</div>`
        : `<div class="refine-row">
            <input class="refine-note" data-refine-note="${j.index}"
              placeholder="Refine — e.g. use a bar chart, add a totals row, group by month…">
            <button class="small" data-refine="${j.index}">Refine</button>
          </div>`;
    }
    const status = allApplied(j)
      ? "applied"
      : j.forcing
        ? "building"
        : j.refining
          ? "refining"
          : j.status;
    return `<div class="job-card">
      <div class="job-head">
        <span class="job-name">${escapeHtml(j.name || "notebook")}</span>
        ${pill(status)}
        <span class="job-nb muted">${j.notebook ? "→ " + escapeHtml(j.notebook) : ""}</span>
      </div>
      <div class="job-brief muted">${escapeHtml(j.brief)}</div>
      <div class="job-body">${body}</div>
    </div>`;
  }

  function renderProposal(j, p) {
    let lines = [];
    if (p.code) {
      lines = C.additiveBlockLines(p.code);
    } else if (p.diffs && p.diffs.length) {
      p.diffs.forEach((d) => (lines = lines.concat(C.diffLines(d.before, d.after))));
    }
    const code = lines
      .map((l) => {
        const cls = l.gutter === "+" ? "cl-add" : l.gutter === "-" ? "cl-del" : "cl-ctx";
        return `<div class="cl ${cls}"><span class="cl-g">${l.gutter}</span><span class="cl-t">${C.highlightCode(
          escapeHtml(l.text)
        )}</span></div>`;
      })
      .join("");
    // Keep a button that is applied OR mid-apply disabled even across a tray re-render
    // (a build of another job can trigger a refresh) — the server is idempotent too, so
    // a stray second click can't double-apply, but this avoids the misleading enabled UI.
    const key = j.index + ":" + p.proposal;
    const busy = applying.has(key);
    const done = p.applied;
    const label = done ? "Applied ✓" : busy ? "Applying…" : "Apply";
    return `<div class="proposal">
      ${p.rationale ? `<div class="prop-why muted">${escapeHtml(p.rationale)}</div>` : ""}
      <div class="prop-code">${code || '<span class="muted">(no change)</span>'}</div>
      <div class="prop-actions">
        <button class="small" data-apply="${key}" ${done || busy ? "disabled" : ""}>${label}</button>
        ${j.notebook ? `<button class="small" data-open="${escapeAttr(j.notebook)}">Open notebook</button>` : ""}
      </div>
    </div>`;
  }

  async function onApply(e) {
    clearError();
    const btn = e.currentTarget;
    const key = btn.getAttribute("data-apply");
    const [job, proposal] = key.split(":").map(Number);
    applying.add(key);
    btn.disabled = true;
    btn.textContent = "Applying…";
    const { ok, body } = await postJSON("/api/ai/batch/apply", {
      batch_id: batchId,
      job,
      proposal,
    });
    applying.delete(key);
    if (!ok) {
      showError(body.error || "Apply failed.");
      btn.disabled = false;
      btn.textContent = "Apply";
      return;
    }
    btn.textContent = "Applied ✓";
  }

  // "Build anyway": re-run a PII-blocked job with the guard overridden. The flagged
  // brief is forwarded verbatim, so confirm the override first (it's the human gate the
  // batch otherwise lacks). The job goes building → built via the tray, with the override
  // kept visible on the result.
  async function onForce(e) {
    clearError();
    const idx = Number(e.currentTarget.getAttribute("data-force"));
    if (
      !window.confirm(
        "This brief was flagged as possibly containing sensitive data (e.g. a card, IBAN, " +
          "NHS number, or a person's name). Build the notebook anyway? The brief will be " +
          "sent to the assistant exactly as written."
      )
    ) {
      return;
    }
    e.currentTarget.disabled = true;
    const { ok, body } = await postJSON("/api/ai/batch/force", { batch_id: batchId, job: idx });
    if (!ok) {
      showError(body.error || "Could not start the build.");
      e.currentTarget.disabled = false;
      return;
    }
    scheduleTrayRefresh();
  }

  async function onOpen(e) {
    clearError();
    const path = e.currentTarget.getAttribute("data-open");
    const { ok, body } = await postJSON("/api/open", { path });
    if (ok && body.url) window.open(body.url, "_blank");
    else if (!ok) showError(body.error || "Could not open the notebook.");
  }

  async function onRefine(e) {
    clearError();
    const idx = Number(e.currentTarget.getAttribute("data-refine"));
    const input = document.querySelector(`[data-refine-note="${idx}"]`);
    const feedback = input ? input.value.trim() : "";
    if (!feedback) {
      showError("Type what you'd like changed about this notebook.");
      return;
    }
    e.currentTarget.disabled = true;
    const { ok, body } = await postJSON("/api/ai/batch/refine", {
      batch_id: batchId,
      job: idx,
      feedback,
    });
    if (!ok) {
      showError(body.error || "Could not start the revision.");
      e.currentTarget.disabled = false;
      return;
    }
    scheduleTrayRefresh(); // the "revising…" badge + the updated proposal arrive via the tray
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("build-btn").addEventListener("click", submitJobs);
    $("add-job").addEventListener("click", () => addJobCard());
    $("batch-model").addEventListener("change", () => {
      localStorage.setItem(LS_MODEL, $("batch-model").value);
      populateEfforts();
    });
    $("batch-effort").addEventListener("change", () => {
      localStorage.setItem(LS_EFFORT, $("batch-effort").value);
    });
    // Load models AFTER state so the picker can be gated on whether batch is enabled
    // (avoids briefly showing the model row under a "batch is off" notice).
    loadState().then(loadModels);
  });
})();
