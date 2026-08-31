"use strict";

// Pure, DOM-free helpers for the copilot REPL (and the hub's Copilot sign-in).
// Kept apart from chat.js so they can be unit-tested under Node (see
// tests/js/chat_core.test.js) with no DOM. In the browser this file is loaded
// BEFORE chat.js (and before app.js on the hub) and exposes `ChatCore` both as a bare
// global and as `window.ChatCore` (see the foot of the file); under Node it is
// require()d. Nothing here touches `document`, the network, or
// storage — the value-blind/PII posture lives in chat.js + the hub.

const ChatCore = (function () {
  // -- slash commands -------------------------------------------------------
  // Registry: a name + one-line help. The behaviour lives in chat.js; these are
  // pure metadata + a parser, so each command maps onto an EXISTING capability
  // (no new endpoint, no new wire traffic).
  const COMMANDS = [
    { name: "help", help: "show commands and key bindings" },
    { name: "explain", help: "walk through what this notebook does" },
    { name: "review", help: "review the notebook's logic for correctness risks" },
    // Worded so each line is true in BOTH modes. With `[ai] auto_apply` on the copilot
    // writes the cell itself; with it off it proposes one and waits for Apply. "ask for"
    // covers both; "propose" only described the second, and silently stopped being true
    // for every user the day auto-apply became the default.
    { name: "checks", help: "ask for tie-out / data-quality checks" },
    { name: "sql", help: "ask for a marimo SQL (DuckDB) cell for this notebook" },
    {
      name: "investigate",
      help: "research independent sub-questions in parallel — /investigate <topic>",
    },
    { name: "clear", help: "clear the transcript (keeps the session)" },
    { name: "model", help: "switch model — /model [name]" },
    // These three act on a change that is WAITING — a held one, or (in propose mode)
    // every one. When the copilot applies its own changes there is usually nothing
    // waiting, and "the latest proposal" described a card that was no longer there.
    { name: "apply", help: "apply a change that is waiting for you" },
    { name: "diff", help: "jump to the change waiting for you" },
    { name: "undo", help: "put the notebook back one step (same as Revert)" },
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
      !input.includes(" ")
    );
  }

  // -- trusted routing picker ---------------------------------------------
  // Treat routing metadata as an allowlist, never as a hint that lets a saved
  // browser value invent a model. These helpers are deliberately DOM/storage-free
  // so the fail-safe selection rules stay pinned by the Node suite.
  function trustedModelOptions(routing) {
    if (!routing || routing.enabled !== true || !Array.isArray(routing.trusted_models)) return [];
    const seen = new Set();
    const options = [];
    for (const raw of routing.trusted_models) {
      if (!raw || typeof raw !== "object") continue;
      const id = typeof raw.id === "string" ? raw.id.trim() : "";
      if (!id || seen.has(id)) continue;
      const suppliedName = typeof raw.name === "string" ? raw.name.trim() : "";
      options.push({ id, name: suppliedName || id });
      seen.add(id);
    }
    return options;
  }

  function chooseTrustedModel(routing, saved) {
    const options = trustedModelOptions(routing);
    const allowed = new Set(options.map((m) => m.id));
    const candidates = [saved, routing?.default_trusted_model, options[0]?.id];
    for (const value of candidates) {
      if (typeof value === "string" && allowed.has(value.trim())) return value.trim();
    }
    return "";
  }

  // Which profile is answering, from either the routing metadata or one route
  // event — both carry the same `source` field. Three-way ON PURPOSE: claiming a
  // firm approved an endpoint the user configured themselves is the one direction
  // that does harm, so an absent/unrecognised source under-claims rather than
  // guessing. "managed" is never inferred, only read.
  function routingProfileKind(x) {
    const raw = typeof x?.source === "string" ? x.source.trim().toLowerCase() : "";
    if (raw === "local") return "local";
    if (raw === "managed") return "managed";
    return "unknown";
  }

  // The noun phrase for the customer-data model, matched to that kind.
  function trustedModelPhrase(kind, { capitalise = false } = {}) {
    const text =
      kind === "managed"
        ? "your firm's approved customer-data model"
        : kind === "local"
          ? "the customer-data model you configured"
          : "the customer-data model";
    return capitalise ? text.charAt(0).toUpperCase() + text.slice(1) : text;
  }

  function trustedRoutingAvailable(routing) {
    return routing?.enabled === true && !routing.error && trustedModelOptions(routing).length > 0;
  }

  function chooseRoutingPreference(routing, saved) {
    if (!trustedRoutingAvailable(routing)) return "auto";
    return saved === "trusted" ? "trusted" : "auto";
  }

  function routingNotice(route, switched) {
    if (!route || (route.zone !== "trusted" && route.zone !== "general")) return "";
    const modelValue = route.model || route.trusted_model || route.model_id;
    const model = typeof modelValue === "string" ? modelValue.trim() : "";
    const profile = typeof route.profile_label === "string" ? route.profile_label.trim() : "";
    const identity = [...new Set([profile, model].filter(Boolean))].join(" · ");
    const kind = routingProfileKind(route);
    if (route.zone === "trusted") {
      let text = switched
        ? `This conversation switched to ${trustedModelPhrase(kind)}`
        : `${trustedModelPhrase(kind, { capitalise: true })} is handling this conversation`;
      if (identity) text += ` (${identity})`;
      text += ".";
      if (switched && route.conversation_carried === true) {
        text += " The earlier conversation was carried forward.";
      }
      if (switched && route.conversation_carried === false) {
        text += " The earlier conversation could not be carried; make follow-up requests self-contained.";
      }
      return text;
    }
    if (switched) return "";
    const general = model ? ` (${model})` : "";
    const checker = kind === "local" ? "checker you configured" : "approved data checker";
    return (
      `The ${checker} found this context suitable for the selected general coding model` +
      general +
      `. Mooring will switch this conversation if later content needs ${trustedModelPhrase(kind)}.`
    );
  }

  function privacyChrome(routing) {
    if (routing?.enabled === true) {
      if (!trustedRoutingAvailable(routing)) {
        return {
          badge: "customer-data routing unavailable",
          badgeClass: "danger",
          title: "Customer-data routing is unavailable; messages cannot be sent",
          lead: "mooring copilot · routing unavailable.",
          body: " Customer-data routing is not ready, so this chat cannot send messages.",
          footer: "Reload, or check the customer-data profile in Settings.",
        };
      }
      const kind = routingProfileKind(routing);
      if (kind === "local") {
        return {
          badge: "self-configured routing",
          badgeClass: "warn",
          title:
            "A checker YOU configured routes customer information to an endpoint YOU " +
            "chose; nobody has approved it on your behalf",
          lead: "mooring copilot · self-configured routing.",
          body:
            " You set this route up yourself: no administrator has approved the endpoint " +
            "or the checker. Customer information you deliberately put in notebook code " +
            "or this chat may be sent to the model you selected. Mooring does not " +
            "automatically read raw dataset values or cell results, and recognized " +
            "credential patterns are blocked locally.",
          footer: "Type /help for commands.",
        };
      }
      const whose = kind === "managed" ? "your firm's approved model" : "the selected model";
      return {
        badge: kind === "managed" ? "approved routing" : "customer-data routing",
        badgeClass: "synced",
        title:
          `An approved checker routes customer information to ${whose}; ` +
          "raw dataset values and cell results are not automatically read",
        lead:
          kind === "managed"
            ? "mooring copilot · approved routing."
            : "mooring copilot · customer-data routing.",
        body:
          " An approved checker routes each turn. Customer information you deliberately put in " +
          "notebook code or this chat may be sent to the selected approved model. Mooring does " +
          "not automatically read raw dataset values or cell results, and recognized credential " +
          "patterns are blocked locally.",
        footer: "Type /help for commands.",
      };
    }
    return {
      badge: "schema-only",
      badgeClass: "synced",
      title: "The assistant only ever sees column names & types, never data values",
      lead: "mooring copilot · schema-only.",
      body:
        " The assistant sees this notebook's code and the schema (column names & types) " +
        "of your datasets and loaded dataframes — never the data itself. It looks schemas up " +
        "on its own; just ask.",
      footer: "Type /help for commands. Don't paste real values into a cell or this chat.",
    };
  }

  function routingChangeAllowed(turnState) {
    return turnState === "idle" || turnState === "error";
  }

  function latestRequestGate() {
    let generation = 0;
    return {
      begin() {
        generation += 1;
        return generation;
      },
      isCurrent(candidate) {
        return candidate === generation;
      },
    };
  }

  function routingExpectationMatches(routing, route) {
    const zone = route?.zone;
    if (zone && zone !== "general" && zone !== "trusted") return false;
    return Boolean(routing) === Boolean(zone);
  }

  function generalModelRelevant(routing, preference) {
    return !(trustedRoutingAvailable(routing) && preference === "trusted");
  }

  // Notebook chat choices are overrides, not a second set of machine defaults.
  // Scope is an opaque, stable workspace id supplied by the server, so two
  // workspaces with the same notebook path cannot inherit each other's browser
  // preference. Normalising separators avoids contradictory overrides for the
  // same notebook when a Windows path is later rendered with URL separators.
  function notebookPreferenceKey(scope, notebook, field) {
    const safeScope = typeof scope === "string" && scope.trim() ? scope.trim() : "local";
    const safeNotebook = typeof notebook === "string"
      ? notebook.replace(/\\/g, "/").replace(/^(\.\/)+/, "").replace(/\/{2,}/g, "/")
      : "";
    const safeField = typeof field === "string" ? field : "";
    return (
      "mooring.ai.notebook." +
      encodeURIComponent(safeScope) + "." +
      encodeURIComponent(safeNotebook) + "." +
      encodeURIComponent(safeField)
    );
  }

  function safeStorageGet(storage, key) {
    try {
      return storage?.getItem(key) ?? null;
    } catch {
      return null;
    }
  }

  function safeStorageSet(storage, key, value) {
    try {
      if (value) storage?.setItem(key, value);
      else storage?.removeItem(key);
      return Boolean(storage);
    } catch {
      return false;
    }
  }

  // A stale allowlist-backed override must be removed, not merely ignored. If
  // it remained in storage it could silently spring back to life when a model
  // with the same id was approved again later.
  function readValidNotebookOverride(storage, key, choose) {
    const saved = safeStorageGet(storage, key);
    const selected = choose(saved);
    if (saved && !selected) safeStorageSet(storage, key, "");
    return selected;
  }

  function chooseNotebookOverride(allowedValues, saved) {
    const allowed = new Set((allowedValues || []).filter((v) => typeof v === "string"));
    return typeof saved === "string" && allowed.has(saved.trim()) ? saved.trim() : "";
  }

  function chooseNotebookTrustedOverride(routing, saved) {
    const options = trustedModelOptions(routing);
    // With only one approved model an explicit override cannot change behaviour;
    // keep the control as a truthful fixed "Use global default" instead.
    if (options.length <= 1) return "";
    return chooseNotebookOverride(options.map((m) => m.id), saved);
  }

  function chooseNotebookRoutingOverride(routing, saved) {
    if (!trustedRoutingAvailable(routing)) return "";
    return chooseNotebookOverride(["auto", "trusted"], saved);
  }

  function effectiveRoutingPreference(routing, notebookOverride) {
    const override = chooseNotebookRoutingOverride(routing, notebookOverride);
    return override || chooseRoutingPreference(routing, routing?.default_routing_preference);
  }

  function routingSettingValueAllowed(routing, key, value) {
    if (!trustedRoutingAvailable(routing) || typeof value !== "string") return false;
    if (key === "ai.trusted_model") {
      return value === "" || trustedModelOptions(routing).some((m) => m.id === value);
    }
    if (key === "ai.routing_preference") return value === "auto" || value === "trusted";
    return true;
  }

  function resolvedRoutingValuesValid(routing, trustedModel, preference) {
    return (
      typeof trustedModel === "string" &&
      trustedModel !== "" &&
      trustedModelOptions(routing).some((m) => m.id === trustedModel) &&
      (preference === "auto" || preference === "trusted")
    );
  }

  function resolvedRoutingMatchesRequest(resolved, requested) {
    const trustedModel = requested?.trusted_model;
    const preference = requested?.routing_preference;
    return (
      (!trustedModel || resolved?.trusted_model === trustedModel) &&
      (!preference || resolved?.routing_preference === preference)
    );
  }

  function notebookOverridePayload(values) {
    const out = {};
    for (const key of ["model", "reasoning_effort", "trusted_model", "routing_preference"]) {
      const value = values?.[key];
      if (typeof value === "string" && value) out[key] = value;
    }
    return out;
  }

  function trustedModelsFromEnumOptions(enumOptions) {
    const seen = new Set();
    const models = [];
    for (const option of Array.isArray(enumOptions) ? enumOptions : []) {
      const id = typeof option?.value === "string" ? option.value.trim() : "";
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const name = typeof option.label === "string" && option.label.trim()
        ? option.label.trim()
        : id;
      models.push({ id, name });
    }
    return models;
  }

  // -- /explain: the handover walkthrough ------------------------------------
  // Fixed prompts for the "explain this notebook" flow. All three are pure
  // CONSTANTS — no user text, no dataset values, no interpolation — so the same
  // bytes leave the workspace every time. That is the privacy story (value-free
  // by construction, over the EXISTING chat channel: the outbound PII valve and
  // the per-notebook off-switch still apply), and it is what keeps any wording
  // change review-visible: the exact demands are pinned by tests/js/chat_core.test.js.

  // The fixed line the model must open with — and the disclaimer a notes cell
  // must carry — so a generated walkthrough can never masquerade as human notes.
  const EXPLAIN_DISCLAIMER =
    "Generated by the copilot from the notebook source — verify against the " +
    "notebook before relying on it.";

  // The full canned walkthrough prompt sent by /explain (the transcript shows
  // explainLabel() instead — see chat.js submitMessage).
  function explainPrompt() {
    return (
      "Walk me through this notebook so I can take it over from a teammate.\n" +
      "\n" +
      "First, call mooring_read_notebook_source to read the current source — its " +
      "output numbers every cell with a `# === cell N ===` header, and those numbers " +
      "are your anchors. Use mooring_get_schema where a dataset's columns would make " +
      "a step clearer.\n" +
      "\n" +
      "Open your reply with exactly this line:\n" +
      EXPLAIN_DISCLAIMER +
      "\n" +
      "\n" +
      "Then produce a walkthrough with these sections:\n" +
      "- Purpose: what this notebook is for, in a sentence or two.\n" +
      "- Inputs it reads: each dataset or path read, with the `cell N` it is read in.\n" +
      "- Pipeline stages: the steps in order, each citing its `cell N`; on a large " +
      "notebook group related cells into stages rather than listing every cell.\n" +
      "- Outputs it writes: each file, table or chart produced, with its `cell N`.\n" +
      "- Things to change each period: dates, paths and literals someone would edit " +
      "for the next run, each with its `cell N`.\n" +
      "\n" +
      "Every claim must cite the `cell N` it comes from so it can be checked against " +
      "the source. Do not propose any change — this is a read-only walkthrough."
    );
  }

  // The compact row shown in the transcript in place of the canned prompt.
  function explainLabel() {
    return "/explain — walk me through this notebook";
  }

  // -- /review: a whole-notebook value-blind logic review --------------------
  // The single thing a value-blind copilot is genuinely great at: reasoning over
  // authored code + schema to flag STRUCTURAL correctness risks (fan-out joins,
  // wrong grain, hardcoded periods, dropped rows, un-run cells) — never confirming a
  // number, which would need the data it structurally cannot see. Like /explain this
  // is a pure CONSTANT (no user text, no values), pinned by chat_core.test.js.

  // The verify-first line the review must open with — a logic review is not proof a
  // number is right; it flags risks to check.
  const REVIEW_DISCLAIMER =
    "Logic review from the code and schema — it flags risks; it can't confirm a " +
    "number is correct. Check each point against the notebook.";

  function reviewPrompt() {
    return (
      "Review this notebook's LOGIC for correctness risks, so I can trust the numbers " +
      "before I share them.\n" +
      "\n" +
      "First call mooring_read_notebook_source to read the current source — its output " +
      "numbers every cell with a `# === cell N ===` header, and those numbers are your " +
      "anchors. Use mooring_get_schema on the datasets involved to reason about columns, " +
      "keys, grain and types.\n" +
      "\n" +
      "Open your reply with exactly this line:\n" +
      REVIEW_DISCLAIMER +
      "\n" +
      "\n" +
      "You can see only the code and the schema, never the data values — so review " +
      "STRUCTURE and LOGIC, not whether a specific number is correct. Look for:\n" +
      "- Joins that could fan out (a supposedly one-to-many join on a non-unique key) and " +
      "double-count a sum.\n" +
      "- Aggregations at the wrong grain, or a sum/mean over a column that can be null.\n" +
      "- Hardcoded dates, periods, paths or magic numbers that must change each run.\n" +
      "- Filters that could silently drop rows (an inner join or a strict comparison where " +
      "missing keys or NULLs disappear).\n" +
      "- Period/boundary errors (off-by-one date ranges, inclusive vs exclusive bounds) and " +
      "unit or currency mismatches in arithmetic.\n" +
      "- Cells that define something never used, or that must be re-run in order.\n" +
      "\n" +
      "Return a findings list ordered most-serious first. For each: the `cell N` it is in, " +
      "the risk in one line, and why it matters. If you are unsure, say so rather than " +
      "inventing a problem. Do NOT propose code changes and do NOT ask for data values — " +
      "this is a read-only review; I'll ask if I want a fix."
    );
  }

  // The compact row shown in the transcript in place of the canned prompt.
  function reviewLabel() {
    return "/review — check this notebook's logic for risks";
  }

  // The canned follow-up behind "Add as notes cell": ONE new appended markdown
  // documentation cell. There is now ONE write tool, so the forbidden thing is no
  // longer a set of tool names but the other FIELDS of that tool — `appends` only,
  // never `edits`, `deletes` or `cells` — so the walkthrough can never clobber an
  // existing cell.
  //
  // It says "the notebook-editing tool" rather than naming one: that tool is registered
  // under TWO names, one per mode (ai/tools.py: WRITE_TOOL_NAMES), and this page is
  // never told which mode the session is in — naming the wrong one would be an
  // instruction to call a tool the model has not been given. The session's own system
  // prompt names it exactly once, and does know. What THIS prompt has to pin is the
  // FIELD, which is the same in both modes.
  function notesCellPrompt() {
    return (
      "Now add that walkthrough to the notebook as a notes cell. Add ONE new " +
      "markdown documentation cell using the notebook-editing tool with `appends` " +
      "only — never its `edits`, `deletes` or `cells` fields, and do not touch any " +
      "existing cell.\n" +
      "\n" +
      "The cell's text must begin with this disclaimer line:\n" +
      EXPLAIN_DISCLAIMER +
      "\n" +
      "\n" +
      "First call mooring_read_notebook_source and check the current source: wrap " +
      "the notes in mo.md(...) only if `import marimo as mo` already exists in the " +
      "notebook — never add that import yourself. If it does not exist, use a plain " +
      "fallback you judge safe to append (for example a triple-quoted markdown " +
      "string as the cell body)."
    );
  }

  // -- /checks: propose value-free tie-out checks ----------------------------
  // A fixed prompt (no user text, no values) asking the copilot to author a
  // mooring_checks cell from the schema + source it already sees. Value-free by
  // construction, over the EXISTING chat channel. It does not say who applies the
  // result: with `[ai] auto_apply` on the model's write lands and runs inside its own
  // tool call, and this page is not told which mode it is in. Pinned by
  // tests/js/chat_core.test.js.
  function checksPrompt() {
    return (
      "Add tie-out / data-quality checks for this notebook using the value-free " +
      "`mooring_checks` API.\n" +
      "\n" +
      "First call mooring_read_notebook_source to see the current cells, and " +
      "mooring_get_schema for the datasets involved, so you pick real column and key " +
      "names. Then add ONE new cell (the notebook-editing tool, using `appends`) that:\n" +
      "- begins with `import mooring_checks as mc` and `mc.reset()`;\n" +
      "- asserts the checks that fit THIS notebook — e.g. mc.unique_key(df, \"id\") on any " +
      "key you expect to be unique, mc.no_fanout(left, right, on=\"key\") before a join, " +
      "mc.not_null(df, ...) on columns that must be populated, mc.row_delta(df, prior) " +
      "where a row count should be stable, and mc.reconciles(a, b, tol=...) where a total " +
      "should match a control.\n" +
      "\n" +
      "Choose the columns and keys from the schema and the source only — never ask for " +
      "data values. Briefly say why each check matters."
    );
  }

  // The compact row shown in the transcript in place of the canned prompt.
  function checksLabel() {
    return "/checks — propose tie-out checks for this notebook";
  }

  // -- /sql: propose a marimo SQL (DuckDB) cell ------------------------------
  // A fixed prompt (no user text, no values) asking the copilot to author a marimo
  // `mo.sql` cell from the schema + source it already sees. Value-free by construction:
  // SQL is authored code run locally by marimo; the model never sees the result. Over
  // the EXISTING chat channel. Like /checks it does not say who applies the result —
  // the page is not told whether this session auto-applies. Pinned by
  // tests/js/chat_core.test.js.
  function sqlPrompt() {
    return (
      "Propose a marimo SQL cell for this notebook, running on DuckDB.\n" +
      "\n" +
      "First call mooring_read_notebook_source to see the current cells, and " +
      "mooring_get_schema for the datasets involved, so you use real dataframe and " +
      "column names. Then add ONE new cell (the notebook-editing tool, using `appends`) that:\n" +
      '- runs the query with `result = mo.sql("""...""")` (assign it to a well-named ' +
      "dataframe variable so later cells can use it);\n" +
      "- queries the dataframes already in scope BY THEIR VARIABLE NAME;\n" +
      "- lists the columns explicitly (no SELECT *) using the names from the schema.\n" +
      "\n" +
      "The cell needs `import marimo as mo` in the notebook (add it if the source lacks it) " +
      "and the `duckdb` package in the environment — if duckdb may be missing, note that it " +
      "can be added with `mooring deps add duckdb`. Do NOT pivot or crosstab row values into " +
      "column headers (e.g. DuckDB PIVOT); the resulting column names would be data values.\n" +
      "\n" +
      "Choose the tables and columns from the schema and the source only — never inline a " +
      "data value or ask for one. Briefly say what the query returns."
    );
  }

  function sqlLabel() {
    return "/sql — propose a SQL cell for this notebook";
  }

  // -- /investigate: fan out read-only sub-agents over independent sub-questions ----
  // UNLIKE the canned prompts above, this one INTERPOLATES the analyst's own topic. That
  // is not a new egress channel: the topic is ordinary user prose sent over the ordinary
  // send path, so the outbound PII valve and the per-notebook off-switch apply to it
  // exactly as they would to a typed message. The prompt only asks the model to call
  // mooring_investigate, whose branches are read-only, structurally value-blind sub-agents
  // (see docs/admins/ai-privacy.md#investigate). The fixed WRAPPER around the topic is
  // pinned by tests/js/chat_core.test.js.
  function investigatePrompt(topic) {
    return (
      "Investigate this before you propose anything:\n" +
      "\n" +
      String(topic == null ? "" : topic).trim() +
      "\n" +
      "\n" +
      "Break it into INDEPENDENT sub-questions that can each be researched on their own, " +
      "then call mooring_investigate ONCE with all of them so they run in parallel. Each " +
      "branch is answered by a separate read-only assistant that can read schemas, the " +
      "notebook source, the data dictionary and any semantic model — it cannot write. " +
      "Never put a data value in a sub-question: use names, paths and plain-English asks " +
      "only.\n" +
      "\n" +
      "When the merged findings come back, summarise what you learned, then propose ONE " +
      "change with the propose tools for the analyst to review and apply. If the topic " +
      "does not actually split into independent parts, say so and answer it directly " +
      "instead of fanning out.\n" +
      "\n" +
      "If the mooring_investigate tool is not available to you, do not mention it and do " +
      "not apologise — the analyst has turned the fan-out off. Just research the topic " +
      "yourself with the read tools and answer it."
    );
  }

  // The compact row shown in the transcript in place of the full prompt. It carries the
  // analyst's OWN topic, so the transcript reads back as what they actually asked for.
  function investigateLabel(topic) {
    return "/investigate — " + String(topic == null ? "" : topic).trim();
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
    if (this.items.length && this.items.at(-1) === t) return; // dedup repeats
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
    const m = /(?:^|\s)@([^\s@]*)$/.exec(upto);
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

  // -- traceback guard hold card --------------------------------------------
  // The analyst-facing summary line for a held (sanitised) traceback turn. Pure
  // so it tests under node --test; chat.js renders it above the <pre> preview.
  // `redactions` and `piiFindings` are value-free {line, kind} lists from the
  // "traceback" SSE event — never any withheld text.
  function tracebackHoldSummary(redactions, piiFindings) {
    const n = (redactions || []).length;
    const counted = n
      ? ` (${n} redaction${n === 1 ? "" : "s"})`
      : " (nothing needed redacting)";
    let msg =
      "Held before sending — your message contains a Python traceback, which can " +
      "embed data values. It was rewritten to the value-safe version below" +
      counted +
      ". Only this sanitised version can be sent; the raw paste was not kept. " +
      "Never retype a redacted value in prose.";
    const kinds = [...new Set((piiFindings || []).map((f) => f.kind))];
    if (kinds.length) {
      msg +=
        " The surrounding text also looks like it may contain " +
        kinds.join(", ") +
        " — review it before sending.";
    }
    return msg;
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

  // -- apply gate hold card -------------------------------------------------
  // The copilot's Apply writes a cell AND marimo runs it immediately, so Undo —
  // which only restores the notebook's bytes — is a COMPLETE remedy for ordinary
  // code and NO remedy at all for code that deleted a file or dropped a table.
  // The server-side gate (mooring.ai.codeguard, enforced in app/apply.py) holds
  // such an Apply with HTTP 428 and a `gate` payload; these helpers turn that
  // payload into the analyst-facing wording. Pure, so every string is pinned by
  // tests/js/apply_gate.test.js.
  //
  // Two bands reach the wire: "floor" (never downgradable — Undo is not a remedy)
  // and "ask" (one confirmation). BOTH are held; only the wording and the emphasis
  // differ. Anything else — a missing band, a typo, a mangled payload — is read as
  // "floor", so a broken response over-warns rather than under-warns.
  const GATE_FLOOR_UNDO =
    "Undo puts the notebook back. It can't put back a deleted file or a dropped " +
    "table. Once this runs, that part is permanent.";
  const GATE_ASK_UNDO =
    "Undo puts the notebook back. It can't take back anything this writes or " +
    "sends elsewhere.";
  const GATE_MECHANISM =
    "Nothing has changed yet. Applying writes the change into the notebook, and " +
    "marimo runs it straight away.";
  // Per-finding marker, used only to pick the irreversible lines out of a MIXED
  // verdict (see gateFindingItems).
  const GATE_MARK = "can't be undone";

  // Normalise a 428 body into {band, token, findings}, or null when it carries no
  // usable gate. Defensive because it shapes an irreversible decision.
  function gateFromResponse(data) {
    const g = data && typeof data === "object" ? data.gate : null;
    if (!g || typeof g !== "object") return null;
    return {
      band: g.band === "ask" ? "ask" : "floor", // fail closed: anything else => floor
      token: typeof g.token === "string" ? g.token : "",
      findings: Array.isArray(g.findings) ? g.findings : [],
    };
  }

  // True unless the payload explicitly says "ask" (see the fail-closed note above).
  function gateIsFloor(gate) {
    return !gate || gate.band !== "ask";
  }

  // One item per finding, in the ANALYST's language: {text, floor, mark}. `text` is
  // the server-supplied plain-English `label`, never the `kind` slug and never any
  // code. A finding with no label is dropped rather than falling back to the slug —
  // the hold itself is what stops the apply, so a missing label costs an
  // explanation, not the guard.
  //
  // `mark` labels the individual lines Undo cannot help with, and ONLY in a mixed
  // verdict: when every finding is the un-undoable kind the card's header has
  // already said so, and repeating it per row is noise. A per-finding `band` is
  // optional on the wire — when the server omits it nothing is marked, which is the
  // right way to degrade (a missing mark under-decorates; a wrong one misleads).
  function gateFindingItems(gate) {
    const list = gate && Array.isArray(gate.findings) ? gate.findings : [];
    const seen = new Set();
    const out = [];
    for (const f of list) {
      if (!f || typeof f !== "object") continue;
      const label = String(f.label == null ? "" : f.label).trim();
      if (!label) continue;
      const n = Number(f.line);
      const text = Number.isInteger(n) && n > 0 ? "line " + n + ": " + label : label;
      if (seen.has(text)) continue; // the same finding twice reads as noise
      seen.add(text);
      out.push({ text: text, floor: f.band === "floor", mark: "" });
    }
    const mixed = out.some((i) => i.floor) && out.some((i) => !i.floor);
    if (mixed) out.forEach((i) => { if (i.floor) i.mark = GATE_MARK; });
    return out;
  }

  // Just the lines, for callers that render plain text.
  function gateFindingRows(gate) {
    return gateFindingItems(gate).map((i) => i.text);
  }

  // The hold's one summary line: what happened (nothing) and why we stopped.
  function gateHoldSummary(gate) {
    return gateIsFloor(gate)
      ? "Held before applying — this change can't be taken back."
      : "Held before applying — this change does more than work out an answer.";
  }

  // Every string the hold card renders, chosen by band. `lead` is empty when there
  // is nothing to list, so a findings-less payload never leaves a dangling colon.
  function gateHoldWording(gate) {
    const floor = gateIsFloor(gate);
    const items = gateFindingItems(gate);
    const rows = items.map((i) => i.text);
    return {
      band: floor ? "floor" : "ask",
      floor: floor,
      summary: gateHoldSummary(gate),
      mechanism: GATE_MECHANISM,
      lead: rows.length ? (floor ? "What it would do, permanently:" : "What it would do:") : "",
      items: items,
      rows: rows,
      undoNote: floor ? GATE_FLOOR_UNDO : GATE_ASK_UNDO,
      confirmLabel: floor ? "Run it anyway" : "Apply anyway",
      cancelLabel: "Don't apply",
    };
  }

  // -- the Apply repair loop's attempt bound --------------------------------
  // How many corrective re-proposals ONE proposal may ask the assistant for. Two, not
  // one: a weaker model routinely gets the shape right on its second try and the first
  // bound (a single boolean, "triedFix") gave up exactly there. Not unbounded either —
  // every attempt is a billed turn, and a model that has failed twice on the same error
  // is looping, not converging.
  const MAX_FIX_ATTEMPTS = 2;

  // What an Apply failure should do next. Pure, so the bound is testable rather than
  // buried in a click handler.
  //   status — the HTTP status the apply endpoint answered with
  //   tried  — how many corrective re-proposals this proposal has already asked for
  //   noFix  — a refusal that re-proposing cannot answer (a 428 we could not read)
  // Returns {action, tried}. "conflict": a staleness 409, where the notebook moved under
  // the proposal — re-READING is what's needed, not another attempt at the same write.
  // "fix": hand the error back. "report": say it and stop.
  //
  // A 409 never consumes an attempt, and a 428 (gate hold / re-hold) never reaches here
  // at all: a staleness conflict and a gate hold are not the model getting it wrong, and
  // spending the analyst's second attempt on either would leave nothing for the failure
  // that IS the model's to fix.
  function applyFailureAction(status, tried, noFix) {
    const used = Number.isFinite(tried) && tried > 0 ? tried : 0;
    if (status === 409) return { action: "conflict", tried: used };
    if (noFix || used >= MAX_FIX_ATTEMPTS) return { action: "report", tried: used };
    return { action: "fix", tried: used + 1 };
  }

  // The prompt handed back to the assistant on attempt `tried` (1-based). The second
  // attempt SAYS it is the second — a model told only "this failed" a second time tends
  // to re-send what it just sent.
  function applyFixPrompt(error, tried) {
    const again =
      tried > 1
        ? " This is your second attempt: your previous correction did not apply either, " +
          "so change the approach rather than resending it."
        : "";
    return (
      "The change you proposed could not be applied: " + error + again +
      " Please re-propose a corrected version. Remember each cell is the BODY only — " +
      "no @app.cell, no def, and no return statements."
    );
  }

  // -- the reviewer's view of the same findings ------------------------------
  // The hub's reviews page shows the SAME value-free findings beside a Propose PR's
  // diff, in the same wire shape — deliberately, so one derivation (and one rule that
  // a labelless finding is dropped rather than shown as its slug) serves both.
  //
  // It is NOT a hold. There is nothing to confirm, no token, nothing to click past —
  // it is context beside a diff someone is already reading. Two things follow. The
  // reviewer sees BOTH bands, because they are the one person in the loop who reads
  // Python and that is the whole reason the list exists; so every row carries its own
  // band for styling. And the hold's mixed-verdict mark is dropped: a list that
  // already styles each row by band would only be repeating itself.
  //
  // A missing/unreadable `code` yields NO rows, so the caller renders no block at all.
  // The scan landed after this page shipped: an older payload has no `code` key, and
  // the right answer to that is silence, not an empty box or a broken row.
  function codeFindingRows(code) {
    return gateFindingItems(code).map((i) => ({
      text: i.text,
      // DELIBERATELY the opposite of gateIsFloor's fail-closed rule, and not a bug:
      // an unknown band reads as the QUIETER "ask" here. Fail-closed earns its keep by
      // stopping a write, and this page stops nothing — it is context beside a diff a
      // human is already reading. Over-warning a reviewer who can read the code for
      // themselves spends the warning's credibility on a row that may not deserve it,
      // and the credibility is what makes the real "can't be undone" rows land.
      band: i.floor ? "floor" : "ask",
      floor: i.floor,
    }));
  }

  // The one line above that list. It names the scan, so the reviewer reads the rows as
  // automated context sitting beside the diff rather than as a human's comment.
  function codeFindingLead(code) {
    const n = codeFindingRows(code).length;
    if (!n) return "";
    return "Destructive-code scan — " + n + " finding" + (n === 1 ? "" : "s") + ":";
  }

  // The per-row tag. Fixed strings that name the BAND, never the code.
  function codeFindingTag(row) {
    return row && row.floor ? GATE_MARK : "side effect";
  }

  // -- auto-apply: the receipt --------------------------------------------
  // With `[ai] auto_apply` on, the model's write lands inside its own tool call and
  // marimo runs it; the analyst gets a RECEIPT after the fact instead of an Apply
  // button before it. That is the whole design bet: the Apply button asked someone who
  // does not read Python to judge code at the one moment they could not — before it
  // ran. The judgement moves to after the run, where there are results.
  //
  // Which is exactly why every string on the receipt is decided HERE, pinned by tests,
  // the same way the gate's wording is. The reader of these words does not read Python.
  // "Changed cell 3 · Added cell 8" is their language; `{"op": "edit", "index": 3}`,
  // "append_cell" and "status: applied" are not, and must never surface.

  // The wire summary is {edited: [...], appended: [...], deleted: [...]} — cell numbers
  // in whatever order the applier wrote them. Verb order is fixed so a multi-op write
  // always reads the same way round.
  //
  // Those numbers are ZERO-BASED indices into the file, which is a number the analyst
  // has no way to see: marimo does not print it, and "cell 0" is not a thing that
  // exists on their screen. So they are rendered as a POSITION counted from the top —
  // "the 4th cell" — which is a number they can arrive at by scrolling. Appends have no
  // useful position at all (they are always the new last cells), so they say where they
  // went instead of pretending to a number.
  // Past this many cells the list stops being readable, so say how many instead.
  const RECEIPT_MAX_LISTED = 4;

  // "1st" / "2nd" / "3rd" / "4th"… for a ONE-based position.
  function ordinal(n) {
    const teens = n % 100;
    if (teens >= 11 && teens <= 13) return n + "th";
    return n + ({ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th");
  }

  // Cell numbers, cleaned: integers only, deduped, ascending. Anything else is dropped
  // rather than rendered — a receipt that says "cell undefined" is worse than one that
  // says less.
  function receiptCells(value) {
    const list = Array.isArray(value) ? value : [];
    const seen = new Set();
    const out = [];
    for (const raw of list) {
      // Number(null) is 0 and Number(true) is 1 — coercing blindly would invent a
      // "cell 0" out of a hole in the payload, which is precisely the kind of confident
      // wrong number a receipt must never print.
      const n =
        typeof raw === "number"
          ? raw
          : typeof raw === "string" && raw.trim() !== ""
            ? Number(raw)
            : NaN;
      if (!Number.isInteger(n) || n < 0) continue;
      if (seen.has(n)) continue;
      seen.add(n);
      out.push(n);
    }
    out.sort((a, b) => a - b);
    return out;
  }

  // "the 4th cell" / "the 2nd and 4th cells" / "5 cells" — from ZERO-based indices.
  function receiptCellPhrase(nums) {
    if (nums.length > RECEIPT_MAX_LISTED) return nums.length + " cells";
    const pos = nums.map((n) => ordinal(n + 1));
    if (pos.length === 1) return "the " + pos[0] + " cell";
    return "the " + pos.slice(0, -1).join(", ") + " and " + pos[pos.length - 1] + " cells";
  }

  // Appends land at the END of the notebook, always. Their index is therefore a fact
  // about the file's length rather than about the change, and naming it would invite
  // the analyst to go looking for a number nothing on their screen agrees with.
  function receiptAppendPhrase(n) {
    return n === 1 ? "Added a new cell at the end" : "Added " + n + " new cells at the end";
  }

  // "Changed the 4th cell · Added a new cell at the end". Never empty: a write with no
  // readable summary still happened, and saying nothing would hide it.
  function receiptHeadline(summary) {
    const s = summary && typeof summary === "object" && !Array.isArray(summary) ? summary : {};
    const parts = [];
    const edited = receiptCells(s.edited);
    if (edited.length) parts.push("Changed " + receiptCellPhrase(edited));
    const appended = receiptCells(s.appended);
    if (appended.length) parts.push(receiptAppendPhrase(appended.length));
    const deleted = receiptCells(s.deleted);
    if (deleted.length) parts.push("Removed " + receiptCellPhrase(deleted));
    return parts.length ? parts.join(" · ") : "Changed the notebook";
  }

  // The observation is the value-free line the applier read back off the run ("cell 8
  // ran · sales_q3: 12 columns, 40331 rows"). It is DATA, not markup — chat.js renders
  // it with textContent — so all this does is make it readable.
  const OBSERVATION_MAX = 240;

  // Thousands separators for standalone integers of five or more digits, so a row count
  // reads as a magnitude ("40,331 rows") instead of a digit soup. Deliberately narrow:
  // four digits are left alone (a year is not a count), and a run glued to a letter,
  // dot, dash or underscore is left exactly as written, so an id, a version or a column
  // name is never rewritten.
  function groupDigits(text) {
    return String(text).replace(
      /(^|[^0-9A-Za-z._-])(\d{5,})(?![0-9A-Za-z_-])/g,
      (_m, before, digits) => before + digits.replace(/\B(?=(\d{3})+$)/g, ","),
    );
  }

  // The whole tidied line, however long. The applier appends the clause that names what
  // is NOT bound — the half of the observation that says the change did not work — LAST,
  // so a hard truncation removes exactly the failure and leaves the good news. Nothing
  // may be dropped on the floor: the short form is what is shown, this is what "show
  // more" reveals, and the caller must offer that whenever the two differ.
  function receiptObservationFull(text) {
    const s = typeof text === "string" ? text.replace(/\s+/g, " ").trim() : "";
    return s ? groupDigits(s) : "";
  }

  function receiptObservation(text) {
    const grouped = receiptObservationFull(text);
    if (!grouped) return "";
    return grouped.length > OBSERVATION_MAX
      ? grouped.slice(0, OBSERVATION_MAX - 1).trimEnd() + "…"
      : grouped;
  }

  // Whether the short form left something out — i.e. whether a "show more" is owed.
  function receiptObservationTruncated(text) {
    const full = receiptObservationFull(text);
    return !!full && full !== receiptObservation(text);
  }

  // Where the next receipt goes. One turn can write several times, and those receipts
  // must read as a numbered sequence rather than a stack of unrelated cards — but ONLY
  // while the group they would join is still the same turn's and still on the page.
  // `attached` is the caller's answer to "is that group still in the document?": /clear
  // empties the transcript, and appending into the detached node it left behind would
  // silently swallow every later receipt. (A tray rebuild eating a live card is a
  // mistake this codebase has already made once; this is the pure part of not repeating
  // it.) `prev` is the last group as {turnId, count}, or null when there is none.
  function receiptSequence(prev, turnId, attached) {
    const id = typeof turnId === "string" ? turnId : "";
    const reuse = !!prev && attached === true && prev.turnId === id;
    const count = reuse ? (Number(prev.count) || 0) + 1 : 1;
    // Numbers appear only once there IS a sequence — a lone "1" beside one receipt is
    // noise, so the first receipt is numbered retroactively when the second arrives.
    return { reuse, turnId: id, count, numbered: count >= 2 };
  }

  // -- auto-apply: what Revert actually reverts -----------------------------
  // The one control that makes auto-apply safe, and the one that was lying. mooring
  // takes ONE undo checkpoint per TURN, not per write: five writes in a turn share a
  // single snapshot, so the Revert beside the fifth receipt puts back all five. The
  // button said "Put the notebook back the way it was before this change" and the
  // confirmation said "Reverted the last applied change" — for someone who does not
  // read Python and cannot see the notebook diff, that is the difference between
  // knowing what happened and not.
  //
  // The granularity is not even fixed: the guard refuses to EXTEND a checkpoint after a
  // manual Apply or an Undo lands mid-turn (app/apply.py:_extends_turn), so inside one
  // visually identical group some Reverts undo four writes and some undo one. Only the
  // server can tell them apart, and only if it says so.
  //
  // `writes` is that answer: the server's `checkpoint_writes` — how many model writes
  // the checkpoint this Revert would restore currently covers, counting this one. When
  // it is absent (an older hub, or a payload this client could not read) NOTHING is
  // guessed: `undo_depth` cannot distinguish the two cases (the undo stack is capped at
  // 25, so "the depth did not move" also means "the oldest snapshot was pruned"), and a
  // confident wrong number is the defect being fixed, not a smaller version of it. The
  // wording then says what is actually known — that a turn's changes go back together
  // and the earlier ones MAY go with this one.
  //
  // `position` is which write of the current TURN this is (1-based), counted by the
  // caller across the whole turn rather than off the visible group — /clear empties the
  // transcript without un-writing anything, and the first receipt after it is still not
  // the first write of the turn.
  const REVERT_LABEL = "Revert";
  const REVERT_ONE_TITLE = "Put the notebook back to how it was just before this change.";
  const REVERT_MAYBE_TITLE =
    "Put the notebook back one step. mooring undoes a turn's changes together, so the " +
    "earlier changes from this turn may go back with it.";
  const REVERT_MAYBE_NOTE = "may also undo the earlier changes in this turn";

  function revertScope(writes, position) {
    const n = Number.isInteger(writes) && writes >= 1 ? writes : null;
    const pos = Number.isInteger(position) && position >= 1 ? position : 1;
    if (n === null) {
      // The first write of a turn always opens a fresh checkpoint (the recorded turn id
      // cannot match one that has only just been minted), so this one case is knowable
      // without the server's help.
      if (pos <= 1) return { covers: 1, label: REVERT_LABEL, title: REVERT_ONE_TITLE, note: "" };
      return { covers: null, label: REVERT_LABEL, title: REVERT_MAYBE_TITLE, note: REVERT_MAYBE_NOTE };
    }
    if (n === 1) return { covers: 1, label: REVERT_LABEL, title: REVERT_ONE_TITLE, note: "" };
    return {
      covers: n,
      label: REVERT_LABEL + " " + n + " changes",
      title:
        "Put the notebook back to how it was before the assistant's last " +
        n +
        " changes — not just this one.",
      note: "undoes " + n + " changes together",
    };
  }

  // What the transcript says AFTER a revert lands. `covers` is the scope the button
  // showed (a number, or null for "not known"); `remaining` is the undo depth left.
  function revertedNotice(covers, remaining) {
    const left = Number(remaining);
    const more = Number.isFinite(left) && left > 0 ? Math.floor(left) : 0;
    const earlier = more
      ? " (" + more + " earlier change" + (more > 1 ? "s" : "") + " still undoable with /undo)"
      : "";
    if (covers === null || covers === undefined) {
      return (
        "Put the notebook back one step — any earlier changes from the same turn may " +
        "have gone back with it. Check the notebook." +
        earlier
      );
    }
    const n = Number.isInteger(covers) && covers >= 1 ? covers : 1;
    if (n === 1) return "Reverted the last applied change." + earlier;
    return "Put the notebook back to before the assistant's last " + n + " changes." + earlier;
  }

  // A receipt whose Revert has been taken away by a NEWER change. Only one control may
  // be live at a time (they all pop the same stack), so the older receipt has to say
  // what happened to its own way back.
  //
  // `sameTurn` is the load-bearing distinction. A later write in the SAME turn usually
  // joined this one's checkpoint, so this change has no undo step of its own and telling
  // the analyst that "/undo steps further back" to it is false. A later TURN's write
  // really does sit on its own step above this one.
  function receiptDisplacedNote(sameTurn) {
    return sameTurn === true
      ? "the Revert moved to the newest change in this turn"
      : "superseded · /undo steps back one change at a time";
  }

  // How a receipt reads once a revert has swept over it. The receipt that carried the
  // button knows it went back; the earlier ones it covered only know so when the server
  // said how far the checkpoint reached.
  const RECEIPT_REVERTED_NOTE = "reverted";
  const RECEIPT_MAYBE_REVERTED_NOTE = "may have been reverted with it";

  // -- auto-apply: the stop control ---------------------------------------
  // A turn with no small iteration cap needs a way out, and the way out is the analyst's
  // to press. These decide what the control SAYS, which is the whole safety property: a
  // stop that reads as done while the assistant is still replying is worse than no stop.

  // The turn is winding down, not over: `request_cancel` broadcasts `cancelled` the
  // moment it is asked, and the model still finishes the step it had already started
  // (the turn's real end arrives later, as the usual `idle`). So the wording says the
  // stop registered WITHOUT claiming the assistant has gone quiet.
  // `windingDown` is the caller's answer to "is the turn still live?". It is normally
  // true — but a frame that arrives after the turn already ended must not promise a
  // wind-up that has already happened.
  function cancelledNotice(reason, windingDown) {
    const r = typeof reason === "string" ? reason.trim().toLowerCase() : "";
    const tail =
      windingDown === false
        ? ""
        : " The assistant is wrapping up the step it had already started.";
    // "analyst" is the only reason the stop button produces. Anything else is the
    // session ending a turn for its own reasons, and saying "you stopped this" then
    // would be a lie about who is in control.
    if (!r || r === "analyst" || r === "user") return "You stopped this turn." + tail;
    if (r === "timeout") return "This turn was stopped — it ran out of time." + tail;
    return "This turn was stopped." + tail;
  }

  const STOP_LABEL = "Stop";
  const STOPPING_LABEL = "Stopping…";

  // What the composer's stop control shows. `cancelPending` stays true from the click
  // until the turn actually ends, so the button reads "Stopping…" for exactly as long
  // as that is true — and is insensitive, because the ask is already in flight.
  function stopButtonState(turnState, cancelPending) {
    const busy = turnState === "thinking" || turnState === "streaming";
    if (!busy) return { visible: false, disabled: true, label: STOP_LABEL, title: "" };
    if (cancelPending) {
      return {
        visible: true,
        disabled: true,
        label: STOPPING_LABEL,
        title: "Stopping — the assistant is finishing the step it had already started.",
      };
    }
    return { visible: true, disabled: false, label: STOP_LABEL, title: "Stop this turn (Esc)" };
  }

  // The two ways a stop does NOT end in a stopped turn. Both must be said out loud: a
  // "stopping…" line that quietly becomes an ordinary finish teaches the analyst that
  // the button is decorative.
  function stopOutcomeNotice(outcome) {
    if (outcome === "finished") return "That turn finished on its own before the stop reached it.";
    return "The stop didn't reach the assistant — the turn is still running.";
  }

  // Whether a stop may be STARTED right now — shared by the button and the Esc key so
  // the two routes to the same action cannot drift apart. There must be a session, a
  // live turn, and no ask already in flight.
  function canStopTurn(turnState, cancelPending, hasSession) {
    if (!hasSession || cancelPending) return false;
    return stopButtonState(turnState, false).visible;
  }

  // What the END of a turn should say, given whether a stop was asked for and whether
  // the session acknowledged it. Three ways out and each names itself: a turn that
  // reached its own end while a "stopping…" was on screen must SAY it finished first —
  // letting that line quietly become an ordinary finish is how a stop control stops
  // being believed. `status` is the status-line word, "" to leave the state's own.
  function turnEndOutcome(asked, acknowledged) {
    if (acknowledged) return { status: "stopped", notice: "" };
    if (asked) return { status: "", notice: stopOutcomeNotice("finished") };
    return { status: "", notice: "" };
  }

  // Whether a `cancelled` frame should be reported at all.
  //
  // `request_cancel` broadcasts unconditionally — it cannot know whether the turn had
  // already finished — so a stop that arrives a beat late produces TWO endings for one
  // turn: `idle` first says "that turn finished on its own before the stop reached it",
  // then this frame says "You stopped this turn" and parks the status on "stopped" for a
  // session that is idle and ready. Both cannot be true, and the second one is the one
  // that is wrong, so a frame for a turn whose end has already been reported is dropped.
  // `turnClosed` starts TRUE (no turn has run yet), which also drops the frame a stream
  // replays on reconnect.
  function cancelEventAction(turnClosed, sawCancelled) {
    if (sawCancelled === true) return "drop"; // one acknowledgement per turn
    return turnClosed === true ? "drop" : "report";
  }

  // A `message` frame carrying `notice: true` is mooring's OWN aside (the stop's
  // "(Stopped at your request.)", the tool-ceiling line) — not something the assistant
  // said. Rendering it as assistant prose put words in the model's mouth; rendering the
  // stop notice at all repeated a stop the transcript has already reported in the
  // analyst's own terms.
  function noticeMessageAction(notice, stopped) {
    if (notice !== true) return "prose";
    return stopped === true ? "drop" : "sys";
  }

  // How a finished tool line is marked. Under a stop every remaining call in the batch
  // comes back as the terminal refusal, so the ✗ that follows would blame the assistant
  // for the analyst's own decision. Those close as stopped — neither ✓ nor ✗ — which is
  // what style.css has always said the intent was.
  function toolDoneMark(success, stopping) {
    if (success === true) return { cls: "ok", glyph: "⏺" }; // ⏺
    if (stopping === true) return { cls: "stopped", glyph: "⏹" }; // ⏹
    return { cls: "fail", glyph: "✗" }; // ✗
  }

  // -- auto-apply: saying which mode this chat is in ------------------------
  // `[ai] auto_apply` defaults ON, so an existing user's copilot stops asking without
  // ever announcing it, and the first evidence is a receipt for a change that has
  // already landed and run. The mode is a fact about what the next turn will DO to their
  // notebook, so it is stated up front, in the transcript, before the first turn.
  function autoApplyBanner(on) {
    if (on === true) {
      return (
        "This copilot changes your notebook itself. When it writes a cell the change " +
        "lands and marimo runs it straight away — you get a receipt here, with a " +
        "Revert. A change a Revert could not take back (deleting files, running a " +
        "program, overwriting a report) still stops and asks you first. Press Stop, or " +
        "Esc, to end a turn. To approve every change yourself instead, turn off “Let " +
        "the copilot apply reversible changes itself” in Settings ▸ AI copilot."
      );
    }
    return (
      "This copilot proposes changes and waits: nothing touches your notebook until " +
      "you press Apply ▸."
    );
  }

  // /help, in the mode the chat is actually in. The rows used to advertise /apply as
  // "apply the latest proposal" and offer an "a/s apply or skip" key hint — three
  // controls that do nothing at all unless a change is being HELD for the analyst.
  function helpRows(autoApply) {
    const waiting = autoApply === true ? "a change the copilot is holding for you" : "the latest proposal";
    return [
      ["/help", "show this help"],
      ["/explain", "walk through what this notebook does"],
      ["/review", "review the notebook's logic for correctness risks"],
      ["/checks", "ask for tie-out / data-quality checks"],
      ["/sql", "ask for a marimo SQL (DuckDB) cell"],
      ["/investigate <topic>", "research independent sub-questions in parallel"],
      ["/clear", "clear the transcript (keeps the session)"],
      ["/model [name]", "list or switch the model"],
      ["/apply", "apply " + waiting],
      ["/diff", "jump to " + waiting],
      ["/undo", "put the notebook back one step (same as Revert)"],
      ["/retry", "resend your last message"],
    ];
  }

  function helpKeys(autoApply) {
    const applySkip =
      autoApply === true
        ? "a/s apply or skip a change the copilot is holding for you"
        : "a/s apply or skip a proposal";
    return (
      "Keys: Enter send · Shift+Enter newline · ↑/↓ recall input · @ reference a " +
      "dataset · " +
      applySkip +
      " (when the prompt is empty/unfocused) · Esc clear the draft / close a menu — or " +
      "stop the turn in flight"
    );
  }

  // -- transcript entries for events this page did not write --------------
  // The stream is the analyst's only account of what happened to their notebook, so an
  // event that cannot be read must still leave a row: silence reads as "nothing
  // happened", and by this point something already has. These take an arbitrary payload
  // and get a sentence out of it, degrading rather than dropping.

  // The first readable sentence in a payload, whatever the sender chose to call it.
  function eventNoteText(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return "";
    for (const key of ["text", "detail", "message", "summary", "error"]) {
      const value = data[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
    return "";
  }

  // The AUTOMATIC run report — mooring re-running the whole notebook by itself when a
  // change the model wrote did not complete. Today that happens inside a tool call with
  // nothing on screen for minutes. Whatever shape the event settles on, this renders it:
  // `{state}`, `{ran_clean}`, `{sent, redactions}` and a bare `{text}` are all understood.
  function runReportNote(data) {
    const d = data && typeof data === "object" && !Array.isArray(data) ? data : {};
    const state = typeof d.state === "string" ? d.state.trim().toLowerCase() : "";
    const redactions = Array.isArray(d.redactions) ? d.redactions : [];
    if (state === "running" || state === "start" || state === "started") {
      return {
        text:
          "Running your whole notebook to find out why that change did not complete. " +
          "This can take a few minutes; the assistant gets the result, never a value.",
        sent: "",
        redactions: [],
      };
    }
    if (d.ran_clean === true) {
      return { text: "Ran the notebook: every cell ran clean. Nothing to report.", sent: "", redactions: [] };
    }
    const sent = typeof d.sent === "string" ? d.sent.trim() : "";
    if (sent) {
      return {
        text: "Ran the notebook: it failed. This is exactly what was sent to the assistant:",
        sent,
        redactions,
      };
    }
    const fallback = eventNoteText(d);
    return {
      text: fallback || "mooring ran the notebook and told the assistant what came back.",
      sent: "",
      redactions,
    };
  }

  // A model write that did NOT land. Today the model is told and the analyst is not, so
  // "the write failed" and "the write worked and something else broke" look identical
  // from the transcript — with a notebook that may or may not have changed.
  const APPLY_FAILED_LEAD = {
    conflict:
      "The assistant tried to change a cell that had moved underneath it, so nothing " +
      "was written. Your notebook is unchanged.",
    disabled:
      "The assistant tried to change this notebook, but the copilot is switched off " +
      "for it. Nothing was written.",
    cancelled: "You stopped the turn before that change was written. Nothing was written.",
  };

  function applyFailedNote(data) {
    const d = data && typeof data === "object" && !Array.isArray(data) ? data : {};
    const status = typeof d.status === "string" ? d.status.trim().toLowerCase() : "";
    const lead =
      APPLY_FAILED_LEAD[status] ||
      "The assistant tried to change the notebook and it did not land. Nothing was written.";
    const detail = eventNoteText(d);
    return detail && detail !== lead ? lead + " " + detail : lead;
  }

  // The stream dropped mid-turn and came back. A reconnect replays readiness, not
  // receipts — so a change the model made while the connection was down is on disk with
  // no row here and no way back on screen. Say that, rather than let the gap read as
  // "the assistant did nothing".
  function streamResumedNotice() {
    return (
      "The connection to the assistant dropped and came back. Anything it changed " +
      "while it was down is in your notebook but not in this transcript — check the " +
      "notebook, and use /undo if you need to step back."
    );
  }

  // -- reasoning effort -----------------------------------------------------
  // The effort picker sits beside the model in BOTH the chat window and the batch
  // page, and what it shows is what every request carries — so the decision of which
  // option starts selected is the decision of what gets SENT (and billed). It lives
  // here, pure and unit-tested, because it went wrong in three ways at once: a
  // configured effort the list didn't contain was silently discarded, a pick made
  // under one provider selected under another, and switching model dropped the
  // configured default on the floor.

  // The per-provider localStorage key for the effort pick. Namespaced because the
  // same words mean different money on different backends (Copilot's own type is
  // low|medium|high|xhigh, which INTERSECTS OpenAI's list), so a "high" chosen once
  // in Copilot must not silently ride along after switching to OpenAI.
  const LEGACY_EFFORT_KEY = "mooring.ai.effort";
  function effortKey(provider) {
    return LEGACY_EFFORT_KEY + "." + (String(provider || "").trim() || "unknown");
  }

  // One-time move of the un-namespaced key into the Copilot slot. Copilot is the
  // ONLY provider that could have written it: before the OpenAI provider advertised
  // any efforts its picker was permanently hidden, so no pick could be made — and a
  // pick is the only thing that writes the key. Idempotent, never overwrites a
  // namespaced value, and takes the storage so it is testable without a browser.
  function adoptLegacyEffort(storage) {
    if (!storage) return "";
    const legacy = storage.getItem(LEGACY_EFFORT_KEY);
    if (!legacy) return "";
    const key = effortKey("copilot");
    if (!storage.getItem(key)) storage.setItem(key, legacy);
    storage.removeItem(LEGACY_EFFORT_KEY);
    return legacy;
  }

  // Which option the picker should start on, in precedence order: the user's last
  // explicit pick for THIS provider, then the configured `ai.reasoning_effort` (the
  // hub unions it into the list so it is always selectable), then the model's own
  // advertised default, then the head of the list — which providers order so that
  // the head is the "send nothing" sentinel. Every candidate must be IN the list:
  // a <select> silently resolves an unknown value to "", which is how a configured
  // effort turned into an empty wire value. "" when there is nothing to pick from
  // (the caller hides the picker and sends no effort at all).
  function chooseEffort(efforts, saved, configuredDefault, modelDefault) {
    const list = Array.isArray(efforts) ? efforts : [];
    if (!list.length) return "";
    for (const candidate of [saved, configuredDefault, modelDefault]) {
      if (candidate && list.includes(candidate)) return candidate;
    }
    return list[0];
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

  // -- Copilot device-login output -----------------------------------------
  // `copilot login` runs an OAuth DEVICE flow and prints the one-time code +
  // verification URL to stdout, e.g.:
  //   "To authenticate, visit https://github.com/login/device and enter code 4B02-8583."
  // The hub captures those lines (CopilotProvider._drain_login) and returns them
  // via /api/ai/login/poll's `output`. The CLI tries to copy the code to the
  // clipboard but that often fails ("Failed to copy to clipboard…"), and a
  // switch-account flow lands on a device page that needs the code typed in — so
  // the UI MUST surface it. Extract the code + URL (the rest is just "Waiting…").
  // Pure: returns {code, url, lines}; empty strings until the code is printed.
  function parseDeviceLogin(lines) {
    const arr = Array.isArray(lines) ? lines.map((l) => String(l)) : [];
    let code = "";
    let url = "";
    for (const line of arr) {
      if (!code) {
        const m = /\b[A-Z0-9]{4}-[A-Z0-9]{4}\b/.exec(line);
        if (m) code = m[0];
      }
      if (!url) {
        const u = /https?:\/\/[^\s]+/.exec(line);
        if (u) url = u[0].replace(/[.,);]+$/, ""); // drop trailing sentence punctuation
      }
    }
    return { code, url, lines: arr.filter((l) => l.trim()) };
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
  const TOKEN_RE = /(#[^\n]*)|("[^"\n]*"|'[^'\n]*')|([A-Za-z_]\w*)/g;
  function highlightCode(escaped) {
    if (typeof escaped !== "string") return "";
    return escaped.replace(TOKEN_RE, function (m, com, str, word) {
      if (com) return '<span class="tok-com">' + com + "</span>";
      if (str) return '<span class="tok-str">' + str + "</span>";
      if (word) return PY_KW.has(word) ? '<span class="tok-kw">' + word + "</span>" : word;
      return m;
    });
  }

  // -- assistant-prose markdown renderer (escape-first; XSS-safe by contract) -
  // Renders the copilot's streamed reply into read-friendly HTML: GFM tables,
  // headings, ordered/unordered/nested/task lists, blockquotes, links, and
  // inline code/bold/italic/strike. It lives HERE (not chat.js) so it is unit-
  // tested under `node --test` — including the value-blind XSS contract.
  //
  // THE CONTRACT (do not weaken): renderMarkdown escapes ALL model text up front
  // with mdEscape, then the block/inline passes only ever splice in mooring's own
  // fixed tags around that already-escaped text — no raw model output ever reaches
  // innerHTML. The two spots that reintroduce characters mdEscape leaves alone are
  // handled explicitly: a link href is scheme-allow-listed (http/https/mailto or a
  // scheme-less relative URL) and quote-encoded (mdSafeHref); everything else stays
  // inert because < > & are already entities. Perfect CommonMark nesting is a
  // non-goal — readability is. chat_core.test.js pins this; keep it green.

  function mdEscape(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Caps blockquote recursion and list nesting so an absurdly deep (e.g. 5000×
  // '>') model reply can't overflow the stack and drop the WHOLE reply to
  // plaintext. Real content never nests remotely this deep.
  const MD_MAX_DEPTH = 16;

  // Allow only hrefs that cannot script: http(s)/mailto, or a scheme-less
  // relative/anchor URL. Returns a quote-encoded href, or null to drop the link.
  // ASCII control chars (incl. NUL) are stripped FIRST: a browser's URL parser
  // ignores a leading C0 control (and interior tab/newline) when it resolves a
  // link, so a byte like U+0001 before "javascript:" would otherwise sneak a live
  // scheme past the allow-list below — the string wouldn't start with [a-z], the
  // scheme check would miss it, and the URL would pass as "relative". Strip the
  // controls so we validate the SAME url the browser will act on.
  function mdSafeHref(url) {
    const u = String(url).replace(/[\x00-\x1F\x7F]/g, "").trim();
    if (!u) return null;
    const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(u);
    if (scheme) {
      const s = scheme[1].toLowerCase();
      if (s !== "http" && s !== "https" && s !== "mailto") return null;
    }
    return u.replace(/"/g, "&quot;"); // < > & are already entities from mdEscape
  }

  // Emphasis on a PLAIN text run — the scanner in mdInline has already carved out
  // code spans and links, so a '*' inside `code` or inside a link URL can never
  // pair with one outside it (no misnesting, no href injection).
  function mdEmph(t) {
    return t
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      // bold/italic require a non-space just inside the markers, so ordinary
      // prose like "2 * 3 * 4" or "SELECT *" is not italicised (CommonMark
      // flanking, applied to the '*' forms that collide with everyday text).
      .replace(/\*\*([^\s*](?:[^*]*?[^\s*])?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*/g, "$1<em>$2</em>");
  }

  // Inline rendering on already-escaped text. A left-to-right scan first carves
  // out the two constructs whose CONTENT must be protected from emphasis — an
  // inline-code span (emitted VERBATIM) and a link (URL validated by mdSafeHref,
  // never emphasis-processed) — replacing each with an opaque placeholder
  // (SENT + index + SENT, where SENT is a U+0000 built at runtime so the source
  // stays plain ASCII). Emphasis (**/*/~~) then runs ONCE over the reduced
  // string, so it can legitimately WRAP a code span or link (**`code`**) yet a
  // '*' INSIDE a code span, or in a URL, can never pair with one outside it.
  // Placeholders are restored last. renderMarkdown strips U+0000 from the input,
  // so the sentinel can't collide with model text; the label recurses (a strict
  // substring, so it terminates). Bounded label/URL lengths keep the link scan
  // linear on hostile input (a long run of unmatched '[' can't scan to
  // end-of-line at every '['), and the ']'-ahead memo stops link attempts once
  // no ']' remains.
  function mdInline(s) {
    s = String(s);
    const SENT = String.fromCharCode(0); // U+0000 placeholder delimiter
    const store = [];
    const stash = (html) => {
      store.push(html);
      return SENT + (store.length - 1) + SENT;
    };
    const linkRe = /\[([^\]]{0,999})\]\(([^)\s]{0,1999})\)/y; // sticky + bounded
    let acc = "";
    let i = 0;
    let noClose = s.length; // once a ']' search from here fails, none lies ahead
    while (i < s.length) {
      const ch = s[i];
      if (ch === "`") {
        // A run of K backticks opens a code span closed by the next run of
        // EXACTLY K backticks (CommonMark). This keeps an inline ```cmd``` from
        // being swallowed (and its text deleted) by the block-fence split, and
        // renders its content verbatim.
        let k = 1;
        while (s[i + k] === "`") k++;
        let close = -1;
        for (let j = i + k; j < s.length; ) {
          if (s[j] === "`") {
            let r = 1;
            while (s[j + r] === "`") r++;
            if (r === k) { close = j; break; }
            j += r;
          } else {
            j++;
          }
        }
        if (close !== -1) {
          let code = s.slice(i + k, close);
          // CommonMark: strip ONE flanking space unless the content is all spaces.
          if (code.length > 1 && code[0] === " " && code[code.length - 1] === " " && code.trim() !== "") {
            code = code.slice(1, -1);
          }
          acc += stash("<code>" + code + "</code>");
          i = close + k;
          continue;
        }
        acc += s.slice(i, i + k); // an unclosed backtick run -> literal backticks
        i += k;
        continue;
      } else if (ch === "[" && i < noClose) {
        if (s.indexOf("]", i + 1) === -1) {
          noClose = i; // no ']' anywhere ahead — stop attempting links entirely
        } else {
          linkRe.lastIndex = i;
          const m = linkRe.exec(s);
          if (m) {
            const href = mdSafeHref(m[2]);
            const label = mdInline(m[1]);
            acc += href
              ? stash('<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + "</a>")
              : stash(label); // unsafe/invalid URL -> keep the label as plain text
            i = linkRe.lastIndex;
            continue;
          }
        }
      }
      acc += ch;
      i++;
    }
    const restore = new RegExp(SENT + "(\\d+)" + SENT, "g");
    return mdEmph(acc).replace(restore, (m, n) => store[+n] || "");
  }

  // -- table (a '\|' inside a cell is a literal pipe) ----------------------
  function mdSplitCells(s) {
    const cells = [];
    let cur = "";
    for (let k = 0; k < s.length; k++) {
      const ch = s[k];
      if (ch === "\\" && s[k + 1] === "|") { cur += "|"; k++; continue; }
      if (ch === "|") { cells.push(cur); cur = ""; continue; }
      cur += ch;
    }
    cells.push(cur);
    return cells;
  }
  function mdSplitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return mdSplitCells(s).map((c) => c.trim());
  }
  // A GFM delimiter row: every cell is optional-colon + dashes + optional-colon.
  function mdIsDelimRow(line) {
    if (line.indexOf("-") === -1) return false;
    const cells = mdSplitRow(line);
    return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
  }
  function mdAlign(cell) {
    const c = cell.trim();
    const l = c.startsWith(":");
    const r = c.endsWith(":");
    if (l && r) return "center";
    if (r) return "right";
    if (l) return "left";
    return "";
  }
  function mdParseTable(lines, start) {
    const header = mdSplitRow(lines[start]);
    const aligns = mdSplitRow(lines[start + 1]).map(mdAlign);
    const ncol = header.length;
    let i = start + 2;
    const rows = [];
    while (i < lines.length && lines[i].trim() !== "" && lines[i].indexOf("|") !== -1) {
      rows.push(mdSplitRow(lines[i]));
      i++;
    }
    const cls = (idx) => (aligns[idx] ? ' class="md-al-' + aligns[idx] + '"' : "");
    const th = header.map((c, idx) => "<th" + cls(idx) + ">" + mdInline(c) + "</th>").join("");
    const body = rows
      .map((r) => {
        let tds = "";
        for (let idx = 0; idx < ncol; idx++) {
          tds += "<td" + cls(idx) + ">" + mdInline(r[idx] !== undefined ? r[idx] : "") + "</td>";
        }
        return "<tr>" + tds + "</tr>";
      })
      .join("");
    const html =
      '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
      th + "</tr></thead><tbody>" + body + "</tbody></table></div>";
    return { html, next: i };
  }

  // -- blockquote (the '>' marker is '&gt;' after mdEscape) ----------------
  function mdParseBlockquote(lines, start, depth) {
    const inner = [];
    let i = start;
    while (i < lines.length && /^\s*&gt;/.test(lines[i])) {
      inner.push(lines[i].replace(/^\s*&gt;\s?/, ""));
      i++;
    }
    // Recurse so a quote can hold paragraphs/lists and nested (>>) quotes; depth
    // is threaded through so mdFormatBlocks can stop before the stack overflows.
    return { html: "<blockquote>" + mdFormatBlocks(inner.join("\n"), depth + 1) + "</blockquote>", next: i };
  }

  // -- lists (ordered / unordered / nested / task) -------------------------
  function mdListLine(line) {
    const m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (!m) return null;
    const indent = m[1].replace(/\t/g, "  ").length;
    const ordered = /\d/.test(m[2]);
    let text = m[3];
    let task = false;
    let checked = false;
    const tm = /^\[([ xX])\]\s+(.*)$/.exec(text);
    if (tm) { task = true; checked = tm[1] !== " "; text = tm[2]; }
    return { indent, ordered, task, checked, text };
  }
  function mdParseList(lines, start) {
    const items = [];
    let i = start;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim() === "") {
        // A single blank between items keeps a "loose" list together.
        let j = i + 1;
        while (j < lines.length && lines[j].trim() === "") j++;
        if (j < lines.length && mdListLine(lines[j])) { i = j; continue; }
        break;
      }
      const li = mdListLine(line);
      if (li) { items.push(li); i++; continue; }
      // An indented non-marker line continues the previous item's text.
      if (items.length && /^\s+\S/.test(line)) {
        items[items.length - 1].text += " " + line.trim();
        i++;
        continue;
      }
      break;
    }
    return { html: mdBuildList(items), next: i };
  }
  // Fold the flat item list into a nested tree by indent width, then emit.
  function mdBuildList(items) {
    if (!items.length) return "";
    const root = { ordered: items[0].ordered, indent: items[0].indent, items: [] };
    const stack = [root];
    for (const it of items) {
      let top = stack[stack.length - 1];
      if (it.indent > top.indent && top.items.length && stack.length < MD_MAX_DEPTH) {
        const parent = top.items[top.items.length - 1];
        const child = { ordered: it.ordered, indent: it.indent, items: [] };
        parent.children = child;
        stack.push(child);
        top = child;
      } else {
        while (stack.length > 1 && it.indent < top.indent) {
          stack.pop();
          top = stack[stack.length - 1];
        }
      }
      top.items.push({ text: it.text, task: it.task, checked: it.checked, children: null });
    }
    return mdEmitList(root);
  }
  function mdEmitList(list) {
    const tag = list.ordered ? "ol" : "ul";
    const lis = list.items
      .map((it) => {
        const body = it.task
          ? '<span class="md-check">' + (it.checked ? "☑" : "☐") + "</span> " + mdInline(it.text)
          : mdInline(it.text);
        const child = it.children ? mdEmitList(it.children) : "";
        return "<li" + (it.task ? ' class="md-task"' : "") + ">" + body + child + "</li>";
      })
      .join("");
    return "<" + tag + ">" + lis + "</" + tag + ">";
  }

  // -- block driver: classify each line, consume multi-line blocks whole ---
  function mdFormatBlocks(segment, depth) {
    depth = depth || 0;
    const lines = segment.split("\n");
    const out = [];
    let para = [];
    const flushPara = () => {
      if (para.length) {
        out.push("<p>" + para.map(mdInline).join("<br>") + "</p>");
        para = [];
      }
    };
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim() === "") { flushPara(); i++; continue; }
      const h = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      if (h) {
        flushPara();
        out.push("<h" + h[1].length + ">" + mdInline(h[2]) + "</h" + h[1].length + ">");
        i++;
        continue;
      }
      if (line.indexOf("|") !== -1 && i + 1 < lines.length && mdIsDelimRow(lines[i + 1])) {
        flushPara();
        const t = mdParseTable(lines, i);
        out.push(t.html);
        i = t.next;
        continue;
      }
      if (/^\s*&gt;/.test(line) && depth < MD_MAX_DEPTH) {
        flushPara();
        const q = mdParseBlockquote(lines, i, depth);
        out.push(q.html);
        i = q.next;
        continue;
      }
      if (mdListLine(line)) {
        flushPara();
        const l = mdParseList(lines, i);
        out.push(l.html);
        i = l.next;
        continue;
      }
      para.push(line);
      i++;
    }
    flushPara();
    return out.join("");
  }

  // Escape first, carve out fenced code (kept verbatim, already escaped), then
  // format the prose blocks. Returns null on any error so chat.js can fall back
  // to plain textContent — a reply is never lost to a formatting bug.
  function renderMarkdown(text) {
    try {
      // Drop U+0000 up front so model text can't forge the mdInline placeholder
      // sentinel (which is a U+0000 built at runtime — see mdInline).
      const clean = mdEscape(text).split(String.fromCharCode(0)).join("");
      // A fenced block requires a newline after the opening ``` line; a same-line
      // ```...``` is an inline code span (handled in mdInline), not a block — so
      // its content is never eaten here.
      const parts = clean.split(/```[^\n]*\n([\s\S]*?)```/g);
      let html = "";
      parts.forEach((part, i) => {
        if (i % 2 === 1) {
          html += '<pre class="cell-code"><code>' + part.replace(/\n+$/, "") + "</code></pre>";
        } else {
          html += mdFormatBlocks(part);
        }
      });
      return html;
    } catch (_e) {
      return null;
    }
  }

  return {
    COMMANDS,
    parseSlash,
    unescapeSlash,
    filterCommands,
    isSlashTyping,
    trustedModelOptions,
    trustedModelsFromEnumOptions,
    chooseTrustedModel,
    trustedRoutingAvailable,
    routingProfileKind,
    trustedModelPhrase,
    chooseRoutingPreference,
    routingNotice,
    privacyChrome,
    routingChangeAllowed,
    latestRequestGate,
    routingExpectationMatches,
    generalModelRelevant,
    notebookPreferenceKey,
    safeStorageGet,
    safeStorageSet,
    readValidNotebookOverride,
    chooseNotebookOverride,
    chooseNotebookTrustedOverride,
    chooseNotebookRoutingOverride,
    effectiveRoutingPreference,
    routingSettingValueAllowed,
    resolvedRoutingValuesValid,
    resolvedRoutingMatchesRequest,
    notebookOverridePayload,
    explainPrompt,
    explainLabel,
    reviewPrompt,
    reviewLabel,
    checksPrompt,
    checksLabel,
    sqlPrompt,
    sqlLabel,
    investigatePrompt,
    investigateLabel,
    notesCellPrompt,
    HistoryRing,
    mentionMatch,
    filterDatasets,
    applyMention,
    additiveBlockLines,
    diffLines,
    piiBadge,
    tracebackHoldSummary,
    scanErrorMessage,
    gateFromResponse,
    gateIsFloor,
    gateFindingItems,
    gateFindingRows,
    gateHoldSummary,
    gateHoldWording,
    MAX_FIX_ATTEMPTS,
    applyFailureAction,
    applyFixPrompt,
    codeFindingRows,
    codeFindingLead,
    codeFindingTag,
    receiptHeadline,
    receiptObservation,
    receiptObservationFull,
    receiptObservationTruncated,
    receiptSequence,
    revertScope,
    revertedNotice,
    receiptDisplacedNote,
    RECEIPT_REVERTED_NOTE,
    RECEIPT_MAYBE_REVERTED_NOTE,
    cancelledNotice,
    cancelEventAction,
    noticeMessageAction,
    toolDoneMark,
    autoApplyBanner,
    helpRows,
    helpKeys,
    eventNoteText,
    runReportNote,
    applyFailedNote,
    streamResumedNotice,
    stopButtonState,
    stopOutcomeNotice,
    canStopTurn,
    turnEndOutcome,
    effortKey,
    adoptLegacyEffort,
    chooseEffort,
    parseDeviceLogin,
    highlightCode,
    renderMarkdown,
    cleanJobs,
    PY_KW,
  };
})();

// Expose to consumers by BOTH supported paths. A top-level `const` is a global LEXICAL
// binding — reachable as the bare identifier `ChatCore` (how chat.js and app.js use it)
// but NOT a property of `window`, so `window.ChatCore` would otherwise be undefined.
// Mirroring it onto `window` is belt-and-suspenders: it stops a `window.ChatCore.*` call
// silently throwing (the uncaught TypeError that once broke the batch builder's
// "Add to queue"). Guarded so the Node test runner (no `window`) still require()s cleanly.
if (typeof window !== "undefined") window.ChatCore = ChatCore;
if (typeof module !== "undefined" && module.exports) module.exports = ChatCore;
