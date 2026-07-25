.PHONY: help test gate compile pytest smoke venv clean-venv bump-patch bump-minor bump-major

VENV := .venv
PYTHON := $(VENV)/bin/python
STAMP := $(VENV)/.requirements-installed

help:
	@echo "make test         Full gate: byte-compile, pytest, hook smoke tests"
	@echo "make venv         Create $(VENV) and install requirements"
	@echo "make clean-venv   Remove $(VENV)"
	@echo "make bump-patch   Run the gate, bump patch version, commit, tag"
	@echo "make bump-minor   Run the gate, bump minor version, commit, tag"
	@echo "make bump-major   Run the gate, bump major version, commit, tag"

# Create the virtualenv and install requirements. The stamp file is keyed to
# requirements.txt, so edits to the deps trigger a reinstall on the next run.
$(STAMP): requirements.txt
	@test -d $(VENV) || { echo "Creating virtualenv in $(VENV)..."; python3 -m venv $(VENV); }
	@$(PYTHON) -m pip install --quiet --upgrade pip
	@$(PYTHON) -m pip install --quiet -r requirements.txt
	@touch $(STAMP)
	@echo "Dependencies installed in $(VENV)"

venv: $(STAMP)

# `test` is the release gate, not just the unit suite. Ordered cheapest-first
# so a syntax error surfaces in a second rather than after the full suite.
#
#   compile  every hook and script imports cleanly. A hook with a syntax error
#            is worse than a missing one: it fires on every tool call.
#   pytest   the suite, including manifest/JSON validity and the routing
#            invariants (self-context, planning-root exemption, guard rules).
#   smoke    the fail-open contract, exercised as the harness does it: garbage
#            on stdin must never block a tool call. Unit tests assert this per
#            hook; this runs every hook the manifest actually wires up, so a
#            newly wired hook cannot skip the check by having no test.
test: gate
gate: compile pytest smoke
	@echo "gate: PASS"

compile: $(STAMP)
	@$(PYTHON) -m compileall -q hooks scripts bump-version.py
	@echo "compile: ok"

pytest: $(STAMP)
	@$(PYTHON) -m pytest tests -q

smoke: $(STAMP)
	@$(PYTHON) scripts/smoke_hooks.py

clean-venv:
	@rm -rf $(VENV)
	@echo "Removed $(VENV)"

# Version bumps are gated on the full suite: `gate` is a prerequisite, so a
# failure aborts before bump-version.py or any git write happens.
#
# The bump runs entirely inside one shell invocation, deliberately. Doing it as
# `$(eval VERSION := $(shell python3 bump-version.py ...))` looks equivalent and
# is not: make expands the whole recipe before running any of it, so the version
# file gets rewritten before the preflight checks below have executed -- leaving
# a bumped plugin.json behind on an aborted release. Keep the checks and the
# write in the same shell, in order.
bump-patch: gate
	@$(MAKE) --no-print-directory do-bump PART=patch

bump-minor: gate
	@$(MAKE) --no-print-directory do-bump PART=minor

bump-major: gate
	@$(MAKE) --no-print-directory do-bump PART=major

.PHONY: do-bump
do-bump:
	@test -n "$(PART)" || { echo "ERROR: PART not set (use bump-patch/minor/major)"; exit 1; }
	@git rev-parse --git-dir >/dev/null 2>&1 || { echo "ERROR: not a git repository - cannot commit or tag"; exit 1; }
	@test -z "$$(git status --porcelain .claude-plugin/plugin.json)" || { echo "ERROR: .claude-plugin/plugin.json has uncommitted changes - aborting"; exit 1; }
	@VERSION=$$(python3 bump-version.py $(PART)) || exit 1; \
	 test -n "$$VERSION" || { echo "ERROR: bump-version.py produced no version - aborting"; exit 1; }; \
	 git add .claude-plugin/plugin.json || exit 1; \
	 git commit -m "v$$VERSION" || exit 1; \
	 git tag -a "v$$VERSION" -m "v$$VERSION" || exit 1; \
	 echo "Bumped to v$$VERSION"
