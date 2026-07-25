#!/usr/bin/env python3
"""PreToolUse sensitivity gate (matcher: Agent).

Runs before tier selection. Some data must never be delegated to a worker
regardless of how cheap the task is; task type says nothing about that, so it
cannot live in the routing decision table. This gate is the separate step: it
inspects the Agent spawn payload and blocks the delegation when sensitive
content is headed to a worker that is not explicitly allowed to receive it.

Config: $MODEL_ROUTER_SENSITIVITY_CONFIG, else <cwd>/.claude/router-sensitivity.json.
No config file means the gate is OFF (exit 0) -- it is opt-in, because a plugin
that blocked all delegation out of the box would be broken.

Config format (a JSON list of rules, or {"rules": [...]}):
    [
      {
        "name": "pii",
        "patterns": ["\\bNL\\d{2}[A-Z0-9]{10,}\\b"],
        "paths": ["secrets/**", "*.pem"],
        "allow_agents": []
      }
    ]
A rule matches when any regex in `patterns` matches the spawn text OR any glob
in `paths` matches a path-like token in it. On match, if the target agent is
not in `allow_agents`, the spawn is blocked (exit 2).

Fail CLOSED -- the deliberate exception to the router's fail-open hooks. A config
that exists but will not parse, or a rule whose regex will not compile, blocks
the delegation: a false negative here leaks data, a false positive only makes
you do the work inline. Only "no config at all" is inert.
"""
import fnmatch
import json
import os
import re
import sys


def config_path(payload):
    env = os.environ.get("MODEL_ROUTER_SENSITIVITY_CONFIG")
    if env:
        return env
    cwd = payload.get("cwd") or os.getcwd()
    return os.path.join(cwd, ".claude", "router-sensitivity.json")


def collect_strings(value, out):
    """Every string value reachable in the tool_input, so sensitive data is
    caught wherever it sits (prompt, description, nested fields)."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            collect_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            collect_strings(v, out)


def block(msg):
    print(f"model-router: {msg}", file=sys.stderr)
    return 2


def gate_is_off(payload):
    """True when no sensitivity config exists, i.e. the gate is opt-in and off.

    Used to decide what a malformed payload means: with no config there is
    nothing to protect and nothing to block, so fail open like the cost hooks.
    """
    try:
        return not os.path.exists(config_path(payload))
    except Exception:
        return False  # cannot even resolve the config path: assume it is on


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unparseable hook input: nothing to gate, fail open
    if not isinstance(payload, dict):
        # A payload that is not an object is not a spawn we can inspect. With
        # no config the gate is off anyway; with one, we cannot confirm the
        # data is safe to delegate, and unconfirmed means blocked.
        if gate_is_off({}):
            return 0
        return block(
            "hook payload was not an object, so this Agent spawn could not be "
            "screened against the sensitivity config; refusing to delegate "
            "(fail closed).")
    if payload.get("tool_name", "") != "Agent":
        return 0

    path = config_path(payload)
    if not os.path.exists(path):
        return 0  # gate is opt-in

    try:
        with open(path) as f:
            config = json.load(f)
    except Exception:
        return block(
            f"sensitivity config at {path} is unreadable; refusing to delegate "
            "(fail closed). Fix the file, or delete it to disable the gate.")

    rules = config.get("rules") if isinstance(config, dict) else config
    if not isinstance(rules, list):
        return block(
            f"sensitivity config at {path} must be a list of rules or "
            '{"rules": [...]}; refusing to delegate (fail closed).')

    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("subagent_type") or "general-purpose"
    strings = []
    collect_strings(tool_input, strings)
    text = "\n".join(strings)
    tokens = text.split()

    for rule in rules:
        if not isinstance(rule, dict):
            return block(
                f"malformed rule in {path} (not an object); refusing to "
                "delegate (fail closed).")
        name = rule.get("name", "unnamed")
        allow = rule.get("allow_agents") or []

        hit = False
        for pat in rule.get("patterns") or []:
            try:
                if re.search(pat, text):
                    hit = True
                    break
            except re.error:
                return block(
                    f"rule '{name}' in {path} has an invalid regex; refusing "
                    "to delegate (fail closed).")
        if not hit:
            for glob in rule.get("paths") or []:
                if any(fnmatch.fnmatch(tok, glob) for tok in tokens):
                    hit = True
                    break

        if hit and target not in allow:
            return block(
                f"sensitivity rule '{name}' matched content in this Agent "
                f"spawn, and '{target}' is not in its allow_agents. This data "
                "must not be delegated; handle it inline in the main loop. "
                f"(Rule and allow-list live in {path}.)")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # Never exit on an uncaught traceback: exit 1 is neither "allow" nor
        # "deny" to the harness. Resolve it the way this gate resolves
        # everything else -- blocked unless the gate is off entirely.
        if gate_is_off({}):
            sys.exit(0)
        sys.exit(block(f"sensitivity gate errored ({exc.__class__.__name__}); "
                       "refusing to delegate (fail closed)."))
