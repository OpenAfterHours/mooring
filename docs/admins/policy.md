---
icon: lucide/shield-check
---

# Team policy: rules the client actually enforces

Because analysts have no git, **mooring is the only road into the shared repo**.
A gate in this client therefore covers *100% of a team's pushes* — a claim no
server-side scanner can make for this audience, because there is no other client
to bypass it with.

The `[policy]` block in the repo's **synced** `mooring.toml` is how you use that
position. It travels with the repo like any other tracked file, it is visible and
diffable in the repo's history, and every mooring that syncs the repo enforces it.

```toml
# <workspace>/mooring.toml  — synced; push it like any other change
[policy]
min_version  = "0.4.29"
push_guard   = "block"
propose_only = ["reports/**", "data/**"]
ai_off       = ["hr/**", "**/*.private.py"]

[policy.settings]
"ai.pii.enabled" = true
"ai.context"     = false
```

Check what is in force at any time:

```bash
mooring policy show
mooring doctor          # includes a "Team policy" row
```

!!! warning "Policy is defence in depth, not a guarantee"

    It binds the mooring client, which is the only client your analysts have. It
    does not bind someone who uses git, the GitHub web UI, or the API directly.
    For those, use branch protection and required reviews on GitHub — policy and
    branch protection are complements, not substitutes.

## The rule that makes it safe: policy can only *tighten*

A synced file is, in the threat model that matters, **attacker-controlled**:
anyone with repo write, a compromised account, or a merged malicious PR can edit
it. So the one thing `[policy]` must never be able to do is *weaken* a teammate's
own safety settings — otherwise a compromised repo becomes a way to switch off
everyone's guard at once.

That is enforced structurally, not by convention:

- **Settings** — each governed setting declares exactly ONE value a policy may
  pin it to (the stricter one). A `[policy.settings]` entry that names the other
  value is **dropped**, with a reason you can see in `mooring policy show`. There
  is no branch in the code that can produce a looser result.
- **The push guard** — `push_guard` composes by *maximum* on the scale
  `warn` → `block`. Writing `push_guard = "warn"` cannot lower a repo that
  already sets `[guard] push = "block"`.
- **Path rules** — `propose_only` and `ai_off` are additive restriction sets, so
  adding a pattern can only ever restrict *more*. `ai_off` is UNIONed with the
  existing `[ai] disabled_notebooks` list, so a policy can never re-enable a
  notebook someone had turned the copilot off for.
- **There is no per-machine override and no "policy off" switch** — an escape
  hatch would itself be a weakening.

Policy sits **above the whole three-layer config merge**
([configuration](configuration.md)), including environment variables: setting
`MOORING_AI_PII=0` cannot switch off a scan the policy forces on.

## A malformed rule is ignored, never obeyed and never fatal

Each rule is parsed independently and defensively. An unknown key, a wrong type,
an unusable path pattern, or an entry that tried to loosen a setting drops **that
one rule** — the rest of the policy still applies, nothing crashes, and nothing
is made less restrictive. Every dropped rule is reported:

```console
$ mooring policy show
Team policy for acme/nbs (synced mooring.toml [policy]):
  push guard: block (policy raised it from warn)
  propose-only: reports/** (no direct push — use Propose)
  ignored: [policy] 'propose_ony': unknown policy key — ignored
```

`mooring doctor` raises a **warn** when any rule was ignored — an ignored rule
protects nobody, and believing you are covered when you are not is the failure
worth surfacing.

A corrupt `mooring.toml` is reported (`doctor` **fails**) but does **not** stop
mooring working: a shared file that could wedge every teammate's app would be a
worse weapon than any policy it could carry, and this audience has no git with
which to pull the fix. Availability wins, loudly.

## Removing the policy is the one weakening left

Nothing in the file can make a rule looser — but someone with repo write can
still **delete the block**, and an absent policy looks exactly like a repo that
never had one. So each machine remembers locally (under the sync-excluded
`.mooring/`) that it once saw a policy in force here, and reports the
disappearance:

```console
$ mooring policy show
  ! this repo HAD a policy and no longer does — see below
  ignored: this repo HAD a team policy and no longer does — someone removed the
           [policy] block. Check the repo's history before trusting the change.
```

`mooring doctor` warns for the same reason, and keeps warning until a policy is
back.

!!! note "What that record is, and is not"

    It is a **local, unsigned breadcrumb**. It catches an accident, or a remote
    attacker who only has repo write — the threat this whole feature is about.
    It does **not** survive onto a fresh machine (nothing has been seen there
    yet), and it does not defend against someone who can already write to the
    analyst's own disk, since they could delete the breadcrumb as easily as the
    policy. Making it stronger needs a signature and somewhere trustworthy to
    keep the key, which mooring does not have. Turning a silent removal into a
    visible one is the honest ceiling; pair it with branch protection on the
    `mooring.toml` path if the repo warrants it.

Path patterns are routed through the same sanitiser the rest of the synced file
uses: an absolute path, a drive letter, or a `..` escape is refused outright, so
a pattern can never address anything outside the workspace. Patterns are only
ever *matched* against repo-relative paths — never handed to the filesystem.

## The rules

### `min_version` — advisory

```toml
[policy]
min_version = "0.4.29"
```

Below this version mooring warns loudly: on `mooring push` / `mooring propose`,
in `mooring doctor`, in `mooring policy show`, and on the hub's Settings page.

**It never refuses to run**, and that is deliberate. A blocking floor is a
repo-wide self-DoS with no recovery path — if pushing is blocked you cannot push
the fix to `mooring.toml` either. It also buys little: a client old enough to
matter predates `[policy]` and would not read the floor at all. Treat it as "nudge
the team to update", not as a security control.

### `push_guard` — escalate the secret / PII push guard

```toml
[policy]
push_guard = "block"
```

The generalisation of `[guard] push` (which keeps working — see
[configuration](configuration.md)). In
`block` mode there is no "Push anyway": a finding must be fixed, or retired per
line with a `# mooring: push-ok` comment that is visible in the diff.

### `propose_only` — some paths change only through review

```toml
[policy]
propose_only = ["reports/**", "data/**"]
```

A matching file can never be pushed **directly** to the shared branch. It is
withheld at the push seam itself — the same mechanism the secret scanner uses —
so the bytes never reach GitHub. **Propose** is the road: `mooring propose` (or
the hub's Propose button) sends the same file to a personal review branch and
opens a pull request.

**Deleting** a matching file is blocked the same way. A deletion is a direct
write to the shared branch too, and destroying a review-gated file is the change
that most needs review — so it goes through the same gate, and must also go via
Propose.

Unlike a scanner finding, a propose-only block has **no override**: there is no
confirm token, and `--acknowledge-findings` does not clear it.

### `ai_off` — the copilot is off for these paths

```toml
[policy]
ai_off = ["hr/**", "**/*.private.py"]
```

The glob generalisation of the per-notebook `[ai] disabled_notebooks` opt-out.
Matching notebooks show as AI-disabled in the hub, refuse to open a chat, and
refuse an Apply — checked again at each of those seams, so disabling a path
mid-session takes effect immediately.

### `[policy.settings]` — pin a safety setting for everyone

```toml
[policy.settings]
"ai.pii.enabled"   = true
"ai.traceback_guard" = true
"ai.apply_guard"   = true
"ai.context"       = false
"ai.code_index"    = false
"ai.batch.enabled" = false
```

| Key | May be pinned to | Effect |
|-----|------------------|--------|
| `ai.enabled` | `false` | The copilot is off for the whole team. |
| `ai.pii.enabled` | `true` | The outbound PII scan cannot be turned off locally. |
| `ai.pii.block_prompt` | `true` | A PII hit always holds the prompt (never warn-only). |
| `ai.pii.scan_notebook_source` | `true` | The PII-dense notebook warning stays on. |
| `ai.traceback_guard` | `true` | Pasted tracebacks are always sanitised. |
| `ai.apply_guard` | `true` | The Apply check cannot be turned off locally. |
| `ai.apply_runs` | `false` | An applied cell is staged, never run by the act of applying. |
| `ai.context` | `false` | Team context files are never sent. |
| `ai.code_index` | `false` | The team code library is never sent. |
| `ai.notebook_catalog` | `false` | No repo-wide notebook catalog. |
| `ai.live_schema` | `false` | No live kernel schema reads. |
| `ai.semantic_model` | `false` | No Power BI semantic-model reads. |
| `ai.batch.enabled` | `false` | No unattended batch builds. |

Writing the *other* value is refused when authoring (`mooring policy set`) and
ignored when parsing — the same rule from both directions.

Note that the two Apply keys point in **opposite directions**, and both are the
stricter end of their own setting. `ai.apply_guard` may be pinned to `true` (the
check is armed) and `ai.apply_runs` to `false` (an applied cell is staged rather
than run). Neither can be spelled the other way round: there is no way to write a
policy that switches the Apply check off, and none that forces a teammate's
applied cells to run. See [the Apply check](ai-privacy.md#apply-gate).

On each machine, a pinned setting shows on the hub's **Settings** page as
*Set by your team*, with its control disabled and a note saying where the lock
came from. It is not silently ignored: the endpoint answers a write with a `409`
naming the policy, and there is no confirm dialog that can talk past it. A user
can still move the setting *towards* the safe value.

## Authoring a policy

`mooring policy set` / `unset` edit the **synced** `mooring.toml`, so authoring a
policy is a push like any other — it reaches the team on your next `mooring push`
and rides the push guard on the way.

```bash
mooring policy show

mooring policy set min-version 0.4.29
mooring policy set push-guard block
mooring policy set propose-only "reports/**" "data/**"
mooring policy set ai-off "hr/**"
mooring policy set setting ai.pii.enabled true

mooring policy unset propose-only
mooring policy unset setting ai.pii.enabled

mooring push          # ← the policy only reaches your team once you push it
```

`set propose-only` / `set ai-off` replace the whole list (pass every pattern you
want); `unset` removes the rule entirely. Relaxing a policy is an ordinary,
visible, diffable edit to the shared file — which is exactly the point: there is
no *local* way to relax it.

**Pattern syntax** is deliberately small: `*` matches within one path segment,
`**` matches any number of segments, `?` matches one character. Character classes
are not supported (a `[` is a literal). Matching is case-insensitive, and a
pattern with no wildcard also covers everything beneath it (`ai_off = ["hr"]`
covers `hr/pay.py`).

!!! warning "A pattern that matches nothing looks right and protects nobody"

    `*` stays inside **one** folder, which is the usual way a rule silently
    covers less than intended:

    | You probably meant | Write |
    |---|---|
    | every `*.private.py`, at any depth | `**/*.private.py` (not `*.private.py`, which is repo-root only) |
    | a Power BI report and its model | `reports/Sales*` (not `reports/Sales`, which matches neither `reports/Sales.pbip` nor `reports/Sales.SemanticModel/`) |
    | everything under a folder | `hr/**`, or just `hr` |

    `mooring policy show` lists any pattern matching nothing in your workspace
    right now, so run it after authoring a rule.

## Upgrading an existing repo

A repo with no `[policy]` block behaves **exactly** as it did before — every rule
is absent-by-default. The two settings this generalises are folded in, not
replaced:

- `[ai] disabled_notebooks` keeps working and keeps being written by the hub's
  per-notebook AI toggle. `ai_off` is unioned on top.
- `[guard] push` keeps working. `push_guard` can only raise it.

So you can adopt `[policy]` one rule at a time, and nothing you already rely on
changes.

## Related

- [Configuration](configuration.md) — the three-layer per-machine config the
  policy sits above.
- [Why the copilot can't see your data](ai-privacy.md) — the settings most worth
  pinning, and what they actually guarantee.
- [Threat model](threat-model.md).
