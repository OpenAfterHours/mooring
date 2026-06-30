/* Night/day-sea backdrop for the hub + settings pages: a deterministic starfield
   (shown only in dark themes via CSS) and a one-time boat voyage — the boat sails
   in from the left and moors on the right, then stops (no loop). Without this file,
   or under prefers-reduced-motion, the scene is a static moored boat (the CSS
   resting state). Adapted from docs/assets/javascripts/landing.js — keep the boat
   visually in sync with the docs scene. No external dependencies; pure DOM. */
(function () {
  // Deterministic [0,1) so the starfield is identical every load (no layout jitter).
  function rand(i, salt, seed) {
    var x = Math.sin((i + 1) * 12.9898 + salt * 78.233 + seed) * 43758.5453;
    return x - Math.floor(x);
  }

  function renderStars(host) {
    if (host.childElementCount > 0) return; // idempotent
    var count = parseInt(host.getAttribute("data-count") || "120", 10);
    var seed = parseInt(host.getAttribute("data-seed") || "7", 10);
    var frag = document.createDocumentFragment();
    for (var i = 0; i < count; i++) {
      var amber = rand(i, 4, seed) > 0.86;
      var lg = rand(i, 3, seed) > 0.85;
      var el = document.createElement("i");
      var cls = [];
      if (lg) cls.push("lg");
      if (amber) cls.push("amber");
      if (cls.length) el.className = cls.join(" ");
      el.style.top = (rand(i, 1, seed) * 100).toFixed(2) + "%";
      el.style.left = (rand(i, 2, seed) * 100).toFixed(2) + "%";
      el.style.setProperty("--hub-tw-delay", (rand(i, 5, seed) * 4).toFixed(2) + "s");
      el.style.setProperty("--hub-tw-duration", (2.4 + rand(i, 6, seed) * 2.2).toFixed(2) + "s");
      frag.appendChild(el);
    }
    host.appendChild(frag);
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function setup() {
    var scene = document.querySelector(".hub-scene");
    if (!scene) return;

    var stars = scene.querySelector(".hub-stars");
    if (stars) renderStars(stars);

    var boat = scene.querySelector("[data-hub-boat]");
    if (!boat || boat.dataset.hubVoyaged) return; // once per DOM
    boat.dataset.hubVoyaged = "1";

    // Reduced motion: leave the CSS resting state (moored, anchor down) untouched.
    var mq = window.matchMedia;
    if (mq && mq("(prefers-reduced-motion: reduce)").matches) return;

    var rope = scene.querySelector("[data-hub-rope]");
    var anchor = scene.querySelector("[data-hub-anchor]");

    // Stow the anchor and park the boat off-screen left BEFORE first paint — this
    // script is the last element in <body>, so it runs during parse, with the
    // scene already in the DOM. Avoids a flash of the moored boat before sailing.
    boat.style.transition = "none";
    boat.style.left = "-16%";
    if (rope) { rope.style.transition = "none"; rope.style.height = "4px"; }
    if (anchor) { anchor.style.transition = "none"; anchor.style.transform = "translateX(-50%) translateY(6px)"; }
    void boat.offsetWidth; // commit the stowed/off-screen state

    (async function voyage() {
      try {
        await sleep(450); // hold off-screen a beat
        // Sail in to the moored spot (the CSS --boat-moor resting position).
        boat.style.transition = "left 6800ms cubic-bezier(0.37, 0.02, 0.4, 1)";
        boat.style.left = "";
        await sleep(7050);
        // Drop the anchor: release rope + anchor back to the CSS resting (dropped) state.
        if (rope) { rope.style.transition = "height 2200ms cubic-bezier(0.5, 0, 0.7, 1)"; rope.style.height = ""; }
        if (anchor) { anchor.style.transition = "transform 2200ms cubic-bezier(0.5, 0, 0.7, 1)"; anchor.style.transform = ""; }
        // Moored. The voyage does not loop.
      } catch (e) {
        // On any failure, settle straight into the moored resting state.
        boat.style.left = "";
        if (rope) rope.style.height = "";
        if (anchor) anchor.style.transform = "";
      }
    })();
  }

  // Runs now (the scene is parsed before this script); the listener is a fallback
  // if the script is ever moved/deferred. Both paths are idempotent.
  setup();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  }
})();
