"use strict";

// The reviewer inbox: list teammates' open proposals, show a cell-aware diff of one,
// and Approve / Request-changes (posts a GitHub PR review). All diff text is set via
// textContent — notebook source is untrusted and must never inject markup into the hub.

const $ = (id) => document.getElementById(id);
let current = null; // the PR number of the open review, or null

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
  for (const r of items) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = `Review #${r.number}`;
    btn.addEventListener("click", () => openReview(r));
    const title = document.createElement("span");
    title.textContent = ` ${r.title || "(no title)"}`;
    const who = document.createElement("span");
    who.className = "muted";
    who.textContent = ` — ${r.author || "unknown"}${r.updated ? " · " + r.updated.slice(0, 10) : ""}`;
    li.append(btn, title, who);
    list.appendChild(li);
  }
}

async function openReview(r) {
  showError("");
  current = r.number;
  $("review-detail").classList.remove("hidden");
  $("detail-title").textContent = `#${r.number} ${r.title || ""}`;
  $("detail-gh").href = r.url || "#";
  $("detail-author").textContent = r.author ? `Proposed by ${r.author}` : "";
  $("review-note-text").value = "";
  const box = $("detail-files");
  box.textContent = "Loading the diff…";
  try {
    const data = await api("/api/reviews/detail", { number: r.number });
    renderFiles(data.files || []);
  } catch (e) {
    showError(e.message);
    box.textContent = "";
  }
  $("review-detail").scrollIntoView({ block: "nearest" });
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
    $("review-detail").classList.add("hidden");
    current = null;
    await loadList(); // the reviewed PR drops off the inbox
  } catch (e) {
    showError(e.message);
  } finally {
    for (const b of [$("btn-approve"), $("btn-request")]) b.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("reviews-refresh").addEventListener("click", loadList);
  $("btn-approve").addEventListener("click", () => submit("APPROVE"));
  $("btn-request").addEventListener("click", () => submit("REQUEST_CHANGES"));
  loadList();
});
