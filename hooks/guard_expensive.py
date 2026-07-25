#!/usr/bin/env python3
"""PreToolUse guard (matcher: Agent): keep expensive spawns deliberate.

Two things get blocked:

1. **Generic reasoning agents** (`general-purpose`, `Explore`, `Plan`,
   `claude`, and a bare Agent call with no `subagent_type`, which the harness
   resolves to `general-purpose`). These bill at main-loop rates, so using one
   for locate/extract/summarise work pays reasoning prices for grep.
2. **Escalation-ceiling agents** -- registry entries marked
   `escalation_only`, or sitting on the top tier. These are reachable, but
   only as an escalation: a spawn whose prompt carries the
   `[router-escalation ...]` marker is allowed through, because the routing
   protocol only emits that marker after a cheaper tier verifiably failed.

Agents registered in `router-config.json` (including profile-registered ones
from other plugins, e.g. `sdd-planner:quality-scanner`) are allowed: the
registry is the statement that they are a legitimate routing target at their
declared tier. Unregistered agents are allowed too -- this guard exists to
stop expensive defaults, not to be an allowlist for every plugin installed.

Escape hatch: `MODEL_ROUTER_ALLOW_EXPENSIVE=1` allows everything for the
session. Fail-open on any parse or config problem.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_config  # noqa: E402

ESCALATION_MARKER = "[router-escalation"


def spawn_text(tool_input):
    parts = []
    for key in ("prompt", "description"):
        value = tool_input.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def deny(message):
    print(f"model-router: {message}", file=sys.stderr)
    return 2


def main():
    if os.environ.get("MODEL_ROUTER_ALLOW_EXPENSIVE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unparseable input: fail open
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    agent_type = router_config.normalize(tool_input.get("subagent_type", ""))
    if not agent_type:
        # An omitted subagent_type resolves to general-purpose.
        agent_type = "general-purpose"

    try:
        config = router_config.load(payload.get("cwd") or os.getcwd())
    except Exception:
        return 0  # config trouble must never wedge a spawn

    if router_config.is_generic(agent_type, config):
        return deny(
            f"blocked reasoning-tier agent '{agent_type}'. It bills at "
            "main-loop rates. Route locate/map -> model-router:scout, "
            "extract/summarise/classify -> model-router:extractor, mechanical "
            "edits -> model-router:mechanic or model-router:builder, and keep "
            "reasoning in the main loop. If the task genuinely needs "
            "main-loop breadth, set MODEL_ROUTER_ALLOW_EXPENSIVE=1 for this "
            "session."
        )

    entry = router_config.lookup(agent_type, config)
    if not isinstance(entry, dict):
        return 0  # unregistered: not this guard's business

    ceiling = router_config.ceiling_tier(config)
    by_flag = entry.get("escalation_only") is True
    by_tier = entry.get("tier") == ceiling
    if not (by_flag or by_tier):
        return 0

    if ESCALATION_MARKER in spawn_text(tool_input):
        return 0  # a verified escalation; the protocol earned this spawn

    # Name why this agent counts as the ceiling. A tier-based block on an agent
    # nobody thinks of as expensive is nearly always a misconfigured `ceiling`,
    # and that is only diagnosable if the message says which rule fired.
    reason = "marked escalation_only" if by_flag \
        else f"on the ceiling tier '{ceiling}'"
    cheaper = entry.get("escalates_from")
    hint = f" Start at '{cheaper}' and escalate on verified failure." if cheaper else ""
    fix = "" if by_flag else (
        f" If '{ceiling}' is not meant to be your most expensive tier, set "
        '"ceiling" in router-config.json to the tier that is.')
    return deny(
        f"blocked escalation-ceiling agent '{agent_type}' ({reason}) spawned "
        f"as a routing default.{hint} A genuine escalation prefixes its prompt "
        f"with '{ESCALATION_MARKER} from <agent>]' and includes the failed "
        "attempt's footer; that form is allowed through. Otherwise set "
        f"MODEL_ROUTER_ALLOW_EXPENSIVE=1 for this session.{fix}"
    )


if __name__ == "__main__":
    sys.exit(main())
