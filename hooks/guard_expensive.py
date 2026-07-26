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

    # The registry is consulted before any denial. Registering an agent is a
    # deliberate statement that it is a routing target at a known tier, so it
    # outranks the generic-agent list: a project that registers `Explore` with
    # a tier has said what it means, and the guard exists to stop accidental
    # expensive defaults, not deliberate ones.
    entry = router_config.lookup(agent_type, config)

    if not isinstance(entry, dict):
        if router_config.is_generic(agent_type, config):
            return deny(
                f"blocked reasoning-tier agent '{agent_type}'. It bills at "
                "main-loop rates. Route locate/map -> model-router:scout, "
                "extract/summarise/classify -> model-router:extractor, "
                "mechanical edits -> model-router:mechanic or "
                "model-router:builder, and keep reasoning in the main loop. "
                "If this agent is a deliberate routing target, register it in "
                ".claude/router-config.json under `agents` with its tier. To "
                "allow it just for this session, set "
                "MODEL_ROUTER_ALLOW_EXPENSIVE=1."
            )
        return 0  # unregistered and not generic: not this guard's business

    ceiling = router_config.ceiling_tier(config)

    if entry.get("escalation_only") is False:
        return 0  # explicit opt-in: covers the agent's tier and any override

    # A spawn may name a model directly, which is how the protocol retries an
    # agent one tier up without swapping it for a different one. That override
    # is the effective tier, so the ceiling has to be judged on it too --
    # otherwise `scout` with model=fable reaches the ceiling untouched.
    by_flag = entry.get("escalation_only") is True
    by_tier = entry.get("tier") == ceiling
    by_model = router_config.names_tier(tool_input.get("model"), ceiling)
    if not (by_flag or by_tier or by_model):
        return 0

    if ESCALATION_MARKER in spawn_text(tool_input):
        return 0  # a verified escalation; the protocol earned this spawn

    # Name why this agent counts as the ceiling. A tier-based block on an agent
    # nobody thinks of as expensive is nearly always a misconfigured `ceiling`,
    # and that is only diagnosable if the message says which rule fired.
    if by_flag:
        reason = "marked escalation_only"
    elif by_tier:
        reason = f"on the ceiling tier '{ceiling}'"
    else:
        reason = (f"requested with model '{tool_input.get('model')}', which is "
                  f"the ceiling tier '{ceiling}'")
    cheaper = entry.get("escalates_from")
    hint = f" Start at '{cheaper}' and escalate on verified failure." if cheaper else ""
    # Always name the per-agent opt-in. Reaching for the session-wide env var
    # to dispatch one expensive agent disables the guard for everything else
    # too, which is a worse outcome than the spawn it was meant to permit.
    optin = (
        f' To make \'{agent_type}\' a routing target in its own right, set '
        f'"escalation_only": false for it under `agents` in '
        f'.claude/router-config.json.')
    if not by_flag:
        optin += (
            f" If '{ceiling}' is not meant to be your most expensive tier, set "
            '"ceiling" to the tier that is instead.')
    return deny(
        f"blocked escalation-ceiling agent '{agent_type}' ({reason}) spawned "
        f"as a routing default.{hint} A genuine escalation prefixes its prompt "
        f"with '{ESCALATION_MARKER} from <agent>]' and includes the failed "
        f"attempt's footer; that form is allowed through.{optin} To allow every "
        "expensive spawn this session, set MODEL_ROUTER_ALLOW_EXPENSIVE=1."
    )


if __name__ == "__main__":
    sys.exit(main())
