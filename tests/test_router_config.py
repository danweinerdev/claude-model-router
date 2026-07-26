import json
import subprocess
import sys

import pytest
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
            if "tier" not in entry:
                # An annotation: adds fields to an agent that already exists
                # (an escalation link on a built-in worker, say). It merges
                # field-wise, so it must not need to restate tier/capabilities.
                assert name in config["agents"], \
                    f"{path.name}:{name} has no tier and no entry to extend"
                continue
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


def test_escalation_only_is_authoritative_in_both_directions():
    config = router_config.load()
    ceiling = router_config.ceiling_tier(config)
    # explicit false opts in even on the ceiling tier
    assert not router_config.is_escalation_only(
        {"tier": ceiling, "escalation_only": False}, config)
    # explicit true guards even on a cheap tier
    assert router_config.is_escalation_only(
        {"tier": "haiku", "escalation_only": True}, config)
    # absent falls back to the tier
    assert router_config.is_escalation_only({"tier": ceiling}, config)
    assert not router_config.is_escalation_only({"tier": "haiku"}, config)


def test_non_boolean_escalation_only_falls_back_to_tier():
    config = router_config.load()
    ceiling = router_config.ceiling_tier(config)
    for junk in ("yes", 1, None, [], {}):
        assert router_config.is_escalation_only(
            {"tier": ceiling, "escalation_only": junk}, config) is True


# --- escalation links -------------------------------------------------------

@pytest.fixture(scope="module")
def escalation_config(tmp_path_factory):
    """Registry with every shipped profile active, so links are resolvable."""
    names = sorted(p.stem for p in (ROOT / "profiles").glob("*.json"))
    root = tmp_path_factory.mktemp("escalation")
    write(root / ".claude" / "router-config.json", {"profiles": names})
    return router_config.load(str(root))


def tier_rank(entry, config):
    order = config["tiers"]
    tier = entry.get("tier")
    return order.index(tier) if tier in order else -1


def test_escalation_always_moves_up_a_tier(escalation_config):
    """A link that points down or sideways is a worse retry than the failure.

    This is the mechanical guard against re-dispatching to the rung that just
    failed, or swapping a specialist for a cheaper worker on the way "up".
    """
    config = escalation_config
    registry = router_config.agents(config)
    for name, entry in registry.items():
        target_name = entry.get("escalates_to")
        if not target_name:
            continue
        target = router_config.lookup(target_name, config)
        assert target is not None, f"{name} escalates_to unknown '{target_name}'"
        assert tier_rank(target, config) > tier_rank(entry, config), \
            f"{name} -> {target_name} does not move up a tier"


def test_escalates_from_names_a_cheaper_agent(escalation_config):
    config = escalation_config
    for name, entry in router_config.agents(config).items():
        source_name = entry.get("escalates_from")
        if not source_name:
            continue
        source = router_config.lookup(source_name, config)
        assert source is not None, f"{name} escalates_from unknown '{source_name}'"
        assert tier_rank(source, config) < tier_rank(entry, config), \
            f"{name} claims to be reached from '{source_name}', which is not cheaper"


def test_escalation_links_agree_in_both_directions(escalation_config):
    """A one-sided link is how the target becomes unreachable in practice."""
    config = escalation_config
    registry = router_config.agents(config)
    for name, entry in registry.items():
        if entry.get("escalates_to"):
            target = router_config.lookup(entry["escalates_to"], config)
            back = target.get("escalates_from")
            assert back is None or router_config.lookup(back, config) is entry \
                or back == name, \
                f"{name} -> {entry['escalates_to']} but it names '{back}' as its source"


def test_escalation_preserves_the_capability(escalation_config):
    """Escalating must not swap the specialist for a pricier generalist.

    The next rung has to be able to do the job that just failed, so it must
    share a capability with the agent it replaces.
    """
    config = escalation_config
    for name, entry in router_config.agents(config).items():
        target_name = entry.get("escalates_to")
        if not target_name:
            continue
        target = router_config.lookup(target_name, config)
        shared = set(entry.get("capabilities") or []) & set(target.get("capabilities") or [])
        assert shared, \
            f"{name} -> {target_name} shares no capability; the retry cannot do the job"


def test_profile_annotation_does_not_erase_the_builtin_entry(escalation_config):
    """The sdd-planner profile adds an escalation link to `builder`."""
    config = escalation_config
    builder = router_config.agents(config)["builder"]
    assert builder["tier"] == "sonnet"
    assert "implement-from-plan" in builder["capabilities"]
    assert builder["escalates_to"] == "sdd-planner:code-implementer"


def test_base_registry_is_self_contained():
    """model-router must work with no profiles and no other plugin installed."""
    config = router_config.load()
    assert not config["profiles"]
    for name, entry in router_config.agents(config).items():
        assert ":" not in name, f"base registry references external agent {name}"
        target = entry.get("escalates_to")
        assert target is None or target in config["agents"], \
            f"{name} escalates to '{target}', absent from the base registry"


def test_shipped_config_names_no_other_plugin():
    text = (ROOT / "router-config.json").read_text()
    assert "sdd-planner" not in text


def test_names_tier_matches_bare_and_full_model_ids():
    assert router_config.names_tier("fable", "fable")
    assert router_config.names_tier("claude-fable-5", "fable")
    assert not router_config.names_tier("claude-sonnet-5", "fable")
    # anything uninterpretable names no tier, so the guard fails open
    for junk in (None, "", 42, [], "some-local-llm"):
        assert not router_config.names_tier(junk, "fable")
