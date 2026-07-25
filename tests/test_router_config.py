import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

import router_config  # noqa: E402


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# --- shipped defaults -------------------------------------------------------

def test_shipped_config_parses_and_matches_module_defaults():
    shipped = json.loads((ROOT / "router-config.json").read_text())
    assert shipped["tiers"] == router_config.DEFAULTS["tiers"]
    assert set(router_config.DEFAULTS["agents"]) <= set(shipped["agents"])
    for name, entry in shipped["agents"].items():
        assert entry["tier"] in shipped["tiers"], name
        assert entry["capabilities"], name


def test_every_builtin_agent_has_a_definition_file():
    config = router_config.load()
    for name, entry in router_config.agents(config).items():
        if ":" in name:
            continue  # profile-registered, owned by another plugin
        assert (ROOT / "agents" / f"{name}.md").is_file(), name


# --- lookup -----------------------------------------------------------------

def test_lookup_matches_bare_and_namespaced_forms():
    assert router_config.tier_of("scout") == "haiku"
    assert router_config.tier_of("model-router:scout") == "haiku"
    assert router_config.tier_of("nope") is None


def test_generic_agents_recognised():
    for name in ("general-purpose", "Explore", "Plan", "claude"):
        assert router_config.is_generic(name)
    # substring lookalikes are not generic
    assert not router_config.is_generic("claude-code-guide")
    assert not router_config.is_generic("scout")


# --- merging ----------------------------------------------------------------

def test_project_config_retiers_one_agent_without_restating_registry(tmp_path):
    write(tmp_path / ".claude" / "router-config.json",
          {"agents": {"scout": {"tier": "sonnet", "capabilities": ["locate"]}}})
    config = router_config.load(str(tmp_path))
    assert config["agents"]["scout"]["tier"] == "sonnet"
    assert "extractor" in config["agents"], "untouched agents must survive"


def test_project_config_can_raise_the_inline_budget(tmp_path):
    write(tmp_path / ".claude" / "router-config.json", {"guard": {"inline_budget": 12}})
    config = router_config.load(str(tmp_path))
    assert router_config.inline_budget(config) == 12
    assert router_config.guard(config)["exempt_planning_root"] is True


def test_malformed_project_config_falls_back_to_defaults(tmp_path):
    path = tmp_path / ".claude" / "router-config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    config = router_config.load(str(tmp_path))
    assert router_config.tier_of("scout", config) == "haiku"


# --- profiles ---------------------------------------------------------------

def test_profile_registers_external_agents(tmp_path):
    write(tmp_path / ".claude" / "router-config.json", {"profiles": ["sdd-planner"]})
    config = router_config.load(str(tmp_path))
    assert router_config.tier_of("sdd-planner:quality-scanner", config) == "sonnet"
    assert router_config.tier_of("sdd-planner:spec-reviewer", config) == "haiku"
    assert router_config.tier_of("sdd-planner:code-implementer", config) == "opus"
    assert "scout" in config["agents"], "built-ins must survive profile merge"


def test_profile_agents_are_not_generic(tmp_path):
    """The whole point: registered externals are routing targets, not agents to map down."""
    write(tmp_path / ".claude" / "router-config.json", {"profiles": ["sdd-planner"]})
    config = router_config.load(str(tmp_path))
    for name in config["agents"]:
        assert not router_config.is_generic(name, config), name


def test_self_context_agents_never_declare_prefetch():
    """`self_context` is the stronger claim; carrying both is contradictory."""
    config = router_config.load()
    for path in (ROOT / "profiles").glob("*.json"):
        profile = json.loads(path.read_text())
        for name, entry in {**config["agents"], **profile["agents"]}.items():
            if entry.get("self_context"):
                assert not entry.get("prefetch"), f"{name} claims both"


def test_sdd_review_lanes_are_self_context(tmp_path):
    """The four lanes find their own diffs/plans/specs by design.

    Prefetching for them, or pre-reading their inputs, collapses four
    independent perspectives into one while still looking like a four-lane
    review. The flag is what stops the router "optimising" that away.
    """
    write(tmp_path / ".claude" / "router-config.json", {"profiles": ["sdd-planner"]})
    config = router_config.load(str(tmp_path))
    lanes = ("drift-detector", "quality-scanner", "spec-compliance",
             "blind-spot-finder")
    for lane in lanes:
        entry = router_config.lookup(f"sdd-planner:{lane}", config)
        assert entry is not None, lane
        assert entry.get("self_context") is True, f"{lane} must be self-context"
        assert not entry.get("prefetch"), f"{lane} must never carry prefetch"


def test_project_overrides_win_over_profile(tmp_path):
    write(tmp_path / ".claude" / "router-config.json",
          {"profiles": ["sdd-planner"],
           "agents": {"sdd-planner:researcher": {"tier": "haiku",
                                                 "capabilities": ["gather-context"]}}})
    config = router_config.load(str(tmp_path))
    assert router_config.tier_of("sdd-planner:researcher", config) == "haiku"


def test_unknown_or_unsafe_profile_names_are_ignored(tmp_path):
    write(tmp_path / ".claude" / "router-config.json",
          {"profiles": ["does-not-exist", "../../etc/passwd", ".hidden"]})
    config = router_config.load(str(tmp_path))
    assert router_config.tier_of("scout", config) == "haiku"


def test_shipped_profiles_are_wellformed():
    config = router_config.load()
    tiers = config["tiers"]
    for path in (ROOT / "profiles").glob("*.json"):
        profile = json.loads(path.read_text())
        assert profile["name"] == path.stem
        assert profile["description"]
        for name, entry in profile["agents"].items():
            assert entry["tier"] in tiers, f"{path.name}:{name}"
            assert entry["capabilities"], f"{path.name}:{name}"
            if entry.get("escalates_from"):
                assert entry["escalates_from"] in config["agents"] \
                    or entry["escalates_from"] in profile["agents"], name


# --- planning root ----------------------------------------------------------

def test_planning_root_from_config_file(tmp_path):
    (tmp_path / "planning-config.json").write_text(json.dumps({"planningRoot": ".plans"}))
    assert router_config.planning_root(str(tmp_path)) == str(tmp_path / ".plans")


def test_planning_root_walks_up_from_subdirectory(tmp_path):
    (tmp_path / "planning-config.json").write_text(json.dumps({"planningRoot": "Planning"}))
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert router_config.planning_root(str(deep)) == str(tmp_path / "Planning")


def test_planning_root_dot_means_config_directory(tmp_path):
    (tmp_path / "planning-config.json").write_text(json.dumps({"planningRoot": "."}))
    assert router_config.planning_root(str(tmp_path)) == str(tmp_path)


def test_planning_root_absent_config_is_none(tmp_path):
    assert router_config.planning_root(str(tmp_path)) is None


# --- registry.py ------------------------------------------------------------

def test_registry_script_lists_agents():
    proc = subprocess.run(
        ["python3", str(ROOT / "scripts" / "registry.py"), "--routes"],
        capture_output=True, text=True)
    assert proc.returncode == 0
    for agent in ("scout", "extractor", "mechanic", "builder", "sage"):
        assert f"`{agent}`" in proc.stdout
    assert "| Agent | Tier | Capabilities | Notes |" in proc.stdout


def test_registry_script_compact_mode():
    proc = subprocess.run(
        ["python3", str(ROOT / "scripts" / "registry.py"), "--compact"],
        capture_output=True, text=True)
    assert proc.returncode == 0
    assert "scout [haiku]:" in proc.stdout


def test_shipped_ceiling_is_a_real_tier():
    config = router_config.load()
    assert config["ceiling"] in config["tiers"]
    assert router_config.ceiling_tier(config) == config["ceiling"]


def test_shipped_ceiling_agents_are_flagged_not_merely_positioned():
    """Every agent on the ceiling tier must also say so explicitly.

    Tier position is the fallback, not the mechanism; relying on it means a
    tiers edit changes which agents are guarded.
    """
    config = router_config.load()
    ceiling = router_config.ceiling_tier(config)
    for name, entry in router_config.agents(config).items():
        if entry.get("tier") == ceiling:
            assert entry.get("escalation_only") is True, name


def test_ceiling_ignores_tier_order(tmp_path):
    write(tmp_path / ".claude" / "router-config.json",
          {"tiers": ["fable", "haiku", "sonnet", "opus"]})
    config = router_config.load(str(tmp_path))
    assert router_config.ceiling_tier(config) == "fable"
