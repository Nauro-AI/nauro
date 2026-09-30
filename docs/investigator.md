# Investigate Nauro use across sessions

`nauro-investigator` reads Claude and Codex logs, relevant project decisions, code, and Git history. It looks for defects, repeated friction, drift, useful outcomes, and missing follow-up across development cycles. It checks contrary evidence and whether an old issue is still current.

The agent returns a report and a research notebook update. It runs on demand. The calling agent saves the research files so a later run can continue. The investigator has read-only instructions on every surface, plus the existing read-only settings in the Codex and Cursor renderers. These controls do not deny every inherited MCP write path; the agent must follow its authority instructions.

## Availability

The definition ships with Nauro's optional bundled subagents. A version containing this definition installs it through `nauro adopt --with-subagents` or `nauro setup all --with-subagents`. These commands also perform their normal setup work. They are not required for reading the definition or reviewing its instructions.

After installation, start a session that can dispatch the configured `nauro-investigator` agent. A generic task name does not load an agent definition. If the runtime cannot select the installed agent, report that limitation before using a different execution method.

The canonical definition is [nauro-investigator.md](../packages/nauro/src/nauro/agents/nauro-investigator.md). Claude Code receives that Markdown; Codex receives TOML rendered from the same instructions. Either can investigate logs from both clients. Installation does not schedule investigations or enable a monitoring loop.

## First run

Give the calling agent a prompt such as:

```text
Use nauro-investigator to study my use of Nauro in this repository
over the last seven days. Read the available Claude and Codex logs.
Look for supported bugs, repeated corrections, useful outcomes,
and gaps between implementation and later use.

Limit the study to 12 parent sessions and two focused investigations.
Include related child sessions and report their count separately.
Include an ordinary development session when available.
Exclude this conversation and the investigation's own lineage.
Check counterevidence and whether each finding is still current.
State coverage gaps and distinguish claims from observed outcomes.
Explain what this run adds to prior knowledge.

Pass ~/.nauro-research/nauro/notebook.md to the investigator if it exists.
Save its report under ~/.nauro-research/nauro/runs/ with a unique dated name.
Save its notebook update to ~/.nauro-research/nauro/notebook.md.
Before saving, confirm the research directory is outside all Git checkouts.
Keep source logs private. Write only these research artifacts.
Do not change product code, project decisions, hooks, or schedules.
```

Replace the repository scope, time window, budget, and research path as needed. Pass any prior reports or exported logs that should inform the study. The default local source roots are `~/.claude/projects/` and `~/.codex/sessions/`. Supply alternate roots when logs live elsewhere. The investigator verifies record shapes and repository scope before reading transcript content.

Replace `nauro` in the research path with a unique project name. Keep the research directory outside all Git checkouts so staging project changes does not include private reports.

The calling agent supplies its full session ID and the investigator's ID when available. The investigator resolves files and parent relationships before reading bodies. It uses exact paths and metadata, rather than broad session-prefix matches. Uncertain relationships and possible records from its own investigation remain outside the findings until their scope is established.

Research reports can contain sensitive project evidence. Keep them local unless you choose to share them. The calling agent should not commit or publish them as part of saving a run.

## Repeat runs

Ask the calling agent to use the same notebook and its linked run reports, inspect new records, and revisit open findings when new evidence warrants it. Each run report records two distinct forms of progress: where parsing stopped and which ranges were actually reviewed as evidence. A parse checkpoint helps locate new records in older sessions; it does not mark all earlier content as reviewed.

Keep the notebook compact. It carries:

- Stable finding IDs with current conclusions and present status.
- Material changes to conclusions, with links to their evidence.
- Missing outcome checks and concrete revisit triggers.
- Links to dated run reports containing source identities, repository revisions, evidence, counterevidence, parse checkpoints, reviewed ranges, and coverage gaps.

Retain prior reports so changed conclusions remain traceable. Reuse finding IDs, leave unchanged findings alone, and keep settled findings as short linked entries. When compacting an older notebook, first preserve any detailed evidence or checkpoints that exist only there in a dated report. Do not duplicate those details in every notebook update.

Before saving an update, the calling agent checks that the notebook still matches the version read by the investigator. If another run changed it, reconcile findings by ID and preserve both sets of evidence. Do not overwrite another run's work or mark unread records as inspected. If saving is unavailable, retain the returned update and report that continuity remains unsaved.

A useful run can conclude that no new finding meets the evidence threshold. A merged fix, a benchmark gain, and a later user benefit remain separate claims. Broader audits and experiments need their own scope. Any proposed change to project judgment still uses Nauro's human approval process.

To check continuity, run a follow-up with the saved notebook and linked reports. Check that it preserves finding IDs, distinguishes new evidence from known results, and examines only new records or named gaps. A quiet follow-up is valid when no new evidence warrants a changed conclusion.
