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

  // One row per dependency-gate finding. Same value-free shape, but no line to
  // point at — the finding is about the whole lock file, so the "line N:" prefix
  // the content rows carry would be noise.
  function depsRows(data) {
    const out = [];
    for (const file of (data && data.sweep_findings) || []) {
      for (const f of file.findings || []) out.push(`${file.path} — ${f.kind}`);
    }
    return out;
  }

  // The per-file confirm tokens to carry on an acknowledged re-POST. Each token
  // binds one file's exact findings to its exact bytes server-side, so a stale
  // acknowledgement never covers a changed file or a new finding. Both guards'
  // tokens travel together — the re-POST is one push.
  function allTokens(data) {
    const lists = Array.isArray(data)
      ? [data]
      : [(data && data.guard_findings) || [], (data && data.sweep_findings) || []];
    const out = [];
    for (const list of lists) for (const f of list) if (f.token) out.push(f.token);
    return out;
  }

  // Whether a response should open the confirm dialog at all, and whether the
  // "Push anyway" button may be offered (never in block mode). `guard_mode` is
  // the mode that applies to THIS response: the server sends "warn" when only
  // the dependency gate fired, because that gate warns about a broken notebook
  // rather than something that must not leave the machine — a content policy
  // must not silently become a wall around lock files.
  function needsDialog(data) {
    if (!data) return false;
    const content = (data.guard_findings || []).length;
    const deps = (data.sweep_findings || []).length;
    return !!(content || deps);
  }
  function canOverride(data) {
    return !!(data && data.needs_confirm) && data.guard_mode !== "block";
  }

  return { rows, depsRows, allTokens, needsDialog, canOverride };
})();

if (typeof window !== "undefined") window.GuardFmt = GuardFmt;
if (typeof module !== "undefined" && module.exports) module.exports = GuardFmt;
