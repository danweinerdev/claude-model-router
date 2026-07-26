import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "guard_expensive.py"
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


def run_guard(payload, env=None):
    return subprocess.run(
        ["python3", str(SCRIPT)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        text=True, capture_output=True,
        env={**ENV, **(env or {})},
    )


def spawn(agent, prompt=""):
    return {"tool_name": "Agent",
            "tool_input": {"subagent_type": agent, "prompt": prompt}}


def test_blocks_sage_by_default():
    proc = run_guard(spawn("model-router:sage"))
    assert proc.returncode == 2
    assert "MODEL_ROUTER_ALLOW_EXPENSIVE" in proc.stderr


def test_allows_sage_when_flagged():
    proc = run_guard(spawn("model-router:sage"),
                     env={"MODEL_ROUTER_ALLOW_EXPENSIVE": "1"})
    assert proc.returncode == 0


def test_allows_sage_on_marked_escalation():
    """The escalation protocol earns the spawn; a first dispatch does not."""
    proc = run_guard(spawn(
        "sage",
        "[router-escalation from builder] tests still fail after two attempts"))
    assert proc.returncode == 0, proc.stderr


def test_blocks_sage_when_marker_is_merely_described():
    proc = run_guard(spawn("sage", "review this module for correctness"))
    assert proc.returncode == 2


def test_allows_cheap_agents():
    for agent in ("model-router:scout", "extractor", "mechanic", "builder"):
        proc = run_guard(spawn(agent))
        assert proc.returncode == 0, f"{agent} should be allowed: {proc.stderr}"


def test_blocks_generic_reasoning_agents():
    for agent in ("general-purpose", "Explore", "Plan", "claude"):
        proc = run_guard(spawn(agent))
        assert proc.returncode == 2, f"{agent} should be blocked"


def test_allows_unregistered_agents():
    """Not an allowlist: another plugin's agent is none of this guard's business."""
    for agent in ("some-plugin:reviewer", "claude-code-guide", "fast-explorer"):
        proc = run_guard(spawn(agent))
        assert proc.returncode == 0, f"{agent} should be allowed: {proc.stderr}"


def test_garbage_input_allows():
    assert run_guard("not json {{{").returncode == 0


# --- escalation ceiling is named, not positional -----------------------------

def write_config(tmp_path, data):
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "router-config.json").write_text(json.dumps(data))
    return {"tool_name": "Agent", "cwd": str(tmp_path)}


def run_in(tmp_path, agent, prompt=""):
    base = {"tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": {"subagent_type": agent, "prompt": prompt}}
    return run_guard(base)


def test_appending_a_cheap_tier_does_not_make_it_the_ceiling(tmp_path):
    """The trap this replaced: the ceiling used to be whatever tier came last.

    Adding a cheap tier (a local model, say) to the end of `tiers` silently
    promoted it to the escalation ceiling and blocked every agent on it.
    """
    write_config(tmp_path, {
        "tiers": ["haiku", "sonnet", "opus", "fable", "local"],
        "agents": {"localscout": {"tier": "local", "capabilities": ["locate"]}},
    })
    assert run_in(tmp_path, "localscout", "find X").returncode == 0


def test_named_ceiling_still_blocks_regardless_of_position(tmp_path):
    write_config(tmp_path, {
        "tiers": ["haiku", "sonnet", "local"],
        "ceiling": "local",
        "agents": {"localtop": {"tier": "local", "capabilities": ["deep-reasoning"]}},
    })
    proc = run_in(tmp_path, "localtop", "think hard")
    assert proc.returncode == 2
    assert "ceiling tier 'local'" in proc.stderr


def test_ceiling_naming_a_missing_tier_falls_back_to_the_last(tmp_path):
    write_config(tmp_path, {
        "tiers": ["a", "b"],
        "ceiling": "nonexistent",
        "agents": {"x": {"tier": "b", "capabilities": ["z"]},
                   "y": {"tier": "a", "capabilities": ["z"]}},
    })
    assert run_in(tmp_path, "x", "go").returncode == 2
    assert run_in(tmp_path, "y", "go").returncode == 0


def test_sage_is_blocked_by_flag_not_by_tier_position(tmp_path):
    """Built-ins must not depend on tier ordering at all."""
    write_config(tmp_path, {
        "tiers": ["haiku", "sonnet", "opus", "fable", "local"],
        "agents": {"scratch": {"tier": "local", "capabilities": ["locate"]}},
    })
    proc = run_in(tmp_path, "sage", "review this")
    assert proc.returncode == 2
    assert "escalation_only" in proc.stderr
    assert run_in(tmp_path, "sage",
                  "[router-escalation from builder] checks failed").returncode == 0


# --- per-agent opt-in from project config ------------------------------------

def test_project_can_opt_an_agent_in_at_the_ceiling(tmp_path):
    """Opting in must be configuration, not a workaround.

    Without this the only ways to dispatch a ceiling-tier agent deliberately
    were MODEL_ROUTER_ALLOW_EXPENSIVE (which disables the guard for every
    other agent too) or faking an escalation marker (which lies to the
    metrics). Both are worse than the spawn they were meant to permit.
    """
    write_config(tmp_path, {"agents": {"sage": {
        "tier": "fable", "escalation_only": False,
        "capabilities": ["deep-reasoning"]}}})
    assert run_in(tmp_path, "sage", "review this design").returncode == 0


def test_opt_in_overrides_a_profile_that_marked_the_agent(tmp_path):
    write_config(tmp_path, {
        "profiles": ["sdd-planner"],
        "agents": {"sdd-planner:code-implementer": {
            "tier": "opus", "escalation_only": False,
            "capabilities": ["implement-from-plan"]}},
    })
    assert run_in(tmp_path, "sdd-planner:code-implementer",
                  "implement task 3").returncode == 0


def test_opt_in_is_scoped_to_the_named_agent(tmp_path):
    """Opting one agent in must not open the gate for the rest."""
    write_config(tmp_path, {"agents": {"sage": {
        "tier": "fable", "escalation_only": False,
        "capabilities": ["deep-reasoning"]}}})
    proc = run_in(tmp_path, "general-purpose", "go wide")
    assert proc.returncode == 2


def test_registering_a_generic_agent_makes_it_routable(tmp_path):
    """Explicit registration outranks the generic-agent list.

    The guard stops accidental expensive defaults. An agent someone wrote into
    the registry with a tier is not accidental.
    """
    write_config(tmp_path, {"agents": {"Explore": {
        "tier": "opus", "capabilities": ["broad-search"]}}})
    assert run_in(tmp_path, "Explore", "sweep the repo").returncode == 0
    # unregistered generics are untouched by that
    assert run_in(tmp_path, "general-purpose", "go wide").returncode == 2


def test_denial_points_at_the_per_agent_opt_in(tmp_path):
    """A guard that only offers a session-wide escape teaches the wrong fix."""
    write_config(tmp_path, {})
    proc = run_in(tmp_path, "sage", "review this")
    assert proc.returncode == 2
    assert '"escalation_only": false' in proc.stderr
    assert "router-config.json" in proc.stderr


# --- model overrides and the ceiling ----------------------------------------

def test_tier_retry_below_the_ceiling_is_allowed(tmp_path):
    """Re-dispatching the same agent one tier up is the default escalation."""
    write_config(tmp_path, {})
    base = {"tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "builder", "model": "opus",
                           "prompt": "[router-escalation from builder] checks failed"}}
    assert run_guard(base).returncode == 0


def test_model_override_cannot_reach_the_ceiling_unguarded(tmp_path):
    """The ceiling is about the model that runs, not the tier on the label.

    Judging only the registered tier let any cheap agent be spawned at the
    ceiling by naming the model directly.
    """
    write_config(tmp_path, {})
    for model in ("fable", "claude-fable-5"):
        base = {"tool_name": "Agent", "cwd": str(tmp_path),
                "tool_input": {"subagent_type": "scout", "model": model,
                               "prompt": "find X"}}
        proc = run_guard(base)
        assert proc.returncode == 2, model
        assert "ceiling" in proc.stderr


def test_model_override_to_ceiling_passes_on_a_real_escalation(tmp_path):
    write_config(tmp_path, {})
    base = {"tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "scout", "model": "fable",
                           "prompt": "[router-escalation from scout] nothing found"}}
    assert run_guard(base).returncode == 0


def test_unrecognised_model_fails_open(tmp_path):
    """A local or proxied model id names no tier the router knows; allow."""
    write_config(tmp_path, {})
    base = {"tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "scout", "model": "some-local-llm",
                           "prompt": "find X"}}
    assert run_guard(base).returncode == 0


def test_opt_in_also_covers_a_model_override(tmp_path):
    write_config(tmp_path, {"agents": {"scout": {"escalation_only": False}}})
    base = {"tool_name": "Agent", "cwd": str(tmp_path),
            "tool_input": {"subagent_type": "scout", "model": "fable",
                           "prompt": "find X"}}
    assert run_guard(base).returncode == 0
