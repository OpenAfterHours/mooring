---
icon: lucide/stethoscope
---

# mooring doctor: diagnostics, will-it-run checks, open-failure triage

!!! note "Status: proposed"
    Designed 2026-07 from a multi-agent ideation review; not yet implemented.
    Scope may change — in particular the later slices (per-notebook checks,
    open-failure triage) may ship long after the first.

## Problem

Mooring's users are non-developers on locked-down Windows laptops. When a
rollout fails — a TLS-intercepting corporate proxy, antivirus quarantining the
extracted site-packages, a revoked token, Python too old, uv missing — they
cannot triage any of it. The failure arrives at the admin as "mooring is
broken", with no way to tell a proxy problem from an expired login. The same is
true per notebook: "this worked last week" can mean a missing package, a
notebook named `polars.py`, a UTF-8 BOM, or a teammate's change — all
indistinguishable from the outside.

For a solo-maintainer project this support load is the growth ceiling. And only
mooring can shorten it: it alone holds the cross-artifact context — the locked
env (`pyproject.toml` + `uv.lock`), the sync manifest, the frozen-vs-uv
delivery mode, the GitHub host and token, and the notebook source — so it alone
can say *which* of the usual suspects actually applies here.

Pieces of the answer already exist, scattered: `mooring selftest`
(`cmd_selftest` in `src/mooring/cli.py`) verifies the bundled packages,
PYTHONPATH, and TLS trust mode offline; `mooring ai pii doctor`
(`cmd_ai_pii_doctor`) already demonstrates the house style — plain-English
pass/fail lines, a curated fix per finding, a meaningful exit code. What is
missing is the umbrella: one command and one hub button that runs everything,
in plain English, and produces a report safe to paste into a ticket.

## Design

Three slices of one diagnosis engine, shipped in this order.

**(a) `mooring doctor` + hub health check.** A new top-level `mooring doctor`
command and a "Health check" button on the hub run roughly ten probes, each
reporting **pass / warn / fail / unknown** with a one-line curated fix:

1. Python version and delivery mode (frozen bundle vs `uv`, via
   `editor.uses_uv()` / `pyproject_env.uv_available()`).
2. Bundled runtime imports (reusing `SELFTEST_PACKAGES` from
   `src/mooring/runtime.py`) and the child-process PYTHONPATH bridge
   (`cli._ensure_child_pythonpath()` — marimo's site dir must be on
   `PYTHONPATH` or notebook kernels can't import the bundled stack).
3. GitHub host reachability with a short timeout, including TLS: an
   `SSLError` against `githost.api_root(cfg.host)` with the OS trust store
   active (`cli._inject_truststore`, `MOORING_TRUSTSTORE`) points at a
   corporate MITM proxy whose root CA isn't installed — the fix line says
   exactly that, for the ticket.
4. Token validity and repo access: `GitHubClient.get_user()` then
   `get_branch_head(cfg.branch)`; the typed errors in `src/mooring/github.py`
   (`AuthFailed`, `NotFound`, `RateLimited`) map one-to-one onto curated
   fixes ("log in again" / "you don't have access to owner/repo, ask the
   admin" / "rate-limited, wait").
5. Config and manifest integrity: `config.load_app_config()` (`ValueError` on
   a malformed host), `manifest.load()` (corrupt `manifest.json`), and the
   workspace `mooring.toml` (`TOMLDecodeError`, as `cmd_adopt` already
   handles).
6. Deps-lock state: `pyproject.toml` present, `uv.lock` present and not stale,
   `pyproject_env.missing_deps()` empty on the frozen path.
7. Workspace placement hints (`runtime.workspace_hint` — legacy location,
   cloud-synced folder).
8. Copilot availability when `[ai]` is enabled, via
   `provider.status(force=True)` (`ProviderStatus.available/connected` in
   `src/mooring/ai/base.py`) — added by the adapter, not the engine (see
   below).

The CLI prints the probe lines and exits 1 if anything failed. Both surfaces
offer a **"Copy report"** block: the same findings, passed through a redaction
step so it is safe to paste into a ticket — no tokens (structurally absent:
probes return curated strings, never raw exception dumps), no enterprise
hostnames, no usernames, home directory collapsed to `~`.

**(b) "Will it run here?" per-notebook static check.** Deterministic, no AI: a
notebook's top-level imports (AST, the way `shadow._top_level_imports` already
parses them) cross-referenced against the repo's declared deps and `uv.lock`;
a UTF-8 BOM check (marimo rejects BOM-prefixed notebooks); the existing shadow
guard (`shadow.folder_shadows` / `root_shadows`) and module sniff
(`notebook_template.opens_as_notebook`). Surfaced as `mooring doctor
<notebook.py>` and an on-demand **Check** row action in the hub — *not* a badge
recomputed on every `/api/state` poll (see Architecture fit). Import names that
can't be mapped to a distribution (`sklearn` vs `scikit-learn` — the divergence
`pyproject_env.importable_names` already documents) report **unknown**, never
"broken".

**(c) Open-failure triage cards.** Today a marimo boot failure reaches the hub
as `Could not start the editor: marimo exited during startup (code 1).`
(`Hub._open` in `src/mooring/hub/server.py`), because `editor.py` launches the
subprocess without capturing stderr. Slice (c) captures stderr to a log file,
attaches the tail to `EditorError`, and pattern-matches known failure modes —
`ModuleNotFoundError` (cross-referenced to the lockfile: "declared but not
bundled" vs "not declared at all"), `SyntaxError` in a startup-imported file,
a port bind failure, a shadow hit — swapping the dead-tab error for a
one-sentence card that offers **existing** recoveries: Revert
(`/api/rollback`), rename (shadow), `mooring deps add`. Anything unmatched gets
a generic card with the Copy-report block. Note an honest narrowing versus the
original sketch: BOM and most in-notebook errors surface inside the marimo tab
(the server boots fine), so they are caught by the *pre-open* checks of slice
(b); the stderr classifier covers server-boot failures only.

## Architecture fit

- **New L2 module `src/mooring/doctor.py`** — the diagnosis engine: a
  `ProbeResult` dataclass (`id`, `title`, `status`, `detail`, `fix`),
  `run_probes()`, `check_notebook()`, `classify_editor_failure()`, and
  `redact()`. It imports downward only: `config`/`auth`/`github`/`githost`
  (L1/L0), `manifest`/`pyproject_env`/`editor` (L2), `shadow`/`paths` (L0).
  Direct `requests` use for the reachability probe is fine — the
  `marimo-internals-isolated` contract in `.importlinter` bans raw HTTP only
  for `mooring.ai`.
- **The Copilot probe cannot live in the engine**: `ai/` is L3, above L2, so
  `doctor.py` importing `mooring.ai` would be a backwards import. The adapters
  (L4 `cli.py` and `hub/`) may import both, so each appends the Copilot probe
  to the engine's list — the same adapter-orchestration pattern the proposed
  [push guard](push-guard.md) uses for the scanners at its push seam.
- **A new `.importlinter` contract** pins this: source `mooring.doctor`,
  forbidden `mooring.ai`, `mooring.hub`, `mooring.cli`. (It cannot join
  `sync-domain-is-core`, which also forbids `mooring.editor` — doctor
  legitimately needs the editor.)
- **Touched modules**: `cli.py` (new subcommand + dispatch), `hub/server.py`
  (new endpoints), `hub/static/app.js` + `index.html` (health panel, row
  action), `editor.py` (stderr capture, slice c), `pyproject_env.py` (a new
  `locked_names()` reading `uv.lock`).
- **New surfaces**: CLI `mooring doctor [path]`; hub `POST /api/doctor` and
  `POST /api/doctor/notebook`. No new config keys.
- **Never blocks hub startup**: probes run only on demand, off the event loop
  (`asyncio.to_thread`, as `api_undo` already does), with short per-probe
  network timeouts. `run_hub()`'s existing `warmup()` is untouched.
- `mooring selftest` stays as-is (it is the offline build-verification subset,
  used in frozen-build smoke tests); `doctor` reuses its pieces rather than
  replacing it. `mooring ai pii doctor` also stays — `doctor` links to it in
  its Copilot probe's fix line rather than duplicating it.

## Implementation plan

**Phase 1 — engine + `mooring doctor` CLI (M).** Independently shippable; this
is the slice that cuts ticket load.

1. Create `src/mooring/doctor.py`: `ProbeResult`, `run_probes(cfg, app_cfg,
   extra_probes=())`, `render_lines()` (the plain-text house style
   `shadow.warning_lines` and `cmd_ai_pii_doctor` established), and
   `redact(text, cfg)`.
2. Implement probes 1–7 above from the existing seams: `runtime.SELFTEST_PACKAGES`,
   `cli._truststore_disabled` (move the env-var check into `doctor.py` so the
   hub can reuse it without importing the CLI), `githost.api_root`,
   `auth.get_token`, `GitHubClient`, `manifest.load`, `pyproject_env`.
3. Add the parser entry in `cli._build_parser()` (with the shared `--repo`
   argument), `cmd_doctor(app_cfg, cfg, args)`, a `_dispatch` branch, and a
   `telemetry.log_event("doctor", ...)` with pass/warn/fail counts only.
4. Add the `.importlinter` contract for `mooring.doctor`.

**Phase 2 — hub health check + Copy report (S).**

1. Add `Hub.api_doctor` and a `Route("/api/doctor", hub.api_doctor,
   methods=["POST"])` in `create_app()`; run the engine via
   `asyncio.to_thread`, appending the Copilot probe when
   `self.app_cfg.ai_enabled`.
2. Add a "Health check" control to `hub/static/index.html` and a render
   function in `app.js` (a panel of pass/warn/fail rows plus a Copy-report
   button using the clipboard pattern the existing `copilot-copy` handler
   uses).

**Phase 3 — will-it-run static check (M).**

1. Add `pyproject_env.locked_names(workspace)` parsing `uv.lock` `[[package]]`
   names with `tomllib`; keep `declared_deps()` as the fallback when the lock
   is absent.
2. Add `doctor.check_notebook(workspace, rel_path, ...)`: AST import scan,
   stdlib exclusion via `sys.stdlib_module_names`, lock/declared/bundle
   cross-reference (three-way by delivery mode: `uses_uv` vs
   `missing_deps`), BOM sniff, `opens_as_notebook`, and the shadow guard with
   the `(extra, ignore)` policy the adapters already assemble
   (`cli._shadow_policy` / `Hub._shadow_policy` — pass them in, keeping the
   engine policy-free like `shadow.scan`).
3. CLI: `mooring doctor <path>` runs the per-notebook check. Hub: a
   `POST /api/doctor/notebook` endpoint plus a **Check** entry in
   `fileActions()` in `app.js` (rendered through the existing `actionsMenu`
   dropdown). Cache results keyed by `(path, mtime_ns)` alongside the hub's
   existing `_notebook_cache` pattern — this is an adaptation of the sketch's
   "hub badge": a badge recomputed per `/api/state` poll would AST-parse every
   notebook on every refresh, so the check is on-demand with cached results
   instead.
4. Deferred from this phase: marimo-graph undefined-name analysis (needs a
   marimo import — legal outside `ai/`, but heavy and version-sensitive;
   revisit once the cheap checks prove trustworthy) and referenced-path
   existence booleans (high false-positive risk on dynamic paths).

**Phase 4 — open-failure triage cards (M).**

1. In `editor.py`, redirect the subprocess's stderr to a per-launch file under
   `paths.user_log_dir()`; on failure, `_wait_ready()` / `ensure_started()`
   attach the last ~40 lines to `EditorError` as a structured field.
2. Add `doctor.classify_editor_failure(stderr_tail, workspace)` returning
   `(kind, sentence, recovery)` for the known modes, with a generic fallback.
3. In `Hub._open`, replace the bare 502 string with `{error, card}` where
   `card` names the recovery; in `app.js` `openAction()`, render the card with
   buttons wired to the existing actions (`/api/rollback`, `/api/reveal`,
   the deps hint). `cmd_open` in `cli.py` prints the same sentence.

## Testing

Offline throughout — GitHub is mocked with the `responses` library, as in
`tests/test_github.py` and `tests/test_auth.py`.

- **New `tests/test_doctor.py`**: each probe against mocked outcomes (401 →
  the `AuthFailed` fix line, 404 → repo-access, connection error vs `SSLError`
  → proxy/CA wording, corrupt `manifest.json`, stale lock); `check_notebook`
  fixtures (BOM file, unmapped import → **unknown**, shadowed name);
  `classify_editor_failure` against canned stderr samples, including an
  unrecognized one that must yield the generic card.
- **Redaction pinned tests**, in the style of the `SECRET_VALUE_DO_NOT_LEAK`
  privacy tests: build a report with a sentinel token in the environment, a
  sentinel GHE hostname, and a sentinel username, and assert none of the three
  appears anywhere in the Copy-report text. These are the tests that make the
  "safe to paste" claim honest.
- **Extend `tests/test_hub.py`**: `/api/doctor` returns structured results and
  does not require login; `/api/doctor/notebook` respects the
  `_ws_file`-style path rules; hub startup performs no probe work.
- **Extend `tests/test_editor.py`**: stderr capture lands in the log file and
  the `EditorError` tail is populated (using the fake-subprocess seams the
  file already uses).
- **JS** (`node --test tests/js/`): if the health-panel rendering gets a pure
  helper (status ordering, report text assembly), test it beside
  `tests/js/files_tree.test.js`; otherwise no JS tests are needed.

## Risks and mitigations

- **The report itself becomes a leak.** Mitigated structurally: probes emit
  curated strings, never raw exception reprs (a `requests` exception can embed
  full URLs); `redact()` is a single choke point with pinned sentinel tests.
  If redaction can't be made airtight for some field, the field is dropped
  from the copyable report rather than scrubbed.
- **False alarms erode trust faster than missing checks.** Fewer,
  high-confidence probes; anything uncertain (dynamic imports, unmappable
  distribution names) reports **unknown** with a neutral sentence, never
  "broken". The deferred items in Phase 3 stay deferred until the cheap
  checks have a track record.
- **stderr pattern-matching is brittle across marimo versions.** The
  classifier is a small ordered pattern list with a mandatory generic
  fallback card; a marimo upgrade can only degrade a specific card back to
  generic, never mislabel. Canned-stderr tests are re-checked when the marimo
  floor moves (`tests/test_marimo_floor.py` is the existing precedent for
  pinning that floor).
- **Network probes slowing or wedging the hub.** Probes run only on user
  demand, off the event loop, with short timeouts; the hub health check never
  runs at startup.
- **Scope creep into a linter product.** The boundary is: diagnose, then offer
  *existing* recoveries. No auto-fixing, no style opinions, no new recovery
  machinery.

## Dependencies and sequencing

- **No prerequisites** — Phase 1 rides entirely on existing seams and is the
  recommended early ship alongside the [push guard](push-guard.md).
- [Offline mode](offline-mode.md) classifies the same network failures
  (unreachable vs auth vs rate-limit) at the sync seam; whichever ships first
  should host the shared classification so the other reuses it — the doctor's
  probe 3/4 wording and offline mode's banner must agree.
- The triage cards' recoveries lean on the Revert/Undo actions the
  [local safety net](local-safety-net.md) extends; they compose but don't
  block each other.
- A triage card that can't be classified is a natural handoff to the
  [traceback fixer](traceback-fixer.md) (copilot-extra-only, built on the
  egress choke point) — the deterministic card ships first and never requires
  the AI.
- The per-notebook check shares the "warn at the moment of choice" philosophy
  of the [staleness guard](staleness-guard.md); the two warnings should share
  the hub's warning surface rather than stacking dialogs.
- Fix lines reference settings documented in
  [Configuration](../../admins/configuration.md); the layering rules this plan
  must respect are in [Architecture](../index.md).
