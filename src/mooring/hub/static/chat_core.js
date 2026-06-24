"use strict";

// Pure, DOM-free helpers for the copilot REPL. Kept apart from chat.js so they
// can be unit-tested under Node (see tests/js/chat_core.test.js) with no DOM.
// In the browser this file is loaded BEFORE chat.js and exposes `ChatCore`
// globally; under Node it is require()d. Nothing here touches `document`, the
// network, or storage — the value-blind/PII posture lives in chat.js + the hub.

var ChatCore = (function () {
  // -- slash commands -------------------------------------------------------
  // Registry: a name + one-line help. The behaviour lives in chat.js; these are
  // pure metadata + a parser, so each command maps onto an EXISTING capability
  // (no new endpoint, no new wire traffic).
  const COMMANDS = [
    { name: "help", help: "show commands and key bindings" },
    { name: "clear", help: "clear the transcript (keeps the session)" },
    { name: "model", help: "switch model — /model [name]" },
    { name: "apply", help: "apply the latest proposal" },
    { name: "diff", help: "jump to the latest proposal" },
    { name: "undo", help: "undo the last applied change" },
    { name: "retry", help: "resend your last message" },
  ];

  // Classify a composer line. A line beginning with a single "/" is a command;
  // "//" escapes to a literal message that itself starts with "/". Returns
  // {cmd, arg} for a command, or null for ordinary prose.
  function parseSlash(input) {
    if (typeof input !== "string") return null;
    if (!input.startsWith("/")) return null;
    if (input.startsWith("//")) return null; // escaped -> literal message
    const body = input.slice(1).trim();
    if (!body) return { cmd: "", arg: "" };
    const sp = body.indexOf(" ");
    if (sp === -1) return { cmd: body.toLowerCase(), arg: "" };
    return { cmd: body.slice(0, sp).toLowerCase(), arg: body.slice(sp + 1).trim() };
  }

  // Strip ONE leading slash from a "//…" escaped literal, so the message sent is
  // what the user typed minus the escape.
  function unescapeSlash(input) {
    return typeof input === "string" && input.startsWith("//") ? input.slice(1) : input;
  }

  // Commands whose name starts with `prefix` (no leading slash), for the menu.
  function filterCommands(prefix) {
    const p = String(prefix || "").toLowerCase();
    return COMMANDS.filter((c) => c.name.startsWith(p));
  }

  // True while the input is still being typed AS a slash command (a leading "/"
  // and no space yet) — i.e. show the command menu.
  function isSlashTyping(input) {
    return (
      typeof input === "string" &&
      input.startsWith("/") &&
      !input.startsWith("//") &&
      input.indexOf(" ") === -1
    );
  }

  // -- input history (in-memory ONLY) --------------------------------------
  // Never persisted: a held-PII prompt's plaintext must not survive on disk, so
  // this ring lives and dies with the page session.
  function HistoryRing(max) {
    this.items = [];
    this.max = max || 100;
    this.cursor = -1; // -1 = not navigating (live buffer)
    this.draft = ""; // the unsent buffer stashed while navigating
  }
  HistoryRing.prototype.push = function (text) {
    const t = String(text || "").trim();
    this.cursor = -1;
    this.draft = "";
    if (!t) return;
    if (this.items.length && this.items[this.items.length - 1] === t) return; // dedup repeats
    this.items.push(t);
    if (this.items.length > this.max) this.items.shift();
  };
  // Move toward OLDER entries. `current` is the live buffer, stashed on first up.
  HistoryRing.prototype.prev = function (current) {
    if (!this.items.length) return null;
    if (this.cursor === -1) {
      this.draft = String(current || "");
      this.cursor = this.items.length;
    }
    if (this.cursor > 0) this.cursor -= 1;
    return this.items[this.cursor];
  };
  // Move toward NEWER entries; stepping past the newest restores the draft.
  HistoryRing.prototype.next = function () {
    if (this.cursor === -1) return null;
    this.cursor += 1;
    if (this.cursor >= this.items.length) {
      this.cursor = -1;
      return this.draft;
    }
    return this.items[this.cursor];
  };

  // -- @-mention (dataset) detection ---------------------------------------
  // Find an "@partial" token ending at `caret`. Returns {start, query} or null.
  // Value-free by construction: the token only ever references a dataset PATH —
  // the chat never carries a data value, and the inserted text goes through the
  // same outbound PII gate as any prose.
  function mentionMatch(text, caret) {
    if (typeof text !== "string") return null;
    const at = typeof caret === "number" ? caret : text.length;
    const upto = text.slice(0, at);
    const m = upto.match(/(?:^|\s)@([^\s@]*)$/);
    if (!m) return null;
    return { start: at - m[1].length - 1, query: m[1] };
  }

  function filterDatasets(datasets, query) {
    const q = String(query || "").toLowerCase();
    return (datasets || []).filter((d) => String(d).toLowerCase().includes(q)).slice(0, 8);
  }

  // Replace the @-token at [start, caret) with "@<path> " in `text`.
  function applyMention(text, start, caret, path) {
    return text.slice(0, start) + "@" + path + " " + text.slice(caret);
  }

  // -- additive proposal block ---------------------------------------------
  // An APPEND proposal adds a whole new cell, so the honest rendering is an
  // all-additions block, NOT a diff. Returns one entry per source line.
  function additiveBlockLines(code) {
    const src = String(code || "").replace(/\n+$/, "");
    return src.split("\n").map((line) => ({ gutter: "+", text: line }));
  }

  // -- line diff (for an edit / rewrite proposal) --------------------------
  // An edit/rewrite REPLACES existing source, so the honest rendering is a real
  // old→new diff. Pure LCS line diff: returns {gutter, text} entries with gutter
  // " " (context), "-" (removed) or "+" (added). An empty side yields all
  // additions/removals (so an append-shaped op still reads correctly).
  function _toLines(s) {
    const t = String(s || "").replace(/\n+$/, "");
    return t === "" ? [] : t.split("\n");
  }
  // Above this LCS table area, skip the O(n*m) minimal diff (a huge whole-notebook
  // rewrite would build a multi-million-cell table on the UI thread) and fall back to
  // a coarse "all removed, then all added" block — still readable, never janky.
  const DIFF_MAX_AREA = 250000;
  function diffLines(before, after) {
    const a = _toLines(before);
    const b = _toLines(after);
    const n = a.length;
    const m = b.length;
    if (n * m > DIFF_MAX_AREA) {
      return a
        .map((t) => ({ gutter: "-", text: t }))
        .concat(b.map((t) => ({ gutter: "+", text: t })));
    }
    // LCS length table (suffixes), then walk it to emit a minimal diff.
    const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const out = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        out.push({ gutter: " ", text: a[i] });
        i++;
        j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        out.push({ gutter: "-", text: a[i] });
        i++;
      } else {
        out.push({ gutter: "+", text: b[j] });
        j++;
      }
    }
    while (i < n) out.push({ gutter: "-", text: a[i++] });
    while (j < m) out.push({ gutter: "+", text: b[j++] });
    return out;
  }

  // -- outbound-PII guard badge --------------------------------------------
  // Map the guard status (from /api/ai/chat/open) into a topbar badge so the
  // analyst sees BEFORE sending whether their prompt is scanned for PII — not
  // only after a finding comes back. Pure: returns {text, cls, title} (cls is
  // "on"/"off"; chat.js paints it), or null when no status was supplied.
  function piiBadge(guard) {
    if (!guard) return null;
    const scanned = "cards, IBANs, NHS numbers, emails and UK NINOs";
    if (!guard.enabled) {
      return {
        text: "PII-off",
        cls: "off",
        title:
          "Outbound PII pre-flight scan is OFF — your prompts are NOT scanned for " +
          scanned +
          " before being sent. (The schema-only guarantee still holds.) Turn it on " +
          "with ai.pii.enabled; run `mooring ai pii doctor` to check.",
      };
    }
    // "partial": the guard runs the structured scan, but a configured name pass
    // can't (its model/extra isn't available) — so the badge must NOT read as full
    // protection. This is the state that used to surface, contradictorily, as a
    // green badge plus a "sent unchecked" error after sending.
    const partial = !!guard.names && !guard.names_active;
    let title = "Outbound PII guard is ON — each prompt is scanned for " + scanned + " before it leaves";
    if (guard.names && guard.names_active) {
      title += ", plus person/organisation names (" + (guard.backend || "ner") + ")";
    } else if (partial) {
      title +=
        ". Name detection is configured but its model isn't available, so NAMES are NOT " +
        "scanned (structured PII still is) — run `mooring ai pii doctor`";
    }
    title += guard.block
      ? ". A hit holds the message for your confirmation."
      : ". A hit warns you, but the message is still sent.";
    return partial
      ? { text: "PII-partial", cls: "partial", title }
      : { text: "PII-active", cls: "on", title };
  }

  // The analyst-facing message for a guard_prompt scan_error code (see
  // mooring.ai.pii.guard_prompt): only a STRUCTURED-scan failure means the prompt
  // went truly unchecked; a NAMES-only failure still scanned structured PII, so it
  // must not claim "unchecked".
  function scanErrorMessage(code) {
    if (code === "names") {
      return (
        "Name detection couldn't run — your message was scanned for structured PII " +
        "(cards, IBANs, NHS numbers, emails, NINOs) but not names."
      );
    }
    return "PII pre-flight scan could not run — your message was sent unchecked.";
  }

  // -- batch jobs -----------------------------------------------------------
  // The batch composer is a list of per-notebook cards, each with its OWN free-form
  // brief (multi-line, as detailed as the analyst likes — bullet points, columns,
  // the charts they want), an optional name, and an optional dataset PATH. A textarea
  // per job is what lets a brief be detailed: there is no line/blank-line/`---`
  // delimiter to collide with the prose. cleanJobs takes the raw rows read off the
  // form and returns the jobs to submit: trim each field, KEEP internal newlines, and
  // drop any row with no brief. It deliberately does NOT derive a name — the server
  // names an unnamed job from its brief. Value-free by construction: a brief is an
  // instruction and a dataset is a path, and the brief still passes the outbound PII
  // gate (the non-interactive batch policy) before it reaches the model.
  function cleanJobs(rows) {
    const out = [];
    for (const r of rows || []) {
      const brief = String((r && r.brief) || "").trim();
      if (!brief) continue;
      out.push({
        name: String((r && r.name) || "").trim(),
        brief: brief,
        dataset: String((r && r.dataset) || "").trim(),
      });
    }
    return out;
  }

  // -- conservative Python highlight, XSS-safe by contract -----------------
  // MUST be called with text that is ALREADY HTML-escaped. It runs in a SINGLE
  // pass and only wraps <span>s around whole source tokens (comment / string /
  // word); it never emits a source character un-escaped and never re-scans the
  // markup it inserts, so it cannot reopen injection on model output. If you are
  // unsure, call it with escapeHtml(text) and it stays safe.
  const PY_KW = new Set(
    (
      "False None True and as assert async await break class continue def del " +
      "elif else except finally for from global if import in is lambda nonlocal " +
      "not or pass raise return try while with yield match case"
    ).split(" ")
  );
  // One token per match: a comment to end-of-line, a single-line string, or an
  // identifier word. Anything else is left verbatim. Quotes are matched LITERAL
  // because chat.js's escapeHtml only escapes & < > (the code is rendered as
  // element text, not an attribute), so " and ' survive un-escaped — and being
  // harmless in a text context, highlighting them opens no injection.
  const TOKEN_RE = /(#[^\n]*)|("[^"\n]*"|'[^'\n]*')|([A-Za-z_][A-Za-z0-9_]*)/g;
  function highlightCode(escaped) {
    if (typeof escaped !== "string") return "";
    return escaped.replace(TOKEN_RE, function (m, com, str, word) {
      if (com) return '<span class="tok-com">' + com + "</span>";
      if (str) return '<span class="tok-str">' + str + "</span>";
      if (word) return PY_KW.has(word) ? '<span class="tok-kw">' + word + "</span>" : word;
      return m;
    });
  }

  return {
    COMMANDS,
    parseSlash,
    unescapeSlash,
    filterCommands,
    isSlashTyping,
    HistoryRing,
    mentionMatch,
    filterDatasets,
    applyMention,
    additiveBlockLines,
    diffLines,
    piiBadge,
    scanErrorMessage,
    highlightCode,
    cleanJobs,
    PY_KW,
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = ChatCore;
