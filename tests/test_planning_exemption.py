"""The inline budget must not deny spec-driven planning work.

Lifecycle skills (/plan, /code-review, /excavate) read many artifacts in the
primary context by design. Those reads are exempt; code exploration is not,
and an operation that touches both is not.
"""
import json
import subprocess
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "guard_inline.py"
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
BUDGET = {"MODEL_ROUTER_INLINE_BUDGET": "2"}


def run_guard(payload, env=None):
    return subprocess.run(
        ["python3", str(SCRIPT)],
        input=json.dumps(payload), text=True, capture_output=True,
        env={**ENV, **BUDGET, **(env or {})},
    )


def payload(tool, session, cwd, **tool_input):
    return {"session_id": session, "prompt_id": "p1", "cwd": str(cwd),
            "tool_name": tool, "tool_input": tool_input}


def planning_repo(tmp_path):
    (tmp_path / "planning-config.json").write_text(json.dumps({"planningRoot": ".plans"}))
    (tmp_path / ".plans" / "Plans" / "Feature").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    return tmp_path


def exhaust(session, cwd, n=3):
    for _ in range(n):
        run_guard(payload("Grep", session, cwd, path="src", pattern="x"))


def test_planning_reads_are_never_denied(tmp_path):
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    exhaust(session, repo)
    for _ in range(10):
        proc = run_guard(payload(
            "Read", session, repo,
            file_path=str(repo / ".plans" / "Plans" / "Feature" / "01-Phase.md")))
        assert proc.returncode == 0, proc.stderr


def test_code_reads_are_still_denied_past_budget(tmp_path):
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    exhaust(session, repo)
    proc = run_guard(payload("Read", session, repo,
                             file_path=str(repo / "src" / "main.py")))
    assert proc.returncode == 2
    assert "model-router:extractor" in proc.stderr


def test_planning_reads_do_not_consume_budget(tmp_path):
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    plan = str(repo / ".plans" / "Plans" / "Feature" / "01-Phase.md")
    for _ in range(20):
        assert run_guard(payload("Read", session, repo, file_path=plan)).returncode == 0
    # budget is 2 and none of the above spent any of it
    assert run_guard(payload("Read", session, repo,
                             file_path=str(repo / "src" / "a.py"))).returncode == 0
    assert run_guard(payload("Read", session, repo,
                             file_path=str(repo / "src" / "b.py"))).returncode == 0
    assert run_guard(payload("Read", session, repo,
                             file_path=str(repo / "src" / "c.py"))).returncode == 2


def test_mixed_operation_is_not_exempt(tmp_path):
    """`grep -r foo src Plans` must not launder code exploration."""
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    exhaust(session, repo)
    proc = run_guard(payload("Bash", session, repo,
                             command="grep -r foo .plans/Plans src"))
    assert proc.returncode == 2


def test_bash_grep_confined_to_planning_root_is_exempt(tmp_path):
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    exhaust(session, repo)
    proc = run_guard(payload("Bash", session, repo,
                             command="grep -rn status .plans/Plans"))
    assert proc.returncode == 0, proc.stderr


def test_conventional_dirs_exempt_without_planning_config(tmp_path):
    """A repo with no planning-config.json still gets the artifact directories."""
    (tmp_path / "Specs" / "Auth").mkdir(parents=True)
    session = uuid.uuid4().hex
    exhaust(session, tmp_path)
    proc = run_guard(payload("Read", session, tmp_path,
                             file_path="Specs/Auth/README.md"))
    assert proc.returncode == 0, proc.stderr


def test_unscoped_operation_is_not_exempt(tmp_path):
    """A bare `ls` names no path; unscoped exploration is what the budget is for."""
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    exhaust(session, repo)
    assert run_guard(payload("Bash", session, repo, command="ls")).returncode == 2
    assert run_guard(payload("Grep", session, repo, pattern="x")).returncode == 2


def test_exemption_can_be_switched_off(tmp_path):
    repo = planning_repo(tmp_path)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "router-config.json").write_text(
        json.dumps({"guard": {"exempt_planning_root": False}}))
    session = uuid.uuid4().hex
    exhaust(session, repo)
    proc = run_guard(payload(
        "Read", session, repo,
        file_path=str(repo / ".plans" / "Plans" / "Feature" / "01-Phase.md")))
    assert proc.returncode == 2


def test_subagents_are_never_throttled(tmp_path):
    """Workers reading files is the point of delegating; never throttle them."""
    repo = planning_repo(tmp_path)
    session = uuid.uuid4().hex
    for _ in range(10):
        body = payload("Read", session, repo, file_path=str(repo / "src" / "x.py"))
        body["agent_type"] = "scout"
        assert run_guard(body).returncode == 0
