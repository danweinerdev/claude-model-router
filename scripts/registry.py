#!/usr/bin/env python3
"""Print the effective agent registry.

Used by the SessionStart hook (so the main loop knows which agents exist and
at what tier before it routes anything) and by `/model-router:models`.

    registry.py            markdown table of every registered agent
    registry.py --routes   plus profile-supplied routing rows
    registry.py --compact  one line per agent, for context injection
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))
import router_config  # noqa: E402


def sort_key(item, tiers):
    name, entry = item
    tier = entry.get("tier") if isinstance(entry, dict) else None
    rank = tiers.index(tier) if tier in tiers else len(tiers)
    return (rank, name)


def main(argv):
    compact = "--compact" in argv
    with_routes = "--routes" in argv

    config = router_config.load()
    tiers = config.get("tiers") or router_config.DEFAULTS["tiers"]
    registry = router_config.agents(config)
    if not registry:
        return 0

    profiles = config.get("profiles") or []
    rows = sorted(registry.items(), key=lambda i: sort_key(i, tiers))

    if compact:
        for name, entry in rows:
            entry = entry if isinstance(entry, dict) else {}
            caps = ", ".join(entry.get("capabilities") or []) or "-"
            print(f"{name} [{entry.get('tier', '?')}]: {caps}")
        return 0

    print("| Agent | Tier | Capabilities | Notes |")
    print("|---|---|---|---|")
    for name, entry in rows:
        entry = entry if isinstance(entry, dict) else {}
        caps = ", ".join(entry.get("capabilities") or []) or "-"
        notes = []
        if entry.get("escalation_only"):
            notes.append("escalation only")
        if entry.get("read_only"):
            notes.append("read-only")
        if entry.get("self_context"):
            # Loud on purpose: prefetching for these is a correctness bug.
            notes.append("**self-context - never prefetch**")
        elif entry.get("prefetch"):
            notes.append("prefetch via " + "/".join(entry["prefetch"]))
        print(f"| `{name}` | {entry.get('tier', '?')} | {caps} | "
              f"{'; '.join(notes) or '-'} |")

    if profiles:
        print(f"\nActive profiles: {', '.join(profiles)}")

    if with_routes:
        routes = config.get("routes")
        if isinstance(routes, list) and routes:
            print("\n| Task signals | Route |")
            print("|---|---|")
            for route in routes:
                if not isinstance(route, dict):
                    continue
                target = route.get("route", "?")
                if route.get("escalates_to"):
                    target += f" → {route['escalates_to']} (on verified failure)"
                print(f"| {route.get('signals', '?')} | {target} |")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)  # context injection must never break a session
