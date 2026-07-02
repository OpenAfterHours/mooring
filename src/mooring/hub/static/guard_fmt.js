"use strict";

// Pure, DOM-free helpers for the push guard's warn-and-confirm flow: findings
// rows for the dialog, the token set for the "Push anyway" re-POST, and the
// re-POST decision. Loaded before app.js (bare global + window, the
// files_tree.js idiom); under Node it is require()d by tests/js.

const GuardFmt = (function () {
  // One value-free row per finding: "notebooks/a.py — line 12: GitHub token".
  function rows(guardFindings) {
    const out = [];
    for (const file of guardFindings || []) {
      for (const f of file.findings || []) {
        out.push(`${file.path} — line ${f.line}: ${f.kind}`);
      }
    }
    return out;
  }

  // The per-file confirm tokens to carry on an acknowledged re-POST. Each token
  // binds one file's exact findings to its exact bytes server-side, so a stale
  // acknowledgement never covers a changed file or a new finding.
  function allTokens(guardFindings) {
    return (guardFindings || []).map((f) => f.token).filter(Boolean);
  }

  // Whether a response should open the confirm dialog at all, and whether the
  // "Push anyway" button may be offered (never in block mode).
  function needsDialog(data) {
    return !!(data && data.guard_findings && data.guard_findings.length);
  }
  function canOverride(data) {
    return !!(data && data.needs_confirm) && data.guard_mode !== "block";
  }

  return { rows, allTokens, needsDialog, canOverride };
})();

if (typeof window !== "undefined") window.GuardFmt = GuardFmt;
if (typeof module !== "undefined" && module.exports) module.exports = GuardFmt;
