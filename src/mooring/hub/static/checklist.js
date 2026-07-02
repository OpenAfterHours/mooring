"use strict";

// Pure, DOM-free derivation of the hub's first-run checklist — the ramp that
// teaches the working rhythm (pull → open → duplicate a draft → push/propose) by
// ticking itself off. Loaded BEFORE app.js (exposes `Checklist` as a bare global +
// on `window`, the files_tree.js idiom); under Node it is require()d by tests/js.
// Nothing here touches the DOM, network, or storage — app.js owns the rendering
// and the per-repo localStorage record this derives from.

const Checklist = (function () {
  // Filenames "Duplicate as draft" mints: {stem}-draft.py / {stem}-{login}-draft.py,
  // plus the -2, -3 collision counters. Shared with app.js's bulk-push confirm so
  // the two features can't disagree about what counts as a draft.
  const DRAFT_RE = /-draft(?:-\d+)?\.py$/;

  // Row states that PROVE a pull has happened: each needs a synced manifest base
  // (so the "pulled" item self-heals from /api/state). "local"/"new local" exist
  // without any remote tracking, and "new remote"/"deleted remotely" come straight
  // from the remote diff BEFORE any pull — a brand-new joiner sees the whole team
  // repo as "new remote", which proves the opposite of a pull.
  const PULLED_STATES = new Set([
    "synced", "modified", "deleted locally", "remote changed", "conflict", "mixed", "in review",
  ]);

  const ITEMS = [
    { id: "pulled", label: "Pull the team's notebooks" },
    { id: "opened", label: "Open a notebook" },
    { id: "duplicated", label: "Duplicate a draft to experiment safely" },
    { id: "pushed", label: "Push or propose a change" },
  ];

  // The four items with their done-ness: `pulled` and `duplicated` re-derive from
  // the file rows (they survive a cleared localStorage); `opened` and `pushed` are
  // stored flags app.js sets on success, with `pushed` also true while a proposal
  // review is open (the review banner proves a propose happened).
  function derive(files, review, stored) {
    const rows = files || [];
    const s = stored || {};
    const done = {
      pulled: rows.some((f) => PULLED_STATES.has(f.state)),
      opened: !!s.opened,
      duplicated: !!s.duplicated || rows.some((f) => DRAFT_RE.test(f.path || "")),
      pushed: !!s.pushed || !!review,
    };
    return ITEMS.map((item) => ({ id: item.id, label: item.label, done: done[item.id] }));
  }

  function isDone(items) {
    return (items || []).length > 0 && items.every((item) => item.done);
  }

  // Per-repo key so a second repo ramps afresh; `repo` is the slug /api/state
  // reports (e.g. "acme/nbs").
  function storageKey(repo) {
    return `mooring.checklist.${repo || "default"}`;
  }

  return { derive, isDone, storageKey, DRAFT_RE, ITEMS };
})();

if (typeof window !== "undefined") window.Checklist = Checklist;
if (typeof module !== "undefined" && module.exports) module.exports = Checklist;
