---
icon: lucide/shield-check
---

# Push guard: scan every push for secrets, PII, and raw data

!!! success "Status: implemented"
    All four phases shipped 2026-07-02: the orchestrator
    (`src/mooring/pushguard.py` — detectors stay in `ai/`), the injected
    `guard_fn` withhold seam in `sync.push`/`propose`, warn-and-confirm at both
    adapters (per-file confirm tokens binding findings to bytes; `mooring scan`
    + `--acknowledge-findings` on the CLI), `[guard] push = "block"` escalation
    in the synced `mooring.toml`, and recall-last-push (`sync.recall()`, the
    manifest's `last_push` record, `mooring recall`, `/api/recall` + the hub's
    Recall button). One divergence from the sketch: `pushguard` is *not* added
    to the `frozen-core-is-lean` lint contract — `ai/pii.py`'s optional NER
    hooks give it a static (lazily executed) path to spaCy that the
    no-indirect contract would count; the guard is adapter-layer surface, not
    frozen-core surface. `test_egress.py`/`test_pii.py`/`test_secrets.py`
    pass unmodified, as pinned.

## Problem

All of mooring's privacy machinery watches the AI channel: `ai/egress.py` is the
single choke point for what a model sees, `ai/pii.py` and `ai/secrets.py` scan
text before it leaves for Copilot. Nothing watches the more damaging channel —
the push itself. An analyst who hardcodes a warehouse password into a cell,
pastes a client email list into a notebook, or drops a 300k-row customer CSV
into `data/` and clicks **Push** has just published it to an org-readable repo,
where it sits in git history indefinitely.

Because analysts have no git, mooring is the *only* write path into the shared
repo. A client-side gate at mooring's push seam therefore covers **100% of the
team's pushes** — a claim no git-hook or server-side scanning setup can make
for this audience, since there is no other client to bypass it with. The
scanners, the value-free findings contract, and the warn-and-confirm UX all
already exist and are tested; this plan is mostly wiring them to a second seam.

The gap is felt hardest by admins: they enabled the AI guard, read
[ai-privacy](../../admins/ai-privacy.md), and reasonably assume "mooring scans
what leaves the machine" — but today that is only true for the AI channel.

## Design

At push and propose time, every outgoing candidate file that looks like text is
scanned with the existing detectors plus one new heuristic:

- **Secrets** — `mooring.ai.secrets.scan()`: private key blocks, cloud/API
  tokens, connection strings with embedded credentials. High precision by
  design.
- **Structured PII** — `mooring.ai.pii.scan()`: checksum-validated payment
  cards / IBANs / NHS numbers, plus shape-anchored emails and NINOs. Findings
  are value-free `(line, kind)` pairs, never the matched substring.
- **Raw-data heuristic** (new) — "this looks like a data export, not a
  notebook": a conservative row-count check on delimiter-consistent tabular
  text (e.g. a CSV with thousands of consistent data rows). This complements
  the existing size limits in `sync._read_checked()` (`cfg.warn_file_mb` /
  `cfg.max_file_mb`), which catch big files but say nothing about content.

User-visible behaviour:

- **Hub**: clicking Push/Propose (`pushAction` / `proposeAction` or a row
  action in `src/mooring/hub/static/app.js`) scans first. Findings come back as
  a `409` with `needs_confirm` — the same idiom `api_set_settings` in
  `src/mooring/hub/server.py` already uses — listing value-free
  `(file, line, kind)` rows. The user either fixes the file, suppresses a
  reviewed false positive with a per-line pragma, or clicks **Push anyway**,
  which re-POSTs with a confirm token bound to the exact findings set (so a
  *new* finding invalidates a stale confirm, like the token check in
  `api_undo`).
- **CLI**: `mooring push` / `mooring propose` print the findings and exit 1;
  re-running with a new `--acknowledge-findings` flag pushes anyway. A new
  `mooring scan` command runs the same scan over the current push candidates
  without pushing — the push-scoped sibling of `mooring ai pii check`
  (`cmd_ai_pii_check` in `src/mooring/cli.py`).
- **Per-finding suppression**: `ai/pii.py` already skips lines carrying its
  `SUPPRESS_MARKER` comment (`mooring: pii-ok`). The guard adds a sibling
  push-scope marker (`mooring: push-ok`) handled in the new orchestrator —
  deliberately *not* inside `ai/secrets.py`, so AI-egress behaviour stays
  byte-identical.
- **Escalation**: the synced workspace `mooring.toml` (read by
  `src/mooring/workspace_config.py`) gains a `[guard]` section with
  `push = "warn"` (default) or `"block"`. In block mode there is no
  "Push anyway" — the finding must be fixed or pragma-suppressed. There is
  deliberately **no global off switch** and no per-machine override: the
  pragma is the only off-ramp, per finding, visible in the diff.

**Companion: recall last push.** After a bad push, "get it off the branch, now"
matters more than blame. At push time `sync._push_candidate()` knows each
file's pre-push remote SHA (`f.base_sha`) and the newly written SHA; the guard
records them (see the adaptation note below), and a new **Recall last push**
action writes the prior blob back via the Contents API — `put_file(...,
base_sha=<new sha>)` — so if a teammate has pushed since, GitHub rejects it and
the conflict is loud, exactly like a normal stale write (see
[how conflicts work](../../users/conflicts.md)). A file *created* by the push
is recalled with `delete_file()`. The UX is honest: **git history still
retains the recalled commit** — a leaked secret must still be rotated; recall
only stops the bleeding on the branch head.

!!! warning "Adaptation from the ideation sketch"
    The sketch claimed "the manifest knows the pre-push remote SHA". It does
    not — after a push, `_push_candidate()` overwrites `mft.files[f.path]`
    with the *new* SHA and the base is gone. Recall therefore needs a new
    `last_push` section persisted in `.mooring/manifest.json`
    (`src/mooring/manifest.py`), recorded during `_finalize_push()` and
    replaced wholesale on every push (only the *last* push is recallable —
    that is the promise in the name).

## Architecture fit

- **Detectors stay in `ai/` (L3)** — `ai/pii.py` and `ai/secrets.py` are
  stdlib-only, import fine without the `copilot` extra, and move nowhere. This
  was the explicit fork in the sketch (keep in L3 vs. move down a layer);
  keeping them put means zero change to the AI-egress story and no churn in
  `docs/admins/ai-privacy.md` beyond an additive paragraph.
- **New module `src/mooring/pushguard.py`** — the orchestrator: candidate
  file-walk policy, text-extension allowlist, the raw-data heuristic, the
  `mooring: push-ok` marker filter, and the findings-set confirm token. It
  imports `mooring.ai.pii` / `mooring.ai.secrets` and nothing above them —
  the same "one tested home, both adapters reuse it" reasoning as
  `mooring.ai.scan.scan_pii_targets()`. It lives *beside* `ai/`, not inside
  it, because the guard must work with no copilot configured.
- **`.importlinter`** is extended, not bent: add `mooring.pushguard` to the
  `forbidden_modules` of the `identity-below-domain` and `sync-domain-is-core`
  contracts, and add it as a source beside `mooring.ai` in the
  `ai-below-adapters` contract so it can never import `hub` or `cli`. No
  existing contract is weakened.
- **`sync.py` never imports the scanners.** Enforcement inside the push loop
  uses the injected-callable idiom `sync.revert()` already established with
  `snapshot_fn` ("passed in rather than imported"): `sync.push()` and
  `sync.propose()` gain an optional `guard_fn(rel_path, data) -> findings`
  parameter, called on exactly the bytes `_read_checked()` produced for
  upload. A file with unacknowledged findings is *withheld* with a result line
  (the `refused`/`warning` line pattern), never silently. The L2 core stays
  AI-free; the `sync-domain-is-core` contract holds.
- **L4 wires it together** — legal in both directions: `cli.py` and
  `hub/server.py` may import L2 `sync` and the L3 scanners/orchestrator.
- **`ai/egress.py` is untouched.** The push guard is a *second consumer* of
  the scanners, not a change to the sanctioned AI channel; the pinned
  `tests/test_egress.py` suite must pass unmodified.

New surface: `src/mooring/pushguard.py`; `mooring scan` and `mooring recall`
CLI commands; `/api/recall` hub endpoint; a `[guard]` section in the synced
`mooring.toml`; a `last_push` manifest section; `sync.recall()`.

## Implementation plan

1. **Scan core + `mooring scan`** (M) — shippable alone as a lint.
    - New `src/mooring/pushguard.py`: `scan_bytes(rel_path, data)` (decode,
      extension allowlist, size cap, run `pii.scan` + `secrets.scan`, filter
      `mooring: push-ok` lines), the raw-data heuristic, and
      `scan_candidates(workspace, report)` walking
      `report.by_state(*sync.PUSH_STATES)`.
    - Extend `.importlinter` as described above.
    - Add the `mooring scan` subparser and `cmd_scan` in `src/mooring/cli.py`,
      printing `(path, line, kind)` rows like `cmd_ai_pii_check` does, exit 1
      on findings.
2. **Warn-and-confirm at both seams** (M).
    - Add the optional `guard_fn` parameter to `sync.push()` / `sync.propose()`
      (threaded into the `_push_candidate()` loop next to `_read_checked()`),
      with withheld-file result lines.
    - `cmd_push` / `cmd_propose`: pre-scan, print findings, exit 1; wire
      `--acknowledge-findings` into the `push`/`propose` parsers.
    - `api_push` / `api_propose` in `src/mooring/hub/server.py`: scan before
      `_sync_op`; on findings return
      `409 {"needs_confirm": true, "findings": [...], "token": <hash>}` where
      the token is a hash over the sorted findings set + content SHAs
      (stateless — recomputed and compared on the confirmed request).
    - `src/mooring/hub/static/app.js`: teach the shared `action()`/push path
      to render the findings list and a **Push anyway** re-POST carrying the
      token; factor the findings-row formatter as a pure helper for JS tests.
3. **Block escalation** (S).
    - `workspace_config.py`: a `guard_mode(workspace)` reader for `[guard]
      push`, following the fail-open `_read_data` read-side idiom (malformed
      file → `"warn"`, never a wedged team); update the module docstring,
      which currently promises "PATHS only".
    - Honour it in both adapters: block mode refuses the confirm token /
      `--acknowledge-findings`. Document in
      [configuration](../../admins/configuration.md).
4. **Recall last push** (M).
    - `manifest.py`: persist `last_push` (`path -> {prev, new}`, plus branch
      and timestamp) in `load`/`save`; record it in `sync._finalize_push()`
      from data `_push_candidate()` already has.
    - New `sync.recall(client, cfg)`: per file `get_blob(prev)` +
      `put_file(..., base_sha=new)`, or `delete_file(...)` when `prev` is
      absent; `RemoteConflict` → a loud "cannot recall — remote moved" line.
    - `cmd_recall` + parser in `cli.py`; `/api/recall` route + handler in
      `hub/server.py` (through `_sync_op`); a confirm dialog in `app.js`
      stating plainly that git history retains the commit.

## Testing

All offline — GitHub is mocked with the `FakeClient` stub from
`tests/conftest.py` (as in `tests/test_sync.py`) or, at the HTTP layer, with
the `responses` library (as in `tests/test_github.py`).

- **New `tests/test_pushguard.py`** — detector orchestration, the extension
  allowlist, the raw-data heuristic on synthetic CSVs, `mooring: push-ok`
  suppression, and the invariant pin: plant a `SECRET_VALUE_DO_NOT_LEAK`-style
  value in a candidate and assert it appears in **no** finding, result line,
  or JSON payload (findings stay value-free), and that scanned bytes are never
  modified (the guard reads, never rewrites).
- **Extend `tests/test_sync.py`** — `guard_fn` withholds a file (no
  `put_file` call reaches the `FakeClient`); `last_push` recorded on push;
  `sync.recall()` happy path, created-file delete path, and the
  `RemoteConflict` stale path.
- **Extend `tests/test_manifest.py`** — `last_push` round-trips through
  `load`/`save` and old manifests without it still load.
- **Extend `tests/test_hub.py`** — `/api/push` returns the 409
  `needs_confirm` shape; a stale token (findings changed) re-409s; block mode
  refuses the token; `/api/recall` response shape.
- **Extend `tests/test_workspace_config.py`** — `[guard] push` parsing,
  fail-open on malformed TOML.
- **New CLI tests** (pattern of `tests/test_cli_rollback.py`) — `mooring
  scan` exit codes, `--acknowledge-findings`, `mooring recall`.
- **Pinned unchanged**: `tests/test_egress.py`, `tests/test_pii.py`,
  `tests/test_secrets.py` must pass without edits — proof the AI channel's
  behaviour did not move.
- **JS** (`node --test tests/js/`) — cover the pure findings-row formatter
  and the confirm-token re-POST decision logic.

## Risks and mitigations

- **False positives train reflexive "Push anyway" clicks.** The corrosion risk
  the detectors were already designed around (see the precision-over-recall
  notes in `ai/pii.py`). Mitigations: only the existing high-precision
  patterns plus a deliberately conservative row-count heuristic; per-finding
  line pragmas as the sanctioned off-ramp; the confirm token binds to the
  findings set so a *new* finding is never covered by an old confirm; and no
  config key that auto-confirms.
- **Push latency.** Only push candidates are scanned (already the changed-file
  set from `_gather_candidates()`), only text-like extensions, with a per-file
  size cap; `guard_fn` runs on bytes already read for upload, so no extra IO.
  `ai/pii.py`'s `_MAX_LINE` bound already protects against pathological lines.
- **Layering drift / privacy-story confusion.** Decided explicitly: detectors
  stay in `ai/` (L3), orchestration lives in the new `pushguard.py`,
  `.importlinter` contracts are extended, and `ai/egress.py` plus its pinned
  tests are untouched. `docs/admins/ai-privacy.md` gets an *additive*
  paragraph ("the same scanners also watch the push channel") — it must not
  imply the push guard is a guarantee; like the AI scanners it is best-effort
  defence in depth.
- **Recall over-promising.** The commit stays in git history; the UI and CLI
  say so every time, and the docs say "rotate the secret anyway". Only the
  last push is recallable; anything older is
  [version history](version-history.md)'s job.
- **A wedged team in block mode.** Read-side fail-open (malformed
  `mooring.toml` → warn mode) plus the pragma off-ramp mean block mode can
  never make a repo unpushable without a visible, diffable cause.

## Dependencies and sequencing

- **No prerequisites** — phase 1 is pure new surface; phases 2–4 ride existing
  seams. This page is the flagship of the roadmap's "mooring is the only door
  out" thread (see the [roadmap overview](index.md)).
- "Recall last push" (phase 4) lives here by design; it complements the
  *local* restore actions that already shipped (`sync.revert()`, the hub's
  `/api/rollback` + `/api/undo`) and the deeper time machine planned in
  [version history](version-history.md).
- [Review my changes](review-my-changes.md) shares the pre-push moment: its
  cell-aware diff is the natural place to *show* guard findings in context
  later, but neither plan blocks the other.
- [Local safety net](local-safety-net.md) is the local-side twin of this
  remote-side gate; together they complete "nothing silently lost, nothing
  silently leaked".
- The [traceback fixer](traceback-fixer.md) reuses the same scanners on the AI
  channel via the egress choke point — a precision fix to a detector benefits
  both, which is another reason the detectors stay in one place.
- A future publish/delivery feature (rejected for now — see
  [the roadmap overview](index.md)) should be gated on this guard existing.
