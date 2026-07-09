# Releasing nauro

Maintainer runbook for cutting a release of the `nauro` monorepo. Written to be
followed by a human maintainer or driven by an AI agent; the three **GATE**
points are where a human decision is required.

> To use this as an invocable `/nauro-release` skill in Claude Code, copy this
> file to `~/.claude/skills/nauro-release/SKILL.md` and add a `name` +
> `description` YAML frontmatter block. It is intentionally not checked in under
> `.claude/skills/` — that directory is a drift-guarded mirror of the skills
> `nauro adopt` distributes to end users, and release tooling is maintainer-only.

Two packages ship independently to PyPI via tag-triggered trusted-publish
workflows: **nauro** (`nauro-v*` tag) and **nauro-core** (`nauro-core-v*` tag).
The `nauro-v*` tag additionally publishes `server.json` to the MCP registry.

Release the packages that have **shippable source** changes since their last
tag. A package whose only unreleased changes are tests, CI, or docs does not
need a release.

## Authority and gates

A release publishes to PyPI, which is **irreversible** — a version can never be
reused or unpublished. Three hard gates, each requiring explicit human sign-off:

1. **Version choice** — patch / minor / major per package. Recommend, don't assume.
2. **Merge** — approve merging the release PR.
3. **Publish** — explicitly approve pushing the tags (the point of no return).
   Never push a `*-v*` tag without it.

## Conventions this repo enforces

- **No AI-assistant attribution, no internal decision numbers** in commit
  messages, PR bodies, or release notes. `main`'s history and merged PR bodies
  carry neither.
- **Versions are single-sourced** from each `pyproject.toml` via
  `importlib.metadata` (never a hardcoded `__version__`).
- **server.json is version-locked to the `nauro` package.**
  `scripts/check_server_json_version.py` fails the PR if `server.json`
  `.version` or `.packages[0].version` drift from `packages/nauro/pyproject.toml`.
  Irrelevant to a nauro-core-only release.
- **uv.lock must be re-locked.** CI runs `uv sync --locked`, which fails if the
  committed lock does not match the bumped `pyproject` versions. Always run
  `uv lock` and commit the result as part of the bump.
- Tags are annotated, named `nauro-core-v<X.Y.Z>` / `nauro-v<X.Y.Z>`, created at
  the release PR's squash-merge commit.
- Squash-merge the release PR (matches repo history).

## Step 0 — Determine scope (what is unreleased)

```
git tag --sort=-creatordate | grep '^nauro-v' | head -1
git tag --sort=-creatordate | grep '^nauro-core-v' | head -1

# Shippable (src) changes since the last shared release commit <SHA>
git log <SHA>..origin/main --oneline
git diff --stat <SHA>..origin/main -- packages/nauro-core/src
git diff --stat <SHA>..origin/main -- packages/nauro/src
git diff --stat <SHA>..origin/main -- server.json
```

If a package's `src` tree is untouched, do not release it. Report the per-package
change set so the exact scope is visible (and anything not ready can be held).

## Step 1 — Decide versions (GATE)

Classify each releasing package's changes: backward-compatible fix/refinement →
patch; additive user-facing feature → minor; breaking public-API change → major
(the 1.0 contract surface is frozen — a break is a big deal, flag it hard).
Surface a recommendation and the alternatives; wait for the pick.

When `nauro-core` is bumped and `nauro` also releases, bump nauro's
`nauro-core>=X,<2` floor to the new nauro-core version.

## Step 2 — Branch and bump

```
git checkout main && git pull --ff-only
git checkout -b release-<summary>
```

Edit only what the release needs:

- `packages/nauro-core/pyproject.toml` — `version` (if nauro-core releasing)
- `packages/nauro/pyproject.toml` — `version` and the `nauro-core>=...,<2` floor (if nauro releasing)
- `server.json` — `.version` and `.packages[0].version` to the new **nauro** version (only if nauro releasing)
- `uv.lock` — run `uv lock` after the pyproject edits; commit the result

## Step 3 — Verify locally (the guard scripts are stdlib-only, no venv needed)

```
python3 scripts/check_server_json_version.py        # meaningful only if nauro released
python3 scripts/check_single_sourced.py packages/nauro/src
uv lock --check                                     # lock is current
uv sync --locked --package nauro-core --all-extras --python 3.12   # must pass
uv sync --locked --package nauro --all-extras --python 3.12        # must pass
git diff --stat                                     # expect only the intended files
```

Commit with a clean message, subject `Release nauro <X> and nauro-core <Y>`
(name only the packages actually releasing).

## Step 4 — Release PR

Push the branch and open the PR. The body states the version moves, what ships
per package, the checks, and the post-merge tag steps. Wait for **all** CI checks
green (lint incl. both guard scripts, import-linter, nauro + nauro-core across
Python 3.10–3.14).

## Step 5 — Merge (GATE)

On approval, squash-merge with a clean subject `Release ... (#NNN)`. Then
`git checkout main && git pull --ff-only`; confirm the bumped versions are on
`main`.

## Step 6 — Tag and publish (GATE — irreversible)

Only on explicit publish approval. Tag the merge commit and push — each tag
triggers its trusted-publish workflow (OIDC, no tokens):

```
git tag -a nauro-core-v<Y> <merge-sha> -m "nauro-core <Y>"
git tag -a nauro-v<X>      <merge-sha> -m "nauro <X>"
git push origin nauro-core-v<Y> nauro-v<X>          # push only the tags being released
```

## Step 7 — Watch the publish workflows

```
gh run list --workflow=publish-nauro-core.yml --limit 1
gh run list --workflow=publish-nauro.yml --limit 1
# after completion, confirm conclusions (the nauro run has two jobs):
gh run view <id> --json jobs -q '.jobs[] | .name + ": " + .conclusion'   # publish + publish-registry
```

`publish-nauro.yml` re-verifies `server.json` against the tag before the
MCP-registry publish, so a drift that slipped past CI still fails here (loudly,
post-tag). Confirm PyPI:

```
curl -s https://pypi.org/pypi/nauro/json      | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['version'])"
curl -s https://pypi.org/pypi/nauro-core/json | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['version'])"
```

## Step 8 — GitHub Releases

Create a release per tag with notes, matching the house style:

- **nauro-core** — a short prose paragraph of what changed, ending with a
  public-API stability line (e.g. "The curated public API is unchanged since <prev>.").
- **nauro** — a `## Highlights` bullet list, then
  `Requires nauro-core >= <Y> (published alongside).` and a breaking-changes line.

```
gh release create nauro-core-v<Y> --verify-tag --title "nauro-core <Y>" --notes-file <file>
gh release create nauro-v<X>      --verify-tag --title "nauro <X>"      --notes-file <file>
```

## Step 9 — Clean up

Delete the merged release branch locally (`git branch -d`); the remote branch
auto-deletes on merge.

## Failure handling

- Release-PR CI red on `uv sync --locked` → the lock was not re-locked; run
  `uv lock`, commit, push.
- `check_server_json_version.py` fail → `server.json` `.version` /
  `.packages[0].version` do not match the nauro pyproject; fix before the PR.
- Publish workflow fail after tag → a PyPI version is consumed; you cannot
  re-tag the same version. Diagnose, then cut the next patch. Never delete or
  re-push a released tag.
- If only one package changed, release only that package: push only its tag, and
  skip server.json/registry unless it is the nauro tag.
