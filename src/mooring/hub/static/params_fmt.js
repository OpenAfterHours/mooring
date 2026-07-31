"use strict";

// Pure, DOM-free helpers for the parameterised-run card: how one value's row reads, how the
// whole fan-out reads while it is going, and what the summary says when it stops. Loaded
// before app.js (bare global + window, the schedule_fmt.js idiom); under Node it is
// require()d by tests/js.
//
// The wording rules live here once and are unit-tested because the feature's one real
// danger is a PARTIAL pack that reads as a complete one. So: the summary NEVER says
// "done" unless every value ran clean, a cancelled fan-out always names the values that
// never ran, and a value whose artifact failed to write never reads as delivered.

const ParamsFmt = (function () {
  const BAD = "bad";
  const WARN = "warn";
  const GOOD = "good";
  const IDLE = "idle";

  // Per-value outcomes, mirroring app/param_runs.py's vocabulary.
  const OUTCOMES = {
    ok: { text: "ran clean", tone: GOOD },
    failed: { text: "failed", tone: BAD },
    cancelled: { text: "cancelled", tone: WARN },
    skipped: { text: "not run", tone: IDLE },
  };

  // The badge for one value's row: {text, tone}. A value that ran clean but produced NO
  // artifact is deliberately not plain green — the run happened, the deliverable did not,
  // and that gap is exactly what someone assembling a pack needs to see.
  function valueState(run) {
    if (!run) return { text: "", tone: IDLE };
    const outcome = OUTCOMES[run.outcome];
    if (!outcome) return { text: "", tone: IDLE };
    if (run.outcome === "ok" && run.ran && !run.artifact) {
      return { text: "ran clean, no artifact", tone: WARN };
    }
    return outcome;
  }

  // The detail line under a value's row: the curated reason, or where the artifact landed.
  // Never marimo's stderr — the server does not send it and this never invents it.
  function valueDetail(run) {
    if (!run) return "";
    if (run.artifact) return run.artifact;
    return run.reason || "";
  }

  // Rows for the whole card: one per declared value, in the order they will run, whether
  // or not the server has reported them yet. Building from `values` rather than from
  // `runs` is what makes a partial fan-out visible: a value that never ran still has a
  // row, marked "queued", instead of silently not existing.
  function rows(snap) {
    const s = snap || {};
    const values = s.values || [];
    const byValue = new Map();
    for (const run of s.runs || []) byValue.set(run.value, run);
    return values.map((value, i) => {
      const run = byValue.get(value);
      if (run) return { value, index: i + 1, run, state: valueState(run), detail: valueDetail(run) };
      const running = s.running === value;
      return {
        value,
        index: i + 1,
        run: null,
        state: { text: running ? "running…" : s.done ? "not run" : "queued", tone: IDLE },
        detail: "",
      };
    });
  }

  function counts(snap) {
    const runs = (snap || {}).runs || [];
    const total = ((snap || {}).values || []).length;
    const clean = runs.filter((r) => r.outcome === "ok").length;
    const failed = runs.filter((r) => r.outcome === "failed").length;
    return { total, clean, failed, done: runs.length };
  }

  // "2 of 3" — how far through the fan-out we are. 0 when nothing has finished.
  function progress(snap) {
    const c = counts(snap);
    return c.total ? Math.round((c.done / c.total) * 100) : 0;
  }

  // The card's headline. While it runs it says which value is going; once it stops it says
  // whether the pack is COMPLETE, and never implies completeness it cannot back up.
  function summary(snap) {
    const s = snap || {};
    const c = counts(s);
    if (s.error) return s.error;
    if (!s.done) {
      if (s.cancelling) return "Cancelling… the notebook is being stopped.";
      const now = s.running ? `${s.param} = ${s.running}` : "starting";
      return `Running ${now} (${c.done} of ${c.total} done). One value at a time.`;
    }
    if (c.total && c.clean === c.total) {
      return `Done — all ${c.total} value(s) ran clean.`;
    }
    const bits = [];
    if (c.failed) bits.push(`${c.failed} failed`);
    const missing = c.total - c.clean - c.failed;
    if (missing > 0) bits.push(`${missing} did not run`);
    const tail = bits.length ? ` (${bits.join(", ")})` : "";
    return `INCOMPLETE — ${c.clean} of ${c.total} value(s) ran clean${tail}.`;
  }

  // The tone for the whole card, so the banner colour matches the wording above.
  function tone(snap) {
    const s = snap || {};
    const c = counts(s);
    if (s.error) return BAD;
    if (!s.done) return IDLE;
    if (c.total && c.clean === c.total) return GOOD;
    return c.failed ? BAD : WARN;
  }

  // Parse what the user typed into the "for" box just enough to preview it — the SERVER
  // is the authority (mooring.params.parse_spec) and re-validates everything. This exists
  // so the button can read "Run 3 times" before anything is executed, which is the last
  // cheap moment to notice a typo.
  function previewValues(text) {
    const raw = String(text || "");
    const at = raw.indexOf("=");
    if (at < 0) return { name: "", values: [] };
    const name = raw.slice(0, at).trim();
    const values = [];
    for (const item of raw.slice(at + 1).split(",")) {
      const piece = item.trim();
      if (!piece) continue;
      const range = expandRange(piece);
      if (range) values.push(...range);
      else values.push(piece);
    }
    return { name, values };
  }

  // Whole numbers (1..12) and calendar months (2026-01..2026-06) — the same closed
  // vocabulary the server expands, so the preview never promises a range it will refuse.
  function expandRange(piece) {
    const at = piece.indexOf("..");
    if (at < 0) return null;
    const lo = piece.slice(0, at).trim();
    const hi = piece.slice(at + 2).trim();
    const months = /^(\d{4})-(0[1-9]|1[0-2])$/;
    const ml = months.exec(lo);
    const mh = months.exec(hi);
    if (ml && mh) {
      const first = Number(ml[1]) * 12 + Number(ml[2]) - 1;
      const last = Number(mh[1]) * 12 + Number(mh[2]) - 1;
      if (last < first) return [];
      const out = [];
      for (let n = first; n <= last && out.length <= 200; n++) {
        out.push(`${String(Math.floor(n / 12)).padStart(4, "0")}-${String((n % 12) + 1).padStart(2, "0")}`);
      }
      return out;
    }
    if (/^-?\d{1,9}$/.test(lo) && /^-?\d{1,9}$/.test(hi)) {
      const first = Number(lo);
      const last = Number(hi);
      if (last < first) return [];
      const out = [];
      for (let n = first; n <= last && out.length <= 200; n++) out.push(String(n));
      return out;
    }
    return null;
  }

  return { valueState, valueDetail, rows, counts, progress, summary, tone, previewValues, OUTCOMES };
})();

if (typeof window !== "undefined") window.ParamsFmt = ParamsFmt;
if (typeof module !== "undefined" && module.exports) module.exports = ParamsFmt;
