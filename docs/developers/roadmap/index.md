---
icon: lucide/map
---

# Roadmap

!!! note "Status: proposals"
    Everything in this section is a **design plan, not shipped code**. The set
    came out of a structured ideation review in July 2026 — seven independent
    ideation passes (analyst daily loop, team collaboration, admin/governance,
    data in / results out, AI expansion, competitive landscape, reliability),
    deduplicated into 25 candidate features and ranked by three judges with
    deliberately different priorities (ship-next-month pragmatism, 12-month
    strategy, and scepticism). The twelve that survived are planned here.
    Scope and ordering may change; each page states its own dependencies.

## The common thread

The strongest ideas all exploit the same structural fact: **because analysts
have no git, mooring is the only road into the shared repo — and the only door
out of it.** That means mooring can make guarantees nothing else can make for
this audience (a client-side gate covers 100% of a team's pushes), and it means
the git history sitting in every repo is invisible to users until mooring gives
them a door to it. These plans cash in on that position rather than imitating
what cloud notebook platforms do.

## The plans

| Plan | Status | Effort | One line |
| --- | --- | --- | --- |
| [Push guard](push-guard.md) | ✅ Shipped 2026-07 | M | Run the existing secrets/PII scanners on every push — the flagship trust feature, mostly wiring. |
| [Local safety net](local-safety-net.md) | ✅ Shipped 2026-07 | S | Trash + universal Undo toast + activity ledger: completes "nothing is silently lost" on the local side. |
| [Staleness guard](staleness-guard.md) | ✅ Shipped 2026-07 | S | Warn at Open when the remote moved — conflicts prevented at the moment of choice, not discovered at push. |
| [mooring doctor](mooring-doctor.md) | ✅ Slice (a) shipped 2026-07 | S–L | Plain-English diagnostics for locked-down Windows machines; turns "it broke" tickets into copy-pasteable reports. |
| [Version history](version-history.md) | ✅ Phases 1–3 shipped 2026-07 | M | The git-free time machine: browse and restore any past version of a file from the repo's own history. |
| [Review my changes](review-my-changes.md) | Planned | M | Cell-aware pre-push diff plus an optional "What changed?" note that becomes the commit message. |
| [Pull digest](pull-digest.md) | Planned | M | "What changed while you were away" — computed against each analyst's personal sync horizon. |
| [Duplicate as draft](duplicate-as-draft.md) | Planned | S | A fearless personal copy of any notebook, plus a first-run checklist for new teammates. |
| [Offline mode](offline-mode.md) | Planned | M | Degrade gracefully when GitHub is unreachable instead of looking broken. |
| [Handover explainer](handover-explainer.md) | Planned | S | One-shot copilot walkthrough of an inherited notebook, cell by cell. |
| [Traceback fixer](traceback-fixer.md) | Planned | M | Debug from a traceback without the model ever seeing the data values inside it. |
| [Power BI semantic model](pbi-semantic-model.md) | Planned | M | Let the copilot read synced PBIP tables, relationships, and DAX — schema and authored code, never data. |

The five shipped plans are the review's consensus top five; each page's status
admonition records exactly what landed and what remains open.

## Suggested sequencing

1. **Quick trust wins** — [staleness guard](staleness-guard.md),
   [local safety net](local-safety-net.md), and
   [duplicate as draft](duplicate-as-draft.md) are small, ride existing seams,
   and are felt in the first week of use.
2. **The flagship** — the [push guard](push-guard.md) converts the value-blind
   scanner investment into a whole-product trust story, and the first slice of
   [mooring doctor](mooring-doctor.md) cuts support load for everything that
   follows.
3. **History and legibility** — [version history](version-history.md), then
   [review my changes](review-my-changes.md) *before*
   [pull digest](pull-digest.md): the push note is the input that makes the
   digest legible, and the two share one cell-differ module.
4. **Resilience and copilot depth** — [offline mode](offline-mode.md) (whose
   error-classification work also feeds the doctor),
   [handover explainer](handover-explainer.md),
   [traceback fixer](traceback-fixer.md), and the
   [Power BI semantic model](pbi-semantic-model.md).

## Considered and set aside

The same review deliberately rejected several plausible features, so the
reasoning isn't lost when they resurface:

- **Presence badges** ("Maria has this open") — stale badges after laptop sleep
  teach users to ignore all badges; the [staleness guard](staleness-guard.md)
  buys most of the value at a tenth of the cost.
- **A full in-hub proposal Review Deck** (cell diffs, sandbox runs, sign-off) —
  heuristic cell matching shown to a sceptical senior reviewer is where trust
  dies; let the cell-differ harden in
  [review my changes](review-my-changes.md) first, then revisit a minimal
  read-and-approve card.
- **Cell-anchored discussions** — anchor drift orphans threads, and Teams/email
  already own that conversation for this audience.
- **Scheduled refresh via Task Scheduler** — a silently stale board report is
  worse than no feature; unattended runs on analyst laptops are a support
  tarpit.
- **Dependency licence/vulnerability audit** — Dependabot already does this
  server-side on the repo mooring syncs; a docs pointer delivers most of it.
- **Publish-to-Pages / Outlook delivery** — deliberately commits output *data
  values* to an org-readable repo and depends on a background-job framework the
  hub doesn't have; revisit after the push guard exists to gate it.
- **Connections vault + warehouse catalog** — "not yet": real need, but
  database-driver support is a permanent support burden for a solo maintainer.
  Revisit if the push guard shows hardcoded connection strings are common.
- **Schema-drift sentinel** (synced schema snapshots) — the snapshot file would
  churn and conflict in exactly the way mooring exists to prevent; the
  guardrail-cell half can ride the copilot later.

Every plan here respects the invariants in [Architecture](../index.md): no git
on analyst machines, Windows-first, no runtime installs in frozen builds, a
structurally value-blind copilot (see
[why it can't see your data](../../admins/ai-privacy.md)), and a hub that stays
simple.
