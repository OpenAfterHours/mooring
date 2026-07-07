"use strict";

// Shared appearance module for the hub and every sub-page (settings, activity,
// reviews, chat, batch). The zero-flash pre-paint stays INLINE in each <head>
// (it must run before first paint); this file carries the parts that were
// otherwise copy-pasted into five page scripts:
//   • the localStorage key + applyTheme writer (one source of truth), and
//   • a `storage` follower so changing the appearance in one mooring tab (the
//     hub's Appearance select, or the Settings form) re-themes every other open
//     mooring tab live — which the static sub-pages never used to do.
// The server (config.toml, surfaced via /api/state) stays the source of truth;
// callers use MooringTheme.applyTheme when THEY change the theme, and listen for
// the `mooring:theme` event to sync their own controls when another tab does.
(function () {
  const LS_THEME = "mooring.ui.theme"; // same origin, shared across every tab

  function applyTheme(theme) {
    if (!theme) return;
    document.documentElement.dataset.theme = theme;
    try {
      // Only rewrite on a real change so we don't fire redundant storage events.
      if (localStorage.getItem(LS_THEME) !== theme) localStorage.setItem(LS_THEME, theme);
    } catch {
      // localStorage may be unavailable (private mode / blocked); best-effort.
    }
  }

  // Another same-origin mooring tab changed the appearance — follow it live. The
  // originating tab already wrote localStorage, so just reflect it on <html> and
  // let this page sync any of its own controls via the `mooring:theme` event.
  window.addEventListener("storage", (event) => {
    if (event.key === LS_THEME && event.newValue) {
      document.documentElement.dataset.theme = event.newValue;
      window.dispatchEvent(new CustomEvent("mooring:theme", { detail: event.newValue }));
    }
  });

  window.MooringTheme = { LS_THEME, applyTheme };
})();
