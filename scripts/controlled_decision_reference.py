"""One explicit isolated MCP call with evidence, using the installed decision tool."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from nauro.auth import ActiveCredentials
from nauro.mcp.decision_reference import bind_decision_reference
from nauro.mcp.stdio_server import mcp
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.decision_reference_contract import _json, validate_arguments


def credentials(path: Path) -> ActiveCredentials:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Credentials must be an owner-only regular file")
        value = _json(stream.read(65537))
    if set(value) != {"user_id", "access_token"} or not all(
        isinstance(v, str) and v for v in value.values()
    ):
        raise ValueError("Invalid credentials")
    return ActiveCredentials(**value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--discard-response", action="store_true")
    args = parser.parse_args()
    manifest = _json(args.manifest.read_bytes())
    if (
        set(manifest) != {"endpoint", "project_id", "actor_id", "isolated"}
        or manifest["isolated"] is not True
    ):
        parser.error("Use the reviewed isolated execution manifest")
    # This driver is specific to the existing probe, never a production URL selector.
    if manifest["endpoint"] != "https://decision-probe.nauro.ai/mcp":
        parser.error("The manifest must select the isolated probe endpoint")
    raw = args.request.read_bytes()
    request = _json(raw)
    mode = validate_arguments(request, manifest["project_id"])
    if args.discard_response and mode != "submit":
        parser.error("Response discard is limited to explicit submit")
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "prepared_only",
                    "request_sha256": hashlib.sha256(raw).hexdigest(),
                    "mode": mode,
                    "network_called": False,
                }
            )
        )
        return 0
    if args.credentials is None:
        parser.error("Execution requires dedicated credential-file access")
    credential_path = args.credentials
    evidence = {
        "manifest": manifest,
        "request": request,
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "started_outcome_unknown",
        "discard_response": args.discard_response,
    }
    fd = os.open(args.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(evidence) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        started = time.monotonic()
        trace = []

        def record_http(response: httpx.Response) -> None:
            body = _json(response.request.content)
            trace.append(
                {
                    "method": body["method"],
                    "rpc_id": body.get("id"),
                    "http_status": response.status_code,
                    "aws_request_id": response.headers.get("x-amzn-requestid"),
                    "gateway_request_id": response.headers.get("apigw-requestid"),
                }
            )

        try:
            with httpx.Client(event_hooks={"response": [record_http]}) as http:
                transport = DecisionReferenceTransport(
                    manifest["endpoint"],
                    manifest["project_id"],
                    manifest["actor_id"],
                    http,
                    lambda: credentials(credential_path),
                )
                transport.initialize()
                initialized = time.monotonic()
                bind_decision_reference(mcp, transport)
                tool = mcp._tool_manager.get_tool("propose_decision")
                assert tool is not None
                result = asyncio.run(tool.run(request))
            outcome = {
                "status": "verified",
                "result": result,
                "elapsed_seconds": time.monotonic() - started,
                "initialization_seconds": initialized - started,
                "tool_seconds": time.monotonic() - initialized,
                "http_trace": trace,
            }
            stream.write(json.dumps(outcome) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            if args.discard_response:
                print("Controlled response discard. Discover and recover; do not resubmit.")
                return 3
            print(json.dumps(result))
            return 0
        except Exception:
            stream.write(
                json.dumps(
                    {
                        "status": "no_verified_result",
                        "elapsed_seconds": time.monotonic() - started,
                        "http_trace": trace,
                    }
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
            print(
                "No verified result. Keep evidence and use discovery or recovery. "
                "No retry was sent."
            )
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
