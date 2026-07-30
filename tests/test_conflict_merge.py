"""The cell-level conflict merge (app/conflict_merge.py) and its two hub routes.

The invariants worth pinning are the ones an analyst's work depends on: a cell
only ONE side touched merges itself, a cell BOTH sides touched is never decided
for them, the pre-merge bytes are recoverable, the merged file is local-only and
pushable, and anything the engine cannot merge honestly refuses so the three
whole-file resolutions stay the answer.
"""

import pytest
from conftest import FakeClient, read_local, write_local
from starlette.testclient import TestClient

from mooring import config, manifest, paths, sync, trash
from mooring.app import conflict_merge
from mooring.hub.server import Hub, create_app
from mooring.sync import ConflictStrategy, FileState


def nb(*cells: str) -> bytes:
    """A minimal valid marimo notebook with the given cell bodies, as bytes
    (the test_celldiff.py fixture — one notebook shape across the suite)."""
    parts = ['import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n']
    for code in cells:
        body = "\n".join("    " + line for line in code.splitlines())
        parts.append(f"@app.cell\ndef _():\n{body}\n    return\n\n\n")
    parts.append('if __name__ == "__main__":\n    app.run()\n')
    return "".join(parts).encode("utf-8")


NB = "notebooks/a.py"


def conflict(cfg, mine: bytes, theirs: bytes, base: bytes = None, path: str = NB):
    """Drive a real three-way CONFLICT: pull ``base``, edit locally to ``mine``,
    let a teammate push ``theirs``."""
    client = FakeClient()
    client.seed(path, base if base is not None else nb("x = 1", "y = 2", "z = 3"))
    sync.pull(client, cfg)
    write_local(cfg, path, mine.decode("utf-8"))
    client.seed(path, theirs)
    assert [f.state for f in sync.status(client, cfg).files if f.path == path] == [
        FileState.CONFLICT
    ]
    return client


def merged_cells(cfg, path: str = NB) -> list[str]:
    from mooring import marimo_rt

    return [code for _, code in marimo_rt.read_cells(read_local(cfg, path))]


# -- one-sided changes merge themselves -----------------------------------------


def test_disjoint_edits_need_no_choice_and_keep_both_sides(cfg):
    # The case this feature exists for: two people edited DIFFERENT cells.
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    assert (plan.auto_local, plan.auto_remote, plan.unchanged) == (1, 1, 1)
    assert plan.auto_merged == 2

    outcome = conflict_merge.apply(client, cfg, NB, {})
    assert merged_cells(cfg) == ["x = 99", "y = 2", "z = 42"]
    assert outcome.auto_merged == 2
    assert (outcome.chosen_local, outcome.chosen_remote) == (0, 0)


def test_a_cell_added_on_one_side_only_is_kept(cfg):
    client = conflict(cfg, nb("x = 1", "y = 2", "z = 3", "w = 4"), nb("x = 1", "y = 22", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    conflict_merge.apply(client, cfg, NB, {})
    assert merged_cells(cfg) == ["x = 1", "y = 22", "z = 3", "w = 4"]


def test_a_cell_added_by_the_team_lands_beside_the_code_it_followed(cfg):
    # The team inserted a cell after their first; placement is anchored on the
    # shared version, not appended blindly at the end.
    client = conflict(cfg, nb("x = 11", "y = 2", "z = 3"), nb("x = 1", "mid = 0", "y = 2", "z = 3"))
    conflict_merge.apply(client, cfg, NB, {})
    assert merged_cells(cfg) == ["x = 11", "mid = 0", "y = 2", "z = 3"]


def test_a_cell_deleted_on_one_side_only_is_dropped(cfg):
    client = conflict(cfg, nb("x = 1", "z = 3"), nb("x = 1", "y = 2", "z = 33"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    conflict_merge.apply(client, cfg, NB, {})
    assert merged_cells(cfg) == ["x = 1", "z = 33"]


def test_the_same_edit_on_both_sides_is_agreement_not_a_conflict(cfg):
    # The FILE conflicts (both shas moved) but the cells agree — never ask.
    client = conflict(cfg, nb("x = 1", "y = 5", "z = 3"), nb("x = 1", "y = 5", "z = 30"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    assert plan.auto_both == 1
    conflict_merge.apply(client, cfg, NB, {})
    assert merged_cells(cfg) == ["x = 1", "y = 5", "z = 30"]


# -- a cell both sides changed is always the user's call ------------------------


def test_both_sides_changed_one_cell_needs_a_choice(cfg):
    client = conflict(cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    (contested,) = plan.conflicts
    assert contested.origin == "base" and contested.index_base == 1
    assert (contested.local, contested.remote) == ("y = 200", "y = 300")
    assert "-y = 200" in contested.diff and "+y = 300" in contested.diff

    # No choice -> refuse. Never guess, and never write a half-decided file.
    with pytest.raises(ValueError):
        conflict_merge.apply(client, cfg, NB, {})
    assert read_local(cfg, NB) == nb("x = 1", "y = 200", "z = 3").decode("utf-8")

    outcome = conflict_merge.apply(client, cfg, NB, {contested.id: "remote"})
    assert merged_cells(cfg) == ["x = 1", "y = 300", "z = 3"]
    assert (outcome.chosen_local, outcome.chosen_remote) == (0, 1)


def test_keeping_my_version_of_a_contested_cell(cfg):
    client = conflict(cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    conflict_merge.apply(client, cfg, NB, {plan.conflicts[0].id: "local"})
    assert merged_cells(cfg) == ["x = 1", "y = 200", "z = 3"]


def test_delete_versus_edit_offers_dropping_the_cell(cfg):
    # I deleted the cell; the team edited it. Neither side is obviously right.
    client = conflict(cfg, nb("x = 1", "z = 3"), nb("x = 1", "y = 222", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    (contested,) = plan.conflicts
    assert contested.local is None and contested.remote == "y = 222"
    conflict_merge.apply(client, cfg, NB, {contested.id: "local"})
    assert merged_cells(cfg) == ["x = 1", "z = 3"]  # my deletion stands


def test_a_choice_is_refused_when_the_pick_is_not_a_side(cfg):
    client = conflict(cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    with pytest.raises(ValueError):
        conflict_merge.apply(client, cfg, NB, {plan.conflicts[0].id: "whatever"})


def test_both_sides_added_a_different_cell_in_one_place_is_a_choice(cfg):
    # Two independent additions that pair with each other: keeping both would put
    # two cells defining `total` in one marimo notebook — a hard runtime error.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", "total = x + y"),
        nb("x = 1", "y = 2", "z = 3", "total = x + y + z"),
    )
    plan = conflict_merge.plan(client, cfg, NB)
    (contested,) = plan.conflicts
    assert contested.origin == "both" and contested.index_base is None
    conflict_merge.apply(client, cfg, NB, {contested.id: "remote"})
    assert merged_cells(cfg) == ["x = 1", "y = 2", "z = 3", "total = x + y + z"]


def test_the_identical_cell_added_on_both_sides_appears_once(cfg):
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", "w = 4"),
        nb("x = 1", "y = 22", "z = 3", "w = 4"),
    )
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    conflict_merge.apply(client, cfg, NB, {})
    assert merged_cells(cfg) == ["x = 1", "y = 22", "z = 3", "w = 4"]


# -- non-destructive: the pre-image and the post-merge sync state ---------------


def test_the_pre_merge_bytes_are_recoverable_from_the_trash(cfg):
    mine = nb("x = 99", "y = 2", "z = 3")
    client = conflict(cfg, mine, nb("x = 1", "y = 2", "z = 42"))
    outcome = conflict_merge.apply(client, cfg, NB, {})

    (path, token) = outcome.trashed[0]
    assert path == NB
    (entry,) = trash.entries(cfg.workspace())
    assert entry["action"] == "merge-cells" and entry["path"] == NB
    # And the deposit really restores — the Undo affordance is not a promise on paper.
    trash.restore(cfg.workspace(), token)
    assert read_local(cfg, NB) == mine.decode("utf-8")


def test_after_merging_the_file_is_a_plain_modified_file_you_push(cfg):
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    remote_before = client.tree[NB]
    conflict_merge.apply(client, cfg, NB, {})

    # Nothing was published: the merge is local only.
    assert client.tree[NB] == remote_before
    assert manifest.load(cfg.workspace()).files[NB] == remote_before
    report = sync.status(client, cfg)
    assert [f.state for f in report.files if f.path == NB] == [FileState.MODIFIED]
    assert report.by_state(FileState.CONFLICT) == []

    # ...and the push the analyst then makes is accepted (the sha is current).
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert client.blobs[client.tree[NB]] == nb("x = 99", "y = 2", "z = 42")
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_a_merge_never_writes_before_it_can_finish(cfg):
    # A refused apply must leave the workspace byte-identical: the analyst's copy
    # is the only thing standing between them and a lost afternoon.
    mine = nb("x = 1", "y = 200", "z = 3")
    client = conflict(cfg, mine, nb("x = 1", "y = 300", "z = 3"))
    with pytest.raises(ValueError):
        conflict_merge.apply(client, cfg, NB, {})
    assert read_local(cfg, NB) == mine.decode("utf-8")
    assert trash.entries(cfg.workspace()) == []
    assert [f.state for f in sync.status(client, cfg).files if f.path == NB] == [
        FileState.CONFLICT
    ]


# -- staleness: the plan the user saw is the plan that gets applied -------------


def test_a_teammate_push_between_plan_and_apply_is_refused(cfg):
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    plan = conflict_merge.plan(client, cfg, NB)
    expect = {
        "base_sha": plan.base_sha,
        "local_sha": plan.local_sha,
        "remote_sha": plan.remote_sha,
    }
    client.seed(NB, nb("x = 1", "y = 2", "z = 43"))  # they pushed again
    with pytest.raises(conflict_merge.MergeStale):
        conflict_merge.apply(client, cfg, NB, {}, expect=expect)


def test_my_own_edit_between_plan_and_apply_is_refused(cfg):
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    plan = conflict_merge.plan(client, cfg, NB)
    expect = {"local_sha": plan.local_sha, "remote_sha": plan.remote_sha}
    write_local(cfg, NB, nb("x = 100", "y = 2", "z = 3").decode("utf-8"))
    with pytest.raises(conflict_merge.MergeStale):
        conflict_merge.apply(client, cfg, NB, {}, expect=expect)


# -- refusing honestly: every degradation keeps the three whole-file options ----


def test_a_non_notebook_file_is_never_merged_per_cell(cfg):
    client = FakeClient()
    client.seed("notebooks/data.csv", b"a,b\n1,2\n")
    sync.pull(client, cfg)
    write_local(cfg, "notebooks/data.csv", "a,b\n9,9\n")
    client.seed("notebooks/data.csv", b"a,b\n3,4\n")
    with pytest.raises(conflict_merge.MergeUnavailable):
        conflict_merge.plan(client, cfg, "notebooks/data.csv")
    # The whole-file resolutions still work on it, untouched.
    sync.resolve(client, cfg, "notebooks/data.csv", ConflictStrategy.THEIRS)
    assert read_local(cfg, "notebooks/data.csv") == "a,b\n3,4\n"


def test_a_plain_python_module_is_not_a_notebook(cfg):
    client = conflict(
        cfg, b"VALUE = 2\n", b"VALUE = 3\n", base=b"VALUE = 1\n", path="notebooks/helpers.py"
    )
    with pytest.raises(conflict_merge.MergeUnavailable, match="not a marimo notebook"):
        conflict_merge.plan(client, cfg, "notebooks/helpers.py")


def test_a_wholesale_restructure_degrades_instead_of_guessing(cfg):
    # Nothing of the shared version survives locally — pairing what is left would
    # be a guess dressed up as a per-cell choice.
    client = conflict(
        cfg,
        nb("alpha = 'completely'", "beta = 'different'", "gamma = 'rewrite'"),
        nb("x = 1", "y = 2", "z = 30"),
    )
    with pytest.raises(conflict_merge.MergeUnavailable, match="restructured"):
        conflict_merge.plan(client, cfg, NB)
    sync.resolve(client, cfg, NB, ConflictStrategy.KEEP_BOTH)  # the fallback still works
    assert list((cfg.workspace() / "notebooks").glob("a.remote-*.py"))


def test_an_unparseable_side_degrades(cfg):
    client = conflict(cfg, b"import marimo\napp = marimo.App(\nbroken(", nb("x = 1", "y = 2"))
    with pytest.raises(conflict_merge.MergeUnavailable):
        conflict_merge.plan(client, cfg, NB)


def test_a_file_created_on_both_sides_has_nothing_to_merge_against(cfg):
    # classify() calls this a conflict, but there is no common ancestor.
    client = FakeClient()
    write_local(cfg, NB, nb("mine = 1").decode("utf-8"))
    client.seed(NB, nb("theirs = 1"))
    assert [f.state for f in sync.status(client, cfg).files if f.path == NB] == [
        FileState.CONFLICT
    ]
    with pytest.raises(conflict_merge.MergeUnavailable, match="separately"):
        conflict_merge.plan(client, cfg, NB)


def test_a_remotely_deleted_file_has_no_cells_to_merge_with(cfg):
    client = FakeClient()
    client.seed(NB, nb("x = 1"))
    sync.pull(client, cfg)
    write_local(cfg, NB, nb("x = 2").decode("utf-8"))
    client.remove(NB)
    with pytest.raises(conflict_merge.MergeUnavailable, match="deleted"):
        conflict_merge.plan(client, cfg, NB)


def test_a_locally_deleted_file_points_at_use_remote(cfg):
    client = FakeClient()
    client.seed(NB, nb("x = 1"))
    sync.pull(client, cfg)
    (cfg.workspace() / NB).unlink()
    client.seed(NB, nb("x = 2"))
    with pytest.raises(conflict_merge.MergeUnavailable, match="Use remote"):
        conflict_merge.plan(client, cfg, NB)


# -- the hub endpoints ----------------------------------------------------------


@pytest.fixture
def merge_client(tmp_path, monkeypatch):
    """A configured hub over a tmp workspace whose GitHub is the in-memory fake."""
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.setenv("MOORING_TOKEN", "t")
    spec = config.RepoSpec(
        alias="ws", owner="acme", repo="nbs", workspace_path=str(tmp_path / "ws")
    )
    hub = Hub(config.AppConfig(repos=(spec,), active_alias="ws"))
    fake = FakeClient()
    monkeypatch.setattr(Hub, "client", lambda self: fake)
    with TestClient(create_app(hub)) as client:
        yield client, fake, hub.cfg


def test_endpoint_plans_then_writes_the_merge(merge_client):
    client, fake, cfg = merge_client
    fake.seed(NB, nb("x = 1", "y = 2", "z = 3"))
    sync.pull(fake, cfg)
    write_local(cfg, NB, nb("x = 99", "y = 2", "z = 3").decode("utf-8"))
    fake.seed(NB, nb("x = 1", "y = 2", "z = 42"))

    plan = client.post("/api/resolve/cells", json={"path": NB}).json()
    assert plan["auto_merged"] == 2 and plan["unchanged"] == 1
    assert [c["status"] for c in plan["cells"]] == ["auto", "auto", "auto"]
    # The payload describes cells; it never ships their source.
    assert not any("code" in c or "local" in c for c in plan["cells"])

    resp = client.post(
        "/api/resolve/cells/apply",
        json={
            "path": NB,
            "choices": {},
            "base_sha": plan["base_sha"],
            "local_sha": plan["local_sha"],
            "remote_sha": plan["remote_sha"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_merged"] == 2
    assert body["trashed"] and body["trashed"][0]["path"] == NB  # drives the Undo toast
    assert read_local(cfg, NB) == nb("x = 99", "y = 2", "z = 42").decode("utf-8")


def test_endpoint_409s_with_unavailable_so_the_hub_keeps_the_other_options(merge_client):
    client, fake, cfg = merge_client
    fake.seed("notebooks/data.csv", b"a\n1\n")
    sync.pull(fake, cfg)
    write_local(cfg, "notebooks/data.csv", "a\n9\n")
    fake.seed("notebooks/data.csv", b"a\n3\n")
    resp = client.post("/api/resolve/cells", json={"path": "notebooks/data.csv"})
    assert resp.status_code == 409
    assert resp.json()["unavailable"] is True


def test_endpoint_409s_when_a_side_moved_since_the_plan(merge_client):
    client, fake, cfg = merge_client
    fake.seed(NB, nb("x = 1", "y = 2", "z = 3"))
    sync.pull(fake, cfg)
    write_local(cfg, NB, nb("x = 99", "y = 2", "z = 3").decode("utf-8"))
    fake.seed(NB, nb("x = 1", "y = 2", "z = 42"))
    plan = client.post("/api/resolve/cells", json={"path": NB}).json()
    fake.seed(NB, nb("x = 1", "y = 2", "z = 43"))

    resp = client.post(
        "/api/resolve/cells/apply",
        json={
            "path": NB,
            "choices": {},
            "base_sha": plan["base_sha"],
            "local_sha": plan["local_sha"],
            "remote_sha": plan["remote_sha"],
        },
    )
    assert resp.status_code == 409 and resp.json()["stale"] is True


def test_endpoint_400s_on_a_missing_choice_and_an_escaping_path(merge_client):
    client, fake, cfg = merge_client
    fake.seed(NB, nb("x = 1", "y = 2"))
    sync.pull(fake, cfg)
    write_local(cfg, NB, nb("x = 1", "y = 200").decode("utf-8"))
    fake.seed(NB, nb("x = 1", "y = 300"))
    assert client.post("/api/resolve/cells/apply", json={"path": NB}).status_code == 400
    assert client.post("/api/resolve/cells", json={"path": "../out.py"}).status_code == 400
