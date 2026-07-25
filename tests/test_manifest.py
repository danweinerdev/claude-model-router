"""Plugin manifest and packaging invariants."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())

NAME = "model-router"


def test_plugin_manifest_shape():
    assert PLUGIN["name"] == NAME
    assert re.fullmatch(r"\d+\.\d+\.\d+", PLUGIN["version"]), PLUGIN["version"]
    for field in ("description", "author", "license", "repository"):
        assert PLUGIN[field], field


def test_repo_is_a_plugin_not_a_marketplace():
    """This repo is one plugin. The marketplace that lists it is separate.

    A stray marketplace.json here would make the repo installable as its own
    single-plugin marketplace, so the same plugin could be added twice under
    two different marketplace names.
    """
    assert not (ROOT / ".claude-plugin" / "marketplace.json").exists()
    assert not list(ROOT.rglob("marketplace.json"))


def test_bump_version_targets_this_manifest():
    """`make bump-*` must write the file the marketplace actually reads."""
    text = (ROOT / "bump-version.py").read_text()
    assert '".claude-plugin" / "plugin.json"' in text


def test_every_json_file_parses():
    for path in ROOT.rglob("*.json"):
        if ".venv" in path.parts or ".pytest_cache" in path.parts:
            continue
        json.loads(path.read_text())  # raises on malformed


def test_skills_are_namespaced_consistently():
    """Docs promise /model-router:<skill> for every shipped skill."""
    for skill in (ROOT / "skills").iterdir():
        if not skill.is_dir():
            continue
        text = (skill / "SKILL.md").read_text()
        assert text.startswith("---"), skill.name
        assert f"name: {skill.name}" in text, skill.name


# Upstream identifiers that still *function* if left behind -- an env var the
# code no longer reads, a command namespace that resolves to nothing, a state
# path nothing writes. Each is a silent no-op rather than an error, which is
# what makes them worth a test. Prose mentions of the upstream project are
# attribution and deliberately allowed.
STALE_IDENTIFIERS = re.compile(
    r"FRUGAL_[A-Z_]+"
    r"|/frugal:"
    r"|frugal-(sensitivity|inline-|escalation)"
    r"|\.claude/frugal/"
)


def test_no_functional_upstream_identifiers_remain():
    """The rebrand must be complete: a stray env var name is a silent no-op."""
    skip = {".venv", ".git", ".pytest_cache", "__pycache__"}
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or set(path.parts) & skip:
            continue
        if path.name == "test_manifest.py":
            continue  # this file spells the patterns out
        if path.suffix not in (".py", ".md", ".json", ".txt", "") \
                and path.name != "Makefile":
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        found = STALE_IDENTIFIERS.findall(text)
        if found:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"upstream identifiers left in: {offenders}"


def test_readme_documents_every_env_knob():
    """A knob the code reads but the README omits is a knob nobody finds."""
    readme = (ROOT / "README.md").read_text()
    used = set()
    for path in list((ROOT / "hooks").glob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        used |= set(re.findall(r"MODEL_ROUTER_[A-Z_]+", path.read_text()))
    missing = sorted(k for k in used if k not in readme)
    assert not missing, f"undocumented env knobs: {missing}"
