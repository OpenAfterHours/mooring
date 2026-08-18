"use strict";

// Pure, DOM-free wording for the detail panel's RECEIPTS ledger — the mono lines
// that replaced the row's badge cluster (verify / tie-out checks / input
// fingerprints / lineage). Loaded BEFORE app.js (bare global + window, the
// files_tree.js idiom); under Node it is require()d by tests/js. Nothing here
// touches the DOM, network, or storage.
//
// These are the SAME claims the row badges made, with the same honesty caveats in
// the same words — only the presentation changed (a three-character code and a
// line, instead of a pill). Two rules carry over from the badges and are pinned by
// tests: a receipt with no payload produces NO line (never a placeholder, never a
// reassuring "nothing to report"), and a claim that has gone stale keeps its date
// and drops to muted rather than being dropped.
//
// Codes are three characters so the ledger stays a column, and they carry the
// meaning WITHOUT the colour (the panel is not allowed to signal by colour alone):
//   "ok "  something ran or matched     "chg"  something moved under you
//   "!! "  something failed             "lin"  recorded lineage

const ReceiptsFmt = (function () {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // "2026-08-12T…" -> "12 Aug". "" for anything unparseable, so a corrupt receipt
  // degrades to an undated line rather than "Invalid Date".
  function dayText(iso) {
    if (!iso) return "";
    const d = new Date(String(iso).slice(0, 10) + "T00:00:00");
    if (isNaN(d.getTime())) return "";
    return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  // The trust receipt from a Verify run. The server only sends `verified` while it
  // still matches the file's content SHA, so a stale "ran clean" never rides
  // edited code — the line is simply absent once the notebook moves on.
  function verified(v) {
    if (!v) return null;
    const day = dayText(v.ran_at);
    const when = v.ran_at ? ` (${String(v.ran_at).slice(0, 10)})` : "";
    if (v.passed) {
      return {
        code: "ok ",
        tone: "ok",
        text: day ? `ran clean · ${day}` : "ran clean",
        title: `This notebook ran clean end-to-end when last verified${when}. ` +
          "Value-free and local; clears when you edit it.",
      };
    }
    const cells = v.cells_failed
      ? `${v.cells_failed} cell${v.cells_failed === 1 ? "" : "s"} failed`
      : "failed to run";
    return {
      code: "!! ",
      tone: "warn",
      text: day ? `${cells} · ${day}` : cells,
      title: `This notebook did not run clean when last verified${when} — open it to ` +
        "see which cell failed.",
    };
  }

  // Value-free tie-out results (mooring_checks): counts only, never a data value.
  function checks(c) {
    if (!c || !c.total) return null;
    const failed = c.failed || 0;
    const total = c.total || 0;
    if (failed > 0) {
      return {
        code: "!! ",
        tone: "warn",
        text: `${failed} of ${total} tie-out check${total === 1 ? "" : "s"} failing`,
        title: `${failed} of ${total} tie-out check(s) are failing — open the notebook to see which.`,
      };
    }
    return {
      code: "ok ",
      tone: "ok",
      text: `${total} tie-out check${total === 1 ? "" : "s"} pass`,
      title: `${total} tie-out check(s) passing (mooring_checks). Value-free and never pushed.`,
    };
  }

  // Value-free input/output fingerprints (mooring_inputs). An output that moved is
  // the same alarm as an input that moved — the numbers this notebook PUBLISHES are
  // no longer the ones it published last run — so either goes amber.
  function inputs(inp) {
    if (!inp || (!inp.total && !inp.outputs)) return null;
    const changed = inp.changed || 0;
    const total = inp.total || 0;
    const outputs = inp.outputs || 0;
    const outputsChanged = inp.outputs_changed || 0;
    if (changed > 0 || outputsChanged > 0) {
      const bits = [];
      if (changed) bits.push(`${changed} of ${plural(total, "pinned input")}`);
      if (outputsChanged) bits.push(`${outputsChanged} of ${plural(outputs, "output")}`);
      return {
        code: "chg",
        tone: "warn",
        text: `${bits.join(" + ")} changed`,
        title: `Of this notebook's ${total} pinned input(s) and ${outputs} recorded ` +
          `output(s), ${changed + outputsChanged} changed since the last run (content, row ` +
          "count, or schema) — check the numbers still hold. Value-free and local.",
      };
    }
    const bits = [];
    if (total) bits.push(`${plural(total, "input")} pinned`);
    if (outputs) bits.push(plural(outputs, "output"));
    return {
      code: "ok ",
      tone: "ok",
      text: bits.join(", "),
      title: `${total} input(s) and ${outputs} output(s) fingerprinted (content hash + ` +
        "shape + schema), unchanged since the last run. Value-free and never pushed.",
    };
  }

  // Recorded lineage ("3 notebooks read this"). The wording — entirely positive
  // claims, each dated — stays in LineageFmt; a null there means the payload
  // supports no claim, so there is no line. A stale claim is kept, dated and muted.
  function lineage(lin, fmt) {
    const L = fmt || (typeof LineageFmt !== "undefined" ? LineageFmt : null);
    if (!lin || !L) return null;
    const parts = L.badge(lin);
    if (!parts) return null;
    return {
      code: "lin",
      tone: lin.stale ? "muted" : "accent",
      // LineageFmt.badge leads with its own ⇄ glyph for the pill; the ledger's
      // three-character code does that job here, so strip it.
      text: parts.text.replace(/^\u21C4\s*/, ""),
      title: parts.title,
    };
  }

  // Every receipt a file supports, in reading order: did it run, did it tie out,
  // did its inputs hold, who else depends on it. Absent payloads contribute nothing.
  function lines(file, fmt) {
    const f = file || {};
    return [verified(f.verified), checks(f.checks), inputs(f.inputs), lineage(f.lineage, fmt)]
      .filter(Boolean);
  }

  return { lines, verified, checks, inputs, lineage, dayText };
})();

if (typeof window !== "undefined") window.ReceiptsFmt = ReceiptsFmt;
if (typeof module !== "undefined" && module.exports) module.exports = ReceiptsFmt;
