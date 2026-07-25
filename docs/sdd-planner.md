# Using model-router with sdd-planner

[sdd-planner](https://github.com/danweinerdev/ai-agentic-sdd-planner) is a spec-driven development plugin: lifecycle skills (`/research` → `/specify` → `/design` → `/plan` → `/implement` → `/code-review` → `/debrief`) plus eight agents of its own. Run naively alongside the router, the two fight - the router maps sdd-planner's specialists down to cheap workers, and the inline guard denies lifecycle skills mid-workflow.

The shipped profile resolves both.

## Enable

```
/model-router:profiles enable sdd-planner
```

which writes into the project's `.claude/router-config.json`:

```json
{ "profiles": ["sdd-planner"] }
```

Verify with `python3 <plugin-root>/scripts/registry.py --routes`.

## What the profile changes

**1. sdd-planner's agents become routing targets at their real tiers.**

| Agent | Tier | Routes for |
|---|---|---|
| `sdd-planner:spec-reviewer` | haiku | spec testability, completeness, ambiguity |
| `sdd-planner:researcher` | sonnet | compound context gathering |
| `sdd-planner:plan-reviewer` | sonnet | plan completeness, feasibility, conventions |
| `sdd-planner:drift-detector` | sonnet | review lane - diff + plan |
| `sdd-planner:quality-scanner` | sonnet | review lane - diff + code, intent-blind |
| `sdd-planner:spec-compliance` | sonnet | review lane - diff + specs/designs |
| `sdd-planner:blind-spot-finder` | sonnet | review lane - diff only |
| `sdd-planner:code-implementer` | opus | escalation target for plan tasks |

Without this, the router's "generic agents are never routing targets" rule has no way to tell a specialist from a generic reasoning agent, and the cheapest-tier instinct pushes review work onto `extractor`. That is exactly the wrong trade: the review lanes earn their tier on judgement, not on volume.

**2. Plan tasks start at sonnet, not opus.** `builder` (sonnet) takes a scoped plan task first. `sdd-planner:code-implementer` (opus) is registered `escalates_from: builder` - reached after the task's acceptance checks verifiably fail, prefixed `[router-escalation from builder]`. For mechanical tasks this is most of the saving; for genuinely hard ones you pay one cheap attempt on the way to the right tier.

If a plan task is known to be high-risk, route straight to `code-implementer` - the decision table permits starting high when the task type demands it. The escalation ladder is a default, not a rule against judgement.

**3. The four review lanes are never merged or downgraded.** `/sdd-planner:code-review` dispatches `drift-detector`, `quality-scanner`, `spec-compliance` and `blind-spot-finder` in parallel, each with a deliberately different input bundle. Their value is intent isolation. Collapsing them into one cheaper reviewer produces a single-pass review cosplaying as a four-lane one - the profile's routes say so explicitly so the router doesn't "optimise" it.

**4. Planning-root reads stop tripping the inline guard.** See below.

## The planning-root exemption

The router's `PreToolUse` inline guard denies main-loop `Read`/`Grep`/`Glob`/searchy-`Bash` past a budget (default 5 per prompt), on the theory that iterative discovery belongs in a haiku worker. sdd-planner's lifecycle skills legitimately read many artifacts in the primary context - `/plan` walks phase files, `/code-review` resolves plan metadata, `/excavate` sweeps the tree.

So `guard_inline.py` resolves the planning root exactly as sdd-planner does (`shared/path-resolution.md`: walk up for `planning-config.json`, resolve its `planningRoot`), and operations confined to it are not counted. The default `.plans` root works in every written form (`.plans`, `./.plans`, or an absolute path). With no `planning-config.json` anywhere, the conventional directory names (`.plans/`, `Plans/`, `Specs/`, `Designs/`, `Research/`, `Brainstorm/`, `Decisions/`) are exempt instead.

Detection covers where each tool actually puts its path. `Read` uses `file_path`, `Grep` uses `path`, and `Bash` has its path arguments parsed out of the command. `Glob` is the awkward one: it carries the directory inside `pattern`, and `Grep`'s `glob` filter does the same, so the guard takes the pattern's literal prefix (`.plans/**/*.md` gives `.plans`). A pattern with no literal prefix, such as `**/*.py`, could match anywhere and stays counted. `Grep`'s own `pattern` is a regex and is never read as a path, so a regex that happens to look like `.plans/Plans` buys no exemption for a `src` scan.

The exemption is deliberately narrow:

- An operation touching **anything** outside the planning root is counted as normal - `grep -r foo .plans/Plans src` does not launder a code sweep through it.
- An operation naming **no path at all** (a bare `ls`, a `Grep` with no `path`) is not exempt; unscoped exploration is what the budget exists to catch.
- Turn it off with `{"guard": {"exempt_planning_root": false}}` in `.claude/router-config.json`.

**Exempt from the guard is not exempt from the policy.** Reading is haiku work regardless of what is being read. Scanning a directory of specs, pulling frontmatter out of a set of phase files, or summarising a plan is `extractor` work; locating which plan covers a feature is `scout` work. The exemption exists so the guard doesn't break `/plan` mid-run, not to make bulk artifact reading free. The profile's first two route rows say this, and the `UserPromptSubmit` reminder repeats it every turn.

## Prefetch - and the lanes it must never touch

Subagents cannot spawn subagents. An sdd-planner agent that has to *find* its inputs is a sonnet model paying reasoning rates for grep, and it is the one delegation it cannot make for itself. Where that applies, do the locating in the primary context and hand over pointers:

```
1. scout      → "which phase file defines the auth tasks?"                 (haiku)
2. extractor  → "pull tasks[] frontmatter from .plans/Plans/Auth/02-*.md"  (haiku)
3. sdd-planner:researcher  ← gets file:line pointers + the extracted tasks
```

`sdd-planner:researcher` carries `prefetch: ["scout", "extractor"]` in the profile for exactly this. **`prefetch` is opt-in per agent** - an agent without the field gets dispatched with the task and gathers for itself.

### The four review lanes are the exception, and it is not negotiable

`drift-detector`, `quality-scanner`, `spec-compliance` and `blind-spot-finder` are marked `self_context: true`. **Never prefetch for them. Never pre-read their inputs.**

They were built to find their own diffs, plans and specs precisely so that the primary context holds none of it. Two distinct things break if the router "helps":

1. **The orchestrator gets contaminated.** `/code-review`'s hard contract is that the primary context resolves dispatch metadata - plan path, phase doc path, diff range - and does *not* read plan bodies, spec bodies, design bodies, or diff contents. Its job afterwards is to synthesise four independently-formed reports, including noticing where they disagree. A synthesiser that has already read the diff is no longer weighing four views; it is grading them against its own.
2. **The lane's isolation is breached.** Each lane's value is what it was *not* shown. Injecting prefetched material into `blind-spot-finder` - whose entire guarantee is diff-and-nothing-else - makes it a differently-prompted `quality-scanner`. You still get four reports. They just stop being four perspectives.

The failure is silent, which is what makes it worth a hook-level flag rather than a footnote: the output still has four sections and still reads like a four-lane review.

So for `/sdd-planner:code-review` the router's entire job is **to stay out of the way**: dispatch the four in one message, let each gather, and don't route anything into them. Metadata resolution is `/code-review`'s own step 1, not a routing decision - the router does not insert `scout` into it.

The same rule generalises beyond sdd-planner: any agent whose product is an isolated perspective gets `self_context: true`, and the routing skill treats it as a correctness rule outranking every cost rule in the policy.

## Making reads cheaper, in order of preference

1. **Prefetch with `scout`/`extractor`** - but only for agents the registry marks `prefetch`. Cuts tokens without cutting synthesis.
2. **Downgrade genuinely read-only agents.** `/model-router:models sdd-planner:spec-reviewer=haiku` - already haiku by default. Agents that gather *and* synthesise lose real quality at haiku; prefer (1) where it is allowed.
3. **For `self_context` agents, neither.** No prefetch (correctness), no tier downgrade (judgement is what you are buying). The review lanes cost what they cost; the saving comes from everything *around* them.

## Hook coexistence

Both plugins register `PreToolUse` hooks and they do not conflict:

| Hook | Owner | Scope |
|---|---|---|
| `guard_inline.py` | model-router | main loop only - returns 0 immediately for any subagent |
| `guard_expensive.py` | model-router | `Agent` spawns only |
| `guard_sensitive.py` | model-router | `Agent` spawns only |
| `reviewer-bash-guard.py` | sdd-planner | `Bash` from its seven read-only agents only; fails open elsewhere |

sdd-planner's guard applies to its reviewers' Bash calls; the router's inline guard explicitly never throttles subagents. The one overlap - a `Bash` call from the main session - is seen by both, and sdd-planner's fails open for the main session by design. Either hook denying blocks the call, so a denial message always names which plugin issued it.

Both `SessionStart` hooks inject context (the decision ledger, the routing policy plus registry). They are additive.

## Caveats

- The profile registers the plugin's **built-in** agents. Project-supplied review lanes (`.claude/agents/*-reviewer.md` with `reviewLane: true`) are discovered and dispatched by sdd-planner itself; register them in `.claude/router-config.json` if you want them tiered.
- Tiers in the profile are read from sdd-planner's agent frontmatter as of the version this was written against. If the plugin retiers an agent, update the profile - the router has no way to detect the drift, and `registry.py` will keep reporting the stale tier.
- The escalation ladder for plan tasks changes which agent writes your code. If your plans assume `code-implementer` throughout, either flag tasks as high-risk or drop the `builder` row from the profile's routes.
