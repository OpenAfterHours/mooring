---
icon: lucide/wifi-off
---

# Offline and outage graceful mode

!!! note "Status: proposed"
    Designed 2026-07 from a multi-agent ideation review; not yet implemented.
    Scope may change — this page records the intended shape and the reasoning.

## Problem

GitHub goes down, the corporate proxy hiccups, or an analyst opens their laptop
on a train. Everything mooring needs to show them is already on disk — the
files themselves, plus `.mooring/manifest.json` with the last-synced SHA of
every tracked file — yet today the app refuses to show it without the network.

The failure mode is worse than an error message. `GitHubClient` (in
`src/mooring/github.py`) classifies HTTP status codes in `_check()` — 401 →
`AuthFailed`, 404 → `NotFound`, 409 (and sha-mismatch 422s) →
`RemoteConflict` — but a
transport-level failure (`requests.exceptions.ConnectionError`, a timeout, a
proxy TLS error) is never caught or classified anywhere. In the hub,
`Hub.api_state` catches only `AuthFailed` and `GitHubError`, so a connection
error escapes to Starlette as a 500; `app.js` then renders "Request failed
(500)" and — because the error response carries no `logged_in` or `mode` —
hides the entire files card. To the analyst it looks like their notebooks are
*gone*. In `_sync_op` the same exception happens to be caught (only because
`requests.RequestException` subclasses `OSError`) and the banner shows a raw
urllib3 "Max retries exceeded" blob. On the CLI, `cmd_status`/`cmd_pull`/
`cmd_push` catch nothing and `main()` re-raises after logging, so the user gets
a full Python traceback. For a local-first app, looking broken whenever the
network is broken is a trust killer: analysts cannot tell "GitHub is down"
from "my work is lost".

## Design

Three pieces, no hidden magic:

1. **Classify transport errors in the GitHub client.** `GitHubClient` gains a
   single send seam that maps `requests` transport exceptions to typed errors:
   a new `Unreachable(GitHubError)` for connection failures and timeouts, and a
   new `TlsFailure(Unreachable)` for SSL errors (the corporate-proxy
   interception signature). Classification is deliberately conservative: an
   HTTP 401 keeps raising `AuthFailed` exactly as today (a fixable auth problem
   must never be hidden behind an "offline" banner), and any other
   `RequestException` stays a generic `GitHubError`. This taxonomy is shared
   groundwork for [mooring doctor](mooring-doctor.md) — one investment, two
   features.

2. **Persist the last-seen remote view.** On every successful status/pull/push
   preamble (`sync._prepare`, which already fetches the branch head and remote
   tree), write a small sibling cache next to the manifest —
   `.mooring/remote-cache.json` — holding the head commit, the in-scope
   `path → blob sha` map, the sync scope it was captured under, and a
   timestamp. This is *not* the manifest base: after a pull that skipped a
   conflict, `sync.pull` deliberately blanks `head_commit` so the next cycle
   refetches and re-detects the conflict, so the manifest's `files` cannot
   stand in for the remote. The cache is the last remote tree we *observed*,
   conflicts included.

3. **Fall back, loudly.** When the client raises `Unreachable`, a new
   `sync.cached_status(cfg)` computes the same three-way `StatusReport` from
   manifest + cache + a local scan — pure local reads, no client. The hub then
   renders the normal files table with an amber banner instead of an error
   page:

   > GitHub unreachable — showing sync state as of 09:12. Editing works
   > normally; push and pull will resume when you're back online.

   Open/Reveal/Delete/Undo and the AI copilot stay live (none of them need the
   team repo; the marimo editor is a local subprocess). Push, Propose, Pull,
   Revert, and the conflict-resolution actions are hidden with that explanation
   rather than a stack trace — each of them needs the network. A conflicted
   file stays badged `conflict`; it just can't be resolved until the network
   returns. The CLI mirrors this: `mooring status` prints the cached report
   under a loud `OFFLINE — showing sync state as of <time>` header, and
   `mooring pull`/`push`/`propose` exit with a one-line explanation.

   **There is no offline push queue.** Pushes stay explicit. A queued push that
   fires later, against a remote that moved in the meantime, would violate
   "conflicts are never silent" in spirit even though the Contents API `sha`
   check would technically catch it.

## Architecture fit

Everything lands in existing modules; no new module, so `.importlinter` needs
no edits (a standalone `remote_cache.py` would have required adding it to
three contracts, everywhere `mooring.manifest` is listed — keeping the cache
in `manifest.py`, whose docstring is already "local sync state", avoids
that):

- **L1 `src/mooring/github.py`** — the two new exception types and the
  transport-classification seam. Stays a thin client; imports nothing new.
- **L2 `src/mooring/manifest.py`** — the `RemoteCache` dataclass and its
  atomic load/save (same `tmp` + `os.replace` idiom as `save()`).
- **L2 `src/mooring/sync.py`** — writes the cache in `_prepare`; new
  `cached_status()` built from the existing pure pieces (`scan_local`,
  `compute_status`). Imports only `manifest` and `github` symbols it already
  imports — the sync-domain-is-core contract holds.
- **L4 `src/mooring/hub/server.py` + `hub/static/`** — degraded `/api/state`,
  friendlier `_sync_op` errors, the banner. New endpoint: none. New DOM: one
  `#offline-banner` element.
- **L4 `src/mooring/cli.py`** — catches `Unreachable` per command / in
  `main()`. The hub still does not import the CLI.

The AI layer is untouched: no new egress channel, no change to
[value-blindness](../../admins/ai-privacy.md). The cache holds only paths and
SHAs — data the manifest already stores.

## Implementation plan

### Phase 1 — classify transport errors (S, independently shippable)

1. In `src/mooring/github.py`, add `class Unreachable(GitHubError)` and
   `class TlsFailure(Unreachable)`, and a private `_send()` helper that wraps
   every `self._session.get/put/post/delete` call (in `get_user`,
   `get_branch_head`, `get_full_tree`, `get_blob`, `create_ref`, `put_file`,
   `delete_file`), mapping `requests.exceptions.SSLError → TlsFailure`,
   `ConnectionError`/`Timeout → Unreachable`, and any other
   `RequestException → GitHubError` — each with a plain-English message and
   the original exception as `__cause__`.
2. In `src/mooring/hub/server.py`, extend `_sync_op` to catch `Unreachable`
   *before* the existing `(GitHubError, OSError)` pair (as a `GitHubError`
   subclass it would otherwise be swallowed by that generic handler)
   and return a 503 with "GitHub is unreachable — your changes are safe on
   disk; push or pull again when you're back online."
3. In `src/mooring/cli.py`, catch `Unreachable` in `main()` before the
   `BaseException` re-raise: log via `telemetry.log_error` as today, then
   `sys.exit()` with the classified message instead of a traceback.

### Phase 2 — the remote-view cache (S)

1. In `src/mooring/manifest.py`, add a `RemoteCache` dataclass
   (`head_commit`, `fetched_at`, `files: dict[str, str]`, `scope_folders`,
   `scope_exclude`), a `remote-cache.json` filename constant beside
   `MANIFEST_NAME`, and `load_cache`/`save_cache` using the same atomic-write
   idiom as `save()`.
2. In `src/mooring/sync.py`, have `_prepare` save the cache (best-effort,
   `contextlib.suppress(OSError)`) after `_remote_entries` returns — the
   moment we know the remote view is fresh, on every status/pull/push/propose.
3. Add `sync.cached_status(cfg)` returning `(StatusReport, fetched_at)` or
   `None` when there is no cache **or the recorded scope differs from
   `cfg.folders`/`cfg.exclude`** (mirroring `_scope_matches` — a
   narrower-scope cache must not masquerade as the current remote). It runs
   `manifest.load` + `scan_local` + `compute_status(mft, local, cache.files,
   cache.head_commit, review=mft.review_files)`; `_reconcile_review` is
   skipped (it needs the network), so open-proposal state is simply carried
   as-is.

### Phase 3 — hub degraded mode (M)

1. Restructure `Hub.api_state` so the `Unreachable` fallback covers both
   `self.username()` (a network call on a cold start) and `sync.status()`:
   on `Unreachable`, call `sync.cached_status`; fill `files`/`artifacts` via
   the existing `_files_artifacts`; add
   `body["offline"] = {"reason": "network" | "tls", "as_of": <iso time>}`;
   keep `logged_in` true when a token exists. Crucially, only `AuthFailed`
   keeps triggering `auth.delete_token` — an outage must never log the user
   out.
2. Add `#offline-banner` to `src/mooring/hub/static/index.html` (beside the
   existing `#review-banner`/`#adopt-banner`) with an amber treatment in
   `style.css` (the `--warn` token that `.badge.pull`/`.badge.mixed` already
   use).
3. In `src/mooring/hub/static/app.js`: a module-level `offlineMode` flag set
   in `refresh()` from `state.offline`; render the banner with the loud
   timestamp; keep hiding `btn-pull`/`btn-push`/`btn-propose` (extend the
   existing `state.logged_in` gate with `!state.offline`); in `fileActions`,
   skip Push/Propose/Revert and the three conflict-resolve actions when
   offline, leaving Open/Reveal/Delete/Undo/AI/View-on-GitHub as they are.

### Phase 4 — CLI degraded status (S)

1. In `src/mooring/cli.py`, `cmd_status` catches `Unreachable` and prints the
   `sync.cached_status` report under an `OFFLINE — GitHub unreachable; showing
   sync state as of <time>` header (or just the classified message when no
   cache exists yet). The Phase 1 `main()` handler already covers
   pull/push/propose.

## Testing

All offline, as usual — GitHub is mocked with the `responses` library, and the
sync suite's `FakeClient` (in `tests/conftest.py`) can raise the new
exceptions directly.

- **`tests/test_github.py`** — register `responses` entries whose `body` is a
  `requests.exceptions.ConnectionError` / `SSLError` and pin the mapping to
  `Unreachable` / `TlsFailure`. Pin the conservative-classification invariant:
  **an HTTP 401 always raises `AuthFailed`, never `Unreachable`**.
- **`tests/test_manifest.py`** — `RemoteCache` round-trip, atomic write,
  missing/corrupt file → `None`.
- **`tests/test_sync.py`** — `_prepare` writes the cache on a successful
  status; `cached_status` returns `None` on a scope mismatch; and the key
  invariant pin: **a file that classified `CONFLICT` on the last online status
  still classifies `CONFLICT` from the cache** (because the cache stores the
  observed remote tree, not the manifest base).
- **`tests/test_hub.py`** — with `Hub.client` monkeypatched to a fake whose
  methods raise `Unreachable` (the existing `configured` fixture pattern):
  `/api/state` returns the cached files plus the `offline` payload with
  `as_of`; the stored token is **not** deleted; `/api/push` returns 503 with
  the friendly message and changes nothing on disk.
- **New `tests/test_cli_offline.py`** — `mooring status` prints the OFFLINE
  header + cached rows; `mooring push` exits non-zero with the one-liner, no
  traceback.
- **JS** — the banner is rendered from server-provided fields with no logic
  worth extracting, so no new `node --test tests/js/` helper is planned; the
  payload contract that drives it is pinned in `tests/test_hub.py`.

## Risks and mitigations

- **Stale status can mislead** — "3 in sync" as of yesterday is not "3 in
  sync". The timestamp is loud and lives in the banner *and* the summary line;
  the banner is amber, not the normal quiet chrome; and conflicts stay marked
  conflicted because the cache is the observed remote tree. The cache is also
  **display-only**: pull/push/propose always refetch live (their `_prepare`
  path is unchanged), so a stale or corrupt cache can mislead a glance but can
  never corrupt a sync decision.
- **Misclassifying an auth failure as an outage** would hide a fixable problem
  behind a "come back later" banner. Mitigation: classification happens only
  at the transport layer; anything that produced an HTTP response goes through
  `_check()` unchanged, and the 401 → `AuthFailed` pin is a named test. A
  captive portal returning 200 HTML stays a plain `GitHubError` (loud), not
  offline.
- **The offline-push-queue temptation.** Deliberately rejected, forever a
  non-goal of this page: surprise-firing queued writes later is against the
  product's "conflicts are never silent" contract in spirit, even where the
  Contents API `sha` check would catch the collision.
- **Cache skew across repo switches and scope changes** — the cache lives in
  the per-workspace `.mooring/` directory, so it switches with the workspace;
  it records its scope and `cached_status` refuses a mismatch rather than
  showing a narrower tree as the whole remote.
- **Windows-first**: the cache write reuses the manifest's `os.replace`
  atomic-write idiom, which is already proven on the primary CI platform.

## Dependencies and sequencing

- **Feeds [mooring doctor](mooring-doctor.md)**: the Phase 1 error taxonomy
  (offline vs auth vs proxy TLS) is exactly the classification the doctor's
  connectivity checks need — build it here once, consume it there.
- **Coordinates with the [staleness guard](staleness-guard.md)**: both need a
  notion of "how fresh is my view of the remote"; the cache's `fetched_at` is
  the natural shared datum, and offline the guard must stay silent rather than
  fire on a head it cannot check.
- **Complements the [local safety net](local-safety-net.md)**: offline editing
  is only trustworthy because every safety artifact (undo snapshots, trash,
  the manifest) is already local.
- No dependency on the [push guard](push-guard.md) or the differ shared by
  [review my changes](review-my-changes.md) and [pull digest](pull-digest.md);
  those are network-era features that simply disable under the same banner
  when offline.
- Standalone otherwise: Phase 1 is worth shipping alone (it deletes the
  tracebacks), and each later phase is independently useful.
