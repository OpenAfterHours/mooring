"use strict";

// Pure, DOM-free helpers for the version-history panel: human labels for a
// version row and the restore-over gate. Loaded before app.js (bare global +
// window, the files_tree.js idiom); under Node it is require()d by tests/js.

const HistoryFmt = (function () {
  // "12 Jun 2026 08:05" from an ISO timestamp; "" when unparsable.
  function dateText(iso) {
    const ts = Date.parse(iso);
    if (Number.isNaN(ts)) return "";
    const d = new Date(ts);
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()} ${hm}`;
  }

  // One human line per version, leading with WHEN and WHO (not the sha):
  // "12 Jun 2026 08:05 · phil — Update sales.py via mooring (abc1234)".
  function versionLabel(v) {
    const when = dateText(v.date) || "unknown date";
    const who = v.author ? ` · ${v.author}` : "";
    const message = v.message ? ` — ${v.message}` : "";
    return `${when}${who}${message} (${v.short || String(v.sha || "").slice(0, 7)})`;
  }

  // Restore-over is gated like Revert: .py only (a lone PBIP member would
  // corrupt the artifact; data files restore as copies).
  function canRestoreOver(path) {
    return typeof path === "string" && path.endsWith(".py");
  }

  // Rows with a remote past: anything except never-synced local files.
  function hasHistory(file) {
    return !!file && file.state !== "local" && file.state !== "new local";
  }

  return { dateText, versionLabel, canRestoreOver, hasHistory };
})();

if (typeof window !== "undefined") window.HistoryFmt = HistoryFmt;
if (typeof module !== "undefined" && module.exports) module.exports = HistoryFmt;
