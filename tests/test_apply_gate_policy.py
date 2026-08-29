"""The two Apply knobs: ``[ai] apply_guard`` (arms the gate) and ``[ai] apply_runs``
(does an applied cell execute), and the team policy's one-directional hold on both.

The load-bearing property is the one ``mooring.policy`` exists for: an entry in the
synced, attacker-controlled ``[policy.settings]`` survives ONLY when it equals the
knob's single ``safe`` value. These two knobs point in opposite directions — ``safe``
is ``apply_guard = true`` and ``apply_runs = false`` — so this file tests each in BOTH
directions rather than trusting the parametrised sweep in ``test_policy.py``.
"""

from __future__ import annotations

import tomllib

import pytest

from mooring import config, paths, policy, workspace_config
from mooring.app.apply import ApplyGateHeld, ApplyGuard

# Kept byte-identical to test_hub.py's fixtures on purpose: the gate is enforced in
# ONE place, so the cell that is held there must be the cell that is held here.
_NB_SRC = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)
_FLOOR_CELL = 'import os\nos.remove("data/old.csv")\n'  # un-downgradable band


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace plus an isolated per-machine config.toml.

    ``apply_with_undo`` reads the guard flag off DISK at the moment of the write, so
    without this the developer's real config would decide whether the gate is armed.
    """
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_AI_APPLY_GUARD", raising=False)
    monkeypatch.delenv("MOORING_AI_APPLY_RUNS", raising=False)
    (tmp_path / "appdata").mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "nb.py").write_text(_NB_SRC, "utf-8", newline="\n")
    return ws


def _set_local(text: str) -> None:
    paths.user_config_file().write_text(text, "utf-8", newline="\n")


def _set_shared(ws, text: str) -> None:
    """Write the SYNCED mooring.toml — the channel an attacker controls."""
    (ws / workspace_config.WORKSPACE_CONFIG_NAME).write_text(text, "utf-8", newline="\n")


def _apply_floor_cell(ws):
    return ApplyGuard().apply_with_undo(
        ws / "nb.py", ws, "nb.py", [{"op": "append", "code": _FLOOR_CELL}]
    )


# -- ai.apply_guard: the gate is armed by config, read at the write ------------


def test_guard_on_by_default_holds_a_floor_cell(workspace):
    with pytest.raises(ApplyGateHeld):
        _apply_floor_cell(workspace)
    assert "os.remove" not in (workspace / "nb.py").read_text("utf-8")


def test_guard_off_applies_the_same_cell(workspace):
    _set_local("[ai]\napply_guard = false\n")
    assert _apply_floor_cell(workspace) == 1  # applied, one undo step, no hold
    assert "os.remove" in (workspace / "nb.py").read_text("utf-8")


def test_the_flag_is_read_at_the_write_not_captured(workspace):
    """Turning the guard off (or a policy turning it on) must bite on the NEXT Apply.
    One ApplyGuard instance, two answers — nothing about the flag is cached on it."""
    guard = ApplyGuard()
    ops = [{"op": "append", "code": _FLOOR_CELL}]
    with pytest.raises(ApplyGateHeld):
        guard.apply_with_undo(workspace / "nb.py", workspace, "nb.py", ops)
    _set_local("[ai]\napply_guard = false\n")
    assert guard.apply_with_undo(workspace / "nb.py", workspace, "nb.py", ops) == 1


def test_policy_forces_the_guard_on_over_a_local_off(workspace):
    """The reason the knob is policy-governed: a team can arm the gate for everyone,
    and the local file — env vars included — cannot answer back."""
    _set_local("[ai]\napply_guard = false\n")
    _set_shared(workspace, '[policy.settings]\n"ai.apply_guard" = true\n')
    with pytest.raises(ApplyGateHeld):
        _apply_floor_cell(workspace)
    assert "os.remove" not in (workspace / "nb.py").read_text("utf-8")


def test_a_policy_that_tries_to_disarm_the_guard_is_ignored(workspace):
    """The permissive spelling is DROPPED, so a compromised mooring.toml cannot switch
    the gate off — the one thing this whole design has to make unexpressible."""
    _set_shared(workspace, '[policy.settings]\n"ai.apply_guard" = false\n')
    pol = policy.load(workspace)
    assert pol.settings == {}
    assert any("ai.apply_guard" in reason for reason in pol.ignored)
    with pytest.raises(ApplyGateHeld):
        _apply_floor_cell(workspace)


def test_a_held_apply_leaves_no_trace_with_the_guard_armed(workspace):
    from mooring import notebook_undo

    before = (workspace / "nb.py").read_bytes()
    with pytest.raises(ApplyGateHeld):
        _apply_floor_cell(workspace)
    assert (workspace / "nb.py").read_bytes() == before
    assert notebook_undo.depth(workspace, "nb.py") == 0


# -- ai.apply_runs: policy may force staging, never execution ------------------


def test_policy_forces_autorun_off_over_a_local_on(workspace):
    _set_local("[ai]\napply_runs = true\n")
    _set_shared(workspace, '[policy.settings]\n"ai.apply_runs" = false\n')
    app_cfg = policy.tighten_app_config(config.load_app_config(), workspace)
    assert app_cfg.ai_apply_runs is False


def test_policy_cannot_force_autorun_on(workspace):
    """The mirror image, and the direction that must be impossible: a repo cannot make
    a teammate's applied cells execute."""
    _set_local("[ai]\napply_runs = false\n")
    _set_shared(workspace, '[policy.settings]\n"ai.apply_runs" = true\n')
    pol = policy.load(workspace)
    assert pol.settings == {}
    assert any("ai.apply_runs" in reason for reason in pol.ignored)
    app_cfg = policy.tighten_app_config(config.load_app_config(), workspace)
    assert app_cfg.ai_apply_runs is False  # the local choice survives untouched


# -- the two knobs' safe values point in opposite directions -------------------


def test_the_two_apply_knobs_pin_in_opposite_directions():
    """Both are the STRICTER end of their own setting, which is why one is ``true``
    and the other ``false``. Spelled out because "policy pins the safe value" reads
    like "policy pins true" until you meet a knob where it does not."""
    guard = policy.KNOB_BY_KEY["ai.apply_guard"]
    runs = policy.KNOB_BY_KEY["ai.apply_runs"]
    assert guard.safe is True
    assert runs.safe is False
    for knob, tight in ((guard, "true"), (runs, "false")):
        pol = policy.parse(tomllib.loads(f'[policy.settings]\n"{knob.key}" = {tight}\n'))
        assert pol.settings == {knob.key: knob.safe}
