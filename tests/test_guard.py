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
