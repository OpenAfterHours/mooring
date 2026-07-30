"""The cell-level conflict merge (app/conflict_merge.py) and its two hub routes.

The governing rule for this feature is that a WRONG merge is far worse than no
merge, so the tests come in two halves: the merges that must happen without
asking (a cell only one side touched), and — the longer half — the cases where
mooring must refuse and hand the conflict back to the three whole-file
resolutions rather than guess. A guess here does not mislabel a panel; it writes
the wrong cell to the analyst's disk and then pushes it to the team.
"""

import pytest
from conftest import FakeClient, read_local, write_local
from starlette.testclient import TestClient

from mooring import config, manifest, paths, sync, trash
from mooring.app import conflict_merge
from mooring.hub.server import Hub, create_app
from mooring.sync import ConflictStrategy, FileState


def nb(*cells: str, header: str = "", app: str = "", decorators: dict | None = None) -> bytes:
    """A minimal valid marimo notebook with the given cell bodies, as bytes (the
    test_celldiff.py fixture — one notebook shape across the suite), plus the frame
    pieces a merge has to carry: a PEP 723 ``header`` block, extra
    ``marimo.App(...)`` arguments, and per-cell ``@app.cell(...)`` options."""
    parts = [header, f'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App({app})\n\n\n']
    for index, code in enumerate(cells):
        body = "\n".join("    " + line for line in code.splitlines())
        options = (decorators or {}).get(index, "")
        # Bare `@app.cell` with no options, exactly as marimo's codegen emits it, so
        # a merged file can be compared byte-for-byte against this fixture.
        decorator = f"@app.cell({options})" if options else "@app.cell"
        parts.append(f"{decorator}\ndef _():\n{body}\n    return\n\n\n")
    parts.append('if __name__ == "__main__":\n    app.run()\n')
    return "".join(parts).encode("utf-8")


NB = "notebooks/a.py"
FRESH = ("base_sha", "local_sha", "remote_sha")


def expect_of(plan) -> dict:
    return {key: getattr(plan, key) for key in FRESH}


def merge(client, cfg, choices=None, path: str = NB):
    """Plan then apply, always passing the plan's own three shas. The staleness key
    is mandatory, so no test can accidentally exercise a blank one."""
    plan = conflict_merge.plan(client, cfg, path)
    return conflict_merge.apply(client, cfg, path, choices or {}, expect=expect_of(plan))


def conflict(cfg, mine: bytes, theirs: bytes, base: bytes = None, path: str = NB):
    """Drive a real three-way CONFLICT: pull ``base``, edit locally to ``mine``, let
    a teammate push ``theirs``."""
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

    outcome = merge(client, cfg)
    assert merged_cells(cfg) == ["x = 99", "y = 2", "z = 42"]
    assert outcome.auto_merged == 2
    assert (outcome.chosen_local, outcome.chosen_remote) == (0, 0)


def test_a_cell_added_on_one_side_only_is_kept(cfg):
    client = conflict(cfg, nb("x = 1", "y = 2", "z = 3", "w = 4"), nb("x = 1", "y = 22", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    merge(client, cfg)
    assert merged_cells(cfg) == ["x = 1", "y = 22", "z = 3", "w = 4"]


def test_a_cell_added_by_the_team_lands_beside_the_code_it_followed(cfg):
    # The team inserted a cell after their first; placement is anchored on the
    # shared version, not appended blindly at the end.
    client = conflict(cfg, nb("x = 11", "y = 2", "z = 3"), nb("x = 1", "mid = 0", "y = 2", "z = 3"))
    merge(client, cfg)
    assert merged_cells(cfg) == ["x = 11", "mid = 0", "y = 2", "z = 3"]


def test_a_cell_deleted_on_one_side_only_is_dropped(cfg):
    client = conflict(cfg, nb("x = 1", "z = 3"), nb("x = 1", "y = 2", "z = 33"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    merge(client, cfg)
    assert merged_cells(cfg) == ["x = 1", "z = 33"]


def test_the_same_edit_on_both_sides_is_agreement_not_a_conflict(cfg):
    # The FILE conflicts (both shas moved) but the cells agree — never ask.
    client = conflict(cfg, nb("x = 1", "y = 5", "z = 3"), nb("x = 1", "y = 5", "z = 30"))
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    assert plan.auto_both == 1
    merge(client, cfg)
    assert merged_cells(cfg) == ["x = 1", "y = 5", "z = 30"]


# -- both sides added something: KEEP BOTH, never pair by similarity ------------


def test_two_unrelated_additions_are_both_kept(cfg):
    # THE regression that must never come back. `import polars as pl` and
    # `import altair as alt` are 77% similar and completely unrelated; pairing
    # them by similarity turned "we each added an import" into a choice whose
    # either answer deleted a teammate's cell. git would keep both; so do we.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", "import polars as pl"),
        nb("x = 1", "y = 2", "z = 3", "import altair as alt"),
    )
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    merge(client, cfg)
    assert merged_cells(cfg) == [
        "x = 1",
        "y = 2",
        "z = 3",
        "import polars as pl",
        "import altair as alt",
    ]


def test_two_similar_but_distinct_additions_are_both_kept(cfg):
    # 83% similar, two genuinely different frames.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", 'df_north = load("north")'),
        nb("x = 1", "y = 2", "z = 3", 'df_south = load("south")'),
    )
    assert conflict_merge.plan(client, cfg, NB).conflicts == ()
    merge(client, cfg)
    assert merged_cells(cfg)[-2:] == ['df_north = load("north")', 'df_south = load("south")']


def test_the_identical_cell_added_on_both_sides_appears_once(cfg):
    # Byte-identical is IDENTITY, not similarity — the one safe collapse.
    client = conflict(
        cfg, nb("x = 1", "y = 2", "z = 3", "w = 4"), nb("x = 1", "y = 22", "z = 3", "w = 4")
    )
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == () and plan.auto_both == 1
    merge(client, cfg)
    assert merged_cells(cfg) == ["x = 1", "y = 22", "z = 3", "w = 4"]


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
        conflict_merge.apply(client, cfg, NB, {}, expect=expect_of(plan))
    assert read_local(cfg, NB) == nb("x = 1", "y = 200", "z = 3").decode("utf-8")

    outcome = merge(client, cfg, {contested.id: "remote"})
    assert merged_cells(cfg) == ["x = 1", "y = 300", "z = 3"]
    assert (outcome.chosen_local, outcome.chosen_remote) == (0, 1)


def test_keeping_my_version_of_a_contested_cell(cfg):
    client = conflict(cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    merge(client, cfg, {plan.conflicts[0].id: "local"})
    assert merged_cells(cfg) == ["x = 1", "y = 200", "z = 3"]


def test_delete_versus_edit_offers_dropping_the_cell(cfg):
    # I deleted the cell; the team edited it. Neither side is obviously right.
    client = conflict(cfg, nb("x = 1", "z = 3"), nb("x = 1", "y = 222", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    (contested,) = plan.conflicts
    assert contested.local is None and contested.remote == "y = 222"
    merge(client, cfg, {contested.id: "local"})
    assert merged_cells(cfg) == ["x = 1", "z = 3"]  # my deletion stands


def test_a_choice_is_refused_when_the_pick_is_not_a_side(cfg):
    client = conflict(cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    with pytest.raises(ValueError):
        conflict_merge.apply(
            client, cfg, NB, {plan.conflicts[0].id: "whatever"}, expect=expect_of(plan)
        )


# -- a merged notebook marimo would refuse to run is never written --------------


def test_two_additions_defining_the_same_name_refuse_rather_than_break_the_notebook(cfg):
    # Individually valid halves, composed into a MultipleDefinitionError. Nothing
    # downstream catches this: it would write, push, and break for the whole team.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", "total = x + y"),
        nb("x = 1", "y = 2", "z = 3", "total = x + y + z"),
    )
    with pytest.raises(conflict_merge.MergeUnavailable, match="'total' in two cells"):
        merge(client, cfg)
    assert read_local(cfg, NB) == nb("x = 1", "y = 2", "z = 3", "total = x + y").decode("utf-8")
    assert trash.entries(cfg.workspace()) == []


def test_a_rename_colliding_with_a_teammates_new_cell_never_writes(cfg):
    # I rename base cell 3's variable to `total`; they append a cell defining
    # `total`. Reported clean before the fix, and the written file did not run.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "total = 3"),
        nb("x = 1", "y = 2", "z = 3", "total = x + y"),
    )
    with pytest.raises(conflict_merge.MergeUnavailable):
        merge(client, cfg)
    assert read_local(cfg, NB) == nb("x = 1", "y = 2", "total = 3").decode("utf-8")


def test_cell_local_underscore_names_do_not_collide(cfg):
    # marimo scopes an `_`-prefixed name to its own cell, so two cells may both
    # define `_tmp` — refusing on those would make the feature useless.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", "_tmp = 1\nfirst = _tmp"),
        nb("x = 1", "y = 2", "z = 3", "_tmp = 2\nsecond = _tmp"),
    )
    merge(client, cfg)
    assert merged_cells(cfg)[-2:] == ["_tmp = 1\nfirst = _tmp", "_tmp = 2\nsecond = _tmp"]


def test_definitions_inside_a_function_body_are_that_scope_s_not_the_cell_s(cfg):
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", "def mine():\n    total = 1\n    return total"),
        nb("x = 1", "y = 2", "z = 3", "def theirs():\n    total = 2\n    return total"),
    )
    merge(client, cfg)  # `total` is local to each function — no collision
    assert len(merged_cells(cfg)) == 5


# -- rewritten vs deleted: never tell an analyst they deleted something ---------


def test_a_cell_rewritten_past_the_similarity_threshold_is_still_that_cell(cfg):
    # Textually 29% similar to the base cell, but it still defines `revenue` —
    # marimo's own dataflow identity says these are one cell. Before the fix the
    # panel called it "you deleted it" plus a new cell, and taking the team's
    # version duplicated `revenue`.
    rewrite = 'revenue = pl.read_parquet("s").select(pl.col("amount").sum()).item() * 1.2'
    client = conflict(
        cfg,
        nb("import polars as pl", rewrite, "z = 3"),
        nb("import polars as pl", "revenue = 1", "z = 30"),
        base=nb("import polars as pl", "revenue = 1", "z = 3"),
    )
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.conflicts == ()
    # One cell, changed by me and taken — not a deletion plus an addition.
    assert [(c.origin, c.side) for c in plan.cells] == [
        ("base", "unchanged"),
        ("base", "local"),
        ("base", "remote"),
    ]
    merge(client, cfg)
    assert merged_cells(cfg) == ["import polars as pl", rewrite, "z = 30"]


def test_an_unmatchable_cell_on_both_sides_refuses_rather_than_guessing(cfg):
    # Nothing in common textually and no shared definition: mooring cannot tell a
    # rewrite from a delete-plus-add, so it says so instead of picking a story.
    client = conflict(
        cfg,
        nb("a = 1", "b = 2", "totally = 'different'"),
        nb("a = 1", "b = 2", "c = 30"),
        base=nb("a = 1", "b = 2", "c = 3"),
    )
    with pytest.raises(conflict_merge.MergeUnavailable, match="cannot line up"):
        conflict_merge.plan(client, cfg, NB)


# -- the notebook's frame (PEP 723 header + marimo.App options) -----------------


PEP723 = '# /// script\n# dependencies = ["polars==1.2"]\n# ///\n'


def test_the_teams_script_dependencies_survive_the_merge(cfg):
    # Rebuilding from cells alone dropped the team's dependency pin, and the next
    # push published that revert — a teammate's work deleted by a "merge".
    client = conflict(
        cfg,
        nb("x = 99", "y = 2", "z = 3"),
        nb("x = 1", "y = 2", "z = 42", header=PEP723, app='app_title="Sales"'),
    )
    plan = conflict_merge.plan(client, cfg, NB)
    assert plan.frame_side == "remote"
    merge(client, cfg)
    written = read_local(cfg, NB)
    assert 'dependencies = ["polars==1.2"]' in written
    assert 'app_title="Sales"' in written
    assert merged_cells(cfg) == ["x = 99", "y = 2", "z = 42"]


def test_my_own_header_survives_when_the_team_did_not_touch_theirs(cfg):
    client = conflict(
        cfg,
        nb("x = 99", "y = 2", "z = 3", header=PEP723),
        nb("x = 1", "y = 2", "z = 42"),
    )
    assert conflict_merge.plan(client, cfg, NB).frame_side == "local"
    merge(client, cfg)
    assert 'dependencies = ["polars==1.2"]' in read_local(cfg, NB)


def test_both_sides_changing_the_header_refuses(cfg):
    # A frame cannot be split cell by cell, so there is nothing honest to offer.
    client = conflict(
        cfg,
        nb("x = 1", "y = 2", "z = 3", header='# /// script\n# dependencies = ["polars"]\n# ///\n'),
        nb("x = 1", "y = 2", "z = 3", header='# /// script\n# dependencies = ["pandas"]\n# ///\n'),
    )
    with pytest.raises(conflict_merge.MergeUnavailable, match="header"):
        conflict_merge.plan(client, cfg, NB)


def test_a_taken_remote_cell_keeps_its_app_cell_options(cfg):
    # The team deliberately DISABLED an expensive cell. Emitting it as a bare
    # @app.cell re-enables it, so it runs the moment the notebook is opened.
    client = conflict(
        cfg,
        nb("x = 99", "y = 2", "z = 3"),
        nb("x = 1", "y = 2", "z = 42", decorators={2: "disabled=True, hide_code=True"}),
    )
    merge(client, cfg)
    written = read_local(cfg, NB)
    assert "@app.cell(disabled=True, hide_code=True)" in written
    assert merged_cells(cfg) == ["x = 99", "y = 2", "z = 42"]


# -- non-destructive: the pre-image and the post-merge sync state ---------------


def test_the_pre_merge_bytes_are_recoverable_from_the_trash(cfg):
    mine = nb("x = 99", "y = 2", "z = 3")
    client = conflict(cfg, mine, nb("x = 1", "y = 2", "z = 42"))
    outcome = merge(client, cfg)

    (path, token) = outcome.trashed[0]
    assert path == NB
    (entry,) = trash.entries(cfg.workspace())
    assert entry["action"] == "merge-cells" and entry["path"] == NB
    # And the deposit really restores — the Undo affordance is not a promise on paper.
    trash.restore(cfg.workspace(), token)
    assert read_local(cfg, NB) == mine.decode("utf-8")


def test_a_merge_that_cannot_bank_a_pre_image_does_not_write(cfg, monkeypatch):
    # sync's pre-images are best-effort because GitHub still has them; these are
    # the analyst's UNPUSHED work, so the trash IS the recovery story.
    mine = nb("x = 99", "y = 2", "z = 3")
    client = conflict(cfg, mine, nb("x = 1", "y = 2", "z = 42"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(trash, "deposit", boom)
    with pytest.raises(conflict_merge.MergeUnavailable, match="recoverable copy"):
        merge(client, cfg)
    assert read_local(cfg, NB) == mine.decode("utf-8")
    assert [f.state for f in sync.status(client, cfg).files if f.path == NB] == [
        FileState.CONFLICT
    ]


def test_a_merge_that_cannot_bank_because_of_the_size_cap_does_not_write(cfg, monkeypatch):
    mine = nb("x = 99", "y = 2", "z = 3")
    client = conflict(cfg, mine, nb("x = 1", "y = 2", "z = 42"))
    monkeypatch.setattr(trash, "deposit", lambda *a, **k: None)  # over the per-file cap
    with pytest.raises(conflict_merge.MergeUnavailable, match="too large"):
        merge(client, cfg)
    assert read_local(cfg, NB) == mine.decode("utf-8")


def test_after_merging_the_file_is_a_plain_modified_file_you_push(cfg):
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    remote_before = client.tree[NB]
    merge(client, cfg)

    # Nothing was published: the merge is local only.
    assert client.tree[NB] == remote_before
    assert manifest.load(cfg.workspace()).files[NB] == remote_before
    report = sync.status(client, cfg)
    assert [f.state for f in report.files if f.path == NB] == [FileState.MODIFIED]
    assert report.by_state(FileState.CONFLICT) == []

    # ...and the push the analyst then makes is accepted (the sha is current).
    result = sync.push(client, cfg, sleep=lambda s: None)
    assert result.pushed == 1
    assert merged_cells(cfg) == ["x = 99", "y = 2", "z = 42"]
    assert sync.status(client, cfg).by_state(FileState.CONFLICT) == []


def test_a_merge_never_writes_before_it_can_finish(cfg):
    # A refused apply must leave the workspace byte-identical: the analyst's copy
    # is the only thing standing between them and a lost afternoon.
    mine = nb("x = 1", "y = 200", "z = 3")
    client = conflict(cfg, mine, nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    with pytest.raises(ValueError):
        conflict_merge.apply(client, cfg, NB, {}, expect=expect_of(plan))
    assert read_local(cfg, NB) == mine.decode("utf-8")
    assert trash.entries(cfg.workspace()) == []
    assert [f.state for f in sync.status(client, cfg).files if f.path == NB] == [
        FileState.CONFLICT
    ]


# -- staleness: the plan the user saw is the plan that gets applied -------------


def test_a_teammate_push_between_plan_and_apply_is_refused(cfg):
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    expect = expect_of(conflict_merge.plan(client, cfg, NB))
    client.seed(NB, nb("x = 1", "y = 2", "z = 43"))  # they pushed again
    with pytest.raises(conflict_merge.MergeStale):
        conflict_merge.apply(client, cfg, NB, {}, expect=expect)


def test_my_own_edit_between_plan_and_apply_is_refused(cfg):
    client = conflict(cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    expect = expect_of(conflict_merge.plan(client, cfg, NB))
    write_local(cfg, NB, nb("x = 100", "y = 2", "z = 3").decode("utf-8"))
    with pytest.raises(conflict_merge.MergeStale):
        conflict_merge.apply(client, cfg, NB, {}, expect=expect)


@pytest.mark.parametrize("missing", FRESH)
def test_a_merge_that_does_not_say_which_versions_it_saw_is_refused(cfg, missing):
    # Fail-CLOSED: a blank sha is a malformed request, not a waiver. Cell ids are
    # positional, so an unchecked plan silently re-binds every choice the user made.
    client = conflict(cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = conflict_merge.plan(client, cfg, NB)
    expect = expect_of(plan) | {missing: ""}
    with pytest.raises(ValueError, match=missing):
        conflict_merge.apply(client, cfg, NB, {plan.conflicts[0].id: "local"}, expect=expect)
    assert read_local(cfg, NB) == nb("x = 1", "y = 200", "z = 3").decode("utf-8")


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


def test_the_restructure_floor_is_fail_closed_at_exactly_half(cfg):
    # Two of four base cells kept is EXACTLY the floor. A boundary that decides
    # whether an analyst's cells are merged or guessed at belongs on the safe side.
    base = nb("a = 1", "b = 2", "c = 3", "d = 4")
    client = conflict(cfg, nb("a = 1", "b = 2"), nb("a = 1", "b = 2", "c = 3", "d = 44"), base=base)
    with pytest.raises(conflict_merge.MergeUnavailable, match="restructured"):
        conflict_merge.plan(client, cfg, NB)

    # Three of four is above the floor, so the merge is offered — and my deletion
    # of `d` against their edit of it is a plain delete-versus-edit choice.
    client = conflict(
        cfg, nb("a = 1", "b = 2", "c = 3"), nb("a = 1", "b = 2", "c = 3", "d = 44"), base=base
    )
    (contested,) = conflict_merge.plan(client, cfg, NB).conflicts
    assert (contested.index_base, contested.local, contested.remote) == (3, None, "d = 44")


def test_an_unparseable_side_names_that_side_rather_than_reading_zero_cells(cfg):
    # marimo's converter NEVER fails on bad input: it swallows what it cannot parse
    # into the header and returns zero cells. Read leniently, that reads as "they
    # deleted everything". The loud reader is what makes the message name the side.
    client = conflict(cfg, b"import marimo\napp = marimo.App(\nbroken(", nb("x = 1", "y = 2"))
    with pytest.raises(conflict_merge.MergeUnavailable, match="could not read the cells"):
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


def _stage(fake, cfg, mine: bytes, theirs: bytes):
    fake.seed(NB, nb("x = 1", "y = 2", "z = 3"))
    sync.pull(fake, cfg)
    write_local(cfg, NB, mine.decode("utf-8"))
    fake.seed(NB, theirs)


def test_endpoint_plans_then_writes_the_merge(merge_client):
    client, fake, cfg = merge_client
    _stage(fake, cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))

    plan = client.post("/api/resolve/cells", json={"path": NB}).json()
    assert plan["auto_merged"] == 2 and plan["unchanged"] == 1
    assert [c["status"] for c in plan["cells"]] == ["auto", "auto", "auto"]

    resp = client.post(
        "/api/resolve/cells/apply",
        json={"path": NB, "choices": {}, **{k: plan[k] for k in FRESH}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_merged"] == 2
    assert body["trashed"] and body["trashed"][0]["path"] == NB  # drives the Undo toast
    assert read_local(cfg, NB) == nb("x = 99", "y = 2", "z = 42").decode("utf-8")


def test_the_plan_payload_ships_no_cell_source_except_a_contested_diff(merge_client):
    # Run against a plan that HAS a contested cell — the vacuous version of this
    # test passed with source in the payload. An auto cell's entry must carry no
    # code at all; a contested cell's diff carries both versions on purpose (the
    # picker shows it), which is pinned here so it stays a decision, not a leak.
    client, fake, cfg = merge_client
    _stage(fake, cfg, nb("x = 11", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = client.post("/api/resolve/cells", json={"path": NB}).json()

    auto = [c for c in plan["cells"] if c["status"] == "auto"]
    (contested,) = [c for c in plan["cells"] if c["status"] == "choice"]
    assert len(auto) == 2
    for cell in plan["cells"]:
        assert not {"code", "local", "remote"} & set(cell)
    # "x = 11" is my edit to an AUTO cell: it must appear nowhere in the payload.
    assert "x = 11" not in str(plan)
    assert "y = 200" in contested["diff"] and "y = 300" in contested["diff"]


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
    _stage(fake, cfg, nb("x = 99", "y = 2", "z = 3"), nb("x = 1", "y = 2", "z = 42"))
    plan = client.post("/api/resolve/cells", json={"path": NB}).json()
    fake.seed(NB, nb("x = 1", "y = 2", "z = 43"))

    resp = client.post(
        "/api/resolve/cells/apply",
        json={"path": NB, "choices": {}, **{k: plan[k] for k in FRESH}},
    )
    assert resp.status_code == 409 and resp.json()["stale"] is True


def test_endpoint_400s_when_the_request_omits_the_shas_it_was_planned_against(merge_client):
    client, fake, cfg = merge_client
    mine = nb("x = 99", "y = 2", "z = 3")
    _stage(fake, cfg, mine, nb("x = 1", "y = 2", "z = 42"))
    resp = client.post("/api/resolve/cells/apply", json={"path": NB, "choices": {}})
    assert resp.status_code == 400
    assert read_local(cfg, NB) == mine.decode("utf-8")  # nothing written


def test_endpoint_400s_on_a_missing_choice_and_an_escaping_path(merge_client):
    client, fake, cfg = merge_client
    _stage(fake, cfg, nb("x = 1", "y = 200", "z = 3"), nb("x = 1", "y = 300", "z = 3"))
    plan = client.post("/api/resolve/cells", json={"path": NB}).json()
    resp = client.post(
        "/api/resolve/cells/apply",
        json={"path": NB, "choices": {}, **{k: plan[k] for k in FRESH}},
    )
    assert resp.status_code == 400
    assert client.post("/api/resolve/cells", json={"path": "../out.py"}).status_code == 400
