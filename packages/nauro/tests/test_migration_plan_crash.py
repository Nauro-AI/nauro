from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nauro.store.migration_admission import inspect_migration
from nauro.sync import migration_preservation as preservation
from nauro.sync.generation_attachment import InitialAttachmentSession
from tests.test_migration_preservation import saved as saved_fixture

saved = saved_fixture

_CHILD = """
import base64, json, os, sys
from pathlib import Path
import httpx
from nauro.auth import DEFAULT_AUTH_REDIRECT_URI
from nauro.store.resolution import resolve_project_binding
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_connection import attachment_connection
from nauro.sync.migration_admission import load_migration_plan
from nauro.sync.migration_preservation import preserve_migration_source

args = json.load(sys.stdin)
source, root = Path(args["source"]), Path(args["root"])
record, raw = load_migration_plan(source)
binding = resolve_project_binding(record.project_id, None, use_cwd=False)
def wire(request):
    if request.url.path == "/projects":
        return httpx.Response(200, json={"authority": "generation_owner", "projects": [
            {"project_id": record.project_id, "role": "owner"}]})
    assert request.url.path == "/generations/projection"
    return httpx.Response(200, json={"projection": json.loads(raw)["projection"],
                                  "manifest_base64": args["manifest"]})
original_open, original_replace = os.open, os.replace
def crash_open(path, flags, *a, **kw):
    fd = original_open(path, flags, *a, **kw)
    if Path(path).parent == root and Path(path).name.startswith(".plan"):
        if args["phase"] == "partial":
            os.write(fd, raw[:17])
        if args["phase"] in {"created", "partial"}:
            os._exit(73)
    return fd
def crash_replace(source, destination):
    if Path(destination) == root / "plan.json" and args["phase"] == "before_replace":
        os._exit(73)
    original_replace(source, destination)
    if Path(destination) == root / "plan.json" and args["phase"] == "after_replace":
        os._exit(73)
os.open, os.replace = crash_open, crash_replace
with httpx.Client(transport=httpx.MockTransport(wire)) as client:
    session = InitialAttachmentSession(binding, Path(args["repo"]),
        attachment_connection(DEFAULT_AUTH_REDIRECT_URI), client)
    preserve_migration_source(record, session)
raise AssertionError("Crash point was not reached")
"""


@pytest.mark.parametrize("phase", ["created", "partial", "before_replace", "after_replace"])
def test_plan_publication_process_crash_resumes(saved, phase):
    record, plan, session, _, _, _ = saved
    source = session.binding.store_path
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    manifest = session.client.get(
        "https://mcp.nauro.ai/generations/projection",
        headers={"Authorization": "Bearer test-token"},
    ).json()
    result = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD],
        input=json.dumps(
            {
                "source": str(source),
                "root": str(plan.backup_root),
                "repo": str(session.repo),
                "phase": phase,
                "manifest": manifest["manifest_base64"],
            }
        ),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 73, result.stderr
    assert inspect_migration(source) == record
    session = InitialAttachmentSession(
        session.binding, session.repo, session.connection, session.client
    )
    for _ in range(2):
        assert preservation.preserve_migration_source(record, session) == plan.backup_root
        assert (plan.backup_root / "plan.json").read_bytes() == plan.manifest_json
        assert inspect_migration(source) == record
    assert {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    } == before
    for entry in plan.entries:
        assert (plan.backup_root / entry.destination_path).read_bytes() == before[
            source.joinpath(entry.source_path).relative_to(source)
        ]
    assert not list(plan.backup_root.glob(".plan*"))


@pytest.mark.parametrize(
    "damage", ["bytes", "oversize", "name", "digest", "link", "hardlink", "directory", "published"]
)
def test_plan_scratch_uncertainty_refuses_without_cleanup(saved, tmp_path, damage):
    record, plan, session, _, _, calls = saved
    root = plan.backup_root
    root.mkdir()
    scratch = root / preservation._plan_pending(plan.manifest_json)
    evidence = tmp_path / "evidence"
    evidence.write_bytes(plan.manifest_json[:17])
    if damage == "link":
        scratch.symlink_to(evidence)
    elif damage == "hardlink":
        scratch.hardlink_to(evidence)
    else:
        scratch.write_bytes(b"unknown" if damage == "bytes" else evidence.read_bytes())
    if damage == "oversize":
        scratch.write_bytes(plan.manifest_json + b"x")
    if damage == "digest":
        scratch = scratch.rename(root / (".plan-" + "0" * 64 + ".pending"))
    elif damage == "name":
        scratch = scratch.rename(root / ".plan.json.0123456789abcdef.tmp")
    elif damage == "directory":
        (root / "legacy").mkdir()
    elif damage == "published":
        (root / "plan.json").write_bytes(plan.manifest_json)
    before = scratch.read_bytes()
    with pytest.raises((ValueError, preservation.MigrationAdmissionError)):
        preservation.preserve_migration_source(record, session)
    assert scratch.read_bytes() == before
    assert evidence.read_bytes() == plan.manifest_json[:17]
    assert inspect_migration(session.binding.store_path) == record
