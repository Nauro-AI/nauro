"""Prompt templates for LLM-based context extraction.

Convention: no Jinja2 — use f-strings and string templates only.
"""

EXTRACTION_SYSTEM_PROMPT = """\
You are a structured context extractor for software projects. You analyze git \
commit diffs and produce a JSON object describing the meaningful context \
captured in that commit.

Your output MUST be a single JSON object with these keys:

{
  "decisions": [
    {
      "title": "Short title of the decision",
      "rationale": "Why this choice was made — only if evident from the diff or message",
      "rejected": [{"alternative": "Name of rejected approach", "reason": "Why it was rejected"}],
      "confidence": "high" | "medium" | "low",
      "decision_type": "architecture|library_choice|pattern|refactor|...",
      "reversibility": "easy|moderate|hard",
      "files_affected": ["list of key files affected by this decision"]
    }
  ],
  "questions": ["Unresolved questions, TODOs, or uncertainties from the diff"],
  "state_delta": "One sentence: what moved forward in this commit" | null,
  "signal": {
    "architectural_significance": 0.0 to 1.0,
    "novelty": 0.0 to 1.0,
    "rationale_density": 0.0 to 1.0,
    "reversibility": 0.0 to 1.0,
    "scope": 0.0 to 1.0
  },
  "composite_score": 0.0 to 1.0,
  "skip": true | false,
  "reasoning": "Brief explanation of why this was scored this way"
}

## Signal dimensions

Rate each dimension independently from 0.0 to 1.0:

**architectural_significance**: Does this change system structure, introduce new components, \
or alter interfaces? High (0.7-1.0) for new services, API changes, database schema changes. \
Low (0.0-0.3) for internal refactors that don't change boundaries.

**novelty**: Is this a new decision vs routine work? High for first-time technology choices, \
new patterns. Low for applying an established pattern to a new area.

**rationale_density**: How much reasoning is present in the input? High when the input \
contains explicit tradeoff analysis, rejected alternatives, constraints. Low when it's just \
"did X."

**reversibility**: How hard is this to undo? High (meaning high signal — capture it) for \
hard-to-reverse decisions like database choices, API contracts, authentication architecture. \
Low for easily reversible choices.

**scope**: How many components/files/teams does this affect? High for cross-cutting concerns. \
Low for single-file changes.

The composite_score is calculated as: \
(architectural_significance * 0.3) + (novelty * 0.2) + (rationale_density * 0.2) + \
(reversibility * 0.15) + (scope * 0.15).

The "reasoning" field MUST always be populated — it explains why you scored the way you did.

## Skip short-circuit

Set skip=true for obvious no-ops WITHOUT computing signal dimensions:
- Formatting-only changes (linter runs, whitespace)
- Dependency version bumps with no code changes
- Typo corrections
- Merge commits with no unique content
- Lockfile-only changes (poetry.lock, package-lock.json)
- .gitignore additions
- Linting fixes with no behavioral change

When skip=true, decisions must be an empty list, questions an empty list, \
state_delta null, all signal dimensions 0.0, composite_score 0.0.

## Decision extraction rules

1. Only extract decisions when the commit message or diff contains explicit \
reasoning about WHY a choice was made — tradeoffs, constraints, alternatives \
considered, or migration rationale. A description of WHAT was implemented is \
not rationale. "S3 was chosen for sync" is a description; "S3 was chosen over \
R2 because boto3 works natively without region workarounds" is rationale. If \
you can only describe WHAT, set skip=true. Do NOT fabricate rationale.

2. A single commit should typically produce 0-2 decisions, and never more \
than 3. If a commit implements multiple aspects of one coherent architectural \
choice (e.g., adding a sync layer with config, daemon, merge, remote, and \
state modules), extract ONE decision covering the whole choice — not one \
decision per file or module. Err on the side of fewer, richer decisions. More \
than 2 decisions from a single commit is a strong signal you are over-splitting.

3. Do not infer decisions from file structure or naming. Seeing daemon.py, \
merge.py, or config.py does not mean those represent separate architectural \
decisions. Only extract decisions supported by explicit reasoning in the \
commit message or diff content, not from the presence of files.

4. For the "rejected" field, only list alternatives that are explicitly replaced \
or mentioned. Each entry must have an "alternative" name and a "reason" it was \
rejected. If a commit switches from Redis to Postgres, Redis is a rejected \
alternative. If a commit just adds Postgres, there is no rejected alternative — \
use an empty list.

5. decision_type classifies the decision: "architecture" for structural changes, \
"library_choice" for dependency decisions, "pattern" for coding patterns, \
"refactor" for reorganization, "api_design" for API shape choices, \
"infrastructure" for deployment/CI/ops, "data_model" for schema/data decisions.

6. reversibility: "easy" = can be changed in a single PR, "moderate" = requires \
coordinated changes across multiple files/services, "hard" = involves data \
migrations, external API contracts, or public interfaces.

7. Extract questions only from TODO comments, FIXME notes, question marks in \
commit messages, or code that suggests uncertainty. Do NOT generate questions \
about things that the commit itself implements or resolves. If the diff adds a \
merge strategy, do not ask "What merge strategy should be used?" Only flag \
questions that remain genuinely open after the commit.

8. state_delta should be one sentence describing what moved forward. If the \
commit is trivial (skip=true), set state_delta to null.

9. confidence reflects how clearly the decision rationale is supported by the \
diff: "high" = explicit rationale in message or comments, "medium" = rationale \
inferable from the change, "low" = decision is visible but reasoning is unclear.

10. files_affected should list the key files relevant to the decision (not every \
file in the commit, just those central to the decision)."""


COMPACTION_EXTRACTION_SYSTEM_PROMPT = """\
You are extracting structured architectural decisions from a Claude Code session \
summary. This summary was produced by Claude's compaction system and already \
identifies key technical decisions and concepts. Your job is to structure these \
into the decision format, not to discover new ones.

Extract ONLY decisions that are explicitly stated or strongly implied. Do not \
fabricate rationale.

The input is already filtered for relevance — don't second-guess what's important. \
Focus on structuring: identify decision boundaries, extract rejected alternatives, \
classify type/reversibility.

The compaction summary is typically 1-3K tokens — much shorter than a raw \
transcript. Be conservative: if the summary mentions something in passing without \
clear rationale, flag it as a question, not a decision.

Your output MUST be a single JSON object with these keys:

{
  "decisions": [
    {
      "title": "Short title of the decision",
      "rationale": "Why this choice was made",
      "rejected": [{"alternative": "Name", "reason": "Why rejected"}],
      "confidence": "high" | "medium" | "low",
      "decision_type": "architecture|library_choice|pattern|refactor|...",
      "reversibility": "easy|moderate|hard",
      "files_affected": ["list of key files"]
    }
  ],
  "questions": ["Unresolved questions or uncertainties mentioned in the summary"],
  "state_delta": "One sentence: what moved forward in this session" | null,
  "signal": {
    "architectural_significance": 0.0 to 1.0,
    "novelty": 0.0 to 1.0,
    "rationale_density": 0.0 to 1.0,
    "reversibility": 0.0 to 1.0,
    "scope": 0.0 to 1.0
  },
  "composite_score": 0.0 to 1.0,
  "skip": true | false,
  "reasoning": "Brief explanation of scoring"
}

Signal dimension guidance is the same as for commit extraction. The composite_score \
formula is: (architectural_significance * 0.3) + (novelty * 0.2) + \
(rationale_density * 0.2) + (reversibility * 0.15) + (scope * 0.15).

Set skip=true only if the summary contains no decisions, no questions, and no \
meaningful state change (e.g., a session that was abandoned before any work)."""


def build_extraction_user_prompt(
    commit_message: str,
    diff_summary: str,
    changed_files: list[str],
    existing_decisions: list[str] | None = None,
) -> str:
    """Build the user message for the extraction model.

    Args:
        commit_message: The git commit message.
        diff_summary: Abbreviated diff content (truncated to fit context).
        changed_files: List of file paths changed in the commit.
        existing_decisions: Optional list of existing decision titles for dedup.

    Returns:
        Formatted user prompt string.
    """
    files_list = "\n".join(f"  - {f}" for f in changed_files)

    dedup_section = ""
    if existing_decisions:
        titles = "\n".join(f"  - {t}" for t in existing_decisions)
        dedup_section = f"""

## Existing decisions in store
{titles}

Do not extract decisions that substantially overlap with any of the above."""

    return f"""\
Analyze this git commit and extract structured context.

## Commit message
{commit_message}

## Changed files
{files_list}

## Diff
```
{diff_summary}
```
{dedup_section}

Respond with a single JSON object."""


def build_compaction_extraction_prompt(compaction_summary: str) -> str:
    """Build the user message for extracting from a compaction summary.

    Args:
        compaction_summary: The compaction summary text from Claude Code.

    Returns:
        Formatted user prompt string.
    """
    return f"""\
Extract structured architectural decisions from this Claude Code session summary.

## Session Summary
{compaction_summary}

Respond with a single JSON object."""


# ---------------------------------------------------------------------------
# Schema — defined as an Anthropic tool so we get structured JSON back
# ---------------------------------------------------------------------------

EXTRACTION_TOOL = {
    "name": "record_extraction",
    "description": "Record the structured extraction result from analyzing a git commit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "rationale": {"type": "string"},
                        "rejected": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "alternative": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["alternative", "reason"],
                            },
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "decision_type": {
                            "type": "string",
                            "enum": [
                                "architecture",
                                "library_choice",
                                "pattern",
                                "refactor",
                                "api_design",
                                "infrastructure",
                                "data_model",
                            ],
                        },
                        "reversibility": {
                            "type": "string",
                            "enum": ["easy", "moderate", "hard"],
                        },
                        "files_affected": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["title", "confidence"],
                },
            },
            "questions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "state_delta": {
                "type": ["string", "null"],
            },
            "signal": {
                "type": "object",
                "properties": {
                    "architectural_significance": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "novelty": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "rationale_density": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "reversibility": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "scope": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": [
                    "architectural_significance",
                    "novelty",
                    "rationale_density",
                    "reversibility",
                    "scope",
                ],
            },
            "composite_score": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "skip": {
                "type": "boolean",
            },
            "reasoning": {
                "type": "string",
            },
        },
        "required": [
            "decisions",
            "questions",
            "state_delta",
            "signal",
            "composite_score",
            "skip",
            "reasoning",
        ],
    },
}
