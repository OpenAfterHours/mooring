"use strict";

// Pure, DOM-free formatting for the Activity page: one ledger entry → one human
// sentence, and an ISO timestamp → a compact relative time. Loaded before
// activity.js (bare global + window, the files_tree.js idiom); under Node it is
// require()d by tests/js. Nothing here touches the DOM, network, or storage.

const ActivityFmt = (function () {
  // op → sentence builder. Each returns the human phrase WITHOUT the time
  // prefix; unknown ops fall back to the op name so a new ledger field never
  // renders as a blank row.
  const name = (p) => String(p || "").split("/").pop();

  const OPS = {
    pull: (e) => `you pulled — ${e.summary || "no changes"}`,
    push: (e) => `you pushed — ${e.summary || "no changes"}`,
    propose: (e) => `you proposed changes for review — ${e.summary || ""}`.trim(),
    adopt: (e) => `you adopted folder(s) — ${e.summary || ""}`.trim(),
    resolve: (e) => `you resolved a conflict — ${e.summary || ""}`.trim(),
    delete: (e) => {
      const n = (e.paths || []).length;
      return n > 1
        ? `you deleted ${name(e.path)} (${n} files)`
        : `you deleted ${name(e.path || (e.paths || [])[0])}`;
    },
    duplicate: (e) => `you duplicated ${name(e.path)} as a draft (${name(e.draft)})`,
    verify: (e) => `you verified ${name(e.path)} — ${e.ok ? "ran clean" : "a cell failed"}`,
    rollback: (e) => `you reverted ${name(e.path)} to the last synced version`,
    undo: (e) => `you undid a revert of ${name(e.path)}`,
    trash_restore: (e) => `you restored ${name(e.path)} from the trash`,
    ai_apply: (e) => `the copilot applied a change to ${name(e.path)} (you approved it)`,
    ai_rollback: (e) => `you rolled back a copilot change to ${name(e.path)}`,
  };

  function sentence(entry) {
    const build = OPS[entry.op];
    let text = build ? build(entry) : `${entry.op}${entry.path ? " " + name(entry.path) : ""}`;
    const banked = (entry.trashed || []).length;
    if (banked) text += ` — ${banked} pre-image(s) saved to the trash`;
    return text;
  }

  // "just now" / "N min ago" / "today 16:42" / "yesterday 16:42" / "12 Jun 16:42".
  // Calendar-aware past a day so "yesterday 16:42" reads like a human wrote it.
  function relTime(tsIso, nowMs) {
    const ts = Date.parse(tsIso);
    if (Number.isNaN(ts)) return "";
    const mins = Math.floor((nowMs - ts) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const d = new Date(ts);
    const now = new Date(nowMs);
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    const dayOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((dayOf(now) - dayOf(d)) / 86400000);
    if (days === 0) return `today ${hm}`;
    if (days === 1) return `yesterday ${hm}`;
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${d.getDate()} ${months[d.getMonth()]} ${hm}`;
  }

  return { sentence, relTime };
})();

if (typeof window !== "undefined") window.ActivityFmt = ActivityFmt;
if (typeof module !== "undefined" && module.exports) module.exports = ActivityFmt;
