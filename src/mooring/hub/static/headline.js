"use strict";

// Pure, DOM-free derivation of the hub's headline block — the one sentence that
// replaced the stack of status notices, plus the single primary action and the
// mono text links beside it. Loaded BEFORE app.js (exposes `Headline` as a bare
// global + on `window`, the files_tree.js idiom); under Node it is require()d by
// tests/js. Nothing here touches the DOM, network, or storage.
//
// The whole point of the chart-room header is that ONE fact about the workspace
// leads, so the priority order below is the product decision and is unit-tested:
// offline outranks a conflict, a conflict outranks incoming work, incoming work
// outranks your own outgoing work, and "everything is in sync" is what a clean
// workspace says. First match wins — a workspace with a conflict AND changes to
// push says the conflict, because that is the one that blocks the other.

const Headline = (function () {
  // Small counts read as words in a sentence ("Two updates came in"), the way the
  // repo's other copy does; beyond that a digit is clearer than a long word.
  const WORDS = ["zero", "one", "two", "three", "four", "five", "six",
                 "seven", "eight", "nine", "ten", "eleven", "twelve"];
  function count(n) {
    return n >= 0 && n < WORDS.length ? WORDS[n] : String(n);
  }
  function Count(n) {
    const w = count(n);
    return w.charAt(0).toUpperCase() + w.slice(1);
  }

  const PUSH_STATES = new Set(["modified", "new local", "deleted locally"]);
  const PULL_STATES = new Set(["remote changed", "new remote", "deleted remotely"]);

  function tally(files) {
    let conflicts = 0;
    let toPull = 0;
    let toPush = 0;
    for (const f of files || []) {
      if (f.state === "conflict") conflicts++;
      else if (PULL_STATES.has(f.state)) toPull++;
      else if (PUSH_STATES.has(f.state)) toPush++;
    }
    return { conflicts, toPull, toPush };
  }

  // Link ids the hub knows how to wire. Kept as ids (not handlers) so this module
  // stays DOM-free and the wiring lives in one place in app.js.
  const NEW = { id: "new", label: "new notebook" };
  const SEARCH = { id: "search", label: "search" };
  const PULL = { id: "pull", label: "pull" };
  const PROPOSE = { id: "propose", label: "propose" };

  /**
   * The header block for the current workspace.
   *
   * @param {object} s
   *   mode              "repo" | "local"
   *   loggedIn          boolean
   *   offline           truthy when /api/state carried an `offline` payload
   *   files             the /api/state rows
   *   review            state.review ({branch, compare_url}) or null
   *   pullWaitedAtStart the incoming updates were already there on this session's
   *                     first render — i.e. they predate you sitting down
   *   morning           this session started before noon (local clock)
   * @returns {{text: string, primary: ?{id, label}, links: Array}}
   */
  function derive(s) {
    const st = s || {};
    const { conflicts, toPull, toPush } = tally(st.files);

    // Before anything about sync: a workspace with no repo, and the login wall.
    if (st.mode === "local") {
      return {
        text: "This workspace is local — nothing here is shared yet.",
        primary: { id: "new", label: "+ New notebook" },
        links: [SEARCH],
      };
    }
    if (!st.loggedIn) {
      return {
        text: "Sign in to GitHub to sync with your team.",
        primary: null,
        links: [],
      };
    }

    // Offline: every network action is already hidden (fileActions does the same),
    // so the headline says why rather than offering something that cannot work.
    if (st.offline) {
      return {
        text: "GitHub is unreachable — this is your last synced view.",
        primary: null,
        links: [NEW, SEARCH],
      };
    }

    if (conflicts > 0) {
      return {
        text: conflicts === 1
          ? "One notebook needs you to resolve a conflict."
          : `${Count(conflicts)} notebooks need you to resolve conflicts.`,
        primary: { id: "resolve", label: "Resolve" },
        links: [PULL, NEW, SEARCH],
      };
    }

    if (toPull > 0) {
      // "overnight" is a claim about WHEN the work arrived, so it is only made when
      // the updates were already waiting when this session started AND that was in
      // the morning. Otherwise they are simply waiting — which is always true.
      const overnight = !!st.pullWaitedAtStart && !!st.morning;
      const verb = toPull === 1 ? "update" : "updates";
      const text = overnight
        ? `${Count(toPull)} ${verb} came in from your team overnight.`
        : `${Count(toPull)} ${verb} ${toPull === 1 ? "is" : "are"} waiting from your team.`;
      const links = [NEW];
      if (toPush > 0) links.push({ id: "push", label: `push all · ${toPush}` });
      links.push(SEARCH);
      return { text, primary: { id: "pull", label: "\u2193 Pull" }, links };
    }

    if (toPush > 0) {
      return {
        text: toPush === 1
          ? "One of your changes is ready to push."
          : `${Count(toPush)} of your changes are ready to push.`,
        primary: { id: "push", label: "\u2191 Push all" },
        links: [NEW, PROPOSE, SEARCH],
      };
    }

    if (st.review && st.review.branch) {
      return {
        text: `Your proposal on ${st.review.branch} is waiting for review.`,
        primary: { id: "review-pr", label: "View pull request" },
        links: [NEW, SEARCH],
      };
    }

    return {
      text: "Everything here is in sync with your team.",
      primary: { id: "new", label: "+ New notebook" },
      links: [SEARCH],
    };
  }

  return { derive, count, tally };
})();

if (typeof window !== "undefined") window.Headline = Headline;
if (typeof module !== "undefined" && module.exports) module.exports = Headline;
