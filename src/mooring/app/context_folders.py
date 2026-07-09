"""Resolve which context folders the copilot READS and which ride SYNC.

Two planes meet here (see ``docs/admins/ai-privacy.md``):

* the SYNCED team OFFER — the value-free ``[ai] context_folders`` menu a curator
  publishes in ``<workspace>/mooring.toml`` (:mod:`mooring.workspace_config`);
* the PER-MACHINE consent + legacy fallback in the ``[ai]`` config
  (``context`` bool + the single ``context_dir``).

This module is the ONE place that composes them, because
:attr:`mooring.config.AppConfig.sync_folders` is workspace-BLIND (it cannot read
``mooring.toml``) while both decisions need the synced offer. It imports no adapter,
so the hub and the CLI both call it.

The load-bearing separation: SYNC is decoupled from READ. The WHOLE offer rides
pull/push for any teammate whose consent is on (so every offered folder is covered
by the pre-push secret scan), while :func:`read_dirs` may narrow what a given user
actually feeds the model to a subset of that scanned+synced set — so a folder read
into the copilot is always a folder that was scanned on push.
"""

from __future__ import annotations

from pathlib import Path

from mooring import workspace_config


def shareable_dirs(app_cfg, workspace: Path) -> tuple[str, ...]:
    """The FULL set of context folders that ride push and must be linted before
    sharing — the team OFFER when the repo publishes one, else the per-machine
    ``[ai] context_dir`` (the single-folder legacy default), so an un-curated repo is
    byte-identical to before. A Phase-2 per-user subscription narrows what is READ
    (:func:`read_dirs`) but NEVER what is shared/scanned, so a folder read into the
    copilot is always a folder that was scanned on push."""
    offer = workspace_config.context_folders(workspace)
    if offer:
        return offer
    ctx = str(app_cfg.ai_context_dir).strip("/")
    return (ctx,) if ctx else ()


def _subscription(app_cfg) -> tuple[str, ...] | None:
    """The active repo's per-user context SUBSCRIPTION (``RepoSpec.context_folders``),
    or ``None`` when there is no active repo / no choice recorded."""
    alias = getattr(app_cfg, "active_alias", "")
    if not alias:
        return None
    try:
        spec = app_cfg.spec(alias)
    except KeyError:
        return None
    return getattr(spec, "context_folders", None)


def read_dirs(app_cfg, workspace: Path) -> tuple[str, ...]:
    """The context folders the copilot READS for ``workspace`` — the user's SUBSCRIPTION
    intersected with the team OFFER, the offer staying authoritative.

    The per-machine ``[ai] context`` consent bool still gates whether these are read at
    all (the caller passes it as ``enabled=``). Resolution:

    * no team offer → the legacy single ``[ai] context_dir`` (a subscription only ever
      narrows an offer, never invents one), so an un-curated repo is byte-identical;
    * offer present, subscription ``None`` (no choice) → the WHOLE offer — the opt-out
      default, so publishing an offer doesn't silently blank an already-consented user;
    * offer present, subscription set → ``subscription ∩ offer`` iterating OFFER order
      (an explicit empty subscription therefore reads nothing).

    :func:`shareable_dirs` stays the full offer regardless, so a read folder is always a
    scanned+synced folder.
    """
    offer = workspace_config.context_folders(workspace)
    if not offer:
        ctx = str(app_cfg.ai_context_dir).strip("/")
        return (ctx,) if ctx else ()
    sub = _subscription(app_cfg)
    if sub is None:
        return offer
    return tuple(f for f in offer if f in sub)


def sync_dirs(app_cfg, base_folders: tuple[str, ...], workspace: Path) -> tuple[str, ...]:
    """``base_folders`` (the repo's ``cfg.folders``) unioned with the repo's synced
    ``[sync] folders`` AND the whole team context OFFER.

    So every offered folder rides pull/push — and thus the pre-push secret scan — for
    any teammate whose ``[ai] context`` consent is on. The WHOLE offer syncs even when
    a Phase-2 subscription narrows what that user reads, keeping sync decoupled from
    read. Byte-identical to :func:`mooring.workspace_config.merge_extra_folders` when
    consent is off or the repo publishes no offer.
    """
    merged = workspace_config.merge_extra_folders(base_folders, workspace)
    if not app_cfg.ai_context:
        return merged
    offer = workspace_config.context_folders(workspace)
    if not offer:
        return merged
    return tuple(dict.fromkeys((*merged, *offer)))
