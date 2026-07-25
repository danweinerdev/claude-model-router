# model-router

A generic model router for [Claude Code](https://code.claude.com). It teaches the main loop to send every sub-task to the cheapest execution strategy that can succeed, and to escalate only on verified failure:

```
deterministic tool → haiku worker → sonnet worker → main model → top tier (escalation ceiling)
```

The expensive reasoning model plans and judges; commodity work (locating files, extracting data, mechanical edits) runs on cheap tiers. No framework, no runtime, no API keys - the router is a plugin made of a routing skill, five worker agents, four hooks, and a config file. The harness does the rest.

What makes it *generic*: **the agent roster is data.** `router-config.json` declares which agents exist, what tier each runs at, and what they are good for. Profiles register agents belonging to *other* plugins, so a specialist from another plugin is a first-class routing target rather than something the router flattens into a cheap worker.

Derived from [frugal](https://github.com/ThomasLangbroek/frugal) by Thomas Langbroek (MIT) - see [NOTICE](NOTICE).

## Install

```
/plugin marketplace add danweinerdev/claude-plugins
/plugin install model-router@claude-plugins
```

This repo is the plugin itself; it is listed by the [claude-plugins](https://github.com/danweinerdev/claude-plugins) marketplace, which also carries the `sdd-planner` plugin the interop profile below targets.

## How it works

The main model already reads every request, so it acts as the router at zero marginal cost. The routing skill gives it one decision table:

| Task | Agent | Model |
|---|---|---|
| locate, grep, map structure, find usages | `scout` | Haiku |
| extract, classify, summarise one source | `extractor` | Haiku |
| mechanical edits from a complete spec | `mechanic` | Sonnet |
| implement one scoped task from an approved plan | `builder` | Sonnet |
| design, debugging, ambiguity, risk | main loop | whatever you run |
| beyond the main loop's tier, or isolated deep reviews | `sage` | top tier |

Plus a tool-first rule: if grep, jq, git, terraform or any deterministic command solves the task, no model is called at all.

Any agent in the registry - including profile-registered ones from other plugins - extends that table at its declared tier.

### Escalation (verification first)

Workers do not self-grade their way up the ladder. Every worker ends with a fixed footer:

```
RESULT: <one line>
CHECKS-RUN: <commands run and outcomes, or "none">
UNCERTAINTIES: <or "none">
ESCALATE: yes|no - <reason>
```

The router then applies four rules:

1. If a deterministic check exists (tests, compiler, schema validation, `terraform validate`), run it. Pass = done. Fail = escalate one tier, maximum one retry, then the main loop takes over.
2. No check available: the main model spot-reads the result. It receives it anyway, so judging it costs almost nothing.
3. The worker's `ESCALATE: yes` is advisory input, never the sole trigger. Self-reported confidence from a cheap model is poorly calibrated; observable failure is not.
4. Never start at an expensive tier unless the decision table requires it. `sage` is reached only via high-risk table rows or after escalation exhausts - one attempt, final.

Escalations are marked `[router-escalation from <agent>]`. That marker is load-bearing twice: `guard_expensive.py` admits escalation-ceiling spawns that carry it, and `log_metrics.py` counts escalations by it.

## The registry

`router-config.json` is the roster:

```json
{
  "tiers": ["haiku", "sonnet", "opus", "fable"],
  "ceiling": "fable",
  "agents": {
    "scout": { "tier": "haiku", "capabilities": ["locate", "map-structure"] }
  },
  "generic_agents": ["general-purpose", "Explore", "Plan", "claude"],
  "guard": { "inline_budget": 5, "exempt_planning_root": true },
  "profiles": []
}
```

Resolution order, later wins, with `agents` and `guard` merged key-by-key so a project can retier one agent without restating the registry:

1. `<plugin-root>/router-config.json` - shipped defaults
2. every enabled profile, from `<plugin-root>/profiles/<name>.json`
3. `<cwd>/.claude/router-config.json` - per-project overrides
4. `$MODEL_ROUTER_CONFIG` - explicit override

A missing or malformed config yields built-in defaults rather than an exception. These hooks sit in front of every tool call; a config typo must never wedge a session.

`python3 <plugin-root>/scripts/registry.py --routes` prints the effective registry. The `SessionStart` hook injects it, so the main loop knows what it can route to before it routes anything.

## Interop profiles

A profile makes another plugin's agents routable at their real tiers. Bundled: **sdd-planner**.

```
/model-router:profiles                    # what's available, what's active
/model-router:profiles enable sdd-planner
```

Without it, the router's "generic agents are never routing targets" rule can't distinguish a specialist from a generic reasoning agent, and the cheapest-tier instinct pushes review work onto `extractor` - exactly the wrong trade. With it, `sdd-planner:quality-scanner` routes at sonnet, plan tasks start at `builder` (sonnet) and escalate to `sdd-planner:code-implementer` (opus) only on verified failure, and lifecycle skills stop tripping the inline guard.

**Isolation outranks cost.** An agent marked `self_context: true` gathers its own context by design, and the router is forbidden from prefetching for it, injecting gathered material into it, or pre-reading its inputs. sdd-planner's four review lanes are the canonical case: each is deliberately shown a different partial view, and the orchestrator deliberately holds none of it before synthesising their reports. "Helping" them with a cheap `scout` pass collapses four independent perspectives into one - and does it silently, since the output still looks like a four-lane review. The routing skill treats this as a correctness rule that beats every saving in the policy.

See [docs/sdd-planner.md](docs/sdd-planner.md) for the full integration, and [docs/extending.md](docs/extending.md) to register your own.

## Enforcement

Routing policy in a skill is advisory: the model follows it well, but a prompt cannot *forcibly* prevent anything. The router therefore enforces on three levels:

1. **Policy injection.** A `SessionStart` hook puts the routing policy and the registry in context at every session start; a `UserPromptSubmit` hook re-pins a one-line reminder on every prompt. No drift, nothing to invoke manually.
2. **Inline-exploration budget.** A `PreToolUse` guard counts search-type tool calls (Read, Grep, Glob, search-y Bash) in the main loop. Past the budget (default 5 per prompt) further ones are denied with a pointer to the cheap workers. The budget resets on any foreground delegation or new prompt; worker agents are never throttled. Non-search commands (git, test runners, builds) are never blocked.
3. **Expensive-tier guard.** `guard_expensive.py` blocks generic reasoning agents (`general-purpose`, `Explore`, `Plan`, `claude`, and bare `Agent` calls, which resolve to `general-purpose`) and escalation-ceiling agents spawned as a routing default. Registered agents pass; unregistered ones pass too - this stops expensive defaults, it is not an allowlist for every plugin you have installed.

Judgement lives in prompts; enforcement lives in hooks.

### The planning-root exemption

Spec-driven lifecycle skills legitimately read many artifacts in the primary context, so operations confined to a **planning root** are not counted against the inline budget. The root is resolved the way sdd-planner resolves it (walk up for `planning-config.json`, resolve its `planningRoot`); with no config file, the conventional artifact directories (`Plans/`, `Specs/`, `Designs/`, …) are exempt instead.

It is narrow on purpose: an operation touching anything outside the root is counted normally, so `grep -r foo .plans/Plans src` cannot launder a code sweep; and an operation naming no path at all is never exempt, because unscoped exploration is what the budget is for.

Exempt from the guard is not exempt from the policy - reading is haiku work regardless of *what* is being read. The exemption stops the guard breaking `/plan` mid-run; it does not make bulk artifact reading free.

### Sensitivity gate

Some data must never be delegated however cheap the task looks. If `.claude/router-sensitivity.json` exists, it declares content regexes and path globs plus which workers may still receive matches; `guard_sensitive.py` blocks non-allowed spawns. It **fails closed** - the deliberate exception to the fail-open cost hooks, because a false negative leaks data while a false positive only makes you do the work inline. No config file means the gate is off. See [examples/router-sensitivity.example.json](examples/router-sensitivity.example.json).

## Metrics and the cost report

A `SubagentStop` hook logs one jsonl line per worker run (agent, declared tier, model, token usage, escalation flag) to `~/.claude/model-router/metrics.jsonl`. Run:

```
/model-router:router-stats
```

for cost per tier, escalation rate, and estimated savings versus running the same work on your session's actual main-loop model. Prices live in `scripts/stats.py` (`PRICES`); update them when Anthropic pricing changes. Learning is deliberately offline: read the report, edit the registry.

## Knobs

| Knob | Default | Effect |
|---|---|---|
| `MODEL_ROUTER_INLINE_BUDGET` | `5` | Inline search ops allowed per prompt before the guard denies |
| `MODEL_ROUTER_ALLOW_INLINE=1` | unset | Disables the inline-exploration guard for the session |
| `MODEL_ROUTER_ALLOW_EXPENSIVE=1` | unset | Allows generic and escalation-ceiling agent spawns |
| `MODEL_ROUTER_METRICS_PATH` | `~/.claude/model-router/metrics.jsonl` | Where worker-run metrics are written |
| `MODEL_ROUTER_CONFIG` | unset | Explicit config path, wins over project and shipped config |
| `MODEL_ROUTER_SENSITIVITY_CONFIG` | `.claude/router-sensitivity.json` | Sensitivity gate rules |
| `/model-router:models` | registry tiers | Per-project model overrides, e.g. `scout=sonnet` |
| `/model-router:profiles` | none active | Enable/disable interop profiles, register single agents |
| `.claude/router-config.json` | none | Per-project registry, tiers, ceiling, and guard settings |
| `escalation_only: false` (per agent) | absent | Opts one agent in as a routing target even on the ceiling tier, without disabling the guard for anything else |
| `ceiling` (config) | `fable` | Which tier is the escalation ceiling. Named, not positional, so adding a cheap tier for a local model cannot turn it into the ceiling |
| `.claude/routing-overrides.md` | none | Per-project routing rules; read first, always win |

Too aggressive? `MODEL_ROUTER_ALLOW_INLINE=1` turns the hard guard off while keeping the advisory policy. Want it gone entirely? `/plugin uninstall model-router` - the router keeps no state outside the metrics file.

## Statusline segment (optional)

```
/model-router:setup-statusline
```

Adds a `router $0.03/$1.20 saved` badge (session/lifetime) to your statusline: it creates a minimal statusline if you have none, or merges the segment into your existing one (with your consent, smallest possible edit). A plugin cannot configure `statusLine` automatically - that field is user-owned - so this one-time command is as close as it gets. It prints nothing when no metrics exist yet.

## Commands

| Command | Purpose |
|---|---|
| `/model-router:models` | Show or change the per-project model mapping |
| `/model-router:profiles` | List, enable, disable interop profiles; register single agents |
| `/model-router:router-stats` | Cost per tier, escalation rate, savings |
| `/model-router:setup-statusline` | Add the savings badge to your statusline |

## Evaluating routing quality

No synthetic eval harness: headless scenario evals proved flaky (other plugins' skills win trigger races, model nondeterminism) while measuring little. Evaluate with real usage instead - work normally for a few days, then run `/model-router:router-stats` and read delegation rate, tier mix, and escalation rate. High escalations on one agent means its registry entry is tiered too low; near-zero savings means work is not being delegated.

## Privacy

Metrics are agent names, tiers, model ids, token counts and an escalation flag - one local jsonl line per worker run. No prompt content, no file paths from your projects, no telemetry, nothing leaves your machine. Delete the file at any time; the report starts over.

## Honest trade-offs

- **Advisory unless the guard hooks are enabled.** The skill steers routing; only the hooks enforce it.
- **Profile tiers are a snapshot.** A profile records another plugin's tiers as of when it was written. If that plugin retiers an agent, the profile goes stale silently - there is no detection.
- **Claude Code only.** The router leans on the harness (Agent tool, hooks, parallel delegation). Multi-provider is a documented recipe, not a tested code path - see [docs/litellm-recipe.md](docs/litellm-recipe.md).
- **Metrics are limited** to what hook events and transcripts expose. Escalations are detected via the prompt marker, so escalations performed without it are not counted.
- **The cost claim is about the delegated portion.** Delegated work costs far less than the same work on the top tier, but design, debugging and review stay on the expensive model on purpose. What the router removes is paying reasoning rates for grep. Every install measures itself locally, so nobody has to take this README's word for it.

## Development

```
make test          # full gate: byte-compile, pytest, hook smoke tests
make venv          # just the virtualenv
make clean-venv
```

`make test` is the release gate, not only the unit suite. Ordered cheapest-first:

| Stage | Checks |
|---|---|
| `compile` | every hook and script byte-compiles - a hook with a syntax error is worse than a missing one, since it fires on every tool call |
| `pytest` | the suite: manifest and JSON validity, registry and profile shape, guard behaviour, planning-root exemption, and the routing invariants (`self_context`, escalation marker, no stale upstream identifiers) |
| `smoke` | every hook the manifest actually wires up, exercised as the harness does it - referenced files exist, unusable input fails open, odd-but-valid input decides cleanly (exit 0 or 2, never a traceback), and `SessionStart` really emits the policy and registry |

The smoke stage covers the *manifest* rather than individual hooks, so a newly wired hook can't skip the fail-open contract just by having no unit test.

### Releasing

```
make bump-patch    # or bump-minor / bump-major
```

Each runs the full gate first, then bumps `.claude-plugin/plugin.json`, commits `vX.Y.Z`, and tags it. A failing gate, a dirty `plugin.json`, or a non-git directory aborts before anything is written.

## Licence

MIT - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
