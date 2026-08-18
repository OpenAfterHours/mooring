"use strict";

// The shared chart-room chrome for mooring's sub-pages (settings, activity,
// reviews): the rail's page list, and the in-page section list with its scroll spy.
// Loaded BEFORE each page script (exposes `SubPage` as a bare global + on `window`,
// the files_tree.js idiom).
//
// The page list is built from `data-page` on <body> rather than written into three
// HTML files, so adding a destination — or changing a glyph — happens once. It
// self-mounts on load and no-ops when the page has no rail, which is what keeps it
// safe to load anywhere.
//
// The hub builds its own rail (app.js renderRailNav) because its items carry live
// counts from /api/state; these pages ask the server for nothing but their own data,
// so a static list is the honest version. The markup matches the hub's exactly, so
// the two rails are the same object to CSS.

const SubPage = (function () {
  const PAGES = [
    { id: "notebooks", label: "notebooks", href: "/", glyph: "▤",
      title: "Your notebooks" },
    { id: "reviews", label: "reviews", href: "/reviews", glyph: "✎",
      title: "Review teammates' proposed changes" },
    { id: "activity", label: "activity", href: "/activity", glyph: "⏱",
      title: "Recent activity & trash (local to this machine)" },
    { id: "settings", label: "settings", href: "/settings", glyph: "⚙",
      title: "Settings & preferences" },
  ];

  // One rail row. The page you are on is a <span>, not a link to itself — and
  // carries aria-current, so it is announced as the current page rather than as a
  // dead link.
  function pageItem(page, active) {
    const el = document.createElement(active ? "span" : "a");
    el.className = "rail-item" + (active ? " active" : "");
    if (active) el.setAttribute("aria-current", "page");
    else el.href = page.href;
    el.title = page.title;
    const caret = document.createElement("span");
    caret.className = "rail-caret";
    caret.textContent = active ? "▶" : "";
    caret.setAttribute("aria-hidden", "true");
    const glyph = document.createElement("span");
    glyph.className = "rail-icon";
    glyph.textContent = page.glyph;
    glyph.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "rail-item-label";
    label.textContent = page.label;
    el.append(caret, glyph, label);
    return el;
  }

  function mountPages() {
    const nav = document.getElementById("rail-nav");
    if (!nav) return;
    const active = (document.body.dataset.page || "").trim();
    nav.textContent = "";
    for (const page of PAGES) nav.appendChild(pageItem(page, page.id === active));
  }

  // -- the in-page section list + its scroll spy -----------------------------

  function markSection(id) {
    const nav = document.getElementById("section-nav");
    if (!nav) return;
    for (const item of nav.querySelectorAll(".rail-item")) {
      const on = item.dataset.section === id;
      item.classList.toggle("active", on);
      item.querySelector(".rail-caret").textContent = on ? "▶" : "";
    }
  }

  const READING_LINE = 96; // px below the top of the scrolling pane
  let spyBound = false;

  // Scroll spy: the last section whose heading has passed the reading line wins —
  // the section you are reading, not the one that happens to overlap the viewport.
  // The bottom of the scroll is special-cased to the LAST section: the final group
  // is often shorter than the viewport, so it can never reach the line, and a rail
  // still naming the previous section while you read the last one is simply wrong.
  function spy() {
    const pane = document.querySelector(".centre-body");
    if (!pane) return;
    const all = Array.from(pane.querySelectorAll(".page-section"));
    if (!all.length) return;
    const atBottom = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 4;
    let current = all[0];
    if (atBottom) {
      current = all[all.length - 1];
    } else {
      const line = pane.getBoundingClientRect().top + READING_LINE;
      for (const el of all) {
        if (el.getBoundingClientRect().top <= line) current = el;
      }
    }
    markSection(current.id.replace(/^section-/, ""));
  }

  /**
   * Render the rail's section list for this page and keep it in step with the
   * scroll. `sections` is [{id, label}] in page order; each must match a
   * `<... class="page-section" id="section-<id>">` in the centre body.
   */
  function sections(list) {
    const nav = document.getElementById("section-nav");
    if (!nav) return;
    nav.textContent = "";
    if (!(list || []).length) return;
    const label = document.createElement("div");
    label.className = "rail-label";
    label.textContent = "SECTIONS";
    nav.appendChild(label);
    for (const s of list) {
      const item = document.createElement("a");
      item.className = "rail-item";
      item.href = `#section-${s.id}`;
      item.dataset.section = s.id;
      const caret = document.createElement("span");
      caret.className = "rail-caret";
      caret.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.className = "rail-item-label";
      text.textContent = s.label.toLowerCase();
      item.append(caret, text);
      item.addEventListener("click", (e) => {
        e.preventDefault();
        const target = document.getElementById(`section-${s.id}`);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      nav.appendChild(item);
    }
    const pane = document.querySelector(".centre-body");
    if (pane && !spyBound) {
      // Bound once: a page re-renders its sections on every save, and re-binding
      // each time would stack a listener per save for the life of the page.
      spyBound = true;
      let queued = false;
      const onScroll = () => {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => { queued = false; spy(); });
      };
      pane.addEventListener("scroll", onScroll, { passive: true });
      window.addEventListener("resize", onScroll);
    }
    spy();
  }

  // One mono caps section heading over its content — the same device the hub's
  // detail panel uses for SELECTED / STATE / RECEIPTS.
  function section(id, label) {
    const el = document.createElement("section");
    el.className = "page-section";
    el.id = `section-${id}`;
    const head = document.createElement("div");
    head.className = "panel-label";
    head.textContent = label.toUpperCase();
    el.appendChild(head);
    return el;
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mountPages);
    } else {
      mountPages();
    }
  }

  return { PAGES, sections, section, markSection, mountPages };
})();

if (typeof window !== "undefined") window.SubPage = SubPage;
if (typeof module !== "undefined" && module.exports) module.exports = SubPage;
