---
name: models
description: Show or change which model each registered agent runs on, per project. Use when the user wants to see the current tier-to-model mapping, move an agent to a different model (e.g. scout to sonnet, or a read-heavy agent from another plugin down to haiku), or reset overrides. Triggers on "router models", "change router models", "which model does scout use".
disable-model-invocation: true
---

# Model overrides

Manage the per-project model mapping for every agent in the router's registry -
the built-in workers *and* agents registered by a profile (e.g.
`sdd-planner:researcher`). Defaults come from `router-config.json` plus any
active profiles; per-agent overrides live in the project's
`.claude/routing-overrides.md`, which the routing policy reads first.

Overrides work because the Agent tool accepts a `model` parameter per spawn - no
plugin files, and no other plugin's agent files, are ever edited.

Valid agents: every key in the effective registry. Get the list with:

```
python3 "<plugin-root>/scripts/registry.py"
```

`<plugin-root>` is two directories up from this skill's base directory (the base
directory is announced when this skill loads).

Valid models: `haiku`, `sonnet`, `opus`, `fable`.

## No arguments: show the mapping

1. Run `registry.py` for the effective registry (agent, tier, capabilities).
2. Read the "Model overrides" section of `.claude/routing-overrides.md` if it exists.
3. Show one table: agent, default tier, override (or "-"), effective model. Nothing else.

## Arguments like `scout=sonnet sdd-planner:researcher=haiku`

1. Validate every pair: the agent must be a registry key (accept the bare name
   when it is unambiguous), the model must be one of the four. Invalid agent or
   model: reject with the valid options, change nothing.
2. Create or update the managed section in `.claude/routing-overrides.md`
   (create the file if missing, keep any unmanaged content above it intact):

   ```markdown
   ## Model overrides (managed by /model-router:models)

   | Agent | Model |
   |---|---|
   | scout | sonnet |

   When spawning an agent listed above, pass its listed model as the
   Agent tool's `model` parameter.
   ```

3. Merge with existing overrides: new pairs win, unmentioned pairs stay.
4. Setting an agent to its registry tier removes its row. Empty table: remove the
   whole section.
5. Confirm with the resulting mapping table and apply it immediately to any
   spawns later in this session.

## Argument `reset`

Remove the managed section. If the file is then empty, delete the file.
Show the default mapping.

## Downgrading read-heavy agents

Moving an agent *down* a tier is the cheapest lever the router has, and
read-heavy agents are the usual candidates: an agent whose job is to locate,
scan, or extract rarely needs more than haiku. Two cautions before suggesting it:

- **Downgrade the reading, not the reasoning.** An agent that gathers *and*
  synthesises (a researcher, a reviewer) loses real quality at haiku. Where the
  registry marks an agent `prefetch`, prefer prefetching with `scout`/`extractor`
  in the primary context and passing pointers in - that cuts the same tokens
  without cutting the synthesis.
- **Never prefetch for a `self_context` agent.** Those agents gather their own
  context by design; prefetching for them, or pre-reading their inputs, widens
  what they and the orchestrator can see and destroys the isolation that is the
  whole product. This is a correctness rule and it outranks any saving. Suggest
  the tier lever there, never the prefetch lever.
- **Never downgrade a review lane to save a tier either.** Intent-isolated
  reviewers earn their tier on judgement, not on volume.

When the user asks to make file reads cheaper, check the registry first: propose
prefetch for agents that allow it, the tier downgrade for agents that don't, and
say which one you are recommending and why.
