import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "guard_sensitive.py"
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}

IBAN_RULE = [{"name": "pii", "patterns": [r"\bNL\d{2}[A-Z]{4}\d{10}\b"],
              "allow_agents": []}]


def run_guard(payload, env=None):
    return subprocess.run(
        ["python3", str(SCRIPT)],
        input=json.dumps(payload),
        text=True, capture_output=True,
        env={**ENV, **(env or {})},
    )


def spawn(prompt, agent="model-router:extractor"):
    return {"tool_name": "Agent",
            "tool_input": {"subagent_type": agent, "prompt": prompt}}


def write_config(tmp_path, data):
    cfg = tmp_path / "sensitivity.json"
    cfg.write_text(data if isinstance(data, str) else json.dumps(data))
    return {"MODEL_ROUTER_SENSITIVITY_CONFIG": str(cfg)}


def test_no_config_allows(tmp_path):
    env = {"MODEL_ROUTER_SENSITIVITY_CONFIG": str(tmp_path / "missing.json")}
    proc = run_guard(spawn("NL12ABCD3456789012"), env=env)
    assert proc.returncode == 0


def test_matching_pattern_blocks(tmp_path):
    env = write_config(tmp_path, IBAN_RULE)
    proc = run_guard(spawn("pay out to NL12ABCD3456789012 please"), env=env)
    assert proc.returncode == 2
    assert "pii" in proc.stderr


def test_allow_agents_bypasses(tmp_path):
    rule = [{"name": "pii", "patterns": [r"\bNL\d{2}[A-Z]{4}\d{10}\b"],
             "allow_agents": ["model-router:extractor"]}]
    env = write_config(tmp_path, rule)
    proc = run_guard(spawn("NL12ABCD3456789012", agent="model-router:extractor"), env=env)
    assert proc.returncode == 0


def test_non_matching_allows(tmp_path):
    env = write_config(tmp_path, IBAN_RULE)
    proc = run_guard(spawn("summarise the changelog for me"), env=env)
    assert proc.returncode == 0


def test_path_glob_blocks(tmp_path):
    rule = [{"name": "keys", "paths": ["*.pem"], "allow_agents": []}]
    env = write_config(tmp_path, rule)
    proc = run_guard(spawn("read config/tls/server.pem and summarise"), env=env)
    assert proc.returncode == 2
    assert "keys" in proc.stderr


def test_unparseable_config_blocks(tmp_path):
    env = write_config(tmp_path, "{ this is not json")
    proc = run_guard(spawn("anything"), env=env)
    assert proc.returncode == 2
    assert "fail closed" in proc.stderr


def test_invalid_regex_blocks(tmp_path):
    rule = [{"name": "bad", "patterns": ["("], "allow_agents": []}]
    env = write_config(tmp_path, rule)
    proc = run_guard(spawn("anything"), env=env)
    assert proc.returncode == 2
    assert "invalid regex" in proc.stderr


def test_non_agent_tool_ignored(tmp_path):
    env = write_config(tmp_path, IBAN_RULE)
    proc = run_guard({"tool_name": "Bash",
                      "tool_input": {"command": "echo NL12ABCD3456789012"}},
                     env=env)
    assert proc.returncode == 0


def test_garbage_input_allows():
    proc = subprocess.run(["python3", str(SCRIPT)], input="not json",
                          text=True, capture_output=True, env=ENV)
    assert proc.returncode == 0


def test_rules_dict_form(tmp_path):
    env = write_config(tmp_path, {"rules": IBAN_RULE})
    proc = run_guard(spawn("NL12ABCD3456789012"), env=env)
    assert proc.returncode == 2


def run_raw(stdin, env=None):
    """Send arbitrary bytes, bypassing json.dumps, to exercise bad payloads."""
    return subprocess.run(
        ["python3", str(SCRIPT)],
        input=stdin, text=True, capture_output=True,
        env={**ENV, **(env or {})},
    )


NON_OBJECT_PAYLOADS = ("null", "[1, 2, 3]", '"hello"', "42")


def test_non_object_payload_allows_when_gate_is_off(tmp_path):
    """No config means the gate is inert -- a junk payload must not block."""
    env = {"MODEL_ROUTER_SENSITIVITY_CONFIG": str(tmp_path / "missing.json")}
    for body in NON_OBJECT_PAYLOADS:
        proc = run_raw(body, env=env)
        assert proc.returncode == 0, f"{body!r}: {proc.stderr}"
        assert "Traceback" not in proc.stderr, body


def test_non_object_payload_blocks_when_gate_is_on(tmp_path):
    """A spawn that cannot be screened is a spawn that does not go out.

    Unconfirmed is not the same as safe: the gate's whole premise is that a
    false negative leaks data while a false positive only costs inline work.
    """
    env = write_config(tmp_path, IBAN_RULE)
    for body in NON_OBJECT_PAYLOADS:
        proc = run_raw(body, env=env)
        assert proc.returncode == 2, f"{body!r} should fail closed"
        assert "fail closed" in proc.stderr


def test_never_exits_on_a_traceback(tmp_path):
    """Exit 1 is neither allow nor deny to the harness; it must never happen."""
    env = write_config(tmp_path, IBAN_RULE)
    for body in ("not json {{{", "", '{"tool_name": "Agent"',
                 json.dumps({"tool_name": "Agent", "tool_input": None}),
                 json.dumps({"tool_name": "Agent",
                             "tool_input": {"prompt": {"a": [1, {"b": None}]}}})):
        proc = run_raw(body, env=env)
        assert proc.returncode in (0, 2), f"{body!r} -> {proc.returncode}"
        assert "Traceback" not in proc.stderr, body
