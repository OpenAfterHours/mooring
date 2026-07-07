"use strict";

// The Workbench composes two iframes on the hub origin: the REAL marimo editor
// (cross-origin, so mooring can't script into it — the value-blindness wall) and
// the existing /ai/chat copilot page (same-origin). A draggable splitter resizes
// the copilot panel; a toolbar at the bottom of the panel flips the notebook
// between app view (results only) and the code editor. Nothing here reaches into
// the notebook frame: the notebook opens via POST /api/open and the view is
// controlled purely by setting the iframe `src` (marimo reads ?view-as=present).
(function () {
  const qs = new URLSearchParams(location.search);
  const NOTEBOOK = qs.get("notebook") || "";
  const EXPLAIN = qs.get("explain") === "1";
  const REVIEW = qs.get("review") === "1";

  const LS_WIDTH = "mooring.workbench.chatWidth";
  const MIN_CHAT = 320; // never let the copilot panel drag below this…
  const MIN_NB = 420; // …nor the notebook pane.

  const $ = (id) => document.getElementById(id);
  const workbench = $("workbench");
  const nbFrame = $("nb-frame");
  const nbStatus = $("nb-status");
  const nbWarning = $("nb-warning");
  const chatFrame = $("chat-frame");
  const viewToggle = $("view-toggle");
  const collapseToggle = $("collapse-toggle");
  const reopenAI = $("reopen-ai");
  const splitter = $("splitter");

  let baseUrl = null; // the marimo editor URL WITHOUT the view lever (from /api/open)
  let appView = true; // AI mode defaults to app/present view: watch results, not code

  const nbName = NOTEBOOK.split("/").pop() || "notebook";
  document.title = `mooring · ${nbName}`;

  // -- copilot pane: the existing chat page, bound to this notebook ------------
  let chatSrc = `/ai/chat?notebook=${encodeURIComponent(NOTEBOOK)}`;
  if (EXPLAIN) chatSrc += "&explain=1";
  if (REVIEW) chatSrc += "&review=1";
  chatFrame.src = chatSrc;

  // -- notebook pane ----------------------------------------------------------
  // marimo's app/present view (code hidden, live outputs) is selected by the
  // ?view-as=present query param, read by marimo's frontend on load. baseUrl
  // already carries ?file=…&access_token=…, so append with &.
  function notebookSrc() {
    if (!baseUrl) return "about:blank";
    return appView ? baseUrl + "&view-as=present" : baseUrl;
  }
  function loadNotebook() {
    nbFrame.src = notebookSrc();
  }
  function renderViewToggle() {
    // In app view the action is to REVEAL code; in code view, to hide it again.
    viewToggle.textContent = appView ? "Show code" : "Show app view";
  }

  function showStatus(msg, kind) {
    nbStatus.textContent = msg;
    nbStatus.className = "nb-status" + (kind ? " " + kind : "");
  }
  function showWarning(msg) {
    // A non-fatal note from /api/open — e.g. the notebook shadows an importable
    // module (the polars.py footgun) or a declared dep is missing. The shadow guard
    // exists to pre-empt an inscrutable marimo traceback, so keep the warning VISIBLE
    // until the user dismisses it (the plain-Open path keeps it in the log panel;
    // don't quietly auto-hide it here). Built via DOM (not innerHTML) so the message
    // is inserted as text.
    nbWarning.textContent = "";
    const span = document.createElement("span");
    span.textContent = msg;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "nb-warning-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => nbWarning.classList.add("hidden"));
    nbWarning.append(span, close);
    nbWarning.classList.remove("hidden");
  }

  // Resolve the LIVE marimo URL each session (the port + token are regenerated on
  // every hub launch, so a baked/stale URL would 401) and point the frame at it.
  async function openNotebook() {
    showStatus("Starting the editor…");
    try {
      const resp = await fetch("/api/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NOTEBOOK }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.url) {
        showStatus(data.error || "Couldn't open this notebook.", "error");
        return;
      }
      baseUrl = data.url;
      renderViewToggle();
      // Hide the "starting" overlay once the editor actually paints (after
      // marimo's token-strip redirect resolves to the notebook, not a blank frame).
      nbFrame.addEventListener("load", () => nbStatus.classList.add("hidden"), { once: true });
      loadNotebook();
      if (data.warning) showWarning(data.warning);
    } catch {
      showStatus("Couldn't reach the hub to open this notebook.", "error");
    }
  }

  // -- app / code view toggle (a reload; we can't script the cross-origin frame) --
  viewToggle.addEventListener("click", () => {
    if (!baseUrl) return;
    appView = !appView;
    renderViewToggle();
    loadNotebook();
  });

  // -- collapse / reopen the copilot panel ------------------------------------
  function setCollapsed(collapsed) {
    workbench.classList.toggle("collapsed", collapsed);
    reopenAI.classList.toggle("hidden", !collapsed);
  }
  collapseToggle.addEventListener("click", () => setCollapsed(true));
  reopenAI.addEventListener("click", () => setCollapsed(false));

  // -- splitter --------------------------------------------------------------
  function currentWidth() {
    return parseInt(getComputedStyle(workbench).getPropertyValue("--chat-w"), 10);
  }
  function clampWidth(px) {
    const max = Math.max(MIN_CHAT, window.innerWidth - MIN_NB);
    return Math.min(Math.max(px, MIN_CHAT), max);
  }
  function setChatWidth(px, persist) {
    const w = clampWidth(px);
    workbench.style.setProperty("--chat-w", w + "px");
    if (persist) {
      try {
        localStorage.setItem(LS_WIDTH, String(w));
      } catch {}
    }
  }

  let dragging = false;
  function onMove(e) {
    if (!dragging) return;
    // The copilot is the RIGHT column: its width is the distance from the pointer
    // to the right edge of the viewport.
    setChatWidth(window.innerWidth - e.clientX, false);
  }
  function endDrag() {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("wb-dragging");
    const w = currentWidth();
    if (Number.isFinite(w)) setChatWidth(w, true); // persist the final width
  }
  splitter.addEventListener("pointerdown", (e) => {
    dragging = true;
    // Capture the pointer ON the splitter so pointermove/up/cancel keep arriving
    // even when the pointer leaves the window. Without this, a release OUTSIDE the
    // viewport delivers no pointerup, so `dragging`/body.wb-dragging stay on — and
    // that class sets pointer-events:none on BOTH iframes, freezing the whole
    // workbench until the user clicks the splitter again.
    try {
      splitter.setPointerCapture(e.pointerId);
    } catch {}
    // CRITICAL: an iframe swallows pointer events, so a drag that passes over one
    // would "stick". body.wb-dragging disables pointer-events on both frames.
    document.body.classList.add("wb-dragging");
    e.preventDefault();
  });
  // With pointer capture these fire on the splitter for the whole gesture, even
  // outside the window; pointercancel is the backstop if the capture is lost.
  splitter.addEventListener("pointermove", onMove);
  splitter.addEventListener("pointerup", endDrag);
  splitter.addEventListener("pointercancel", endDrag);
  // Keyboard resize (the splitter is focusable): arrows nudge the panel width.
  splitter.addEventListener("keydown", (e) => {
    const w = currentWidth() || 420;
    if (e.key === "ArrowLeft") setChatWidth(w + 24, true);
    else if (e.key === "ArrowRight") setChatWidth(w - 24, true);
    else return;
    e.preventDefault();
  });
  // Keep the panel within bounds when the window resizes.
  window.addEventListener("resize", () => {
    const w = currentWidth();
    if (Number.isFinite(w)) setChatWidth(w, false);
  });

  // -- boot -------------------------------------------------------------------
  // Clamp any pre-paint-restored width to the current viewport, then open.
  const restored = currentWidth();
  if (Number.isFinite(restored)) setChatWidth(restored, false);
  renderViewToggle();
  if (!NOTEBOOK) {
    showStatus("No notebook specified.", "error");
  } else {
    openNotebook();
  }
})();
