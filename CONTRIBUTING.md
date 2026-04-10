# Contributing to Nauro

Thanks for your interest in contributing to Nauro! This guide covers how to set up your development environment, run tests, and submit changes.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Setup

Clone the repo and sync all workspace packages:

```bash
git clone https://github.com/nauro-ai/nauro.git
cd nauro
uv sync --all-packages --all-extras
```

## Running tests

```bash
# nauro-core tests
uv run pytest packages/nauro-core/tests/ -x -q

# nauro tests (excludes integration tests by default)
uv run pytest packages/nauro/tests/ -x -q -m "not integration"
```

## Linting

```bash
uv run ruff check packages/
uv run ruff format --check packages/
```

To auto-fix:

```bash
uv run ruff check --fix packages/
uv run ruff format packages/
```

## Project structure

This is a uv workspace monorepo with two packages:

- `packages/nauro/` — CLI + local MCP server
- `packages/nauro-core/` — shared pure-Python logic (parsing, validation, constants)

## Submitting changes

1. Fork the repository
2. Create a feature branch (`git checkout -b my-feature`)
3. Make your changes and add tests
4. Ensure all tests pass and linting is clean
5. Commit with a clear message describing the change
6. Open a pull request against `main`

## Architectural decisions

Nauro tracks its own architectural decisions using its decision system. For significant design choices (new patterns, dependency changes, scope cuts), use `propose_decision` via the MCP tools to record the decision with rationale.

## Code style

- Follow existing patterns in the codebase
- All store writes go through `packages/nauro/src/nauro/store/writer.py`
- No Jinja2 — use f-strings and string templates
- Keep external dependencies minimal in `nauro-core` (it has zero runtime deps)

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
