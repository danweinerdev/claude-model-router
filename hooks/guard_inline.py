#!/usr/bin/env python3
"""PreToolUse inline-exploration budget.

The routing policy allows one-shot deterministic commands in the main loop
but wants iterative discovery delegated to cheap workers. This guard makes
that deterministic: after `guard.inline_budget` search-type tool calls within
one user prompt, further ones are denied with a pointer to scout/extractor.
A foreground (blocking) Agent call resets the budget; a background one does
not, so inline work racing a background worker still hits the wall. Subagent
tool calls are never counted or blocked.

Planning-root exemption: reads and searches confined to a spec-driven
planning root (sdd-planner's `planning-config.json`, or the conventional
`Plans/`, `Specs/`, `Designs/`, ... directories) are not counted. Lifecycle
skills like /plan and /code-review legitimately read many artifacts in the
primary context, and denying those breaks the workflow the router is supposed
to cooperate with. The exemption is narrow on purpose -- an operation that
touches anything outside the planning root is counted as normal, so
`grep -r foo src Plans` cannot launder code exploration through it. Exempt
does not mean free: the routing policy still sends bulk artifact reading to
`extractor` at haiku rates.

Fail-open by design: any parse problem allows.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_config  # noqa: E402

SEARCHY_TOOLS = {"Read", "Grep", "Glob"}
# a searchy word counts at any command position (start, or after ; && || | ` $( or
# newline, with optional VAR=x assignments), so prefixes like `cd x && grep` or
# `export F=1; rg` cannot dodge the counter. Words in ordinary arguments do not match.
SEARCHY_BASH = re.compile(
    r"(?:^|[;&|`\n]|\$\()\s*(?:\w+=\S*\s+)*"
    r"(rg|grep|find|fd|ls|tree|cat|head|tail|awk|jq|yq)\b")
# stdout redirected to a file means the command writes, it does not explore.
# `2>` / `2>&1` are stderr plumbing common in real searches; keep counting those.
WRITE_REDIRECT = re.compile(r"(?<![\d&])>|&>")
# Path-shaped argument tokens: anything containing a separator, plus bare
# capitalised words that could name an artifact directory (Plans, Specs).
# Flags are dropped by the caller. A token wrongly judged "not a path" only
# makes the guard stricter, never laxer.
PATH_TOKEN = re.compile(r"^[~./]?[\w.@+-]+(?:/[\w.*?@+-]*)+/?$|^[A-Z][\w-]*/?$")

PATH_FIELDS = ("file_path", "path", "notebook_path", "filePath")


def counter_path(payload):
    key = f"{payload.get('session_id', 'unknown')}-{payload.get('prompt_id', 'unknown')}"
    return os.path.join(tempfile.gettempdir(), f"model-router-inline-{key}")


def bash_path_tokens(command, cwd):
    """Argument tokens from a shell command that name paths."""
    tokens = []
    for raw in re.split(r"[\s;|&]+", command):
        token = raw.strip("'\"`()")
        if not token or token.startswith("-"):
            continue
        if "=" in token and not token.startswith("/"):
            continue
        if PATH_TOKEN.match(token):
            tokens.append(token)
            continue
        # A bare word naming something on disk is a path argument
        # (`grep foo src`), not a search pattern -- and bare lowercase
        # directory names are exactly how code trees get swept in alongside
        # artifact directories. Mistaking a pattern for a path only makes the
        # guard stricter, never laxer, so resolve the ambiguity that way.
        try:
            if os.path.exists(os.path.join(cwd, os.path.expanduser(token))):
                tokens.append(token)
        except (OSError, ValueError):
            continue
    return tokens


def candidate_paths(tool, tool_input, cwd):
    if tool == "Bash":
        return bash_path_tokens(tool_input.get("command", "") or "", cwd)
    paths = []
    for field in PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def under(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), root]) == root
    except (ValueError, OSError):
        return False


def is_planning_op(tool, tool_input, cwd, settings):
    """True when every path this operation touches lives in the planning root.

    An operation naming no path at all (a bare `ls`, a Grep with no `path`)
    is not exempt: it is unscoped, which is exactly the inline exploration the
    budget exists to catch.
    """
    if not settings.get("exempt_planning_root", True):
        return False

    dirs = settings.get("planning_dirs")
    dirs = dirs if isinstance(dirs, list) \
        else router_config.DEFAULTS["guard"]["planning_dirs"]
    try:
        root = router_config.planning_root(cwd)
    except Exception:
        root = None

    paths = candidate_paths(tool, tool_input, cwd)
    if not paths:
        return False

    for path in paths:
        expanded = os.path.expanduser(path)
        absolute = expanded if os.path.isabs(expanded) \
            else os.path.join(cwd, expanded)
        if root and under(absolute, root):
            continue
        # No planning-config.json, or a path outside the resolved root: fall
        # back to the conventional artifact directory names.
        parts = [p for p in os.path.normpath(path).split(os.sep)
                 if p not in ("", ".")]
        if parts and any(part in dirs for part in parts):
            continue
        return False
    return True


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("agent_id") or payload.get("agent_type"):
        return 0  # subagent doing its job; never throttle workers

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}

    if tool == "Agent":
        # A foreground (blocking) dispatch cannot be raced: the loop waits for
        # the result, so resetting the inline budget is safe. A background
        # dispatch returns control immediately and is exactly when inline
        # racing happens, so keep the counter climbing toward the wall.
        backgrounded = tool_input.get("run_in_background", True)
        if not backgrounded:
            try:
                os.remove(counter_path(payload))
            except OSError:
                pass
        return 0
    if os.environ.get("MODEL_ROUTER_ALLOW_INLINE") == "1":
        return 0
    if tool not in SEARCHY_TOOLS:
        if tool != "Bash":
            return 0
        command = tool_input.get("command", "")
        if not SEARCHY_BASH.search(command):
            return 0
        if WRITE_REDIRECT.search(command):
            return 0  # cat/awk/etc. writing a file is not exploration

    cwd = payload.get("cwd") or os.getcwd()
    try:
        config = router_config.load(cwd)
        settings = router_config.guard(config)
    except Exception:
        return 0
    if is_planning_op(tool, tool_input, cwd, settings):
        return 0  # planning-artifact work; the lifecycle skills need it

    path = counter_path(payload)
    try:
        count = int(open(path).read())
    except Exception:
        count = 0
    count += 1
    try:
        with open(path, "w") as f:
            f.write(str(count))
    except OSError:
        return 0

    budget = router_config.inline_budget(config)
    if count <= budget:
        return 0
    print(
        f"model-router: inline search op {count} this prompt exceeds budget of "
        f"{budget}. You are exploring inline at main-loop rates. Delegate the "
        "remaining discovery to model-router:scout (locate) or "
        "model-router:extractor (read/summarise) in one Agent call - both run "
        "on haiku and read the same bytes for a fraction of the cost; the "
        "budget resets when you delegate. Set MODEL_ROUTER_ALLOW_INLINE=1 to "
        "disable this guard.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
