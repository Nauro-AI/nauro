# Cross-surface parity tests

These are Layer-3 cross-surface parity tests. Each one runs the same kernel
operation against two stores and asserts the results are identical:

- `nauro.store.filesystem_store.FilesystemStore` — the local store that ships
  in this package.
- `mcp_server.store.cloud_store.CloudStore` — the S3-backed store that ships in
  the separate, private mcp-server repository.

The guarantee they encode is that the local and cloud surfaces return the same
result envelope for the same inputs, so the two implementations cannot drift
apart unnoticed.

## Where they run in CI

These tests need both the `nauro` CLI and the private `mcp_server` package
importable in one environment. The ordinary `test-nauro` job never installs
`mcp_server`, so it ignores this directory
(`--ignore=packages/nauro/tests/cross_surface`). The `test-paired-server` job
checks out mcp-server at a pinned commit, installs this workspace into the
server environment, and runs this directory with a JUnit assertion that fails
on any skip, so the parity guarantee runs on every pull request.

Each test calls `pytest.importorskip("mcp_server.store.cloud_store", ...)` at
module load. When `mcp_server` is not installed, the test skips with a clear
reason instead of erroring, so running this directory without the cloud store
present is harmless.

## Running them locally

You need both packages importable on one `PYTHONPATH` (the nauro workspace plus
the private mcp-server repo). The server store reads its bucket name at import,
so point it at the shared test bucket. With that in place, run:

```bash
NAURO_S3_BUCKET=nauro-cross-surface-test uv run --package nauro pytest packages/nauro/tests/cross_surface/ -v
```

If `mcp_server` (or its `boto3`/`moto` test dependencies) is not installed, the
tests skip with the CloudStore reason rather than failing.
