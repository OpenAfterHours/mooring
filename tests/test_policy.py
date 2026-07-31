"""Tests for the admin policy: the synced ``[policy]`` block the client enforces.

The three properties this file exists to pin, in order of importance:

1. **Tighten-only.** A policy can never make anything less restrictive than the
   local config already is. Tested per governed setting with a policy that tries
   to loosen it, and per scale/glob rule.
2. **Hostile input degrades to "that rule is ignored".** ``mooring.toml`` is
   attacker-controlled in the threat model that matters, so a wrong type, an
   unknown key, an absolute path, a ``..`` escape, or a pathological glob must
   drop ONE rule — never crash, never weaken, never reach the filesystem.
3. **A repo with no ``[policy]`` block behaves exactly as before.**

Plus the enforcement seams: the propose-only gate actually withholds a file at
``sync.push`` (not merely in the UI), the AI-off globs generalise
``[ai] disabled_notebooks`` without breaking it, and the Settings page refuses a
locked write with an honest 409.
"""

from __future__ import annotations

import tomllib

import pytest
from conftest import FakeClient, write_local

from mooring import config, policy, sync, workspace_config
from mooring.hub import settings_schema


def write_shared(cfg, text: str) -> None:
    """Write the SYNCED mooring.toml for a workspace (the attacker's channel)."""
    workspace = cfg.workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / workspace_config.WORKSPACE_CONFIG_NAME).write_text(text, "utf-8", newline="\n")


# -- 1. the tighten-only rule --------------------------------------------------


def test_every_knob_is_a_real_setting_with_the_stricter_direction():
    """The safe direction is not a second hand-maintained list: it is pinned
    against ``settings_schema``'s own ``weaken_value`` declaration, so the two can
    never drift apart silently (the _connections_runtime idiom)."""
    for knob in policy.KNOBS:
        spec = settings_schema.by_key(knob.key)
        assert spec is not None, f"{knob.key} is not a real editable setting"
        assert spec.type == "bool", f"{knob.key} must be a bool knob"
        if spec.weaken_value is not None:
            assert knob.safe is not spec.weaken_value, (
                f"policy would pin {knob.key} to the value the schema calls weakening"
            )
        # The field path must actually resolve on a live AppConfig.
        obj = config.AppConfig()
        for part in knob.path[:-1]:
            obj = getattr(obj, part)
        assert isinstance(getattr(obj, knob.path[-1]), bool)


@pytest.mark.parametrize("knob", policy.KNOBS, ids=lambda k: k.key)
def test_policy_cannot_loosen_any_setting(tmp_path, knob):
    """THE test: for every governed setting, a policy that names the PERMISSIVE
    value is dropped, and the local value survives untouched."""
    loose = "false" if knob.safe else "true"
    pol = policy.parse(tomllib.loads(f'[policy.settings]\n"{knob.key}" = {loose}\n'))
    assert pol.settings == {}, f"{knob.key}: a loosening entry was honoured"
    assert any(knob.key in reason for reason in pol.ignored)

    # And the composition itself is a no-op: an AppConfig sitting at the
    # permissive end stays there.
    app_cfg = policy._set_path(config.AppConfig(), knob.path, not knob.safe)
    assert policy.tighten(app_cfg, pol) == app_cfg


@pytest.mark.parametrize("knob", policy.KNOBS, ids=lambda k: k.key)
def test_policy_can_tighten_every_setting(knob):
    tight = "true" if knob.safe else "false"
    pol = policy.parse(tomllib.loads(f'[policy.settings]\n"{knob.key}" = {tight}\n'))
    assert pol.settings == {knob.key: knob.safe}
    app_cfg = policy.tighten(
        policy._set_path(config.AppConfig(), knob.path, not knob.safe), pol
    )
    obj = app_cfg
    for part in knob.path[:-1]:
        obj = getattr(obj, part)
    assert getattr(obj, knob.path[-1]) is knob.safe


def test_policy_outranks_an_env_override(tmp_path, monkeypatch):
    """Env vars are local config too — policy sits above the whole three-layer
    merge, so ``MOORING_AI_PII=0`` cannot switch off a policy-forced scan."""
    monkeypatch.setenv("MOORING_AI_PII", "0")
    app_cfg = config.load_app_config(tmp_path / "config.toml")
    assert app_cfg.ai_pii is False
    pol = policy.parse(tomllib.loads('[policy.settings]\n"ai.pii.enabled" = true\n'))
    assert policy.tighten(app_cfg, pol).ai_pii is True


def test_guard_mode_only_ever_rises():
    """The scale knob: MAX on ("warn", "block"), so a policy saying "warn" can
    never lower a repo that already sets [guard] push = "block"."""
    warn_policy = policy.parse({"policy": {"push_guard": "warn"}})
    assert warn_policy.push_guard == ""  # recorded as "says nothing", not as a floor
    assert warn_policy.guard_mode("block") == "block"
    block_policy = policy.parse({"policy": {"push_guard": "block"}})
    assert block_policy.guard_mode("warn") == "block"
    assert block_policy.guard_mode("block") == "block"
    assert policy.Policy().guard_mode("warn") == "warn"


def test_globs_are_additive_only(tmp_path):
    """``ai_off`` UNIONS with the legacy list — a policy can add paths but can
    never re-enable a notebook an existing opt-out turned off."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text(
        '[ai]\ndisabled_notebooks = ["notebooks/hr.py"]\n'
        '[policy]\nai_off = ["reports/**"]\n',
        "utf-8",
    )
    gate = policy.ai_gate(workspace)
    assert gate("notebooks/hr.py") is True  # the legacy list still bites
    assert gate("reports/pay.py") is True  # the policy adds to it
    assert gate("notebooks/sales.py") is False


# -- 2. hostile / malformed input ----------------------------------------------


@pytest.mark.parametrize(
    "toml_text",
    [
        "[policy]\nunknown_key = 1\n",  # unknown key
        "[policy]\nmin_version = 12\n",  # wrong type
        '[policy]\nmin_version = "not-a-version"\n',
        '[policy]\npush_guard = "off"\n',  # not on the scale
        "[policy]\npush_guard = 3\n",
        "[policy]\npropose_only = 7\n",  # wrong type entirely
        "[policy]\nai_off = [1, 2, 3]\n",  # non-string entries
        '[policy]\nai_off = ["../../etc/passwd"]\n',  # escape
        '[policy]\nai_off = ["C:/Windows/System32"]\n',  # absolute (Windows)
        '[policy]\nai_off = ["."]\n',  # the root sentinel
        "[policy]\nsettings = 5\n",  # settings not a table
        '[policy.settings]\n"ai.nonexistent" = true\n',  # unknown setting
        '[policy.settings]\n"ai.context" = "false"\n',  # a string, not a bool
        '[policy.settings]\n"ai.pii.enabled" = 1\n',  # an int, not a bool
    ],
)
def test_hostile_policy_never_weakens_and_never_raises(toml_text):
    pol = policy.parse(tomllib.loads(toml_text))
    # Nothing usable came out of it...
    assert pol.settings == {}
    assert pol.propose_only.patterns == ()
    assert pol.ai_off.patterns == ()
    assert pol.push_guard == ""
    assert pol.min_version == ""
    # ...and the reason is recorded rather than swallowed.
    assert pol.ignored, f"no reason recorded for {toml_text!r}"
    # The composition is a strict no-op on the default config.
    base = config.AppConfig()
    assert policy.tighten(base, pol) == base
    assert pol.guard_mode("warn") == "warn"
    assert pol.guard_mode("block") == "block"


def test_a_rooted_pattern_becomes_repo_relative_never_an_escape():
    """``safe_folder``'s existing behaviour, asserted here because it is the
    security-relevant one: a leading slash is stripped to repo-relative (the
    admin's obvious intent), and the result can never address the real /etc.
    A UNC-shaped pattern lands the same way: leading separators go, so it names
    this repo's folder, never a network share — and globs are only ever MATCHED
    against repo-relative paths, never handed to the filesystem."""
    for rooted in ("/reports/**", "\\reports\\**", "//reports/**"):
        glob = policy.compile_glob(rooted)
        assert glob is not None and glob.pattern == "reports/**", rooted
        assert glob.matches("reports/q1.py")
        assert not glob.matches("etc/passwd")


def test_a_star_run_cannot_be_turned_into_a_backtracking_bomb():
    """``"***"`` would translate to adjacent ``[^/]*`` groups — the classic
    catastrophic-backtracking shape, and a two-line DoS for anyone with repo
    write. Runs collapse, so the emitted regex has no adjacent same-language
    quantifiers, and matching a long non-match returns immediately."""
    glob = policy.compile_glob("data/a***b.csv")
    assert glob is not None and glob.pattern == "data/a*b.csv"
    assert glob.matches("data/a" + "x" * 200 + "b.csv")
    assert not glob.matches("data/a" + "x" * 200)  # would hang if it backtracked
    # An all-stars segment collapses to the one meaningful form, never more.
    assert policy.compile_glob("***").pattern == "**"
    assert policy.compile_glob("reports/*").pattern == "reports/*"
    assert policy.compile_glob("**/**/**").pattern == "**"


def test_policy_block_of_the_wrong_type_is_ignored_whole():
    pol = policy.parse({"policy": ["not", "a", "table"]})
    assert not pol.in_force
    assert pol.ignored


def test_one_bad_rule_does_not_poison_the_good_ones():
    pol = policy.parse(
        tomllib.loads(
            '[policy]\n'
            'min_version = "nope"\n'
            'ai_off = ["../escape", "reports/**"]\n'
            '[policy.settings]\n'
            '"ai.context" = true\n'  # tries to loosen -> dropped
            '"ai.pii.enabled" = true\n'  # legitimate -> kept
        )
    )
    assert pol.min_version == ""
    assert pol.ai_off.patterns == ("reports/**",)
    assert pol.settings == {"ai.pii.enabled": True}
    assert len(pol.ignored) == 3


def test_escaping_or_pathological_globs_are_dropped():
    for bad in ("", "   ", "..", "../x", "..\\x", "C:/x", "c:\\x", "x" * 300, None, 5, {}):
        assert policy.compile_glob(bad) is None
    assert policy.compile_glob("a/**/b/**/c/**/d.py") is None  # more ** than the cap
    assert policy.compile_glob("/".join(["a"] * 25)) is None  # more segments than the cap
    # A "[" is a literal, not a character class — so no pattern can fail to compile.
    glob = policy.compile_glob("reports/[weird].py")
    assert glob is not None and glob.matches("reports/[weird].py")
    assert not glob.matches("reports/w.py")


def test_unparseable_shared_file_is_loud_not_silent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text("this is [not toml", "utf-8")
    pol = policy.load(workspace)
    assert pol.unreadable is True
    assert not pol.in_force
    assert pol.ignored
    # It must still not weaken anything (there is nothing to weaken) and must not raise.
    assert policy.tighten(config.AppConfig(), pol) == config.AppConfig()
    assert policy.guard_mode(workspace) == "warn"


def test_glob_matching_is_case_insensitive_and_slash_agnostic():
    glob = policy.compile_glob("Reports/**")
    assert glob is not None
    assert glob.matches("reports/q1.py")
    assert glob.matches("REPORTS/sub/q1.py")
    assert glob.matches(r"reports\sub\q1.py")  # a Windows-shaped path
    assert not glob.matches("notebooks/q1.py")


def test_glob_semantics():
    star = policy.compile_glob("reports/*.py")
    assert star.matches("reports/a.py")
    assert not star.matches("reports/sub/a.py")  # "*" never crosses a separator
    deep = policy.compile_glob("reports/**")
    assert deep.matches("reports/sub/a.py")
    mid = policy.compile_glob("a/**/b.py")
    assert mid.matches("a/b.py") and mid.matches("a/x/y/b.py")
    q = policy.compile_glob("data/?.csv")
    assert q.matches("data/a.csv") and not q.matches("data/ab.csv")
    # A wildcard-free pattern covers its subtree (naming a folder must not be a no-op).
    folder = policy.compile_glob("hr")
    assert folder.matches("hr") and folder.matches("hr/pay.py")
    assert not folder.matches("hrx/pay.py")


# -- 3. a repo with no [policy] is unchanged -----------------------------------


def test_no_policy_block_changes_nothing(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text(
        '[ai]\ndisabled_notebooks = ["notebooks/hr.py"]\n[guard]\npush = "block"\n', "utf-8"
    )
    pol = policy.load(workspace)
    assert pol == policy.Policy()
    assert not pol.in_force
    base = config.AppConfig()
    assert policy.tighten(base, pol) is base or policy.tighten(base, pol) == base
    # The two settings this feature generalises keep their exact old behaviour.
    assert policy.guard_mode(workspace) == workspace_config.guard_mode(workspace) == "block"
    gate = policy.ai_gate(workspace)
    assert gate("notebooks/hr.py") == workspace_config.is_ai_disabled(workspace, "notebooks/hr.py")
    assert gate("notebooks/other.py") is False
    # And nothing is withheld from a push.
    gate_fn, blocked = policy.make_propose_gate(pol)
    assert gate_fn("notebooks/anything.py", b"x = 1") == []
    assert blocked == {}


def test_no_shared_file_at_all(tmp_path):
    workspace = tmp_path / "nothing-here"
    pol = policy.load(workspace)
    assert pol == policy.Policy()
    assert policy.guard_mode(workspace) == "warn"
    assert policy.ai_gate(workspace)("notebooks/a.py") is False


# -- 4. propose-only enforcement at the sync.push seam -------------------------


def test_propose_only_blocks_a_direct_push_at_the_sync_seam(cfg):
    """Not "the button is hidden": the file never reaches the GitHub client,
    because the block rides sync's injected guard_fn — the same withhold seam the
    secret scanner uses."""
    write_shared(cfg, '[policy]\npropose_only = ["reports/**"]\n')
    write_local(cfg, "reports/q1.py", "x = 1\n")
    write_local(cfg, "notebooks/free.py", "y = 2\n")
    client = FakeClient()

    pol = policy.load(cfg.workspace())
    gate_fn, blocked = policy.make_propose_gate(pol)
    result = sync.push(client, cfg, throttle=0, guard_fn=gate_fn)

    assert "reports/q1.py" not in client.tree  # never uploaded
    assert "notebooks/free.py" in client.tree  # unrelated files still go
    assert [p for p, _ in result.withheld] == ["reports/q1.py"]
    assert "reports/q1.py" in blocked
    assert policy.PROPOSE_ONLY_REASON in blocked["reports/q1.py"]


def test_propose_is_the_road_the_policy_points_at(cfg):
    """The gate is only installed for a DIRECT push; propose must stay open, or a
    propose-only path would be unreachable by any route."""
    write_shared(cfg, '[policy]\npropose_only = ["reports/**"]\n')
    write_local(cfg, "reports/q1.py", "x = 1\n")
    client = FakeClient()
    result = sync.propose(client, cfg, throttle=0, guard_fn=None)
    assert not result.withheld
    assert "proposed reports/q1.py" in result.lines
    assert "reports/q1.py" in client.trees[result.review_branch]


def test_a_policy_block_is_never_acknowledgeable(cfg):
    """Composed guards: acknowledging the scanner's findings (the push guard's
    only override) still leaves the policy gate withholding the file."""
    write_shared(cfg, '[policy]\npropose_only = ["reports/**"]\n')
    write_local(cfg, "reports/q1.py", "x = 1\n")
    client = FakeClient()

    pol = policy.load(cfg.workspace())
    gate_fn, blocked = policy.make_propose_gate(pol)
    # allow_fn is exactly what --acknowledge-findings installs: a scanner that
    # reports nothing. The composition must still refuse.
    combined = policy.compose_guards(lambda rel, data: [], gate_fn)
    sync.push(client, cfg, throttle=0, guard_fn=combined)
    assert "reports/q1.py" not in client.tree
    assert blocked


def test_compose_guards_runs_every_gate():
    combined = policy.compose_guards(
        lambda rel, data: ["a"], None, lambda rel, data: ["b"]
    )
    assert combined("x.py", b"") == ["a", "b"]
    assert policy.compose_guards(None, None) is None


# -- 5. the AI-off globs -------------------------------------------------------


def test_ai_off_globs_generalise_the_notebook_list(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text(
        '[policy]\nai_off = ["hr/**", "*.private.py"]\n', "utf-8"
    )
    gate = policy.ai_gate(workspace)
    assert gate("hr/pay.py")
    assert gate("secrets.private.py")
    assert not gate("notebooks/sales.py")
    assert policy.ai_disabled(workspace, "hr/pay.py")
    assert not policy.ai_disabled(workspace, "notebooks/sales.py")


# -- 6. the version floor (advisory, by design) --------------------------------


def test_min_version_warns_but_never_blocks():
    pol = policy.parse({"policy": {"min_version": "9.9.9"}})
    assert "9.9.9" in pol.version_shortfall("0.4.29")
    assert pol.version_shortfall("9.9.9") == ""
    assert pol.version_shortfall("10.0.0") == ""
    # A pre-release suffix compares on its numeric prefix, not by guesswork.
    assert policy.parse({"policy": {"min_version": "1.2"}}).version_shortfall("1.2.0.dev3") == ""
    # An unparseable running version can never be reported as "too old".
    assert pol.version_shortfall("weird-build") == ""


# -- 7. authoring (writes the SYNCED file) -------------------------------------


def test_set_and_unset_round_trip(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy.set_rule(workspace, policy.RULE_MIN_VERSION, ["1.2.3"])
    policy.set_rule(workspace, policy.RULE_PUSH_GUARD, ["block"])
    policy.set_rule(workspace, policy.RULE_PROPOSE_ONLY, ["reports/**", "reports/**"])
    policy.set_rule(workspace, policy.RULE_SETTING, ["ai.context", "false"])
    pol = policy.load(workspace)
    assert pol.min_version == "1.2.3"
    assert pol.push_guard == "block"
    assert pol.propose_only.patterns == ("reports/**",)  # de-duplicated
    assert pol.settings == {"ai.context": False}
    assert not pol.ignored

    policy.unset_rule(workspace, policy.RULE_PUSH_GUARD)
    policy.unset_rule(workspace, policy.RULE_SETTING, ["ai.context"])
    pol = policy.load(workspace)
    assert pol.push_guard == "" and pol.settings == {}
    assert pol.min_version == "1.2.3"  # untouched


def test_set_refuses_to_author_a_loosening_or_useless_rule(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(ValueError, match="stricter"):
        policy.set_rule(workspace, policy.RULE_SETTING, ["ai.pii.enabled", "false"])
    with pytest.raises(ValueError, match="not policy-governed"):
        policy.set_rule(workspace, policy.RULE_SETTING, ["sync.max_file_mb", "true"])
    with pytest.raises(ValueError):
        policy.set_rule(workspace, policy.RULE_MIN_VERSION, ["banana"])
    with pytest.raises(ValueError, match="patterns"):
        policy.set_rule(workspace, policy.RULE_PROPOSE_ONLY, ["../escape"])
    with pytest.raises(ValueError):
        policy.set_rule(workspace, "no-such-rule", ["x"])
    # Nothing was written by any of the refusals.
    assert not (workspace / "mooring.toml").exists()


def test_setting_push_guard_to_warn_clears_rather_than_records(tmp_path):
    """"warn" is the floor everyone has; recording it must never read as a way
    DOWN from a stricter [guard] push."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text('[guard]\npush = "block"\n', "utf-8")
    policy.set_rule(workspace, policy.RULE_PUSH_GUARD, ["warn"])
    data = tomllib.loads((workspace / "mooring.toml").read_text("utf-8"))
    assert "push_guard" not in data.get("policy", {})
    assert policy.guard_mode(workspace) == "block"


def test_authoring_preserves_every_other_section(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text(
        '[ai]\ndisabled_notebooks = ["notebooks/hr.py"]\n[sync]\nfolders = ["pkg/notebooks"]\n',
        "utf-8",
    )
    policy.set_rule(workspace, policy.RULE_AI_OFF, ["hr/**"])
    data = tomllib.loads((workspace / "mooring.toml").read_text("utf-8"))
    assert data["ai"]["disabled_notebooks"] == ["notebooks/hr.py"]
    assert data["sync"]["folders"] == ["pkg/notebooks"]
    assert data["policy"]["ai_off"] == ["hr/**"]


def test_authoring_refuses_to_overwrite_a_corrupt_shared_file(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text("[broken", "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        policy.set_rule(workspace, policy.RULE_AI_OFF, ["hr/**"])
    assert (workspace / "mooring.toml").read_text("utf-8") == "[broken"


def test_emptying_the_policy_prunes_the_file(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy.set_rule(workspace, policy.RULE_AI_OFF, ["hr/**"])
    assert (workspace / "mooring.toml").exists()
    policy.unset_rule(workspace, policy.RULE_AI_OFF)
    assert not (workspace / "mooring.toml").exists()


def test_nested_and_quoted_settings_spellings_both_parse():
    """An admin may write either TOML spelling; both must produce one policy."""
    quoted = policy.parse(tomllib.loads('[policy.settings]\n"ai.pii.enabled" = true\n'))
    nested = policy.parse(tomllib.loads("[policy.settings.ai.pii]\nenabled = true\n"))
    assert quoted.settings == nested.settings == {"ai.pii.enabled": True}


def test_describe_is_value_free_and_names_the_ignored_rules():
    pol = policy.parse(
        tomllib.loads(
            '[policy]\nmin_version = "1.0"\npush_guard = "block"\n'
            'propose_only = ["reports/**"]\nbogus = 1\n'
        )
    )
    lines = "\n".join(policy.describe(pol, current_version="0.1.0", local_guard="warn"))
    assert "reports/**" in lines
    assert "block" in lines
    assert "bogus" in lines  # the ignored rule is named, not swallowed
    assert "older than the minimum" in lines


# -- 8. the hub: locked settings and the propose-only 409 ----------------------


@pytest.fixture
def hub_client(tmp_path, monkeypatch):
    """A hub bound to a tmp workspace, so the synced mooring.toml under test is
    the one the policy is read from. ``build`` re-constructs the Hub, which is how
    the fold at construction time is observed."""
    from starlette.testclient import TestClient

    from mooring import paths
    from mooring.hub.server import Hub, create_app

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    monkeypatch.delenv("MOORING_GITHUB_HOST", raising=False)
    for var in ("MOORING_AI_PII", "MOORING_AI_CONTEXT", "MOORING_AI_CODE_INDEX"):
        monkeypatch.delenv(var, raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def build():
        spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(workspace))
        return Hub(config.AppConfig(repos=(spec,), active_alias="ws"))

    hub = build()
    with TestClient(create_app(hub)) as client:
        yield client, hub, workspace, build


def test_settings_page_marks_a_locked_row_and_says_where_it_came_from(hub_client):
    client, hub, workspace, _build = hub_client
    (workspace / "mooring.toml").write_text(
        '[policy.settings]\n"ai.pii.enabled" = true\n"ai.context" = false\n', "utf-8"
    )
    payload = client.get("/api/settings").json()
    rows = {row["key"]: row for row in payload["editable"]}
    assert rows["ai.pii.enabled"]["locked"] is True
    assert "mooring.toml" in rows["ai.pii.enabled"]["locked_note"]
    assert "stricter" in rows["ai.pii.enabled"]["locked_note"]
    assert rows["ai.live_schema"]["locked"] is False
    assert rows["ai.live_schema"]["locked_note"] == ""
    # And the page shows the policy block itself, value-free.
    assert payload["policy"]["in_force"] is True
    assert payload["policy"]["locked_keys"] == ["ai.context", "ai.pii.enabled"]


def test_writing_a_locked_setting_is_refused_with_an_honest_409(hub_client):
    client, hub, workspace, _build = hub_client
    (workspace / "mooring.toml").write_text('[policy.settings]\n"ai.pii.enabled" = true\n', "utf-8")
    resp = client.post("/api/settings", json={"key": "ai.pii.enabled", "value": False})
    assert resp.status_code == 409
    body = resp.json()
    assert body["locked"] is True
    assert body["locked_value"] is True
    assert "mooring.toml" in body["message"]
    # A confirm cannot talk past it — unlike a weakening flip, there is no path.
    again = client.post(
        "/api/settings", json={"key": "ai.pii.enabled", "value": False, "confirm": True}
    )
    assert again.status_code == 409 and again.json()["locked"] is True
    # Reset is a write too, so it is refused the same way.
    assert client.post("/api/settings/reset", json={"key": "ai.pii.enabled"}).status_code == 409
    # Nothing was persisted, and the effective config still obeys the policy.
    assert client.get("/api/settings").json()["editable"][0] is not None
    assert config.load_app_config().ai_pii is False  # config.toml untouched by the refusal


def test_a_locked_setting_can_still_be_moved_towards_the_safe_value(hub_client):
    """The lock refuses a WEAKENING write, not every write: setting the pinned
    value is a no-op that leaves config.toml agreeing with the policy."""
    client, _hub, workspace, _build = hub_client
    (workspace / "mooring.toml").write_text('[policy.settings]\n"ai.pii.enabled" = true\n', "utf-8")
    resp = client.post("/api/settings", json={"key": "ai.pii.enabled", "value": True})
    assert resp.status_code == 200


def test_the_hub_actually_runs_with_the_tightened_config(hub_client):
    """Not just the display: Hub.app_cfg is folded at every assignment, so a
    config.toml that says otherwise cannot re-open the setting."""
    _client, _hub, workspace, build = hub_client
    from mooring import config_store

    config_store.set_value("ai.context", True)
    (workspace / "mooring.toml").write_text('[policy.settings]\n"ai.context" = false\n', "utf-8")
    assert config.load_app_config().ai_context is True  # the local file says true
    assert build().app_cfg.ai_context is False  # the app runs with false


def test_no_policy_leaves_the_settings_payload_exactly_as_before(hub_client):
    client, _hub, _workspace, _build = hub_client
    payload = client.get("/api/settings").json()
    assert all(row["locked"] is False for row in payload["editable"])
    assert payload["policy"]["in_force"] is False
    # And a weakening flip still gets its ordinary needs_confirm 409, unchanged.
    resp = client.post("/api/settings", json={"key": "ai.context", "value": True})
    assert resp.status_code == 409 and resp.json()["needs_confirm"] is True
    assert "locked" not in resp.json()


def test_hub_push_returns_policy_blocked_without_a_token(hub_client, monkeypatch):
    """The 409 shape: a propose-only block carries NO token and does not set
    needs_confirm, so the dialog can never offer "Push anyway" for it."""
    client, hub, workspace, _build = hub_client
    (workspace / "mooring.toml").write_text('[policy]\npropose_only = ["reports/**"]\n', "utf-8")
    (workspace / "reports").mkdir()
    (workspace / "reports" / "q1.py").write_text("x = 1\n", "utf-8")

    fake = FakeClient()
    monkeypatch.setattr(hub, "client", lambda: fake)
    resp = client.post("/api/push", json={})
    assert resp.status_code == 409
    body = resp.json()
    assert body["needs_confirm"] is False
    assert body["guard_findings"] == []
    assert [b["path"] for b in body["policy_blocked"]] == ["reports/q1.py"]
    assert "reports/q1.py" not in fake.tree


# -- 9. the CLI surface --------------------------------------------------------


def _run_cli(argv, tmp_path, monkeypatch, workspace):
    from mooring import cli, paths

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.setenv("MOORING_WORKSPACE", str(workspace))
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    return cli.main(argv)


def test_cli_policy_show_set_unset(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _run_cli(["policy", "show"], tmp_path, monkeypatch, workspace) == 0
    assert "no [policy] block" in capsys.readouterr().out

    assert _run_cli(
        ["policy", "set", "propose-only", "reports/**"], tmp_path, monkeypatch, workspace
    ) == 0
    assert "push" in capsys.readouterr().out  # says plainly that it is a synced change

    assert _run_cli(["policy", "show"], tmp_path, monkeypatch, workspace) == 0
    assert "reports/**" in capsys.readouterr().out

    assert _run_cli(["policy", "unset", "propose-only"], tmp_path, monkeypatch, workspace) == 0
    capsys.readouterr()
    assert not policy.load(workspace).in_force


def test_cli_policy_set_refuses_a_loosening_rule(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(SystemExit) as exc:
        _run_cli(
            ["policy", "set", "setting", "ai.pii.enabled", "false"],
            tmp_path, monkeypatch, workspace,
        )
    assert "stricter" in str(exc.value)
    assert not (workspace / "mooring.toml").exists()


def test_cli_push_composes_the_gate_but_propose_does_not(tmp_path):
    """The CLI's own wiring: a direct push gets the propose-only gate, propose
    does not, and --acknowledge-findings never clears a policy block."""
    from mooring import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text('[policy]\npropose_only = ["reports/**"]\n', "utf-8")
    cfg = config.Config(owner="acme", repo="nbs", workspace_path=str(workspace))

    guard_fn, _c, mode, _a, blocked = cli._push_guard_fn(cfg, acknowledge=False)
    assert guard_fn("reports/q1.py", b"x = 1") != []
    assert blocked and mode == "warn"

    ack_fn, _c, _m, _a, ack_blocked = cli._push_guard_fn(cfg, acknowledge=True)
    assert ack_fn("reports/q1.py", b"x = 1") != []  # acknowledging cannot clear it
    assert ack_blocked

    prop_fn, _c, _m, _a, prop_blocked = cli._push_guard_fn(cfg, acknowledge=False, direct=False)
    assert (prop_fn("reports/q1.py", b"x = 1") if prop_fn else []) == []
    assert prop_blocked == {}


def test_cli_push_guard_mode_is_the_policy_raised_one(tmp_path):
    from mooring import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text('[policy]\npush_guard = "block"\n', "utf-8")
    cfg = config.Config(owner="acme", repo="nbs", workspace_path=str(workspace))
    # In block mode --acknowledge-findings is refused entirely (the scanner guard,
    # not the permissive allow_fn, comes back).
    _fn, collected, mode, acknowledged, _b = cli._push_guard_fn(cfg, acknowledge=True)
    assert mode == "block"
    assert acknowledged == {} and collected == {}


def test_cli_policy_show_names_the_ignored_rules(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mooring.toml").write_text(
        '[policy]\nai_off = ["../escape"]\ntypo_key = 1\n', "utf-8"
    )
    assert _run_cli(["policy", "show"], tmp_path, monkeypatch, workspace) == 0
    out = capsys.readouterr().out
    assert "ignored" in out and "typo_key" in out


# -- 10. the doctor probe ------------------------------------------------------


def test_doctor_reports_policy_status(tmp_path):
    from mooring import doctor

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = config.Config(owner="acme", repo="nbs", workspace_path=str(workspace))
    assert doctor._probe_policy(cfg).status == doctor.PASS  # no policy

    (workspace / "mooring.toml").write_text(
        '[policy]\npush_guard = "block"\nai_off = ["hr/**"]\n', "utf-8"
    )
    ok = doctor._probe_policy(cfg)
    assert ok.status == doctor.PASS and "push guard block" in ok.detail

    # An IGNORED rule is the failure mode worth surfacing: the admin believes the
    # team is covered when it is not.
    (workspace / "mooring.toml").write_text('[policy]\nai_off = ["../escape"]\n', "utf-8")
    warn = doctor._probe_policy(cfg)
    assert warn.status == doctor.WARN and "IGNORED" in warn.detail

    (workspace / "mooring.toml").write_text("[broken", "utf-8")
    assert doctor._probe_policy(cfg).status == doctor.FAIL
