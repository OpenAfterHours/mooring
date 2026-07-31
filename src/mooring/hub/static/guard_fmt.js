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

  // One row per file the TEAM POLICY withheld from a direct push (propose-only
  // paths). These carry no token and have no override — Propose is the road.
  function policyRows(data) {
    return ((data && data.policy_blocked) || []).map((b) => `${b.path} — ${b.reason}`);
  }

  // Whether a response should open the confirm dialog at all — ANY of the three
  // guards firing is worth showing — and whether "Push anyway" may be offered.
  //
  // The override rules differ per guard and the server has already folded them
  // into `needs_confirm` ("something here can be acknowledged": content in warn
  // mode, or deps in any mode; never a policy block). `guard_mode` is the mode
  // that APPLIES to this response: the server sends "warn" when no content
  // finding fired, because a content policy has nothing to say about a lock file
  // or a propose-only path and must not silently wall either off.
  function needsDialog(data) {
    if (!data) return false;
    const content = (data.guard_findings || []).length;
    const deps = (data.sweep_findings || []).length;
    const blocked = (data.policy_blocked || []).length;
    return !!(content || deps || blocked);
  }
  function canOverride(data) {
    return !!(data && data.needs_confirm) && data.guard_mode !== "block";
  }

  return { rows, depsRows, policyRows, allTokens, needsDialog, canOverride };
})();

if (typeof window !== "undefined") window.GuardFmt = GuardFmt;
if (typeof module !== "undefined" && module.exports) module.exports = GuardFmt;
