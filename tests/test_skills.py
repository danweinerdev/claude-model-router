from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_setup_statusline_skill_is_command_only():
    text = (ROOT / "skills" / "setup-statusline" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in text
    assert "statusline.py" in text
    assert "consent" in text.lower()


def test_models_skill_knows_agents_models_and_overrides_file():
    text = (ROOT / "skills" / "models" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in text
    # The agent list is registry-driven rather than hardcoded, so the skill
    # must point at the registry instead of enumerating agents itself.
    assert "registry.py" in text
    assert "scout" in text
    for model in ("haiku", "sonnet", "opus", "fable"):
        assert model in text
    assert ".claude/routing-overrides.md" in text
    assert "`model` parameter" in text
    assert "reset" in text


def test_routing_skill_forbids_prefetch_for_self_context_agents():
    """Isolation outranks cost, and the policy must say so in the skill body.

    The SessionStart hook injects this file verbatim, so a rule that only
    lives in docs/ is a rule the main loop never sees.
    """
    text = (ROOT / "skills" / "routing" / "SKILL.md").read_text()
    assert "self_context" in text
    assert "Never prefetch for a self-context agent" in text
    assert "correctness rule" in text


def test_routing_skill_covers_all_agents_and_protocol():
    text = (ROOT / "skills" / "routing" / "SKILL.md").read_text()
    for agent in ("scout", "extractor", "mechanic", "builder", "sage"):
        assert agent in text
    for marker in (
        "[router-escalation from",
        "RESULT:",
        "ESCALATE:",
        ".claude/routing-overrides.md",
        "Never delegate",
    ):
        assert marker in text, f"routing skill missing: {marker}"
