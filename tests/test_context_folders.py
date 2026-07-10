"""The read/sync/shareable folder resolver that composes the per-machine config with
the synced team OFFER (mooring.app.context_folders)."""

from __future__ import annotations

from types import SimpleNamespace

from mooring import workspace_config as wc
from mooring.app import context_folders as ctxdirs


def _cfg(*, context_dir="context", context=True):
    """A minimal AppConfig stand-in: only ai_context_dir / ai_context are read."""
    return SimpleNamespace(ai_context_dir=context_dir, ai_context=context)


def test_shareable_dirs_falls_back_to_legacy_single_folder(tmp_path):
    # No offer published → the per-machine [ai] context_dir, byte-identical to before.
    assert ctxdirs.shareable_dirs(_cfg(), tmp_path) == ("context",)


def test_shareable_dirs_uses_the_offer_when_present(tmp_path):
    wc.set_context_folder(tmp_path, "zeta", True)
    wc.set_context_folder(tmp_path, "alpha", True)
    # The offer REPLACES the legacy default and is sorted.
    assert ctxdirs.shareable_dirs(_cfg(), tmp_path) == ("alpha", "zeta")


def test_shareable_dirs_empty_when_no_offer_and_no_legacy(tmp_path):
    assert ctxdirs.shareable_dirs(_cfg(context_dir=""), tmp_path) == ()


def test_read_dirs_equals_shareable_in_phase1(tmp_path):
    wc.set_context_folder(tmp_path, "finance/dict", True)
    cfg = _cfg()
    assert ctxdirs.read_dirs(cfg, tmp_path) == ctxdirs.shareable_dirs(cfg, tmp_path)


def test_sync_dirs_folds_the_offer_when_consent_on(tmp_path):
    wc.set_context_folder(tmp_path, "finance/dict", True)
    base = ("notebooks", "data")
    got = ctxdirs.sync_dirs(_cfg(context=True), base, tmp_path)
    assert got == ("notebooks", "data", "finance/dict")


def test_sync_dirs_does_not_sync_offer_when_consent_off(tmp_path):
    wc.set_context_folder(tmp_path, "finance/dict", True)
    base = ("notebooks", "data")
    # Consent off → "off = neither read nor synced": byte-identical to merge_extra_folders.
    assert ctxdirs.sync_dirs(_cfg(context=False), base, tmp_path) == wc.merge_extra_folders(
        base, tmp_path
    )


def test_sync_dirs_dedupes_offer_already_in_base(tmp_path):
    wc.set_context_folder(tmp_path, "data", True)  # already a base folder
    base = ("notebooks", "data")
    assert ctxdirs.sync_dirs(_cfg(context=True), base, tmp_path) == ("notebooks", "data")


def test_sync_dirs_no_offer_is_just_merge_extra_folders(tmp_path):
    base = ("notebooks", "data")
    assert ctxdirs.sync_dirs(_cfg(context=True), base, tmp_path) == wc.merge_extra_folders(
        base, tmp_path
    )


# -- nested offers: containment pruning ----------------------------------------
# A top-level context/ sibling never overlapped the synced folders. A NESTED offer does,
# and an overlapping cfg.folders makes sync.synced_paths rglob the same files twice.


def test_sync_dirs_prunes_an_offer_nested_inside_a_synced_folder(tmp_path):
    wc.set_context_folder(tmp_path, "notebooks/context", True)
    got = ctxdirs.sync_dirs(_cfg(context=True), ("notebooks",), tmp_path)
    # Already covered by "notebooks" — naming it again would double-walk the subtree.
    assert got == ("notebooks",)


def test_sync_dirs_prunes_a_base_folder_nested_inside_a_broader_offer(tmp_path):
    # The other direction: the offer is the PARENT of an already-synced folder.
    wc.set_context_folder(tmp_path, "notebooks", True)
    got = ctxdirs.sync_dirs(_cfg(context=True), ("notebooks/team-a", "data"), tmp_path)
    assert got == ("data", "notebooks")


def test_sync_dirs_keeps_a_sibling_offer_that_overlaps_nothing(tmp_path):
    wc.set_context_folder(tmp_path, "reports/finance", True)
    got = ctxdirs.sync_dirs(_cfg(context=True), ("notebooks",), tmp_path)
    assert got == ("notebooks", "reports/finance")


def test_sync_dirs_containment_is_slash_bounded(tmp_path):
    # "notebooks" must not swallow the sibling "notebooks2" — the classic prefix bug.
    wc.set_context_folder(tmp_path, "notebooks2", True)
    got = ctxdirs.sync_dirs(_cfg(context=True), ("notebooks",), tmp_path)
    assert got == ("notebooks", "notebooks2")


def test_pruned_scope_selects_exactly_the_same_files(tmp_path):
    """The prune shrinks the cfg.folders TUPLE, never the set of synced files — and it is
    the duplicate walk (and the duplicate blob hash behind it) that disappears."""
    from mooring import sync

    (tmp_path / "notebooks" / "context").mkdir(parents=True)
    (tmp_path / "notebooks" / "top.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "notebooks" / "context" / "instructions.md").write_text("hi", encoding="utf-8")
    wc.set_context_folder(tmp_path, "notebooks/context", True)

    overlapping = ("notebooks", "notebooks/context")
    pruned = ctxdirs.sync_dirs(_cfg(context=True), ("notebooks",), tmp_path)
    assert pruned == ("notebooks",)

    dup = list(sync.synced_paths(tmp_path, overlapping))
    clean = list(sync.synced_paths(tmp_path, pruned))
    assert len(dup) != len(set(dup))  # the bug the prune removes
    assert len(clean) == len(set(clean))  # no file walked twice
    assert set(clean) == set(dup)  # ...and none lost
    # local_report has no dict to hide the duplicate, so it listed the file twice.
    assert len(sync.local_report(tmp_path, overlapping).files) > len(
        sync.local_report(tmp_path, pruned).files
    )


# -- read_dirs: the per-user subscription ∩ offer (Phase 2) ---------------------


def _app_cfg(sub, *, context_dir="context"):
    """A real AppConfig with one active repo carrying the given subscription."""
    from mooring import config
    from mooring.ai_config import AiConfig

    spec = config.RepoSpec(alias="ws", owner="o", repo="r", context_folders=sub)
    return config.AppConfig(
        repos=(spec,), active_alias="ws", ai=AiConfig(context=True, context_dir=context_dir)
    )


def test_read_dirs_no_offer_falls_back_to_legacy(tmp_path):
    # A subscription only narrows an offer — it can't invent one.
    assert ctxdirs.read_dirs(_app_cfg(("finance",)), tmp_path) == ("context",)


def test_read_dirs_unsubscribed_reads_whole_offer(tmp_path):
    wc.set_context_folder(tmp_path, "a", True)
    wc.set_context_folder(tmp_path, "b", True)
    # None subscription = the opt-out default: publishing an offer never blanks a user.
    assert ctxdirs.read_dirs(_app_cfg(None), tmp_path) == ("a", "b")


def test_read_dirs_subscription_intersects_offer(tmp_path):
    wc.set_context_folder(tmp_path, "a", True)
    wc.set_context_folder(tmp_path, "b", True)
    # Explicit subset — offer order is authoritative.
    assert ctxdirs.read_dirs(_app_cfg(("b",)), tmp_path) == ("b",)


def test_read_dirs_empty_subscription_reads_nothing(tmp_path):
    wc.set_context_folder(tmp_path, "a", True)
    assert ctxdirs.read_dirs(_app_cfg(()), tmp_path) == ()


def test_read_dirs_stale_subscription_is_bounded_by_offer(tmp_path):
    # An unpublished folder in a stale subscription drops out (offer is the ceiling).
    wc.set_context_folder(tmp_path, "a", True)
    assert ctxdirs.read_dirs(_app_cfg(("a", "gone")), tmp_path) == ("a",)


def test_config_store_subscription_round_trip(tmp_path, monkeypatch):
    # The silent-drop guard: a subscription written to config.toml must survive
    # repo_specs_from_data → AppConfig.spec() unchanged, and None/[] must round-trip.
    from mooring import config, config_store, paths

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "cfg")
    config_store.add_repo("ws", owner="acme", repo="analytics", workspace=str(tmp_path / "ws"))

    config_store.set_repo_context_folders("ws", ["finance/dict", "a"])
    assert config.load_app_config(env={}).spec("ws").context_folders == ("a", "finance/dict")

    config_store.set_repo_context_folders("ws", [])  # explicit "read nothing"
    assert config.load_app_config(env={}).spec("ws").context_folders == ()

    config_store.set_repo_context_folders("ws", None)  # clear → read whole offer
    assert config.load_app_config(env={}).spec("ws").context_folders is None
