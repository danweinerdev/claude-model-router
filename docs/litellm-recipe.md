# Multi-provider routing via LiteLLM proxy

Model router itself is provider-agnostic in design but Anthropic-only in v1: agents select model *tiers* (`haiku`, `sonnet`, `fable`) via frontmatter. To back those tiers with other providers, put a [LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy) in front of Claude Code. Claude Code speaks the Anthropic API to whatever `ANTHROPIC_BASE_URL` points at, and LiteLLM translates.

**Caveat: this path is documented, not tested in CI.** Behaviour of non-Claude models inside Claude Code's harness varies; agentic tool use quality differs widely between models.

## litellm-config.yaml

Map the model names Claude Code sends to any provider's models:

```yaml
model_list:
  # cheap tier -> a non-Anthropic budget model
  - model_name: claude-haiku-4-5
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

  # mid tier -> keep on Anthropic
  - model_name: claude-sonnet-5
    litellm_params:
      model: anthropic/claude-sonnet-5
      api_key: os.environ/ANTHROPIC_API_KEY

  # top tier -> keep on Anthropic
  - model_name: claude-fable-5
    litellm_params:
      model: anthropic/claude-fable-5
      api_key: os.environ/ANTHROPIC_API_KEY

litellm_settings:
  drop_params: true
```

## Run it

```bash
pip install 'litellm[proxy]'
litellm --config litellm-config.yaml --port 4000
```

## Point Claude Code at it

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_AUTH_TOKEN=<litellm master key, if set>
claude
```

Model router's routing is unchanged: `scout` still asks for the haiku tier; LiteLLM decides what actually serves it. Cost reporting in `scripts/stats.py` prices by model-name substring, so if you remap a tier to another provider, add a matching `PRICES` entry with that provider's rates.

## Local models

The same mechanism works with Ollama or any OpenAI-compatible endpoint:

```yaml
  - model_name: claude-haiku-4-5
    litellm_params:
      model: ollama/qwen2.5-coder:14b
      api_base: http://localhost:11434
```

Expect degraded tool-calling reliability with small local models; keep them on `extractor`-style tasks and let escalation catch failures.

If you give local models their own tier rather than remapping an existing one, add the tier to `tiers` in `router-config.json` and leave `ceiling` pointing at your real top tier:

```json
{
  "tiers": ["local", "haiku", "sonnet", "opus", "fable"],
  "ceiling": "fable",
  "agents": { "localextractor": { "tier": "local", "capabilities": ["extract"] } }
}
```

`ceiling` is named rather than inferred from the end of `tiers`, so the tier's position in the list does not matter.

Cost reporting needs attention too: `scripts/stats.py` prices by model-name substring and falls back to the top-tier rate for anything it does not recognise, which would report a free local model as your most expensive one. Add a zero-cost `PRICES` entry for it.
