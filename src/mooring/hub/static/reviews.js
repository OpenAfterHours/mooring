"use strict";

// The reviewer inbox: list teammates' open proposals, show a cell-aware diff of one,
// and Approve / Request-changes (posts a GitHub PR review). All diff text is set via
// textContent — notebook source is untrusted and must never inject markup into the hub.
//
// The page is the hub's own shape — a list you pick from, then a detail that REPLACES
// it with a way back — so it uses the same centre-view idiom. The chart-room chrome
// (the rail's page list) comes from subpage.js.

const $ = (id) => document.getElementById(id);
let current = null; // the PR number of the open review, or null

// Show either the list or one review, never both. The header block is about the
// INBOX, so it stands down on a detail — the question it answers is not the one
// being asked there.
function showList() {
  current = null;
  $("reviews-card").classList.remove("hidden");
  $("review-detail").classList.add("hidden");
  $("centre-back").classList.add("hidden");
  $("centre-head").classList.remove("hidden");
}

function showDetail() {
  $("reviews-card").classList.add("hidden");
  $("review-detail").classList.remove("hidden");
  $("centre-back").classList.remove("hidden");
  $("centre-head").classList.add("hidden");
}

// One sentence about the inbox: how many proposals are waiting for YOU, which is the
// only thing this page exists to answer.
function renderHead(items) {
  const n = items.length;
  const word = Headline.count(n);
  $("headline").textContent = n === 0
    ? "Nothing is waiting for your review."
    : `${word.charAt(0).toUpperCase() + word.slice(1)} proposal${n === 1 ? "" : "s"} ` +
      `${n === 1 ? "is" : "are"} waiting for your review.`;
  const now = new Date();
  const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  $("meta-line").textContent = [
    "REVIEWS",
    `${n} WAITING`,
    `${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()}`,
  ].join("\u00a0 / \u00a0");
}

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status}).`);
  return data;
}

function showError(msg) {
  const el = $("error-banner");
  el.textContent = msg || "";
  el.classList.toggle("hidden", !msg);
}

async function loadList() {
  showError("");
  try {
    const data = await api("/api/reviews");
    renderList(data.reviews || []);
  } catch (e) {
    showError(e.message);
  }
}

function renderList(items) {
  const list = $("reviews-list");
  list.textContent = "";
  $("reviews-empty").classList.toggle("hidden", items.length > 0);
  renderHead(items);
  for (const r of items) {
    // The whole row opens the review — a button inside a row you can already click
    // is one target too many. It IS a <button>, so it is focusable and keyboard-
    // operable without any of its own wiring.
    const li = document.createElement("li");
    const row = document.createElement("button");
    row.className = "review-row";
    row.addEventListener("click", () => openReview(r));

    const main = document.createElement("span");
    main.className = "review-row-main";
    const title = document.createElement("span");
    title.className = "row-title";
    title.textContent = r.title || "(no title)";
    const sub = document.createElement("span");
    sub.className = "row-sub";
    sub.textContent = `#${r.number} · ${r.author || "unknown"}` +
      (r.updated ? ` · ${r.updated.slice(0, 10)}` : "");
    main.append(title, sub);

    const state = document.createElement("span");
    state.className = "review-row-state state-word state-review";
    state.textContent = "awaiting you";

    row.append(main, state);
    li.appendChild(row);
    list.appendChild(li);
  }
}

async function openReview(r) {
  showError("");
  current = r.number;
  showDetail();
  $("detail-title").textContent = `#${r.number} ${r.title || ""}`;
  $("detail-gh").href = r.url || "#";
  $("detail-author").textContent = r.author ? `Proposed by ${r.author}` : "";
  $("review-note-text").value = "";
  const box = $("detail-files");
  box.textContent = "Loading the diff…";
  try {
    const data = await api("/api/reviews/detail", { number: r.number });
    renderFiles(data.files || []);
    markCodeBand(data.code_band);
  } catch (e) {
    showError(e.message);
    box.textContent = "";
  }
}

function renderFiles(files) {
  const box = $("detail-files");
  box.textContent = "";
  if (!files.length) {
    box.textContent = "No file changes in this proposal.";
    return;
  }
  for (const f of files) {
    const result = f.diff || {};
    const wrap = document.createElement("div");
    wrap.className = "review-file";
    const head = document.createElement("div");
    head.className = "review-cell-label";
    const detail = DiffFmt.summary(result) || f.status || "";
    head.textContent = detail ? `${f.path} — ${detail}` : f.path;
    wrap.appendChild(head);
    renderCodeFindings(wrap, f.code); // above the diff — read it before the code
    if (result.kind === "cells") {
      for (const block of DiffFmt.buildBlocks(result.cells)) {
        const cell = document.createElement("div");
        cell.className = "review-cell";
        const label = document.createElement("div");
        label.className = `review-cell-label review-${block.status}`;
        label.textContent = block.label;
        cell.appendChild(label);
        if (block.diff) {
          const pre = document.createElement("pre");
          pre.className = "review-cell-diff";
          pre.textContent = block.diff;
          cell.appendChild(pre);
        }
        wrap.appendChild(cell);
      }
    } else if (result.line_diff) {
      const pre = document.createElement("pre");
      pre.className = "review-cell-diff";
      pre.textContent = result.line_diff;
      wrap.appendChild(pre);
    }
    box.appendChild(wrap);
  }
}

// -- the destructive-code scan ------------------------------------------------
// One file's findings, above its diff blocks. INFORMATIONAL: the reviewer is the one
// person in the loop who reads Python, so they see both bands and there is nothing to
// confirm or click past — this is context beside a diff they are already reading, not
// the analyst-facing hold the copilot's Apply puts up.
//
// Value-free like everything else on this page: a line number and a fixed label, never
// a matched substring and never source. Set with textContent, same as the diff.
function renderCodeFindings(wrap, code) {
  const rows = ChatCore.codeFindingRows(code);
  if (!rows.length) return;
  const box = document.createElement("div");
  box.className = "review-code";
  const lead = document.createElement("div");
  lead.className = "review-code-lead";
  lead.textContent = ChatCore.codeFindingLead(code);
  const list = document.createElement("ul");
  list.className = "review-code-list";
  for (const row of rows) {
    const li = document.createElement("li");
    li.className = "rf-row rf-" + row.band;
    const tag = document.createElement("span");
    tag.className = "rf-band";
    tag.textContent = ChatCore.codeFindingTag(row);
    const text = document.createElement("span");
    text.className = "rf-text";
    text.textContent = row.text;
    li.append(tag, text);
    list.appendChild(li);
  }
  box.append(lead, list);
  wrap.appendChild(box);
}

// Badge the review header when ANY changed file carries an irreversible finding, so a
// reviewer knows before they start scrolling. Only "floor" earns it — a badge on every
// proposal that writes a CSV would stop meaning anything.
function markCodeBand(band) {
  const head = $("detail-title");
  const old = head.querySelector(".badge.destructive");
  if (old) old.remove();
  if (band !== "floor") return;
  const badge = document.createElement("span");
  badge.className = "badge destructive";
  badge.textContent = "destructive code";
  badge.title = "At least one changed file contains code Undo can't take back.";
  head.append(" ", badge);
}

async function submit(event) {
  if (!current) return;
  showError("");
  const note = $("review-note-text").value.trim();
  if (event === "REQUEST_CHANGES" && !note) {
    showError("Add a note describing the change you want.");
    $("review-note-text").focus();
    return;
  }
  for (const b of [$("btn-approve"), $("btn-request")]) b.disabled = true;
  try {
    await api("/api/reviews/submit", { number: current, event, body: note });
    showList();
    await loadList(); // the reviewed PR drops off the inbox
  } catch (e) {
    showError(e.message);
  } finally {
    for (const b of [$("btn-approve"), $("btn-request")]) b.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("reviews-refresh").addEventListener("click", loadList);
  $("centre-back").addEventListener("click", showList);
  // Esc backs out of a review, like the hub's detail drawer.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && current !== null) showList();
  });
  $("btn-approve").addEventListener("click", () => submit("APPROVE"));
  $("btn-request").addEventListener("click", () => submit("REQUEST_CHANGES"));
  loadList();
});
