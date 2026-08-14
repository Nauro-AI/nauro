import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

TIMEOUT_SECONDS = 30
EXPECTED_CONTEXT_MARKER = "wheel-smoke project resolved through installed wheel"
EXPECTED_TOOLS = {
    "check_decision",
    "diff_since_last_session",
    "flag_question",
    "get_context",
    "get_decision",
    "get_raw_file",
    "list_decisions",
    "propose_decision",
    "search_decisions",
    "update_state",
}


class SmokeFailure(RuntimeError):
    pass


class StdioSession:
    def __init__(self, executable: str, cwd: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            [executable, "serve", "--stdio"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.stdout_lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.stdout_reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_reader.start()
        self.stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout_lines.put(line)
        self.stdout_lines.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        self.stderr_lines.extend(self.process.stderr)

    def send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise SmokeFailure("stdio server stdin is unavailable")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        response = self._receive(request_id)
        if "error" in response:
            raise SmokeFailure(f"{method} returned an error: {response['error']!r}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise SmokeFailure(f"{method} returned an invalid result: {result!r}")
        return result

    def _receive(self, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(f"timed out waiting for JSON-RPC response {request_id}")
            try:
                line = self.stdout_lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise SmokeFailure(
                    f"timed out waiting for JSON-RPC response {request_id}"
                ) from exc
            if line is None:
                raise SmokeFailure(f"stdio server exited before JSON-RPC response {request_id}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SmokeFailure(f"stdio server emitted non-JSON output: {line!r}") from exc
            if not isinstance(message, dict):
                raise SmokeFailure(f"stdio server emitted an invalid message: {message!r}")
            if message.get("id") == request_id:
                return message
            if "id" in message:
                raise SmokeFailure(
                    f"expected JSON-RPC response {request_id}, got {message.get('id')!r}"
                )

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            return_code = self.process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            self.process.kill()
            self.process.wait(timeout=TIMEOUT_SECONDS)
            raise SmokeFailure("stdio server did not exit after stdin closed") from exc
        self.stdout_reader.join(timeout=TIMEOUT_SECONDS)
        self.stderr_reader.join(timeout=TIMEOUT_SECONDS)
        if self.stdout_reader.is_alive() or self.stderr_reader.is_alive():
            raise SmokeFailure("stdio reader did not stop after the server exited")
        if return_code != 0:
            raise SmokeFailure(f"stdio server exited with status {return_code}")

    def stderr(self) -> str:
        return "".join(self.stderr_lines).strip()


def initialize_project(executable: str, cwd: Path, env: dict[str, str]) -> None:
    try:
        completed = subprocess.run(
            [executable, "init", "wheel-smoke"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure("nauro init timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeFailure(f"nauro init failed with status {completed.returncode}: {detail}")
    try:
        completed = subprocess.run(
            [executable, "update-state", EXPECTED_CONTEXT_MARKER],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure("nauro update-state timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeFailure(
            f"nauro update-state failed with status {completed.returncode}: {detail}"
        )


def run_smoke(executable: str) -> None:
    nauro_home_value = os.environ.get("NAURO_HOME")
    if not nauro_home_value:
        raise SmokeFailure("NAURO_HOME must point to a clean test directory")
    nauro_home = Path(nauro_home_value)
    nauro_home.mkdir(parents=True, exist_ok=True)
    if any(nauro_home.iterdir()):
        raise SmokeFailure("NAURO_HOME must be empty before the smoke test")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="nauro-wheel-smoke-") as workspace_value:
        workspace = Path(workspace_value)
        initialize_project(executable, workspace, env)
        session = StdioSession(executable, workspace, env)
        failure: Exception | None = None
        try:
            initialize_result = session.request(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "nauro-wheel-smoke", "version": "1"},
                },
            )
            if initialize_result.get("protocolVersion") != "2025-11-25":
                raise SmokeFailure(
                    "initialize did not negotiate legacy protocol version 2025-11-25"
                )
            if not isinstance(initialize_result.get("serverInfo"), dict):
                raise SmokeFailure("initialize result is missing serverInfo")

            session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools_result = session.request(2, "tools/list", {})
            tools = tools_result.get("tools")
            if not isinstance(tools, list):
                raise SmokeFailure("tools/list result is missing tools")
            tool_names = {
                tool.get("name") for tool in tools if isinstance(tool, dict) and tool.get("name")
            }
            if len(tools) != len(EXPECTED_TOOLS) or tool_names != EXPECTED_TOOLS:
                raise SmokeFailure(
                    f"tools/list returned {sorted(tool_names)!r}; "
                    f"expected {sorted(EXPECTED_TOOLS)!r}"
                )

            context_result = session.request(
                3,
                "tools/call",
                {
                    "name": "get_context",
                    "arguments": {"cwd": str(workspace), "level": "L0"},
                },
            )
            if context_result.get("isError") is True:
                raise SmokeFailure(f"get_context returned an error: {context_result!r}")
            content = context_result.get("content")
            context_text = (
                "\n".join(
                    item["text"]
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                )
                if isinstance(content, list)
                else ""
            )
            if EXPECTED_CONTEXT_MARKER not in context_text:
                raise SmokeFailure(
                    "get_context did not return the state seeded for the wheel-smoke project: "
                    f"{content!r}"
                )
        except Exception as exc:
            failure = exc
        try:
            session.close()
        except Exception as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            stderr = session.stderr()
            detail = f"\nstdio server stderr:\n{stderr}" if stderr else ""
            raise SmokeFailure(f"{failure}{detail}") from failure


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("nauro_executable")
    args = parser.parse_args()
    try:
        run_smoke(args.nauro_executable)
    except (OSError, SmokeFailure) as exc:
        print(f"nauro wheel stdio smoke failed: {exc}", file=sys.stderr)
        return 1
    print("nauro wheel stdio smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
