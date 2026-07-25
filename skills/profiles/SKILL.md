---
name: profiles
description: List, enable, or disable model-router interop profiles - the bundled registries that make another plugin's agents routable at their declared tiers. Use when the user wants routing to cooperate with another plugin (e.g. sdd-planner), asks which profiles are active, or wants to register a new agent with the router.
disable-model-invocation: true
---

# Interop profiles

A profile is a JSON file under `<plugin-root>/profiles/` that registers agents
belonging to another plugin, so the router treats them as first-class targets at
their declared tier instead of mapping them down to the generic workers.

`<plugin-root>` is two directories up from this skill's base directory.

Profiles are **opt-in per project**: a profile ships with the router but does
nothing until a project's `.claude/router-config.json` names it.

## No arguments: show status

1. `ls <plugin-root>/profiles/*.json` for available profiles; read each one's
   `name` and `description`.
2. Read `.claude/router-config.json` in the project for the active `profiles` list.
3. Show a table: profile, active (yes/no), what it registers (agent count + a
   one-line summary). Then run `python3 "<plugin-root>/scripts/registry.py"` and
   show the effective registry below it.

## Argument `enable <name>` / `disable <name>`

1. Verify `<plugin-root>/profiles/<name>.json` exists. If not, list what does and stop.
2. For `enable`: before writing, check the target plugin is actually installed -
   glob `~/.claude/plugins/cache/*/<name>/` and the project's `.claude/agents/`.
   If it isn't found, say so and ask whether to enable anyway (a profile for an
   absent plugin is inert, not harmful).
3. Create or update `.claude/router-config.json` in the project, adding or
   removing the name in the `profiles` array. Preserve every other key:

   ```json
   {
     "profiles": ["sdd-planner"]
   }
   ```

4. Confirm with the resulting effective registry (`registry.py --routes`).
   Say plainly that it takes effect for spawns from now on; the SessionStart
   injection refreshes on the next session.

## Argument `add <agent-name> tier=<tier> caps=<a,b,c>`

Register a single agent without authoring a profile - for a project-local agent
in `.claude/agents/`, or one plugin's agent you don't want a whole profile for.

1. Validate the tier against `tiers` in the effective config.
2. Merge into the project's `.claude/router-config.json` under `agents`:

   ```json
   {
     "agents": {
       "my-reviewer": { "tier": "sonnet", "capabilities": ["review-lane"] }
     }
   }
   ```

3. Confirm with the resulting registry row.

## Writing a new profile

Copy `profiles/sdd-planner.json` as the shape reference. Required: `name`,
`description`, `agents`. Optional: `routes` (rows appended to the decision
table), `guard` (guard settings the profile needs), `notes`.

Each agent entry takes `tier` (required, one of the configured tiers),
`capabilities` (the signals that should route to it), and optionally
`read_only`, `escalation_only`, `escalates_from`, `prefetch`, `lane`,
`description`. Get the tier from the target agent's own `model:` frontmatter -
guessing it wrong makes the router either overpay or route work to an agent that
cannot do it.

A profile intended for the plugin itself goes in `profiles/`; one specific to a
single repo belongs in that repo's `.claude/router-config.json` instead.
