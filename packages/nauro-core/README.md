# nauro-core

Shared pure-Python library for the [Nauro](https://github.com/nauro-ai/nauro) ecosystem. Provides parsing, validation, context assembly, and constants used by both the Nauro CLI and the remote MCP server.

## Installation

```bash
pip install nauro-core
```

## What's inside

- **`decision_model`** — the Pydantic `Decision` model plus compiled regexes and `parse_decision`/`format_decision` for the Nauro markdown protocol (decision titles, metadata fields, section headers)
- **`constants`** — limits, thresholds, valid values, file paths shared across all Nauro surfaces
- **`parsing`** — pure markdown→data helpers: `extract_stack_summary`, `decisions_summary_lines` (decision parsing lives in `decision_model.parse_decision`, which returns a validated `Decision`)
- **`context`** — `build_l0`/`build_l1`/`build_l2` context assembly from pre-loaded files and parsed decisions
- **`validation`** — structural screening, hash dedup, BM25 similarity for decision conflict detection

## Design principles

- **No I/O** — no filesystem, no network; callers inject pre-loaded data. Runtime dependencies are compute-only (BM25, parsing, validation); embeddings are an optional extra (`nauro-core[embeddings]`)
- **Function injection** — callers pass pre-loaded data (`files: dict[str, str]`, `decisions: list[dict]`); nauro-core never reads files or calls APIs
- **Import isolation** — enforced via `import-linter`: nauro-core cannot import from `nauro` or `mcp_server`

## Usage

```python
from nauro_core import parse_decision, build_l0, compute_hash

# Parse a decision markdown file
decision = parse_decision(markdown_content, "042-use-s3.md")

# Build L0 context from pre-loaded files
context = build_l0(files={"state.md": state, "stack.md": stack}, decisions=decisions)

# Check for duplicate decisions
hash_val = compute_hash(title="Use S3 for storage", rationale="...")
```

## License

Apache 2.0
