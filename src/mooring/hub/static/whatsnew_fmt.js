"use strict";

// Pure, DOM-free helpers for the "What's new" pull-digest panel: relative
// times, entry/group labels, the detail summary line, and the per-repo watch
// set round-trip. Loaded before app.js (bare global + window, the
// history_fmt.js idiom); under Node it is require()d by tests/js.

const WhatsnewFmt = (function () {
  // "just now" / "5 minutes ago" / "3 hours ago" / "2 days ago", falling back
  // to the plain date past ~4 weeks (relative time stops being useful there);
  // "" when unparsable. `now` is a parameter so the helper stays pure.
  function relativeTime(iso, now) {
    const ts = Date.parse(iso);
    if (Number.isNaN(ts)) return "";
    const minutes = Math.floor(Math.max(0, now - ts) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.floor(hours / 24);
    if (days < 28) return `${days} day${days === 1 ? "" : "s"} ago`;
    const d = new Date(ts);
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`;
  }

  // "maria · 2 days ago — fix the June totals": whatever attribution the entry
  // carries; "" when it carries none (the caller shows a placeholder).
  function entryLabel(entry, now) {
    const who = (entry.authors || []).join(", ");
    const when = relativeTime(entry.date || "", now);
    const head = [who, when].filter(Boolean).join(" · ");
    const message = (entry.messages || [])[0] || "";
    if (!head) return message;
    return message ? `${head} — ${message}` : head;
  }

  // One collapsed "push" from the window's commit groups:
  // "maria — fix the June totals (2 commits) · 2 days ago".
  function groupLabel(group, now) {
    const count = group.count > 1 ? ` (${group.count} commits)` : "";
    const when = relativeTime(group.date || "", now);
    const message = group.message || "(no message)";
    return `${group.author || "unknown"} — ${message}${count}${when ? ` · ${when}` : ""}`;
  }

  // The one-line "what actually changed" from /api/whatsnew/detail:
  // cell counts for notebooks, +/− line counts otherwise, sizes-only note last.
  function detailSummary(detail) {
    if (!detail || !detail.kind) return "";
    if (detail.kind === "cells") {
      const parts = [];
      if (detail.changed) parts.push(`${detail.changed} cell${detail.changed === 1 ? "" : "s"} changed`);
      if (detail.added) parts.push(`${detail.added} added`);
      if (detail.removed) parts.push(`${detail.removed} removed`);
      if (detail.unmatched) parts.push(`${detail.unmatched} rewritten`);
      return parts.length ? parts.join(", ") : "no cell changes";
    }
    if (detail.kind === "lines") {
      if (!detail.added && !detail.removed) return "no line changes";
      return `+${detail.added || 0} / −${detail.removed || 0} lines`;
    }
    return "contents not shown (binary or too large)";
  }

  // -- the per-repo watch set (client-side only, the theme-mirror posture) ---

  function watchKey(repo) {
    return `mooring.watch.${repo || ""}`;
  }

  // A stored JSON string (or null/junk) -> a Set of watched paths. Tolerant by
  // design: localStorage content is best-effort, never trusted shape.
  function watchSet(raw) {
    try {
      const parsed = JSON.parse(raw);
      return new Set(Array.isArray(parsed) ? parsed.filter((p) => typeof p === "string") : []);
    } catch {
      return new Set();
    }
  }

  function watchSerialize(set) {
    return JSON.stringify(Array.from(set).sort());
  }

  // Watched entries first (the point of watching), original order within each
  // half — a stable partition, never a re-sort of the server's ordering.
  function sortEntries(entries, watched) {
    const list = (entries || []).slice();
    const isWatched = (e) => !!(watched && watched.has(e.path));
    return list.filter(isWatched).concat(list.filter((e) => !isWatched(e)));
  }

  return {
    relativeTime,
    entryLabel,
    groupLabel,
    detailSummary,
    watchKey,
    watchSet,
    watchSerialize,
    sortEntries,
  };
})();

if (typeof window !== "undefined") window.WhatsnewFmt = WhatsnewFmt;
if (typeof module !== "undefined" && module.exports) module.exports = WhatsnewFmt;
