"use strict";

// Pure, DOM-free staleness helpers for the hub: which rows warrant an "a teammate
// changed this" confirm at Open time, session-dismissal keying, the freshness
// banner's age text, and the focus-refresh throttle. Loaded BEFORE app.js (exposes
// `Freshness` as a bare global + on `window`, the files_tree.js idiom); under Node
// it is require()d by tests/js. Nothing here touches the DOM, network, or storage.

const Freshness = (function () {
  // Row states where the remote moved under the local copy — the only states the
  // Open-time dialog fires for. Everything else (synced, modified, local, …) opens
  // silently; a clean workspace never sees the dialog.
  //   "pull"     → pulling first is the happy path (remote changed).
  //   "deleted"  → the remote DELETED it; pulling would remove the local copy, so
  //                the dialog must not offer "Pull latest and open".
  //   "conflict" → pull skips conflicts; point at the row's resolve actions instead.
  const WARN_KINDS = {
    "remote changed": "pull",
    "deleted remotely": "deleted",
    "conflict": "conflict",
  };

  // The value a session dismissal is keyed to: the remote blob SHA when there is
  // one, else a state marker (a remote deletion carries no sha). "Open my copy
  // anyway" records this; the dialog re-arms only when the key CHANGES — i.e. the
  // remote moved again — so a user who chose to diverge isn't nagged per click.
  function dismissKey(file) {
    return file.remote_sha || "@" + file.state;
  }

  // "pull" | "deleted" | "conflict" | null for a row, honouring dismissals.
  // `dismissed` is a Map(path → dismissKey at the time of dismissal).
  function warnState(file, dismissed) {
    if (!file) return null;
    const kind = WARN_KINDS[file.state];
    if (!kind) return null;
    if (dismissed && dismissed.get(file.path) === dismissKey(file)) return null;
    return kind;
  }

  // Rows a Pull would touch — the freshness banner's "N teammate update(s) waiting".
  function pullCount(files) {
    let n = 0;
    for (const f of files || []) {
      if (f.state === "remote changed" || f.state === "new remote" || f.state === "deleted remotely") n++;
    }
    return n;
  }

  // Compact age for the banner: "just now" under a minute, then minutes/hours/days.
  function ageText(ms) {
    if (!(ms >= 0)) return "";
    const min = Math.floor(ms / 60000);
    if (min < 1) return "just now";
    if (min < 60) return `${min} min ago`;
    const hours = Math.floor(min / 60);
    if (hours < 24) return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
    const days = Math.floor(hours / 24);
    return days === 1 ? "1 day ago" : `${days} days ago`;
  }

  // Whether a tab-focus event should trigger a refresh: only when a successful
  // refresh has happened before (lastStateAt set) and it is older than the
  // throttle — so returning to the tab several times a minute costs nothing.
  function shouldAutoRefresh(lastStateAt, now, throttleMs) {
    return lastStateAt != null && now - lastStateAt >= throttleMs;
  }

  return { warnState, dismissKey, pullCount, ageText, shouldAutoRefresh };
})();

if (typeof window !== "undefined") window.Freshness = Freshness;
if (typeof module !== "undefined" && module.exports) module.exports = Freshness;
