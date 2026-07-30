"use strict";

// Pure, DOM-free helpers for the cell-level conflict merge panel: the human
// label for each cell's fate, the block list app.js renders, the two option
// labels of a contested cell, and the headline count. Loaded before app.js
// (bare global + window, the diff_fmt.js idiom); under Node it is require()d by
// tests/js. Strings only, never HTML — the renderer sets textContent, because
// notebook source is untrusted and must not inject markup into the hub.
//
// The vocabulary is deliberately blunt ("you" / "the team"): a conflict is the
// one moment where an analyst must be certain whose work a click keeps.

const MergeFmt = (function () {
  // Base-derived cells keep their last-synced position (the DiffFmt convention,
  // 1-based for humans); a cell neither side inherited is numbered separately —
  // it has no position in the version both people share.
  function cellName(cell, newNo) {
    return cell.origin === "base" ? `Cell ${cell.index_base + 1}` : `New cell ${newNo}`;
  }

  function cellLabel(cell, newNo) {
    const name = cellName(cell, newNo);
    // Only a cell BOTH of you changed is ever contested — two people's brand-new
    // cells are both kept, never put head to head (see conflict_merge).
    if (cell.status === "choice") return `${name} — you both changed it · choose one`;
    if (cell.side === "unchanged") return `${name} — unchanged`;
    if (cell.dropped) {
      if (cell.side === "both") return `${name} — you both deleted it`;
      return cell.side === "local" ? `${name} — you deleted it` : `${name} — the team deleted it`;
    }
    if (cell.origin !== "base") {
      if (cell.side === "both") return `${name} — you both added it, merged`;
      return cell.side === "local"
        ? `${name} — added by you, merged`
        : `${name} — added by the team, merged`;
    }
    if (cell.side === "both") return `${name} — the same change on both sides, merged`;
    return cell.side === "local"
      ? `${name} — your change, merged`
      : `${name} — the team's change, merged`;
  }

  // The two buttons of a contested cell. A side with no cell DELETED it, so its
  // option says so plainly rather than offering an empty "version".
  function choiceOptions(cell) {
    return [
      {
        value: "local",
        label: cell.has_local ? "Keep my version" : "Drop the cell (you deleted it)",
      },
      {
        value: "remote",
        label: cell.has_remote
          ? "Take the team's version"
          : "Drop the cell (the team deleted it)",
      },
    ];
  }

  // Blocks for the panel, in merged-document order: an auto cell collapses to its
  // one label line, a contested one carries its diff and the two options.
  function buildBlocks(plan) {
    let newNo = 0;
    return ((plan && plan.cells) || []).map((cell) => {
      if (cell.origin !== "base") newNo += 1;
      return {
        id: cell.id,
        status: cell.status,
        label: cellLabel(cell, newNo),
        collapsed: cell.status !== "choice",
        diff: cell.status === "choice" ? cell.diff || "" : "",
        options: cell.status === "choice" ? choiceOptions(cell) : [],
      };
    });
  }

  function conflictIds(plan) {
    return ((plan && plan.cells) || []).filter((c) => c.status === "choice").map((c) => c.id);
  }

  // Contested cells with no decision yet. There is deliberately no default —
  // pre-selecting a side is exactly the silent loss this panel exists to prevent.
  function unresolved(plan, choices) {
    const picked = choices || {};
    return conflictIds(plan).filter(
      (id) => picked[id] !== "local" && picked[id] !== "remote",
    );
  }

  function ready(plan, choices) {
    return !!plan && unresolved(plan, choices).length === 0;
  }

  // "9 cells merged automatically (3 yours · 6 the team's) · 12 unchanged ·
  // 2 need your choice" — the auto count is the headline, because it is the
  // work the analyst does NOT have to do.
  function summary(plan) {
    if (!plan) return "";
    const auto = plan.auto_merged || 0;
    const parts = [`${auto} ${auto === 1 ? "cell" : "cells"} merged automatically`];
    const detail = [];
    if (plan.auto_local) detail.push(`${plan.auto_local} yours`);
    if (plan.auto_remote) detail.push(`${plan.auto_remote} the team's`);
    if (plan.auto_both) detail.push(`${plan.auto_both} identical on both sides`);
    if (detail.length) parts[0] += ` (${detail.join(" · ")})`;
    if (plan.unchanged) parts.push(`${plan.unchanged} unchanged`);
    const n = conflictIds(plan).length;
    parts.push(n ? `${n} ${n === 1 ? "needs" : "need"} your choice` : "nothing left to choose");
    return parts.join(" · ");
  }

  // A notebook's header (PEP 723 script dependencies, marimo.App settings) is
  // merged whole, not per cell — say so when the team's is the one being kept, or
  // it reads as if their dependency pin vanished.
  function frameNote(plan) {
    return plan && plan.frame_from === "remote"
      ? "The team's notebook header — its script dependencies and app settings — is kept."
      : "";
  }

  return {
    cellName,
    cellLabel,
    choiceOptions,
    buildBlocks,
    conflictIds,
    unresolved,
    ready,
    summary,
    frameNote,
  };
})();

if (typeof window !== "undefined") window.MergeFmt = MergeFmt;
if (typeof module !== "undefined" && module.exports) module.exports = MergeFmt;
