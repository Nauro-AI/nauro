# You're Absolutely Wrong

An agent skill that objects to your plan using your project's own history. It has to cite the commit or pull request that supports its objection, or admit it has nothing.

## Example

From a test replay against an earlier state of `doomemacs/core`. The plan was written for the test by an agent that had not seen the history. Shortened, with the maintainer's name removed.

> **Plan:** add a `+all-frames` flag to the `:ui window-select` module, so `aw-scope` becomes `(if (modulep! +all-frames) 'global 'frame)`, set the matching `switch-window` option under the same flag, and document it in three files.

> You're absolutely not doing this to me with a renamed `+global`.
>
> **Objection:** This is effectively the change a maintainer refused on 2025-08-20: a `:ui window-select` module flag that switches `aw-scope` between `'global` and `'frame`. The switch-window half wasn't in that PR, but it's just a second one-variable setting, so it doesn't answer the refusal.
>
> **Evidence:** PR #8459, "feat(aw): add +global flag to make aw-scope configurable" (closed unmerged, labeled `wontfix`)
>
> ```
> -  (setq aw-scope 'frame
> +  (setq aw-scope (if (modulep! +global) 'global 'frame)
> ```
>
> **Why it was undone or refused:** "Module flags are not intended to be substitutes for trivial configuration." That's the maintainer's [comment closing #8459](https://github.com/doomemacs/core/pull/8459#issuecomment-3207503567).
>
> **Strength:** `strong`. A maintainer gave a reason that applies directly to this flag, and the plan doesn't address it.

The tone follows the evidence. A weak case gets "You're absolutely possibly wrong." If it completes the search and finds no supported objection, the whole reply is:

> I have nothing. Proceed.

If missing history or failed searches prevent a conclusion, it says "I can't call this one." and names the gap. Before objecting, it checks the current code and later history for evidence that the old reason no longer applies.

## Install

```bash
gh skill install Nauro-AI/nauro youre-absolutely-wrong@main --agent claude-code --scope user
```

Or copy this folder to `~/.claude/skills/youre-absolutely-wrong/`.

## Use

```
/youre-absolutely-wrong Replace the job queue with cron
/youre-absolutely-wrong plans/auth-rewrite.md
```

It only runs when you invoke it. It needs `git`, and it reads pull requests through `gh` when that's available. It's told only to read, and your agent's permissions still apply.

## What it can't do

- It only knows what was written down. Reasons that never reached a commit, a PR or a doc are invisible to it.
- "I have nothing" means the completed search found no objection that still applies. It does not mean the plan is good. Without `gh`, it cannot check PR history and must disclose that limit.
- It looks for ideas that were tried and undone or refused. It won't catch a plan that's wrong for other reasons.
- It can still get a detail wrong, so read the evidence before you change course.
- Its searches send repository text to your model provider, like anything else your agent reads.

## How it was tested

These tests are small, and other agents are untested.

- **Replays:** agents who had not seen the history wrote plans for earlier states of public repos. An earlier version of the skill, without this voice, found and supported the earlier attempt in 7 of 9 cases: 3 of 5 reverted changes and 4 of 4 refused PRs. It invented no citations. Agents did the grading, not people. These ran as subagents on one model, not through real skill activation. Plans that drifted to a different idea were dropped first, 3 of 12, and the refused PRs were found through a `wontfix` label, so they're unusually clear.
- **Earlier voice:** three of those cases, three real Claude Code runs each. The strength ratings held, and the case with nothing to find said "I have nothing. Proceed." every time.
- **Revision checks:** fresh Claude Code 2.1.318 sessions on Opus 5.5 tested synthetic Git histories for a standing rejection, later acceptance and shallow history. After outcome selection moved before the objection format, all three research outcomes matched expectations. The tests also found unsupported jokes and inconsistent objection length. The final wording changes still need a completed behavioral rerun. An agent reviewed the outputs; these fixtures do not test live GitHub PR searches or establish reliability across models.
- **Our own plans:** on five recent plans from this project, it told their author nothing new. Three got "I have nothing", and two pointed at work the author had already parked.

---

From [Nauro](https://nauro.ai/decision-system). The skill works without it.
