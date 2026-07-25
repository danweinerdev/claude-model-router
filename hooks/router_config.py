#!/usr/bin/env python3
"""Shared configuration for model-router hooks, scripts and skills.

One loader so the guards, the metrics logger and the reporting scripts all
agree on which agents exist, what tier each runs at, and how strict the
inline-exploration budget is.

Resolution order (later wins, shallow-merged per top-level key):

1. `<plugin root>/router-config.json`          -- shipped defaults
2. every profile named in the merged `profiles` list, from
   `<plugin root>/profiles/<name>.json`        -- cross-plugin agent registries
3. `<cwd>/.claude/router-config.json`          -- per-project overrides
4. `$MODEL_ROUTER_CONFIG`, if set              -- explicit override, wins outright

`agents` merges key-by-key rather than wholesale, so a project can retier one
agent without restating the registry.

Fail-open by design: a missing, unreadable or malformed config yields the
built-in defaults rather than an exception. These hooks sit in front of every
tool call; a config typo must never wedge a session. (`guard_sensitive.py`
deliberately does not use this module -- it fails closed, on purpose.)
"""
import json
import os

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Mirrors router-config.json. Duplicated here so the hooks still behave
# sanely if the shipped file is missing from a broken install.
DEFAULTS = {
    "tiers": ["haiku", "sonnet", "opus", "fable"],
    "agents": {
        "scout": {"tier": "haiku", "capabilities": ["locate"]},
        "extractor": {"tier": "haiku", "capabilities": ["extract"]},
        "mechanic": {"tier": "sonnet", "capabilities": ["mechanical-edit"]},
        "builder": {"tier": "sonnet", "capabilities": ["implement-from-plan"]},
        "sage": {"tier": "fable", "capabilities": ["deep-reasoning"]},
    },
    # Reasoning-tier harness agents that bill at main-loop rates. Never a
    # routing target for locate/extract/edit work.
    "generic_agents": ["general-purpose", "Explore", "Plan", "claude"],
    "guard": {
        "inline_budget": 5,
        "exempt_planning_root": True,
        "planning_dirs": [
            "Research", "Brainstorm", "Specs", "Designs", "Plans",
            "Decisions", "Retro", "Diagrams",
        ],
    },
    "profiles": [],
}

MERGE_BY_KEY = ("agents", "guard")


def _read(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _merge(base, overlay):
    """Shallow merge, except `agents` and `guard` which merge per key."""
    for key, value in overlay.items():
        if key in MERGE_BY_KEY and isinstance(value, dict) \
                and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def _profile_path(name):
    # Profiles are plugin-shipped files, not arbitrary paths: reject anything
    # that could escape profiles/ (a project config supplies these names).
    if not isinstance(name, str) or not name or "/" in name or "\\" in name \
            or name.startswith("."):
        return None
    return os.path.join(PLUGIN_ROOT, "profiles", f"{name}.json")


def load(cwd=None):
    """The effective configuration for `cwd` (defaults to the process cwd)."""
    cwd = cwd or os.getcwd()
    config = json.loads(json.dumps(DEFAULTS))  # deep copy

    shipped = _read(os.path.join(PLUGIN_ROOT, "router-config.json"))
    if shipped:
        _merge(config, shipped)

    project = _read(os.path.join(cwd, ".claude", "router-config.json"))
    explicit = _read(os.environ["MODEL_ROUTER_CONFIG"]) \
        if os.environ.get("MODEL_ROUTER_CONFIG") else None

    # Profiles are collected before project overrides are applied, so a
    # project can retier a profile-registered agent.
    requested = list(config.get("profiles") or [])
    for source in (project, explicit):
        if source:
            for name in source.get("profiles") or []:
                if name not in requested:
                    requested.append(name)

    for name in requested:
        path = _profile_path(name)
        profile = _read(path) if path else None
        if profile:
            _merge(config, {k: v for k, v in profile.items()
                            if k not in ("profiles", "name", "description")})

    if project:
        _merge(config, project)
    if explicit:
        _merge(config, explicit)

    config["profiles"] = requested
    return config


def agents(config=None, cwd=None):
    config = config if config is not None else load(cwd)
    registry = config.get("agents")
    return registry if isinstance(registry, dict) else {}


def normalize(agent_type):
    """`model-router:scout` and `scout` name the same agent."""
    if not isinstance(agent_type, str):
        return ""
    return agent_type.strip()


def lookup(agent_type, config=None, cwd=None):
    """Registry entry for a spawn target, or None if unregistered.

    Matches the plugin-namespaced form too: an entry keyed `scout` answers for
    `model-router:scout`, and an entry keyed `sdd-planner:researcher` answers
    for a bare `researcher` only if no unqualified entry shadows it.
    """
    name = normalize(agent_type)
    if not name:
        return None
    registry = agents(config, cwd)
    if name in registry:
        return registry[name]
    if ":" in name:
        _, bare = name.rsplit(":", 1)
        if bare in registry:
            return registry[bare]
    else:
        for key, entry in registry.items():
            if key.endswith(f":{name}"):
                return entry
    return None


def tier_of(agent_type, config=None, cwd=None):
    entry = lookup(agent_type, config, cwd)
    if isinstance(entry, dict):
        tier = entry.get("tier")
        if isinstance(tier, str):
            return tier
    return None


def is_generic(agent_type, config=None, cwd=None):
    """True for reasoning-tier harness agents that bill at main-loop rates."""
    config = config if config is not None else load(cwd)
    name = normalize(agent_type)
    generic = config.get("generic_agents")
    if not isinstance(generic, list):
        generic = DEFAULTS["generic_agents"]
    bare = name.rsplit(":", 1)[-1] if ":" in name else name
    return name in generic or bare in generic


def guard(config=None, cwd=None):
    config = config if config is not None else load(cwd)
    settings = config.get("guard")
    if not isinstance(settings, dict):
        return dict(DEFAULTS["guard"])
    return {**DEFAULTS["guard"], **settings}


def inline_budget(config=None, cwd=None):
    """Budget from the environment if set, else config, else the default."""
    raw = os.environ.get("MODEL_ROUTER_INLINE_BUDGET")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    value = guard(config, cwd).get("inline_budget")
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULTS["guard"]["inline_budget"]


def planning_root(cwd):
    """Resolve sdd-planner's planning root, per shared/path-resolution.md.

    Walk up from `cwd` looking for planning-config.json; resolve its
    `planningRoot` against the directory holding it. No config file anywhere
    means there is no planning root to exempt (None) -- the guard then only
    exempts the conventional artifact directory names.
    """
    try:
        current = os.path.abspath(cwd)
    except (TypeError, ValueError):
        return None
    while True:
        candidate = os.path.join(current, "planning-config.json")
        if os.path.isfile(candidate):
            data = _read(candidate) or {}
            root = data.get("planningRoot")
            if not isinstance(root, str) or root in ("", "."):
                return current
            if os.path.isabs(root):
                return os.path.normpath(root)
            return os.path.normpath(os.path.join(current, root))
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
