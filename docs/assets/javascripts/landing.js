/* mooring landing page: deterministic starfield, copy-to-clipboard, and the
   boat voyage (sail in → drop anchor → moor → raise → sail on, looping).
   Adapted from moonlit-landing.js (OpenAfterHours) + the night-sea prototype. */
(function () {
  function rand(i, salt, seed) {
    var x = Math.sin((i + 1) * 12.9898 + salt * 78.233 + seed) * 43758.5453;
    return x - Math.floor(x);
  }

  function renderStars(host) {
    if (host.childElementCount > 0) return; // idempotent
    var count = parseInt(host.getAttribute("data-count") || "130", 10);
    var seed = parseInt(host.getAttribute("data-seed") || "2", 10);
    var frag = document.createDocumentFragment();
    for (var i = 0; i < count; i++) {
      var top = rand(i, 1, seed) * 100;
      var left = rand(i, 2, seed) * 100;
      var size = rand(i, 3, seed);
      var amber = rand(i, 4, seed) > 0.86;
      var lg = size > 0.85;
      var delay = rand(i, 5, seed) * 4;
      var duration = 2.4 + rand(i, 6, seed) * 2.2;
      var el = document.createElement("i");
      var cls = [];
      if (lg) cls.push("lg");
      if (amber) cls.push("amber");
      if (cls.length) el.className = cls.join(" ");
      el.style.top = top + "%";
      el.style.left = left + "%";
      el.style.setProperty("--mr-tw-delay", delay.toFixed(2) + "s");
      el.style.setProperty("--mr-tw-duration", duration.toFixed(2) + "s");
      frag.appendChild(el);
    }
    host.appendChild(frag);
  }

  function wireScrollCue(root) {
    var cue = root.querySelector("[data-mr-scroll-cue]");
    if (!cue || cue.dataset.mrWired) return;
    cue.dataset.mrWired = "1";
    cue.addEventListener("click", function (e) {
      var target = document.getElementById(cue.getAttribute("href").slice(1));
      if (!target) return; // fall back to the plain anchor jump
      e.preventDefault();
      var mq = window.matchMedia;
      var reduce = mq && mq("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    });
  }

  function wireCopyButtons(root) {
    var buttons = root.querySelectorAll("[data-mr-install]");
    buttons.forEach(function (btn) {
      var copy = btn.querySelector(".mr-copy");
      var cmdEl = btn.querySelector(".mr-cmd");
      var status = btn.querySelector("[data-mr-copy-status]");
      if (!copy || !cmdEl || copy.dataset.mrWired) return;
      copy.dataset.mrWired = "1";
      copy.addEventListener("click", function () {
        var text = cmdEl.textContent || "";
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text);
          }
        } catch (e) {}
        var prev = copy.textContent;
        copy.textContent = "COPIED";
        if (status) status.textContent = "Copied"; // announced via aria-live
        setTimeout(function () {
          copy.textContent = prev;
          if (status) status.textContent = "";
        }, 1400);
      });
    });
  }

  // The boat voyage. A generation counter cancels any in-flight desktop loop
  // when the page re-renders (Material instant navigation), so a detached boat
  // from a previous page stops animating. A per-element flag stops a second
  // init() on the SAME DOM (DOMContentLoaded + document$) from re-running.
  var voyageGen = 0;

  function bootVoyage(root) {
    var boat = root.querySelector("[data-mr-boat]");
    if (!boat) { voyageGen += 1; return; } // left the homepage — stop stragglers
    if (boat.dataset.mrVoyaged) return; // already booted on this DOM
    boat.dataset.mrVoyaged = "1";
    voyageGen += 1;
    var gen = voyageGen;

    var anchor = root.querySelector("[data-mr-anchor]");
    var rope = root.querySelector("[data-mr-rope]");
    if (!anchor || !rope) return;
    var text = root.querySelector("[data-mr-text]");

    var DROP = 152;
    function down() {
      rope.style.height = DROP + "px";
      anchor.style.transform = "translateX(-50%) translateY(" + DROP + "px)";
    }
    function up() {
      rope.style.height = "4px";
      anchor.style.transform = "translateX(-50%) translateY(6px)";
    }
    function showText() {
      if (text) { text.style.transform = "translateX(0)"; text.style.opacity = "1"; }
    }
    function sleep(ms) {
      return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }
    var alive = function () { return gen === voyageGen; };

    var mq = window.matchMedia;
    var reduce = mq && mq("(prefers-reduced-motion: reduce)").matches;
    var narrow = mq && mq("(max-width: 640px)").matches;

    // Reduced motion: a static moored boat (anchor down), text in place. The
    // CSS bob/shimmer are themselves disabled under reduced motion.
    if (reduce) {
      boat.style.transition = "none";
      boat.style.left = "50%";
      rope.style.transition = "none";
      anchor.style.transition = "none";
      down();
      showText();
      return;
    }

    // Mobile: the boat is already moored (anchor down) when the page lands and
    // the hero copy is held off to the left; weigh anchor, then sail off to the
    // right, towing the copy into place behind it — then keep looping the
    // voyage (sail in → moor → sail on), with the copy now left in place.
    if (narrow) {
      boat.style.transition = "none";
      boat.style.left = "50%";
      rope.style.transition = "none";
      anchor.style.transition = "none";
      down();
      if (text) {
        text.style.transition = "none";
        text.style.transform = "translateX(-118%)";
        text.style.opacity = "0";
      }
      void boat.offsetWidth; // commit the moored/hidden state before animating
      // Failsafe: never leave the hero copy hidden, whatever happens below.
      setTimeout(showText, 6000);
      (async function voyage() {
        try {
          // Intro (the bit that only plays once): start moored, weigh anchor,
          // then sail off to the right, towing the hero copy into place.
          await sleep(850); // landed — hold a beat, moored and bobbing
          if (!alive()) return showText();
          // weigh anchor
          rope.style.transition = "height 1700ms cubic-bezier(0.3,0,0.3,1)";
          anchor.style.transition = "transform 1700ms cubic-bezier(0.3,0,0.3,1)";
          up();
          await sleep(2050);
          if (!alive()) return showText();
          // sail off to the right, towing the hero copy into place
          boat.style.transition = "left 3900ms cubic-bezier(0.42,0,0.6,1)";
          boat.style.left = "124%";
          if (text) {
            text.style.transition =
              "transform 2700ms cubic-bezier(0.22,1,0.36,1), opacity 1500ms ease-out";
            showText();
          }
          await sleep(4250); // let the boat clear the right edge
          if (!alive()) return showText();

          // Loop: sail back in → drop anchor → moor → weigh anchor → sail on.
          // The hero copy is already in place, so it stays put from here on.
          while (alive()) {
            // Reset off-screen left, anchor stowed.
            boat.style.transition = "none";
            boat.style.left = "-24%";
            rope.style.transition = "none";
            anchor.style.transition = "none";
            up();
            void boat.offsetWidth; // flush so the next transition takes effect
            await sleep(80);
            if (!alive()) return;

            // 1. Sail in to the mooring spot.
            boat.style.transition = "left 6000ms cubic-bezier(0.37,0.02,0.4,1)";
            boat.style.left = "50%";
            await sleep(6300);
            if (!alive()) return;

            // 2. Drop anchor.
            rope.style.transition = "height 2000ms cubic-bezier(0.5,0,0.7,1)";
            anchor.style.transition = "transform 2000ms cubic-bezier(0.5,0,0.7,1)";
            down();
            await sleep(2300);
            if (!alive()) return;

            // 3. Moored — hold.
            await sleep(3500);
            if (!alive()) return;

            // 4. Weigh anchor.
            rope.style.transition = "height 1800ms cubic-bezier(0.3,0,0.3,1)";
            anchor.style.transition = "transform 1800ms cubic-bezier(0.3,0,0.3,1)";
            up();
            await sleep(2150);
            if (!alive()) return;

            // 5. Sail on, off the right edge, then loop.
            boat.style.transition = "left 5500ms cubic-bezier(0.5,0,0.6,1)";
            boat.style.left = "124%";
            await sleep(5800);
          }
        } catch (e) {
          showText();
        }
      })();
      return;
    }

    // Desktop: sail in → drop anchor → moor → weigh anchor → sail on, looping.
    (async function voyage() {
      while (alive()) {
        // Reset off-screen left, anchor stowed.
        boat.style.transition = "none";
        boat.style.left = "-14%";
        rope.style.transition = "none";
        anchor.style.transition = "none";
        up();
        void boat.offsetWidth; // flush so the next transition takes effect
        await sleep(80);
        if (!alive()) return;

        // 1. Sail in to the mooring spot.
        boat.style.transition = "left 7000ms cubic-bezier(0.37,0.02,0.4,1)";
        boat.style.left = "52%";
        await sleep(7350);
        if (!alive()) return;

        // 2. Drop anchor.
        rope.style.transition = "height 2300ms cubic-bezier(0.5,0,0.7,1)";
        anchor.style.transition = "transform 2300ms cubic-bezier(0.5,0,0.7,1)";
        down();
        await sleep(2600);
        if (!alive()) return;

        // 3. Moored — hold.
        await sleep(4500);
        if (!alive()) return;

        // 4. Weigh anchor.
        rope.style.transition = "height 2000ms cubic-bezier(0.3,0,0.3,1)";
        anchor.style.transition = "transform 2000ms cubic-bezier(0.3,0,0.3,1)";
        up();
        await sleep(2450);
        if (!alive()) return;

        // 5. Sail on, off the right edge, then loop.
        boat.style.transition = "left 6500ms cubic-bezier(0.5,0,0.6,1)";
        boat.style.left = "118%";
        await sleep(6900);
      }
    })();
  }

  function init() {
    document.querySelectorAll(".mr-stars[data-count]").forEach(renderStars);
    wireCopyButtons(document);
    wireScrollCue(document);
    bootVoyage(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Re-run after zensical/Material instant navigation, if available.
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(init);
  }
})();
