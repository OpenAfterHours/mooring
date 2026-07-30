"use strict";

// Pure, DOM-free helpers for the scheduled-refresh board: how a schedule's state reads,
// how its last run reads, and what the card's banner says. Loaded before app.js (bare
// global + window, the files_tree.js idiom); under Node it is require()d by tests/js.
//
// The whole point of the board is that a stale refresh is never silent, so the wording
// rules live here once and are unit-tested: "overdue" always wins over the last outcome,
// a paused schedule always says so, and a run that only LOOKED clean (it could not pull,
// or its tie-outs failed) never reads as green.

const ScheduleFmt = (function () {
  // Row tone, worst first. Drives the badge class and therefore the colour.
  const BAD = "bad";
  const WARN = "warn";
  const GOOD = "good";
  const IDLE = "idle";

  // How each recorded outcome reads on its own, before staleness is considered.
  const OUTCOMES = {
    ok: { text: "ran clean", tone: GOOD },
    degraded: { text: "ran, degraded", tone: WARN },
    checks_failed: { text: "checks failing", tone: BAD },
    failed: { text: "did not run", tone: BAD },
  };

  // "2026-07-30T07:30:00+00:00" -> "30 Jul 07:30". Returns "" for anything unparseable,
  // so a corrupt receipt degrades to a blank cell rather than "Invalid Date".
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const day = String(d.getDate()).padStart(2, "0");
    const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${day} ${month} ${hh}:${mm}`;
  }

  // The state badge for one row: {text, tone}.
  //
  // Ordering is the load-bearing part. Paused and overdue are reported BEFORE the last
  // outcome, because "it ran clean" is a claim about the past and both of those are claims
  // about now — a schedule that ran clean on Monday and has been paused since must never
  // show green on Friday.
  function state(row) {
    if (!row) return { text: "", tone: IDLE };
    if (row.paused) {
      const auto = row.consecutive_failures > 0;
      return {
        text: auto ? `paused after ${row.consecutive_failures} failed run(s)` : "paused",
        tone: auto ? BAD : IDLE,
      };
    }
    const last = row.last_run || {};
    const outcome = OUTCOMES[last.outcome];
    if (row.overdue) {
      return { text: outcome ? `overdue · last ${outcome.text}` : "overdue · never run", tone: BAD };
    }
    if (!outcome) return { text: row.due ? "due now" : "waiting", tone: IDLE };
    return outcome;
  }

  // The detail line under a row: what the last run found, then why it is worth knowing.
  // Counts and curated reasons only — never a data value.
  function detail(row) {
    if (!row) return "";
    const last = row.last_run || {};
    const bits = [];
    if (last.checks_failed) bits.push(`${last.checks_failed} tie-out check(s) failing`);
    if (last.inputs_changed) bits.push(`${last.inputs_changed} input(s) changed`);
    if (last.conflicts) bits.push(`${last.conflicts} file(s) in conflict`);
    // The recorded reason restates whatever the counts already show ("2 of 5 tie-out
    // check(s) failing"), so it only earns a place when there are no counts to show —
    // which is exactly the case it carries new information (offline, signed out, a run
    // error). Otherwise the line would say the same thing twice.
    if (last.reason && !bits.length) bits.push(last.reason);
    return bits.join(" · ");
  }

  // "Verify it again to schedule" style hints — why a row will NOT fire by itself. Empty
  // when the row is in the boring state that auto-runs, so the UI only explains exceptions.
  function autoHint(row) {
    if (!row || row.auto) return "";
    if (row.paused) return "Paused — resume it to run again.";
    if (!row.verified) return "Edited since it was verified — run it once to confirm it still works.";
    if ((row.last_run || {}).outcome === "failed") return "Last run failed — run it manually to retry.";
    return "";
  }

  // The card's banner: the single most important thing about the whole board, or "" for
  // nothing worth saying. Overdue outranks due, because overdue is the failure the feature
  // exists to make impossible to miss.
  function banner(board) {
    const b = board || {};
    const rows = b.schedules || [];
    if (!rows.length) return "";
    const paused = rows.filter((r) => r.paused).length;
    if (b.overdue) {
      const n = b.overdue;
      return `⚠ ${n} refresh${n === 1 ? "" : "es"} overdue — ${n === 1 ? "it has" : "they have"}` +
             " not run since it was due.";
    }
    if (paused) return `${paused} schedule(s) paused — they will not run until resumed.`;
    if (b.due) return `${b.due} refresh(es) due — they run automatically, or use Run now.`;
    return "";
  }

  // Next-due text for a row, e.g. "next 31 Jul 07:30".
  function nextDue(row) {
    const text = when((row || {}).next_due);
    return text ? `next ${text}` : "";
  }

  return { state, detail, autoHint, banner, nextDue, when, OUTCOMES };
})();

if (typeof window !== "undefined") window.ScheduleFmt = ScheduleFmt;
if (typeof module !== "undefined" && module.exports) module.exports = ScheduleFmt;
