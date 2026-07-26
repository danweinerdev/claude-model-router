---
name: routing
description: Cost-optimised task routing. Use at the start of any coding, search, extraction, review, planning, or multi-step task to pick the cheapest execution strategy (deterministic tool, haiku worker, sonnet worker, main model, top-tier worker) and to handle escalation when a worker fails. Also triggers on mentions of cost, budget, routing, delegation, or which model to use.
---

# Model router

Route every sub-task to the cheapest strategy that can succeed. Priorities, in order: correctness, cost, latency, extensibility, simplicity.

The agent roster is **data, not code**: `router-config.json` declares every routable agent, its tier, and its capabilities. Profiles under `profiles/` register agents belonging to other plugins so they are first-class routing targets rather than something to map down. The SessionStart hook prints the effective registry; `python3 <plugin-root>/scripts/registry.py --routes` reprints it on demand.

## Step 0: overrides

If `.claude/routing-overrides.md` exists in the project, read it first. Its rules win over everything below.

If `.claude/router-config.json` exists, it has already been merged into the registry you were shown - don't re-read it to re-derive tiers.

## Step 1: tool first

Before any delegation: if a deterministic command solves the task (grep, rg, jq, yq, sed, awk, git, terraform, kubectl, helm, docker, a compiler, a test runner), run it. No LLM call. Reasoning models are for reasoning.

Step 1 covers **one-shot** commands only: you know the exact command and its output answers the question directly. The moment discovery turns iterative - a second search informed by the first, listing directories to decide what to read next, reading files to summarise them - it is no longer a tool call, it is a locate/extract task. Bright line: the third search/list/read operation on the same question means you are exploring inline; stop and hand the whole question to `scout` or `extractor`, including what you already learned. Every raw tool result you ingest is paid at main-loop rates; a haiku worker reads the same bytes at a fraction of the cost and returns a summary.

**Reading is haiku work.** This holds regardless of *what* is being read - source, logs, configs, or planning artifacts. The inline guard exempts planning-root paths from its budget so lifecycle skills are not denied mid-workflow, but exempt from the guard is not exempt from the policy: scanning a directory of specs, pulling frontmatter out of a set of phase files, or summarising a plan is still `extractor` work. The exemption exists so the guard does not break `/plan` and `/code-review`, not to make bulk artifact reading free.

## Step 1.5: sensitivity gate

Sensitivity is not a task-type signal, so it cannot live in the decision table below. Some data must never leave the main loop for a worker, however cheap the task looks. Decide this **before** tier selection, not by letting a worker fail into it.

If `.claude/router-sensitivity.json` exists in the project, it declares rules (content regexes and path globs) and, per rule, which workers may still receive matching data. The `guard_sensitive.py` hook enforces it at spawn: a matching Agent delegation to a non-allowed worker is blocked, and you handle that sub-task inline. The gate fails closed (a broken config blocks delegation), unlike the cost hooks which fail open. With no such file the gate is off. See `examples/router-sensitivity.example.json`.

This is an enforced default, not a substitute for judgement: for regulated data the human owns the final call on where it may go.

## Step 2: decision table

Decompose the request into sub-tasks. For each, match signals to the cheapest capable agent. These are the built-in workers; **any agent in the registry is routable at its declared tier**, and profile-supplied rows extend this table.

| Task signals | Required capabilities | Route |
|---|---|---|
| "where is X", "what uses Y", map directory, grep logs, locate artifacts | locate | `scout` (haiku) |
| pull fields from docs/logs/frontmatter, classify against given categories, summarise one file or diff, format conversion | extract | `extractor` (haiku) |
| rename, boilerplate, apply known pattern, config value change, test scaffold from example, with a complete spec | mechanical-edit | `mechanic` (sonnet) |
| implement one scoped task from an approved plan, write tests from given cases, fix a simple reproduced bug | implement-from-plan | `builder` (sonnet) |
| design, debugging, ambiguous requirements, trade-offs, anything regulated or risky | reasoning | main loop (you) |
| task exceeds the main loop's own tier, or top-tier work needs an isolated fresh context (parallel deep reviews, synthesis over merged summaries) | deep-reasoning | `sage` (top tier) |

`sage` is never a routing default. If the main loop already runs the top tier, use `sage` only for context isolation, not capability. The expensive guard blocks it unless the spawn carries the escalation marker.

A project can override that per agent: an entry with `"escalation_only": false` in `.claude/router-config.json` is a deliberate statement that the agent is a routing target in its own right, and the guard allows it. If a registered agent is opted in this way, route to it directly when the decision table sends you there; do not treat it as an escalation and do not prefix its prompt with the escalation marker, which would misreport why it ran. Never reach for `MODEL_ROUTER_ALLOW_EXPENSIVE=1` to work around a single blocked spawn: it disables the guard for the whole session.

**Match on capability, not on name.** When a sub-task's required capability appears in a registered agent's `capabilities`, that agent is the route - a specialist at the right tier beats a generic worker at the same tier, because it needs less prompting to get there.

**Generic agents are never routing targets.** `Explore`, `general-purpose`, `claude`, and `Plan` are reasoning-tier and bill at main-loop rates - never spawn them for locate, extract, or summarise work, no matter how broad the fan-out. Map them down: locate/map -> `scout`; extract/summarise/classify -> `extractor`; mechanical edits -> `mechanic`/`builder`; reasoning stays in the main loop. A bare `Agent` call with no `subagent_type` defaults to `general-purpose` (expensive) - always name a registered agent explicitly. The `guard_expensive.py` hook blocks these at spawn time; `MODEL_ROUTER_ALLOW_EXPENSIVE=1` is the deliberate, per-session override for when a task genuinely needs main-loop breadth.

**Registered agents from other plugins are not generic agents.** A profile entry such as `sdd-planner:quality-scanner` (sonnet) is a specialist declared at a known tier; route to it directly. Downgrading it to `extractor` to save a tier destroys the thing you were paying for.

### Prefetch before dispatch

Subagents cannot spawn subagents. So when a registered specialist carries a `prefetch` field, do the locating in the primary context first - dispatch `scout`, then pass the resulting `file:line` pointers into the specialist's prompt. A sonnet or opus specialist hunting for files is paying reasoning rates for grep, and it is the one delegation the specialist cannot make for itself.

**Prefetch only where the registry says so.** `prefetch` is opt-in per agent, not a general licence. Absent the field, dispatch the specialist with the task and let it gather.

### Never prefetch for a self-context agent

Some agents gather their own context **by design**, and cheapening that gathering breaks them. The registry marks these `self_context: true`. For such an agent:

- Never dispatch `scout`/`extractor` on its behalf, and never inject gathered material into its prompt beyond the inputs its own contract names.
- Never read the material yourself "to help it along" - for a reviewer whose findings you will later synthesise, what *you* have read is part of the design. Reading the diff, plan or spec before synthesis contaminates the synthesis.
- Give it its declared inputs (pointers, scopes, ranges) and nothing more.

This is a **correctness rule, not a cost rule**, and it outranks every saving in this document. The value of an isolated agent is what it was *not* shown; a prefetch that saves tokens by widening its inputs has bought nothing and destroyed the reason the agent exists. When cost and isolation conflict, isolation wins.

The clearest case is a panel of intent-isolated reviewers: each lane is deliberately given a different, partial view, and the orchestrator deliberately holds none of it. Fanning `scout` out ahead of them, or pre-reading their inputs to "save" them the work, collapses several independent perspectives into one - and it collapses them *silently*, since the output still looks like a multi-lane review.

### Without profiles

The five built-in workers are the whole ladder when no profile is enabled: `scout`/`extractor` (haiku), `mechanic`/`builder` (sonnet), you, then `sage`. Nothing about the protocol depends on another plugin being installed.

Profiles add specialists; they never replace this ladder. So an escalation link pointing into a profile that is not enabled simply falls back to retrying the same built-in worker at a higher tier, and routing degrades to the built-ins rather than failing. Never dispatch an agent that is not in the registry you were shown on the assumption that some plugin provides it.

## Never delegate

Security-sensitive changes, destructive operations, ambiguous requirements, anything needing user judgement. These stay in the main loop, always.

## Delegation rules

- Delegate only self-contained sub-tasks the prompt can fully specify. If specifying takes longer than doing: do it inline.
- Context handoff: pass pointers (`path:line` ranges, commit SHAs, URLs), never pasted file content. Pasting is billed as main-loop output tokens (top tier ~$50/MTok, and generating them takes wall-clock time); a worker reads the same bytes as haiku input (~$1/MTok) in one round trip. Paste only what the worker cannot retrieve itself - text that exists solely in the conversation (user message, prior tool output, fetched page) - or trivially small snippets (<~200 tokens).
- Batch independent delegations in one message so they run in parallel. Large fan-outs (e.g. review 50 modules): fan out `scout`/`extractor` workers, merge their summaries, do one final reasoning pass yourself.
- Workers end with a footer (`RESULT:` / `CHECKS-RUN:` / `UNCERTAINTIES:` / `ESCALATE:`). A worker reporting ambiguity: resolve it yourself; never re-prompt the worker to guess. Agents from other plugins have their own report formats - judge those on their own terms.

## Escalation protocol (verification first)

1. A deterministic check exists for the worker's output (tests, compiler, schema validation, diff applies, `terraform validate`): run it. Pass = done. Fail = re-dispatch one tier **up**, maximum one retry, prefixing the retry prompt with `[router-escalation from <agent>]` and including the failed attempt's footer. Pick the target in this order:
   - the failed agent's own `escalates_to`, **if that agent is in the registry**. This is a declared, more capable specialist and it always names a higher tier. If the entry names an agent the registry does not have (its profile is not enabled, or its plugin is not installed), treat the link as absent and fall through.
   - otherwise **re-dispatch the same agent one tier up**, by passing the higher tier as the Agent tool's `model` parameter. Same agent, same prompt, more capable model.
   - otherwise take over yourself.

**Escalation only ever moves up, and it keeps the specialisation.** Two ways to get this wrong, both of which silently produce a worse attempt than the one that just failed:

- Reading an escalation link backwards. `escalates_to` points up; `escalates_from` is the reverse view, recording which cheaper agent arrives here. Never re-dispatch to an agent's `escalates_from` after a failure - that is the rung you already tried.
- Swapping the agent when you only needed a better model. Re-dispatching the same agent at a higher tier is the default escalation, because it changes exactly one thing: capability. Reach for a different agent only when the registry declares one, or when the failure was a capability the agent does not have at any tier.
- Dropping the specialist for a generic worker of the same tier. If a specialised agent fails, the next rung is a **more capable agent that can still do that job**, not a general one that happens to cost more. A specialised implementation task escalates to a more capable implementer; it never falls back to a cheaper or more generic implementer, and it never escalates into a read-only agent that cannot perform the task at all.
2. No deterministic check: spot-read the result yourself. You receive it anyway; judging it costs almost nothing.
3. The worker's `ESCALATE: yes` is advisory input to rules 1 and 2, never the sole trigger.
4. If the task still exceeds your own tier after you take over: hand it to `sage` with the full failure history, prefixed `[router-escalation from main]`. One attempt, final.

   `sage` is read-only, so it is the ceiling for *reasoning*, not a ceiling that can finish work. For a task that has to produce changes, `sage` diagnoses and you apply the result, or you re-dispatch the specialist one more time with `sage`'s analysis in the prompt. Never treat handing a task to `sage` as having completed it.
5. Never start at an expensive tier unless the decision table sends you there.

The `[router-escalation ...]` marker is load-bearing twice over: `guard_expensive.py` allows escalation-ceiling spawns that carry it, and `log_metrics.py` counts escalations by it. Emit it on every escalation, never on a first dispatch.
