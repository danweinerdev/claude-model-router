# Extending model-router

The router routes on capabilities, not model names, and its roster is data. There are three extension points, in increasing order of scope:

1. **Register an existing agent** - make an agent that already exists (another plugin's, or your project's) a routing target. No new files.
2. **Add a worker** - a new agent the router owns.
3. **Write a profile** - register a whole plugin's agent set, shipped with the router.

## 1. Register an existing agent

The registry is the whole mechanism. Add the agent to `agents` in your project's `.claude/router-config.json`:

```json
{
  "agents": {
    "my-plugin:sql-reviewer": {
      "tier": "sonnet",
      "capabilities": ["review-lane", "review-sql-migration"],
      "read_only": true
    }
  }
}
```

That is enough for the router to treat it as a first-class target at that tier instead of mapping it down to a generic worker. `/model-router:profiles add my-plugin:sql-reviewer tier=sonnet caps=review-sql-migration` does the same thing interactively.

Get the tier from the target agent's own `model:` frontmatter. Guessing it wrong makes the router either overpay or hand work to an agent that cannot do it.

Entry fields, all optional except `tier`:

| Field | Meaning |
|---|---|
| `tier` | required; one of the configured `tiers` |
| `capabilities` | task signals that should route here - this is what matching keys on |
| `description` | one line, shown in the registry table |
| `read_only` | the agent never writes; informational |
| `escalation_only` | never a routing default; `guard_expensive.py` blocks unmarked spawns |
| `escalates_from` | the cheaper agent that escalates into this one |
| `prefetch` | agents that should gather pointers before this one is dispatched |
| `self_context` | this agent gathers its own context by design - the router must never prefetch for it, never inject gathered material, and never pre-read its inputs |
| `lane` | review-lane identity, for intent-isolated reviewers |

`self_context` and `prefetch` are mutually exclusive, and `self_context` is the stronger claim. Set it on any agent whose product is an *isolated perspective* rather than an answer - adversarial reviewers, independent judges, anything you dispatch several of specifically to get views that were formed without seeing each other's inputs.

Getting this wrong fails silently: prefetching for such an agent still returns a well-formed report, it just returns one that no longer carries the independence you dispatched it for. The routing skill treats `self_context` as a correctness rule that outranks every cost rule in the policy, so mark it whenever you are unsure - the cost of a needless flag is a few tokens, the cost of a missing one is a review that lies about what it is.

### Tiers and the escalation ceiling

`tiers` lists the tier names a registry entry may use. Order is for display
only; nothing behavioural depends on it.

The escalation ceiling is named explicitly by `ceiling`, and agents on that
tier are blocked unless their spawn carries the `[router-escalation ...]`
marker:

```json
{
  "tiers": ["local", "haiku", "sonnet", "opus", "fable"],
  "ceiling": "fable"
}
```

This is worth stating because the ceiling used to be positional (`tiers[-1]`),
which meant appending a cheap tier for a local model silently promoted it to
the ceiling and blocked every agent on it. If `ceiling` names a tier that is
not in `tiers`, the last tier is used as a fallback.

Prefer marking an agent `escalation_only` over relying on its tier. The flag
says what you mean, and it survives a later edit to `tiers` or `ceiling`.

## 2. Add a worker

Create `agents/<name>.md`. The `description` frontmatter advertises capabilities; the router matches task signals against it. Keep the body short and end with the worker footer contract, verbatim:

```markdown
---
name: summariser
description: Batch summarisation across many sources. Capabilities - multi-doc-summarise, merge-summaries. Use when many documents need independent summaries merged into one. Inputs must be fully provided.
tools: Read, Grep, Glob
model: haiku
---

You are summariser, the router's batch summarisation worker.

Rules:
- Summarise only material provided or explicitly pointed to.
- One summary per source, then a merged overview if asked.
- If interpretation or judgement is needed: stop, set ESCALATE: yes, name the ambiguity.

End every reply with exactly this footer:

RESULT: <one line>
CHECKS-RUN: <commands run and outcomes, or "none">
UNCERTAINTIES: <or "none">
ESCALATE: yes|no - <reason>
```

`model` accepts `haiku`, `sonnet`, `opus`, `fable`, `inherit`, or a full model ID.

Then add it to `router-config.json` under `agents` (the frontmatter defines the agent; the registry makes it routable), and add one row to the decision table in `skills/routing/SKILL.md`:

```markdown
| summarise many documents and merge the results | multi-doc-summarise | `summariser` (haiku) |
```

`tests/test_router_config.py` asserts every unqualified registry entry has a matching `agents/<name>.md`, so a registry entry without a file fails the suite.

## 3. Write a profile

A profile registers another plugin's agents and ships with the router, so any project can enable it with one line. Copy `profiles/sdd-planner.json` as the shape reference:

```json
{
  "name": "my-plugin",
  "description": "What this makes routable, in one line.",
  "agents": { "my-plugin:reviewer": { "tier": "sonnet", "capabilities": ["review-lane"] } },
  "routes": [
    { "signals": "reviewing a SQL migration", "route": "my-plugin:reviewer" }
  ],
  "notes": ["Anything the router should know that isn't a tier."]
}
```

Enable it per project with `/model-router:profiles enable my-plugin`, which writes `{"profiles": ["my-plugin"]}` into `.claude/router-config.json`. Profiles are inert until enabled, so shipping one costs nothing to projects that don't use the target plugin.

`routes` rows are appended to the decision table and printed by `scripts/registry.py --routes`. Use them when the tier alone doesn't say *when* to pick the agent.

`tests/test_router_config.py::test_shipped_profiles_are_wellformed` validates every profile in `profiles/`.

## 4. Optionally: price entry

If a new agent uses a model family not in `scripts/stats.py` `PRICES`, add a `(substring, (input, output))` entry so `/model-router:router-stats` prices it correctly. Unknown models are priced at the baseline (top tier), which overstates their cost.

## 5. Test

`tests/test_agents.py` checks frontmatter and the footer contract - add your agent to `EXPECTED` there. `tests/test_router_config.py` checks the registry and profiles.

That is the whole extension surface. If you find yourself wanting to edit routing logic to add an agent, the design has failed; open an issue instead.
