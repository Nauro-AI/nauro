# Contributing to Nauro

Thanks for helping. Nauro is maintained by one person, so this guide tries to save your time and the reviewer's.

## Before you start

- Issues labeled `good first issue` or `help wanted` are ready to pick up. Comment on the issue to say you are taking it, so two people do not do the same work.
- For a bug that is not filed yet, open an issue with the bug form first. For anything larger than a small fix, open an issue and agree on an approach before you write code.
- Some areas always need an issue first. See [What needs an issue first](#what-needs-an-issue-first).
- Report security problems privately, never in a public issue. See [SECURITY.md](SECURITY.md).

## Prerequisites

- Python 3.10 or newer. CI tests 3.10 through 3.14.
- [uv](https://docs.astral.sh/uv/)
- git

## Setup

Fork the repository, then:

```bash
git clone https://github.com/<your-user>/nauro.git
cd nauro
uv sync --locked --all-packages --all-extras
```

`--locked` installs exactly what `uv.lock` pins, the same way CI does. Use `uv run --no-sync` for the commands below so uv does not re-resolve dependencies.

This is a uv workspace with two packages:

- `packages/nauro/`: the CLI and the local MCP server (stdio)
- `packages/nauro-core/`: shared pure-Python logic with no I/O (parsing, validation, context assembly, MCP tool specs)

## Run the tests

```bash
uv run --no-sync pytest packages/nauro-core/tests/ -q
uv run --no-sync pytest packages/nauro/tests/ --ignore=packages/nauro/tests/cross_surface -q
```

What to expect:

- nauro-core runs in a few seconds, with no skips.
- nauro takes a few minutes and skips about 120 tests. The skips are expected:
  - Windows-only filesystem and permission tests, when you are not on Windows
  - paired tests that need the hosted server's code, which is not public
  - parametrized cases that do not apply to a given input
- `packages/nauro/tests/cross_surface` also needs the hosted server, so leave it out as above. The shorter commands in the README also work; they include about 10 extra skipped tests from that directory.

A skip is not a failure. A new skip in the area you changed is worth a second look.

## Run the CI checks before you push

These are the commands from the lint and import-linter jobs in `.github/workflows/ci.yml`. CI runs `check_single_sourced.py`, `check_docstring_budget.py` and `check_server_json_version.py` with a bare `python`; they use only the standard library, so running them through `uv run --no-sync python` gives the same result.

```bash
uv run --no-sync ruff check packages/ benchmarks/ scripts/
uv run --no-sync ruff format --check packages/ benchmarks/ scripts/
uv run --no-sync python scripts/check_ruff_budget.py
uv run --no-sync python -m unittest discover -s scripts -p test_check_ruff_budget.py
uv run --no-sync mypy --config-file packages/nauro/pyproject.toml
uv run --no-sync python scripts/check_single_sourced.py packages/nauro/src
uv run --no-sync python scripts/check_docstring_budget.py packages/nauro/src packages/nauro-core/src
uv run --no-sync python scripts/check_server_json_version.py
(cd packages/nauro-core && uv run --no-sync --package nauro-core lint-imports)
(cd packages/nauro && uv run --no-sync --package nauro lint-imports)
```

Run `mypy` from the repository root, as shown. To fix formatting, run `uv run --no-sync ruff format packages/ benchmarks/ scripts/`.

CI also runs jobs you usually cannot run locally:

- the test suites on Python 3.10 to 3.14
- store and sync tests on macOS and Windows
- a wheel build and install smoke test
- `test-paired-server`, a paired job against the hosted server's code. It needs a maintainer credential, so on a pull request from a fork it shows as skipped. That is expected and nothing you need to fix: a maintainer runs it on your exact commits before merging, and again if you push new commits after that.

Until you have had a pull request merged here, a maintainer approves each CI run on your pull requests, including after you push new commits.

## Manual testing

Never point a development build at your real Nauro data. Set `NAURO_HOME` to a temporary directory:

```bash
export NAURO_HOME="$(mktemp -d)"
uv run --no-sync nauro init --demo
```

`NAURO_HOME` covers the store, the registry and Nauro's config. It does not cover agent configuration in your home directory. `nauro setup` and `nauro adopt` write to files such as `~/.claude.json`, `~/.codex/config.toml`, `~/.cursor/mcp.json` and `~/.claude/skills/`. When you test those commands, also point `HOME` at a temporary directory (`USERPROFILE` on Windows), and run them inside a throwaway git repository.

In tests, use pytest's `tmp_path` fixture; never touch `~/.nauro`.

## What needs an issue first

Some parts of Nauro are covered by semantic versioning or carry security weight. Open an issue and agree on an approach before you send a pull request that touches them.

**The public contract.** These are covered by semantic versioning:

- the CLI command tree, flag names and flag types
- the local MCP tool names and schemas
- the on-disk store format, including `registry.json` and the decision file grammar
- the curated `nauro_core.__all__` API

`packages/nauro/tests/test_public_contract.py` checks the command tree, the tool schemas, the store-format constants and `nauro_core.__all__` against `packages/nauro/tests/snapshots/public_contract_v1.json`. Never regenerate the contract snapshot to make a test pass. A diff there means the change needs a new major version or a maintainer decision.

**nauro-core symbols used by the hosted service.** `packages/nauro-core/tests/hosted_consumer_symbols.txt` and `packages/nauro-core/tests/test_hosted_consumer_contract.py` pin symbols that the hosted service imports. If that test fails, stop and open an issue. Do not regenerate its snapshot under `packages/nauro-core/tests/snapshots/`.

**Security and concurrency code.** Please don't change these without an issue first:

- sync, locks and recovery: `packages/nauro/src/nauro/sync/`, and in `packages/nauro/src/nauro/store/`: `store_lock.py`, `decision_lock.py`, `recovery.py`, `recovery_actions.py`, `post_commit.py`
- generation and record contracts in `packages/nauro/src/nauro/store/`: `generation_*.py`, `*_contract.py`, `*_records.py`
- credentials and Windows security: `sync/writer_credential.py`, `store/_windows_security.py`, the credential logic in `cli/commands/auth.py`
- decision references and repository provenance: `cli/decision_reference.py`, `sync/decision_reference.py`, `store/repo_head.py`
- the write path in nauro-core: `nauro_core/operations/propose_decision.py`, `update_state.py`, `flag_question.py`, `update_stack.py`, `share_context.py`, the `_commit_*.py` modules, and `nauro_core/question_provenance.py`; and the write tools in `packages/nauro/src/nauro/mcp/tools.py`

Better error text and new tests around these areas are welcome. Changes to their control flow, signatures or exception types are not, without an issue first.

**Shared MCP tool descriptions** in `nauro_core/mcp_tools.py` are part of the tool contract. CLI help changes belong in the CLI layer.

**New commands, flags or setup targets** (for example `nauro setup <new-client>`) need an issue that shows a real, observed need. Additions are permanent until the next major version, so convenience or symmetry alone is not enough.

**Dependencies.** Adding or upgrading a dependency needs an issue first. If it is agreed, update the `pyproject.toml` and commit the regenerated `uv.lock` in the same pull request.

## House rules

- **Validate CLI input in the command body** and raise `typer.BadParameter`. Don't switch an option to an enum, `IntRange` or `exists=True`, because that changes the contract metadata.
- **No new regular expressions.** Use plain string operations over a fixed vocabulary.
- **Inputs that already work keep producing byte-identical output.** This applies to import, renderers, status JSON and graph HTML. Add a test that shows it.
- **Don't teach the decision parser new syntax.** Better error messages are welcome.
- **Parse semi-structured input into typed models at the boundary.** Errors are typed; no sentinel strings for control flow.
- **Ratchets only shrink:**
  - the ruff budget: `scripts/check_ruff_budget.py` against `scripts/ruff_budget_baseline.jsonl`. When you remove findings, rewrite the baseline in the same pull request with `--write-baseline`. Never add rows.
  - the mypy ignore list in `packages/nauro/pyproject.toml`
  - the docstring budget: 3 lines for a function, 5 for a class, 15 for a module. The baseline in `scripts/docstring_budget_baseline.txt` is empty, so every docstring must fit.
  - the import-linter contracts in each package's `.importlinter`
- **Code carries no history.** No dates, issue or pull request numbers, or "this used to break" notes in code or docstrings. That belongs in the commit message and the pull request.
- **Plain ASCII hyphens** in CLI output and docs. No em-dashes.
- **Python 3.10 stays supported.**
- **Skill files:** the copies under `.claude/`, `.agents/` and `.cursor/` are generated. Edit the source under `packages/nauro/src/nauro/skills/`; if the skill drift test fails, it prints the command that re-renders each copy.

## Pull requests

- One concern per pull request. Small is better.
- Branch names start with `feat/`, `fix/`, `docs/` or `ci/`, followed by a short slug.
- Fill in the template's three sections: Why, What changed, Test plan. A small change needs a line or two under each.
- Link the issue with `Closes #123`.
- Add or update tests for behavior changes.
- Keep commits readable; the maintainer may squash on merge.

## AI-assisted contributions

AI-assisted contributions are welcome. You are responsible for every line: read it, run the tests and the CI checks above, and be ready to explain it in review.

## Coding agents

You do not need Nauro, its MCP tools or a Nauro account to contribute. The repository's CLAUDE.md is also the maintainer's working context and imports a generated file that is not in your clone; that is expected. Where CLAUDE.md and this guide differ, follow this guide. The skills under `.claude/skills/`, `.agents/skills/` and `.cursor/rules/` call Nauro tools, so you do not need to run them.

## Code of conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Report unacceptable behavior to thomas@nauro.ai.

## License

Nauro is licensed under the [Apache License 2.0](LICENSE). By submitting a contribution, you agree that it is licensed under the same license, as section 5 of the license describes.
