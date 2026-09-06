"""Installed client TLS conformance against the paired server source."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario", ["append_drop", "resolve_drop", "no_change_drop", "no_change_saved"]
)
def test_paired_question_transport(scenario):
    interpreter = os.environ.get("NAURO_SERVER_PYTHON")
    source = os.environ.get("NAURO_SERVER_ROOT")
    if not interpreter or not source:
        pytest.skip("Set NAURO_SERVER_PYTHON and NAURO_SERVER_ROOT for paired question transport.")
    root = Path(source).resolve()
    probe = Path(__file__).parent / "fixtures" / "question_transport_server.py"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
        "NAURO_CLIENT_PYTHON": sys.executable,
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    result = subprocess.run(
        [
            interpreter,
            "-m",
            "pytest",
            "-q",
            "--noconftest",
            "-c",
            str(root / "pyproject.toml"),
            "-p",
            "tests.conftest",
            f"{probe}::test_question_client_restart_over_tls[{scenario}]",
            "--tb=short",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "skipped" not in result.stdout
