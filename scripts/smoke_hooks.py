#!/usr/bin/env python3
"""Smoke-test every hook the manifest actually wires up.

Unit tests cover each hook individually. This covers the *manifest*: it walks
`hooks/hooks.json`, runs whatever is wired there, and enforces the two
contracts that hold for all of them.

1. **Every referenced file exists.** A `${CLAUDE_PLUGIN_ROOT}/...` path that
   does not resolve is a hook that silently never runs -- the harness reports
   nothing useful, and the guard you thought was protecting you isn't.
2. **Unusable input fails open.** These hooks sit in front of every tool call.
   Stdin that is not a usable payload at all -- invalid JSON, a bare `null`,
   an array -- carries nothing to decide from, so every hook must exit 0. A
   hook that blocks on noise wedges the session.
3. **Odd-but-valid input decides cleanly.** A structurally valid payload with
   missing or wrongly-typed fields may legitimately *deny* (an empty
   `tool_input` on an `Agent` matcher is a bare Agent call, which resolves to
   `general-purpose` and is meant to be blocked). What it may not do is crash:
   the exit code must be 0 or 2 with no traceback, because exit 1 is neither
   allow nor deny to the harness.

`guard_sensitive.py` fails *closed*, but only relative to a sensitivity
config. These checks run in an empty temp cwd where no config exists, so it is
inert and must allow like the rest.

Run via `make test`. Exits non-zero on the first contract violation.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "hooks", "hooks.json")
PLUGIN_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[\w./-]+)")

# Input that is not a usable hook payload at all. There is nothing here to
# make a decision from, so every hook must allow. (The sensitivity gate is
# fail-closed, but only relative to a config; this runs in a temp cwd with
# none, so it is inert and must allow too.)
UNUSABLE_INPUTS = [
    ("garbage", "not json {{{"),
    ("empty string", ""),
    ("truncated json", '{"tool_name": "Agent"'),
    ("null", "null"),
    ("array", "[1, 2, 3]"),
    ("string", '"hello"'),
]

# Structurally valid payloads with odd or missing fields. These may legitimately
# deny -- an empty tool_input on an Agent matcher is a *bare* Agent call, which
# the harness resolves to general-purpose and the expensive guard is supposed to
# block. So the contract here is not "must allow", it is "must decide cleanly":
# a defined exit code and no traceback.
ODD_INPUTS = [
    ("empty object", "{}"),
    ("wrong types", json.dumps({"tool_name": 42, "tool_input": "nope",
                                "cwd": None, "session_id": []})),
    ("unknown tool", json.dumps({"tool_name": "Frobnicate",
                                 "tool_input": {"x": 1}})),
    ("null tool_input", json.dumps({"tool_name": "Agent", "tool_input": None})),
    ("nested junk", json.dumps({"tool_name": "Agent",
                                "tool_input": {"prompt": {"a": [1, {"b": None}]}}})),
]

VALID_EXITS = (0, 2)

failures = []


def fail(message):
    failures.append(message)
    print(f"FAIL {message}", file=sys.stderr)


def hook_commands(manifest):
    """(event, matcher, command, timeout) for every wired hook."""
    out = []
    for event, blocks in (manifest.get("hooks") or {}).items():
        for block in blocks:
            for hook in block.get("hooks") or []:
                if hook.get("type") != "command":
                    continue
                out.append((event, block.get("matcher"), hook["command"],
                            hook.get("timeout", 60)))
    return out


def check_references(commands):
    seen = set()
    for _, _, command, _ in commands:
        for rel in PLUGIN_REF.findall(command):
            path = os.path.join(ROOT, rel.lstrip("/"))
            if path in seen:
                continue
            seen.add(path)
            if not os.path.isfile(path):
                fail(f"hooks.json references a missing file: {rel}")
    print(f"references: {len(seen)} checked")


def run(command, stdin, cwd, timeout):
    return subprocess.run(
        ["bash", "-c", command],
        input=stdin, text=True, capture_output=True, cwd=cwd,
        timeout=timeout,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": ROOT},
    )


def attempt(label, command, stdin, cwd, timeout, name):
    try:
        proc = run(command, stdin, cwd, timeout)
    except subprocess.TimeoutExpired:
        fail(f"{label} timed out (>{timeout}s) on {name} input")
        return None
    if "Traceback (most recent call last)" in proc.stderr:
        fail(f"{label} crashed on {name} input; "
             f"stderr: {proc.stderr.strip()[-300:]}")
        return None
    return proc


def check_fail_open(commands):
    """Unusable input must always allow -- there is nothing to decide from."""
    checked = 0
    with tempfile.TemporaryDirectory() as cwd:
        for event, matcher, command, timeout in commands:
            label = f"{event}[{matcher or '*'}]"
            for name, stdin in UNUSABLE_INPUTS:
                proc = attempt(label, command, stdin, cwd, timeout, name)
                if proc is None:
                    continue
                if proc.returncode != 0:
                    fail(f"{label} exited {proc.returncode} on {name} input "
                         f"(must fail open); stderr: {proc.stderr.strip()[:200]}")
                checked += 1
    print(f"fail-open: {checked} hook/input combinations checked")


def check_decides_cleanly(commands):
    """Odd-but-valid payloads may deny, but must never crash or hang.

    Exit 1 is the failure this catches: to the harness it is neither allow nor
    deny, so a hook that gets there has no defined behaviour.
    """
    checked = 0
    with tempfile.TemporaryDirectory() as cwd:
        for event, matcher, command, timeout in commands:
            label = f"{event}[{matcher or '*'}]"
            for name, stdin in ODD_INPUTS:
                proc = attempt(label, command, stdin, cwd, timeout, name)
                if proc is None:
                    continue
                if proc.returncode not in VALID_EXITS:
                    fail(f"{label} exited {proc.returncode} on {name} input "
                         f"(expected one of {VALID_EXITS}); "
                         f"stderr: {proc.stderr.strip()[:200]}")
                checked += 1
    print(f"clean-decision: {checked} hook/input combinations checked")


def check_session_start_emits_policy(commands):
    """The routing policy reaching context is the plugin's whole premise."""
    for event, _, command, timeout in commands:
        if event != "SessionStart":
            continue
        with tempfile.TemporaryDirectory() as cwd:
            proc = run(command, "{}", cwd, timeout)
        if "MODEL ROUTER ACTIVE" not in proc.stdout:
            fail("SessionStart hook did not emit the policy banner")
        for marker in ("Step 1: tool first", "ESCALATE:", "self_context"):
            if marker not in proc.stdout:
                fail(f"SessionStart hook did not emit routing policy: {marker!r}")
        if "| Agent | Tier |" not in proc.stdout:
            fail("SessionStart hook did not emit the agent registry")
        print("session-start: policy and registry emitted")


def main():
    try:
        with open(MANIFEST) as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"FAIL cannot read {MANIFEST}: {exc}", file=sys.stderr)
        return 1

    commands = hook_commands(manifest)
    if not commands:
        print("FAIL hooks.json wires no commands", file=sys.stderr)
        return 1
    print(f"manifest: {len(commands)} wired hooks")

    check_references(commands)
    check_fail_open(commands)
    check_decides_cleanly(commands)
    check_session_start_emits_policy(commands)

    if failures:
        print(f"\nsmoke: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
