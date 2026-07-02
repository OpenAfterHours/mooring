"use strict";

// Pure, DOM-free helpers for the "Review changes" panel (the cell-aware
// pre-push diff): a human label for each cell's fate, the block list app.js
// renders, and the one-line summary. Loaded before app.js (bare global +
// window, the history_fmt.js idiom); under Node it is require()d by tests/js.
// Strings only, never HTML: the renderer sets textContent — notebook source is
// untrusted and must not be able to inject markup into the hub.

const DiffFmt = (function () {
  // "Cell 3 — changed". Positions are 1-based for humans (0-based in the API);
  // an unmatched cell never claims more than "no confident match".
  function cellLabel(entry) {
    const localNo = entry.index_local == null ? null : entry.index_local + 1;
    const baseNo = entry.index_base == null ? null : entry.index_base + 1;
    switch (entry.status) {
      case "added":
        return `Cell ${localNo} — new`;
      case "removed":
        return `Cell ${baseNo} — removed`;
      case "changed":
        return baseNo === localNo
          ? `Cell ${localNo} — changed`
          : `Cell ${localNo} — changed (was cell ${baseNo})`;
      case "unchanged":
        return baseNo === localNo
          ? `Cell ${localNo} — unchanged`
          : `Cell ${localNo} — unchanged (moved from cell ${baseNo})`;
      default: // "unmatched" — the honest ambiguity bucket
        return localNo == null
          ? `Cell ${baseNo} (last synced) — no confident match in your copy`
          : `Cell ${localNo} (your copy) — no confident match in the last-synced version`;
    }
  }

  // Blocks for the panel: an unchanged cell collapses to its one label line
  // (no diff text), everything else carries its unified diff to render.
  function buildBlocks(cells) {
    return (cells || []).map((c) => ({
      status: c.status,
      label: cellLabel(c),
      collapsed: c.status === "unchanged",
      diff: c.status === "unchanged" ? "" : (c.diff || ""),
    }));
  }

  // "2 changed · 1 new · 3 unchanged" for a cell diff; the server's note
  // (fallback reason / sizes) otherwise.
  function summary(result) {
    if (!result) return "";
    if (result.kind !== "cells") return result.note || "";
    const counts = { changed: 0, added: 0, removed: 0, unmatched: 0, unchanged: 0 };
    for (const c of result.cells || []) {
      if (counts[c.status] != null) counts[c.status] += 1;
    }
    const parts = [];
    if (counts.changed) parts.push(`${counts.changed} changed`);
    if (counts.added) parts.push(`${counts.added} new`);
    if (counts.removed) parts.push(`${counts.removed} removed`);
    if (counts.unmatched) parts.push(`${counts.unmatched} unmatched`);
    if (counts.unchanged) parts.push(`${counts.unchanged} unchanged`);
    const text = parts.length ? parts.join(" · ") : "no cells";
    return result.note ? `${text} — ${result.note}` : text;
  }

  return { cellLabel, buildBlocks, summary };
})();

if (typeof window !== "undefined") window.DiffFmt = DiffFmt;
if (typeof module !== "undefined" && module.exports) module.exports = DiffFmt;
