from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
)
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync import history_transport as transport


@pytest.mark.parametrize(
    "days,role,root",
    [
        (None, "owner", False),
        (None, "viewer", False),
        (None, "viewer", True),
        (0, "viewer", True),
        (0, "contributor", False),
        (-1, "maintainer", False),
        (2000, "owner", False),
    ],
)
def test_installed_server_history_contract(tmp_path, monkeypatch, days, role, root):
    python = os.environ.get("NAURO_SERVER_PYTHON")
    if not python:
        pytest.skip("NAURO_SERVER_PYTHON is required for the paired server check")
    server = Path(os.environ.get("NAURO_SERVER_ROOT", str(Path(python).absolute().parents[2])))
    script = Path(__file__).with_name("history_server_probe.py").read_text()
    completed = subprocess.run(
        [python, "-c", script],
        cwd=server,
        env={**os.environ, "AWS_DEFAULT_REGION": "us-east-1"},
        input=json.dumps([days, role, root, None]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    wire = json.loads(completed.stdout)
    identity = GenerationProjectionIdentity.model_validate(wire["identity"])
    binding = ResolvedProjectBinding(
        tmp_path, identity.project_id, "Paired", "cloud", "https://mcp.nauro.ai"
    )
    target = GenerationProjectionTarget(binding, identity)
    monkeypatch.setattr(
        transport,
        "read_active_credentials",
        lambda: ActiveCredentials(identity.installed_for_user_id, "test-token"),
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        sent = subprocess.run(
            [python, "-c", script],
            cwd=server,
            env={**os.environ, "AWS_DEFAULT_REGION": "us-east-1"},
            input=json.dumps([days, role, root, requests[-1]]),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert sent.returncode == 0, sent.stderr
        received = json.loads(sent.stdout)
        assert received["identity"] == wire["identity"]
        return httpx.Response(200, content=base64.b64decode(received["response"], validate=True))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = transport.HttpHistoryTransport(binding.server_url, client).fetch(target, days)
    assert len(requests) == 1
    assert result.matches(identity, days) is True
    if role == "viewer":
        assert "questions-provenance.json" not in result.text
    if root and days is None:
        assert result.diff == "Not enough snapshots for diff."
    elif root:
        assert result.baseline.generation_id == identity.generation_id
    elif days is None:
        assert "Removed file: decisions/001-deleted.md" in result.diff
    if days == 2000:
        assert result.selection == "oldest_fallback"
