"use strict";

// Pure, DOM-free helpers for the scheduled-refresh board: how a schedule's state reads,
// how its last run reads, and what the card's banner says. Loaded before app.js (bare
// global + window, the files_tree.js idiom); under Node it is require()d by tests/js.
//
// The whole point of the board is that a stale refresh is never silent, so the wording
// rules live here once and are unit-tested: "overdue" always wins over the last outcome,
// a paused schedule always says so, and a run that only LOOKED clean (it could not pull,
// or its tie-outs failed) never reads as green. The mirror-image rule matters just as
// much: a one-shot that has already run is DONE, and must never be nagged about as due,
// overdue or paused — the board only worries about refreshes that are still owed.

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

  // A one-shot ("once") schedule that has already run CLEAN is FINISHED: it will never fire
  // again, so every claim the board can otherwise make about it — due, overdue, next due,
  // even paused — describes a future it does not have. The server DERIVES `complete` (the
  // cadence, plus a receipt from inside this one-shot's OWN window, that did not fail — never
  // a stored flag, so it cannot drift out of step with the receipt); this file only decides
  // how it reads. Note what that leaves to the branches below: a one-shot whose run FAILED is
  // NOT done, so it falls through to paused/overdue and keeps the loud badge, exactly like a
  // failed daily. "Done" must never be the word that hides a run which did not finish.
  function isDone(row) {
    return !!(row && row.complete);
  }

  // The state badge for one row: {text, tone}.
  //
  // Ordering is the load-bearing part. Paused and overdue are reported BEFORE the last
  // outcome, because "it ran clean" is a claim about the past and both of those are claims
  // about now — a schedule that ran clean on Monday and has been paused since must never
  // show green on Friday.
  function state(row) {
    if (!row) return { text: "", tone: IDLE };
    // By that same rule "done" outranks paused and overdue as well: it is the strongest
    // available claim about NOW (this one will not run again, ever), which is what those
    // two are groping at and would say wrongly. The outcome still rides along instead of
    // being replaced — "done" must never be the word that hides a run which did not finish.
    if (isDone(row)) {
      const ran = OUTCOMES[(row.last_run || {}).outcome];
      return ran ? { text: `done · ${ran.text}`, tone: ran.tone } : { text: "done", tone: IDLE };
    }
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
    // A finished one-shot never auto-runs (may_auto_run reads its spent window), so without
    // this guard EVERY one of them would draw a hint — and the one it would draw is about a
    // run that is never coming. Same rule as state/nextDue/banner: done outranks the lot.
    if (isDone(row)) return "";
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
    // Finished one-shots are invisible to the banner: they are neither due nor overdue
    // (the server's counts already leave them out), and calling one "paused" would nag
    // about resuming a schedule that has already had its single run. A board holding
    // nothing but finished one-shots therefore says nothing at all, which is correct —
    // there is no freshness left to be worried about.
    const rows = (b.schedules || []).filter((r) => !isDone(r));
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
    // A finished one-shot has no next run. The board still carries a next_due for it (its
    // own window is the only instant it could report), so printing it unguarded would
    // promise a refresh that is never coming.
    if (isDone(row)) return "";
    const text = when((row || {}).next_due);
    return text ? `next ${text}` : "";
  }

  // -- the one-shot date the form OFFERS -------------------------------------
  // Pure, and here rather than in app.js, so the rule below is pinned by a test instead of
  // living only in a comment. Both take an injectable `now` for that reason; the form
  // passes none and gets the wall clock.

  // A LOCAL wall-clock date `days` from `now`, as "YYYY-MM-DD". Deliberately not
  // toISOString().slice(0, 10), which is UTC — west of Greenwich that hands back yesterday
  // for most of the evening, and the date a user picks here is a local wall-clock date.
  // setDate() past the end of a month rolls the month/year over for us.
  function localDate(days = 0, now = new Date()) {
    const when = new Date(now.getTime());
    when.setDate(when.getDate() + days);
    const month = String(when.getMonth() + 1).padStart(2, "0");
    const day = String(when.getDate()).padStart(2, "0");
    return `${when.getFullYear()}-${month}-${day}`;
  }

  // The date to OFFER a one-shot that hasn't got one yet: the first date on which `at` is
  // still ahead of us — today while it is, tomorrow once it isn't.
  //
  // Deliberately not just today. A one-shot whose instant has already passed arrives SPENT:
  // the server calls a one-shot complete once a run lands inside its own window, so switching
  // a daily to "once" after this morning's tick would bank the run that already happened and
  // create a schedule that is finished on arrival — while the hub cheerfully announces it as
  // newly booked. Waiting for a refresh that has already been and gone is exactly the silence
  // this whole board exists to prevent, so the default must never be able to produce it.
  //
  // A past date stays perfectly legal (it means "catch up now"); it just isn't something the
  // form should pick on the user's behalf.
  function firstUnspentDate(at, now = new Date()) {
    const [hour, minute] = String(at || "").split(":");
    const when = new Date(now.getTime());
    when.setHours(Number(hour) || 0, Number(minute) || 0, 0, 0);
    return localDate(when.getTime() > now.getTime() ? 0 : 1, now);
  }

  return {
    state, detail, autoHint, banner, nextDue, isDone, when,
    localDate, firstUnspentDate, OUTCOMES,
  };
})();

if (typeof window !== "undefined") window.ScheduleFmt = ScheduleFmt;
if (typeof module !== "undefined" && module.exports) module.exports = ScheduleFmt;
