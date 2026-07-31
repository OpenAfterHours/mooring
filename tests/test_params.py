"""The parameter model: the spec grammar, the artifact naming, and the injected runtime.

Two properties carry most of the weight here and both are pinned below:

* **A notebook runs UNCHANGED with no parameter.** ``mooring_params.get`` takes a REQUIRED
  default and returns it when the channel is empty, so opening, verifying or refreshing the
  same file behaves exactly as it did before it was parameterised.
* **Two values can never land on one artifact.** Windows filesystems are case-insensitive,
  so ``EMEA``/``emea`` would silently overwrite; the spec is refused at parse time instead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mooring import params

SECRET_VALUE_DO_NOT_LEAK = "board-2026-actuals"


def _runtime(monkeypatch, env: dict | None = None):
    """Import the INJECTED module the way a notebook kernel does — by file path, with no
    mooring package importable — so the test exercises the real payload, not a stand-in."""
    monkeypatch.delenv(params.ENV_VAR, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    path = params.Path(params.__file__).with_name("_params_runtime.py")
    spec = importlib.util.spec_from_file_location("mooring_params_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the spec grammar --------------------------------------------------------


def test_a_plain_list_of_values():
    spec = params.parse_spec("region=EMEA,APAC,AMER")
    assert spec.name == "region"
    assert spec.values == ("EMEA", "APAC", "AMER")
    assert len(spec) == 3


def test_whitespace_and_empty_items_are_forgiven():
    assert params.parse_spec(" region = EMEA , APAC , ").values == ("EMEA", "APAC")


def test_month_ranges_expand_across_a_year_boundary():
    assert params.parse_spec("month=2025-11..2026-02").values == (
        "2025-11", "2025-12", "2026-01", "2026-02",
    )


def test_integer_ranges_expand():
    assert params.parse_spec("q=1..4").values == ("1", "2", "3", "4")


def test_ranges_and_literals_mix_in_one_spec():
    assert params.parse_spec("month=2026-01..2026-03,2026-12").values == (
        "2026-01", "2026-02", "2026-03", "2026-12",
    )


def test_an_unknown_range_form_is_refused_rather_than_guessed():
    # A DAY range needs a step decision mooring has not made. Refusing beats inventing one.
    with pytest.raises(params.ParamError) as exc:
        params.parse_spec("d=2026-01-01..2026-01-31")
    assert "whole numbers" in str(exc.value)


def test_a_backwards_range_is_refused():
    with pytest.raises(params.ParamError):
        params.parse_spec("month=2026-06..2026-01")
    with pytest.raises(params.ParamError):
        params.parse_spec("q=4..1")


def test_a_missing_equals_or_name_or_values_is_refused():
    for bad in ("region", "=EMEA", "region=", "1region=EMEA", "re gion=EMEA"):
        with pytest.raises(params.ParamError):
            params.parse_spec(bad)


def test_the_value_count_is_capped():
    # `--for n=1..10000` is a typo. Finding that out after an hour of rendering is not an
    # acceptable answer, so it is refused before anything runs.
    with pytest.raises(params.ParamError) as exc:
        params.parse_spec(f"n=1..{params.MAX_VALUES + 5}")
    assert str(params.MAX_VALUES) in str(exc.value)
    assert len(params.parse_spec(f"n=1..{params.MAX_VALUES}")) == params.MAX_VALUES


# -- artifacts can never collide ---------------------------------------------


def test_values_that_would_share_an_artifact_name_are_refused():
    # Windows filesystems are case-insensitive: EMEA and emea are ONE file, so the second
    # run would silently overwrite the first — one artifact making two claims.
    with pytest.raises(params.ParamError) as exc:
        params.parse_spec("region=EMEA,emea")
    assert "distinct artifact name" in str(exc.value)


def test_punctuation_that_collapses_to_the_same_slug_is_refused():
    with pytest.raises(params.ParamError):
        params.parse_spec("entity=ACME Ltd,ACME/Ltd")


def test_a_repeated_value_is_refused():
    with pytest.raises(params.ParamError) as exc:
        params.parse_spec("region=EMEA,APAC,EMEA")
    assert "twice" in str(exc.value)


def test_a_value_with_nothing_nameable_is_refused():
    with pytest.raises(params.ParamError) as exc:
        params.parse_spec("region=EMEA,///")
    assert "cannot name an artifact" in str(exc.value)


def test_the_slug_keeps_the_value_readable_and_bounded():
    assert params.slug("EMEA") == "EMEA"
    assert params.slug("2026-01") == "2026-01"
    assert params.slug("ACME Ltd (UK)") == "ACME-Ltd-UK"
    assert "/" not in params.slug("a/b\\c")
    assert len(params.slug("x" * 500)) <= 40


def test_the_variant_names_both_the_parameter_and_the_value():
    # A stakeholder has to tell EMEA from APAC from the FILENAME alone.
    spec = params.parse_spec("region=EMEA,APAC")
    assert spec.variant("EMEA") == "region-EMEA"
    assert spec.variant("APAC") != spec.variant("EMEA")


def test_the_note_states_the_value_and_its_place_in_the_fan_out():
    # This is what makes a PARTIAL fan-out visible from a single artifact.
    spec = params.parse_spec("region=EMEA,APAC,AMER")
    note = spec.note("EMEA", 1, 3)
    assert "region = EMEA" in note and "1 of 3" in note


# -- the run channel ---------------------------------------------------------


def test_env_for_carries_the_value_as_json_on_one_variable():
    spec = params.parse_spec("region=EMEA")
    env = spec.env_for("EMEA")
    assert list(env) == [params.ENV_VAR]
    assert json.loads(env[params.ENV_VAR]) == {"region": "EMEA"}


def test_the_env_var_name_matches_the_injected_runtime():
    # The injected module is standalone (it cannot import mooring), so the two constants
    # are duplicated on purpose — and pinned here so they can never drift apart.
    source = params.Path(params.__file__).with_name("_params_runtime.py").read_text("utf-8")
    assert f'_ENV_VAR = "{params.ENV_VAR}"' in source


# -- the injected runtime ----------------------------------------------------


def test_a_notebook_runs_unchanged_when_no_parameter_is_supplied(monkeypatch):
    # THE hard requirement. With nothing passed, get() is the default — so the same file
    # opens in the editor, verifies, and refreshes on a cadence exactly as before.
    runtime = _runtime(monkeypatch)
    assert runtime.get("region", "EMEA") == "EMEA"
    assert runtime.as_dict() == {}
    assert runtime.names() == []
    assert runtime.is_parameterised() is False


def test_the_default_is_required_so_no_cell_can_only_work_inside_a_fan_out(monkeypatch):
    runtime = _runtime(monkeypatch)
    with pytest.raises(TypeError):
        runtime.get("region")


def test_the_value_arrives_when_the_channel_is_set(monkeypatch):
    runtime = _runtime(monkeypatch, {params.ENV_VAR: json.dumps({"region": "APAC"})})
    assert runtime.get("region", "EMEA") == "APAC"
    assert runtime.is_parameterised() is True
    assert runtime.names() == ["region"]


def test_the_return_type_follows_the_default(monkeypatch):
    runtime = _runtime(
        monkeypatch,
        {params.ENV_VAR: json.dumps({"month": "6", "rate": "1.5", "draft": "no"})},
    )
    assert runtime.get("month", 1) == 6 and isinstance(runtime.get("month", 1), int)
    assert runtime.get("rate", 0.0) == 1.5
    assert runtime.get("draft", True) is False


def test_a_value_that_will_not_convert_raises_loudly(monkeypatch):
    # Falling back to the default would produce an artifact labelled with a value the run
    # never used — the one failure this feature must not have.
    runtime = _runtime(monkeypatch, {params.ENV_VAR: json.dumps({"month": "June"})})
    with pytest.raises(ValueError):
        runtime.get("month", 1)


def test_a_corrupt_channel_degrades_to_no_parameters_rather_than_exploding(monkeypatch):
    for raw in ("not json", "[1,2,3]", '{"region": 5}', ""):
        runtime = _runtime(monkeypatch, {params.ENV_VAR: raw})
        assert runtime.get("region", "EMEA") == "EMEA"


def test_the_runtime_imports_nothing_from_mooring():
    # It runs inside the notebook kernel — the team's locked uv env or the frozen bundle —
    # where `mooring` is not importable. Same contract as mooring_checks.
    source = params.Path(params.__file__).with_name("_params_runtime.py").read_text("utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and " import " in f"{line} import "
    ]
    assert imports == ["from __future__ import annotations", "import json", "import os"]


def test_install_runtime_writes_the_module_where_the_kernel_looks(tmp_path):
    params.install_runtime(tmp_path)
    target = params.pylib_dir(tmp_path) / "mooring_params.py"
    assert target.is_file()
    assert target.read_bytes() == (
        Path(params.__file__).with_name("_params_runtime.py").read_bytes()
    )


def test_install_runtime_is_idempotent_and_never_raises(tmp_path):
    params.install_runtime(tmp_path)
    target = params.pylib_dir(tmp_path) / "mooring_params.py"
    before = target.stat().st_mtime_ns
    params.install_runtime(tmp_path)
    assert target.stat().st_mtime_ns == before  # unchanged bytes -> no rewrite
    # Best-effort: a workspace whose .mooring path is blocked by a FILE must not raise —
    # a missing mooring_params surfaces as an ImportError in the analyst's own cell.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / ".mooring").write_text("not a directory", encoding="utf-8")
    params.install_runtime(blocked)


def test_the_injected_module_is_structurally_unsyncable(tmp_path):
    from mooring import sync

    assert sync.is_synced_path(".mooring/pylib/mooring_params.py") is False


def test_the_editor_installs_the_params_runtime_beside_the_others(tmp_path):
    # The kernel import path is set up in ONE place; a fourth sibling must ride it.
    from mooring import editor

    editor.ensure_runtime_config(tmp_path)
    assert (params.pylib_dir(tmp_path) / "mooring_params.py").is_file()
    assert (params.pylib_dir(tmp_path) / "mooring_checks.py").is_file()


# -- the "does it read the parameter" guard ----------------------------------


def test_reads_parameter_needs_both_the_module_and_the_name():
    good = 'import mooring_params\nregion = mooring_params.get("region", "EMEA")\n'
    assert params.reads_parameter(good, "region") is True
    assert params.reads_parameter(good, "regoin") is False  # the typo this exists to catch
    assert params.reads_parameter('region = "EMEA"', "region") is False
    assert params.reads_parameter("import mooring_params\n", "region") is False


def test_reads_parameter_accepts_either_quote_style():
    assert params.reads_parameter("import mooring_params\nmooring_params.get('m', 1)", "m")


def test_no_data_value_can_reach_the_spec_machinery():
    # The spec only ever holds labels the analyst typed. Nothing here reads a notebook's
    # data, and the value-free posture of the rest of mooring is unaffected.
    spec = params.parse_spec(f"tag={SECRET_VALUE_DO_NOT_LEAK}")
    assert spec.values == (SECRET_VALUE_DO_NOT_LEAK,)
    assert SECRET_VALUE_DO_NOT_LEAK in spec.variant(SECRET_VALUE_DO_NOT_LEAK)
