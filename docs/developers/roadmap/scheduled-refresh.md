---
icon: lucide/clock
---

# Scheduled refresh: unattended notebook runs that cannot go stale in silence

!!! success "Status: fully implemented 2026-07-30 (phases 0–4)"
    **Phase 3 landed too**: `schedule_os.py` (the capability ladder's tiers 2 and 3),
    `mooring schedule background enable|disable|status`, the hub's tier line and
    Enable button, a `doctor.py` probe, and the cross-process workspace lock that
    tiers 2/3 make necessary. Verified against real Windows Task Scheduler on a
    non-elevated account: the XML is accepted, `DisallowStartIfOnBatteries=false`
    and `StartWhenAvailable=true` land as written, and a task-launched run produced
    a freshness-stamped artifact with exit code 4 (degraded — it could not pull)
    propagating to `Last Result`.

    Two things real-machine testing found that the plan did not predict:

    - **`shutil.which("mooring")` is unsafe for this.** It resolved a leftover
      `uv tool install` of **0.4.15** sitting beside the dev checkout; the task
      bound to it and every background run died with an argparse error (no
      `refresh` command) that nobody would ever see. `resolve_command()` now
      anchors every candidate to the *running interpreter* and never consults
      PATH — pinned by `test_resolve_never_uses_a_bare_PATH_lookup`.
    - **Sub-subcommand `--repo` must be on the leaf parsers**, not just the
      `background` parent, or `schedule background enable --repo x` is a usage
      error.

    Tier 2's *logon behaviour* is the one thing not verified end to end — the
    launcher file and the agent loop are both tested (and the agent was observed
    ticking and refreshing), but whether a given estate's EDR permits a Startup
    entry can only be found out on that estate.


    Shipped: the extracted hardened runner (`app/notebook_run.py`, with
    `app/verify_run.py` rewritten onto it), the schedule model
    (`schedule.py` — a lean-core leaf, added to the `frozen-core-is-lean`
    contract), the orchestrator (`app/refresh.py`), the CLI
    (`mooring schedule add|list|rm|pause|resume` and `mooring refresh`), the hub
    board (`hub/routes/schedule.py`, the schedules card, `schedule_fmt.js`), the
    tier 0/1 sweep thread in `run_hub`, and the orchestrator-interop surface
    (`--json` + documented exit codes).

    Two divergences from the sketch, both recorded below: the no-write-authority
    guarantee is pinned by an AST allowlist in `test_refresh.py` rather than an
    import-linter contract (import-linter works at module granularity and cannot
    express "not `sync.push`"), and a successful manual retry now *clears an
    auto-pause* — a case the sketch missed, which would otherwise have stranded
    a fixed notebook behind a second, non-obvious button.

    This plan **reverses** an earlier rejection. The July 2026 review set
    scheduled refresh aside on one sentence — *"a silently stale board report is
    worse than no feature; unattended runs on analyst laptops are a support
    tarpit."* That objection is correct and is the design constraint here, not a
    counter-argument to wave away. Everything below exists to make the two
    failure modes **structurally impossible** rather than documented: staleness
    is never silent (§ *Freshness is a contract, not a hope*), and a failed run
    can damage nothing (§ *Blast radius*).

## Problem

An analyst's month-end board pack, daily reconciliation, or weekly exception
report is the same notebook run against new data on a cadence. Today every one
of those runs is a human remembering: open the hub, Pull, Open, run, Deliver,
attach to an email. The work is mechanical, the schedule is real, and the
forgetting is the failure.

Mooring already has every piece of the machinery:

- `app/verify_run.py` runs a whole notebook headlessly in the team's locked
  environment, with a hardened process-tree kill and a render-then-delete
  discipline that keeps data values off disk.
- `app/deliver.py` renders the same run to a stakeholder HTML in the
  sync-excluded outbox, with a provenance footer.
- `checks.py` and `inputs.py` collect **value-free** receipts from the run —
  did the tie-outs reconcile, did an input change under us.
- `activity.py` records what happened; `doctor.py` explains why it didn't.

What is missing is the *clock*. This plan adds one, and nothing else: no new
run semantics, no new output format, no new privacy surface.

### Why the original objection is answerable

The review's rejection assumed the feature it named — a Task Scheduler entry
that regenerates a report and leaves a file lying around. That version deserves
rejecting. The distinguishing move here is that mooring already computes, on
every run, three **value-free** facts nobody else can compute for this audience:

- did it run (`verify`),
- did the numbers tie out (`mooring_checks`),
- did the inputs change (`mooring_inputs`).

A scheduler that reports those three is not "a report generator". It is a
**morning status board** — *"your daily reconciliation ran at 07:30, but segment
totals no longer reconcile and `rates.csv` changed"* — which is strictly more
valuable than the artifact it produces, and which is exactly the signal that
makes a stale output impossible to miss.

## Design

### Blast radius: a scheduled run has no write authority

The single most important property, and the answer to *"support tarpit"*. A
scheduled run may do exactly three things:

1. **Pull** (`sync.pull` with the default `ConflictStrategy.SKIP` — the same
   conflict-skipping read path an attended Pull uses).
2. **Run the notebook locally**, via the same hardened subprocess the attended
   Verify uses.
3. **Write inside `.mooring/`** — receipts, and optionally one outbox artifact.

It may **never** push, propose, commit, delete a workspace file, resolve a
conflict, or touch `ai/`. There is no code path from the scheduler to
`sync.push`, and the import contract should say so (§ *Layering*).

The consequence is that the worst possible scheduled-run failure is **a local
file that didn't get written**. Nothing in the team repo can be corrupted by an
unattended run, because an unattended run cannot write to the team repo. That
turns the tarpit from "unbounded" to "one machine, one local folder", which is
the same blast radius Deliver already has and which support already handles.

### Freshness is a contract, not a hope

A schedule declares a cadence. That declaration is what makes staleness
*detectable* — without it, "last run 9 days ago" is unclassifiable. With it,
overdue is arithmetic. Four mechanisms, in the order a stale result would be
caught:

**1. The hub board goes amber first.** The schedules card lists every schedule
with its last outcome and next due time. Past `cadence + grace_hours` with no
successful run, the row goes amber and a header notice appears. This reuses the
existing freshness banner idiom in `hub/static/freshness.js` and the `.notice`
banner family.

**2. A failed run never overwrites a good artifact.** The outbox keeps the last
**successful** render. A failed run writes a receipt, not a file. So any artifact
on disk is always a real, complete run — possibly old, never half-rendered and
never a stale-values-under-a-new-date lie.

**3. The artifact carries its own expiry.** `deliver._footer_html` already
stamps origin, notebook, and date. A scheduled render adds one clause:
*"scheduled daily · next refresh due 2026-08-01"*. A manager holding the emailed
HTML three weeks later can see it is overdue **without access to mooring, the
repo, or the analyst**. This is the mechanism that actually answers the
review's objection: the staleness travels with the artifact.

**4. Repeated failure pauses loudly.** `max_failures` consecutive failures (default
3) auto-pauses the schedule and raises a sticky hub notice. A paused schedule is
a visible, actionable state. The anti-pattern this kills is the schedule that
has been failing since March and is still "enabled".

### Preflight: you cannot schedule a notebook that doesn't work

The largest class of unattended-run support tickets is *"it never worked in the
first place"*. That class is closed by a one-call gate:

```python
verified = verify.read_results(workspace).get(rel_posix)
if not (verified and verified["passed"]):
    raise ScheduleRefused("Verify this notebook first — only a notebook that "
                          "ran clean can be scheduled.")
```

This composes with a property `verify.py` already guarantees. Receipts are keyed
to the notebook's content SHA and `read_results` drops any receipt whose SHA no
longer matches the file (`verify.py:131`). So **editing a scheduled notebook
automatically invalidates its verification**, with no new invalidation logic to
write and no cache to get wrong.

An edited-since-verified schedule enters `unverified` state: it still runs (the
edit may well be fine, and refusing to run would itself be a silent failure),
but its failure budget drops to **1** — the first failure pauses it immediately
instead of after three. The hub row shows *"edited since it was verified —
re-verify to restore the full retry budget."*

### The run, and what it reports

For each due schedule, sequentially — never two notebooks at once, and never
concurrent with an attended action (a workspace lockfile under `.mooring/`
guards both):

```
1. preflight   not paused · within failure budget · verified (or unverified-with-budget-1)
2. pull        ConflictStrategy.SKIP; record how many files were skipped as conflicted
3. run         the shared hardened runner (below); deliver=true folds steps 3 and 5 into ONE run
4. read back   checks.read_results() · inputs receipts · exit code · value-free failed-cell count
5. render      on success + deliver=true: the outbox artifact, with the freshness-stamped footer
6. record      a value-free run receipt; activity.record(workspace, "refresh", ...)
7. on failure  bump the consecutive-failure counter; at the threshold, pause + notice
```

Step 2 deserves care: a pull that **skips a conflicted file** means the run
executed against a version that is not the team's latest. That is a degraded
run, not a clean one, and it is recorded as `degraded` with the conflict count —
the hub shows *"ran, but 1 file is in conflict and was not updated."* Silently
running on stale inputs is precisely the failure this feature is accused of, so
it gets its own state.

Step 4 is where the value is. The outcome is a triple, all three already
computed by existing modules and all three value-free:

| Field | Source | Meaning |
| --- | --- | --- |
| `ran` | process exit code | did the notebook execute top to bottom |
| `checks_failed` | `checks.read_results` | did the tie-outs reconcile |
| `inputs_changed` | the `mooring_inputs` receipts | did an input move under us |

**A run that succeeds but fails a tie-out check is a red outcome.** That is the
headline of the whole feature, and it costs nothing to implement because
`mooring_checks` already writes those receipts during any run.

### Extract the hardened runner (a prerequisite, and a simplification)

`app/verify_run.py` currently states, in its own docstring (lines 28–29), that
its run is *"an ATTENDED action — never a scheduler"*. That invariant is
deliberate and must not be quietly violated. It is also conflating two things:
**running a notebook safely** and **recording a trust receipt**.

Phase 0 extracts the first into `app/notebook_run.py`:

- `subprocess.CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` tree kill, so an
  orphaned marimo kernel cannot re-write a value-bearing render after cleanup
  (`verify_run._run_export`, `_kill_tree`);
- render into a sync-excluded path, `unlink` on **every** path including timeout;
- never store stderr — only the value-free count of `MarimoExceptionRaisedError`
  markers;
- the `produced`-file check that distinguishes an environment failure from a
  notebook failure, so a broken `uv.lock` never badges a good notebook red.

`verify_run` and the new `app/refresh.py` then both sit on one hardened path,
and the docstring's claim is rewritten honestly: the *runner* is shared; the
**attended-only** rule stays where it belongs, on the actions that leave the
machine (push, propose, sending the artifact).

This is worth doing on its own merits — those four rules are subtle, hard-won,
and currently live in one function that a second caller would have been tempted
to copy.

### Storage: workspace state, not user config

Schedules live in `.mooring/schedules.json`, alongside the manifest, verify
receipts, and the activity ledger.

Rationale: a schedule references a workspace-relative notebook path, is per-repo
by nature, and must never sync. `.mooring/` is excluded structurally by
`sync.is_synced_path` on both the local scan and the remote tree, so a schedule
cannot ride a push. This also avoids touching `config.py` and the schema-driven
`hub/settings_schema.py` allowlist, which is built for flat scalars and would
need a list-of-tables concept for no benefit.

```json
{
  "version": 1,
  "schedules": [{
    "notebook": "notebooks/daily_board.py",
    "cadence": "weekdays",          
    "at": "07:30",                  
    "deliver": true,                
    "grace_hours": 4,               
    "max_failures": 3,
    "paused": false,
    "consecutive_failures": 0,
    "last_run": {"at": "...", "outcome": "ok", "checks_failed": 0, "inputs_changed": 0}
  }]
}
```

`cadence` is a small closed vocabulary — `hourly | daily | weekdays | weekly` —
deliberately not cron. The audience does not write cron, and a closed vocabulary
is what lets the hub compute and display "next due" without a cron parser.

### The clock is tiered, because many users cannot register an OS task

The original sketch of this plan assumed Windows Task Scheduler. That
assumption is wrong for a meaningful slice of the audience: on managed laptops,
`schtasks.exe` is frequently blocked by AppLocker or Group Policy, and a feature
that works for *some* of a team is not shippable.

(The nuance: a **logon-context** task — `LogonType=InteractiveToken`,
`RunLevel=LeastPrivilege`, no stored password — is often permitted for standard
users; it is the "run whether the user is logged on or not" variant that
reliably needs elevation. So the capability must be **probed**, never assumed
in either direction.)

The reframe that makes this tractable: **unattended was never the valuable
part.** A schedule's value is "I don't have to remember", not "it runs at 3am".
The output is consumed either *in* mooring (the status board) or *from* the
outbox by the analyst attaching it to an email — both require the analyst to
show up. A run at 07:30 while they sleep buys very little over a run at 08:15
when they open the hub.

So the clock is a **capability ladder**, and mooring uses the highest tier the
machine actually permits:

| Tier | Mechanism | Needs | Freshness guarantee |
| --- | --- | --- | --- |
| **0** | Catch-up when the hub opens | nothing | runs next time you open mooring |
| **1** | In-process timer while the hub is open | nothing | fires on cadence if the hub is running |
| **2** | Logon agent from the per-user Startup folder | write to `%APPDATA%\…\Startup` | fires on cadence whenever you are signed in |
| **3** | Windows Task Scheduler task | `schtasks` permitted | true unattended; survives the hub being closed |

Tier 0 is the floor and needs no permissions whatsoever, so **every user gets
the feature**. Tiers 1–3 shorten the latency between "due" and "ran".

Crucially, the tier **is** the freshness guarantee, and mooring says which one
you are on. This needs no new safety machinery: the freshness contract above
already classifies and displays "overdue", so a lower tier simply means overdue
occurs more often and the board and the artifact footer report it honestly.
That contract was designed to let the guarantee vary; this is it doing its job.

#### Tier 0 semantics

**Catch-up, not backfill.** Opening the hub after five days away runs the
schedule **once**, for the current window — never five times. Airflow's
`catchup=True` is a well-known footgun and the same trap applies here. The
question "is there a successful run inside the current cadence window?" is
answerable from the `last_run` receipt already in the schedule record.

**Auto-run only when the preflight is clean.** A verified notebook whose last
run succeeded refreshes automatically on open, using the existing in-flight cue
(`body.busy` + the action status line). Anything unverified, degraded, or
previously failed shows *"1 refresh due — Run now"* instead. This reuses the
preflight state rather than adding a setting, and means an unattended run never
surprises someone whose notebook is in a doubtful state.

#### Tier 2: prefer the Startup folder over the Run key

`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` is writable by any
user and is the ordinary non-admin autostart mechanism. Avoid the equally
permission-free alternative,
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`: Run-key persistence is
textbook malware behaviour and EDR will flag it. A `.lnk` in the Startup folder
is unremarkable by comparison, though some estates block that too — so Tier 2 is
offered, attempted, and gracefully abandoned, never assumed.

The Tier 2 agent is a **lean** process, not the hub: it loads `schedule.py` and
`app/refresh.py` and nothing else, so it carries no marimo/Starlette footprint
while idle.

#### Tier 3: the OS task adapter

**One OS task per repo, not per notebook.** `schtasks` creates a single task
that runs `mooring refresh --due`; mooring itself decides which schedules are
due. This keeps the OS-level surface to one debuggable object per workspace no
matter how many notebooks are scheduled, and means adding a schedule usually
touches no OS state at all.

Registration uses `schtasks /Create /XML`, not the flag form — the flags cannot
express the four settings that decide whether this works on a real laptop:

| Setting | Why it is load-bearing |
| --- | --- |
| `DisallowStartIfOnBatteries=false` | **The default is `true`.** Analysts run on battery constantly; leaving this at its default is the single most likely cause of a schedule that silently never runs. |
| `StartWhenAvailable=true` | A laptop asleep at 07:30 catches up when it wakes, instead of skipping the day. |
| `MultipleInstancesPolicy=IgnoreNew` | A long run overlapping the next trigger is dropped, not stacked. |
| `ExecutionTimeLimit` | A hung kernel is killed by the OS as a backstop to mooring's own timeout. |

`LogonType=InteractiveToken` + `RunLevel=LeastPrivilege`: the task runs **only
when the user is logged on**, which needs no stored password, no `/RP`, and no
elevation — the only variant that survives corporate policy. It also means the
keyring token is available, which a run needs for step 2's pull.

**The invocation path is a real constraint.** The task must name a stable
command:

- frozen `.exe` → `sys.executable`; stable.
- `.pyz` → `python <path-to-pyz>`; stable while the file stays put.
- `uv tool install mooring` → a stable shim; **the recommended path**.
- `uvx mooring` → runs from an ephemeral uv cache that uv may garbage-collect;
  **not stable**.

`schedule_os.resolve_command()` returns a stable command or raises with a
curated fix line in the `doctor.py` idiom: *"Scheduling needs a stable install —
run `uv tool install mooring`, then add the schedule again."* Refusing here is
much better than registering a task that breaks silently in a fortnight.

**Capability detection, not assumption.** `schedule_os.probe()` attempts to
register a trivial no-op task, checks the result, and deletes it — then caches
the answer. An access-denied result demotes the workspace to Tier 2 (or 1/0) and
records *why*, so `mooring doctor` can report *"Task Scheduler is blocked by
policy on this machine — refreshes run when you open mooring instead"* rather
than leaving a user to wonder. A demotion is a normal outcome, not an error.

Non-Windows: only Tiers 0–1 in phase 1 (they are OS-independent). Windows is the
primary platform; `launchd`/`cron` can implement Tier 3 later behind the same
interface. `schedule add` never fails on macOS/Linux — it just reports the tier
it landed on.

### Layering

```
L3.5  app/refresh.py        orchestrator: preflight → pull → run → receipts → deliver
      app/notebook_run.py   the shared hardened runner (phase 0)
L2    schedule.py           model, due-computation, receipts, pause state (lean leaf)
L1    schedule_os.py        schtasks adapter; stdlib + paths only
L4    cli.py · hub/routes/schedule.py
```

`schedule.py` mirrors `verify.py` and `checks.py`: it imports only `paths`,
`gitsha`, and the standard library, so it stays inside the
`frozen-core-is-lean` contract and carries no path to marimo or the Copilot SDK.
`app/refresh.py` is the only module that reaches both `sync` and the runner —
which is exactly what `app/` is for.

Add an import-contract rule asserting `app.refresh` does not import
`sync.push`/`sync.propose`. The "no write authority" claim should be enforced by
CI, not by reading.

### Privacy

Nothing here touches `ai/`, and no channel from a scheduled run reaches the
copilot. The run's rendered HTML embeds real values and lives only in the
sync-excluded outbox, exactly as an attended Deliver's does. Run receipts are
value-free by construction: a boolean, two counts, a timestamp — the same
contract `verify.py` and `checks.py` already hold, reusing their writers rather
than adding a new record format. Telemetry, if enabled, logs the op name and
outcome, never a path (`telemetry.py`'s existing rule).

## Phases

**Phase 0 — extract the hardened runner.** `app/notebook_run.py`; `verify_run`
rewritten onto it with its docstring corrected. No user-visible change. Existing
`test_verify.py` must pass unmodified — that is the check that the extraction
was behaviour-preserving.

**Phase 1 — the model and the runner, no OS scheduler.** `schedule.py`,
`app/refresh.py`, `mooring schedule add|list|rm|pause|resume` and
`mooring refresh --due` / `--now <path>`. Fully usable and fully testable by
invoking `refresh` by hand; the clock is the only thing missing. Ship this
before touching `schtasks` — it means the scheduler adapter is the only variable
when the OS integration is debugged.

**Phase 2 — Tier 0 + Tier 1 and the hub.** Catch-up-on-open, the in-process
timer while the hub runs, the schedules card, the amber-when-overdue board, the
paused-schedule notice, and the freshness-stamped footer clause in `deliver`.

**This is the release that ships the feature to everyone**, because Tiers 0–1
need no permissions and no OS integration at all. It is also where the feature
stops being a CLI tool and becomes the morning status board that justifies it.
Everything after this point is latency reduction.

**Phase 3 — Tier 3 (and Tier 2), for machines that permit it.**
`schedule_os.py`, the XML template, `probe()`'s capability detection with a
recorded reason for any demotion, `resolve_command()`'s stable-install gate, and
`doctor.py` probes (which tier is active and why? task registered? last run?
command still resolvable? token valid?). Tier 2's Startup-folder agent lands
here too, behind the same interface.

Deliberately **after** the hub: by phase 3 the runner, the receipts, and the
board are all proven, so the only new variable is the OS integration — which is
exactly the part that varies per machine and is hardest to debug remotely.

**Phase 4 — orchestrator interop.** `mooring refresh --json`, documented exit
codes, no interactive prompts, no browser launch. Lets a team that already runs
Airflow, Dagster, or a plain Windows Service drive mooring from their existing
infrastructure, for near-zero cost and no new dependencies. See
§ *Why not an orchestrator*.

**Phase 5 (deferred) — the team offer.** A synced `mooring.toml` `[schedule]`
block letting a team *declare* "this notebook is a daily refresh", with each
analyst opting in per machine — the same two-plane offer/pick pattern as the AI
context folders. Deliberately last: the runner must be boring before a team
declaration multiplies it across laptops.

## Why not an orchestrator

Airflow and Dagster were considered and rejected as an *embedded* dependency:

- They are **daemons**. Something must keep the scheduler alive — which on
  Windows is Task Scheduler or a Service, i.e. the exact layer that is blocked
  for part of this audience. An orchestrator sits *above* the layer where laptop
  scheduling actually fails; it does not replace it.
- **Airflow does not support Windows** natively (WSL2/Docker only — unavailable
  on a locked-down laptop) and wants a metadata database, a scheduler, and a
  webserver.
- **Dagster** needs `DAGSTER_HOME`, an instance store, and a long-running daemon
  for schedules to fire, and pulls `grpcio` (compiled) plus
  sqlalchemy/alembic/pydantic. Mooring ships **9 base dependencies** and a
  frozen `.pyz` has no pip at runtime; this would break `frozen-core-is-lean`.
- **They would regress a maintained privacy property.** Both capture task
  stdout/stderr into their own log store. marimo's stderr can quote data values
   — which is precisely why `app/verify_run.py` refuses to store it and counts
  `MarimoExceptionRaisedError` markers instead. Orchestrator logs land outside
  `.mooring/`, are not covered by `sync.is_synced_path`, and on a shared
  instance are readable by anyone with UI access.

The one capability an orchestrator has that mooring lacks is **DAG dependencies**
between notebooks. If that becomes the real requirement, the answer is still not
Airflow: record what a notebook *writes* (extending `_inputs_runtime.py`, which
already records what it reads), and a topological sort over that graph is ~50
lines. Taking on an orchestrator to obtain a topological sort is a bad trade.

Phase 4 serves the legitimate case — a team that **already** runs one on a
server — by making mooring a good citizen for it rather than embedding one.
Note that a central server version inverts the data-locality story (values,
credentials, and artifacts move to the server, and the run needs a PAT or GitHub
App rather than a device-flow keyring token). That is fine for an admin-run
server, but it is a different product posture and should be a deliberate choice.

## Open questions

- **Should a scheduled run pull at all?** Pulling is most of the value (fresh
  code and data), but it is also the only step that touches the network, needs a
  token, and can degrade. A `pull = false` schedule ("just re-run what's on
  disk") is trivially safe and may be the better default for the first release.
- **Hourly cadence on a laptop** — probably a mistake in practice; consider
  refusing anything under daily until there's a real request for it.
- **Notification channel.** At Tiers 0–1 the analyst is *at* the hub when the
  run happens, so the in-page notice is the whole channel and no toast is
  needed. A toast only earns its keep at Tiers 2–3, where a run can fail with
  nobody looking — so it belongs in phase 3, not phase 2.
- **Should Tier 1's timer fire while the analyst is mid-edit?** A refresh
  competes for CPU with the editor and touches the same notebook. Deferring the
  timer while a marimo editor is open for that notebook is probably right, and
  makes Tier 1 degrade to Tier 0 exactly when it should.
- **Interaction with the trash cap.** A daily deliver writes an artifact a day
  into the outbox. The outbox has no retention policy today; scheduling gives it
  one to need (`trash_keep_days` is the obvious model to copy).
