"""Real TLS and process restart against the paired dormant question server."""

import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from tests.test_authenticated_client_recovery import _tls_context
from tests.test_judgment_staging import _object_inventory, _scan_table
from tests.test_question_transport import USER_ID, _body, transport

from tests.conftest import TEST_PROJECT_ID

__all__ = ["transport"]

_CLIENT = """
import json, os, ssl, sys
import httpx
from nauro.store.question_contract import (
    QuestionTransportError, question_payload, resolution_payload
)
from nauro.store.question_records import prepare_question_submission, list_question_submissions
from nauro.sync.question_transport import HttpQuestionTransport
from nauro.sync.question_submission import submit_question, recover_question, retry_question
endpoint, certificate, project, user, action, mode = sys.argv[1:]
with httpx.Client(verify=ssl.create_default_context(cafile=certificate), trust_env=False) as client:
    transport=HttpQuestionTransport(endpoint,client)
    if mode == 'send':
        payload=(question_payload('Frozen question?') if action == 'append'
                 else resolution_payload(('Q1',),'D42'))
        record=prepare_question_submission(project,user,payload)
        try:
            result=submit_question(record.scope,transport)
        except QuestionTransportError:
            os._exit(17)
    else:
        record,=list_question_submissions(project,user)
        result=(retry_question if mode == 'retry' else recover_question)(record.scope,transport)
    print(result.model_dump_json())
"""


@pytest.mark.parametrize(
    "scenario", ["append_drop", "resolve_drop", "no_change_drop", "no_change_saved"]
)
def test_question_client_restart_over_tls(transport, s3_bucket, tmp_path, scenario):
    context, certificate = _tls_context(transport.key, tmp_path)
    calls, receipts, failures = [], [], []
    observation = scenario.startswith("no_change")
    if observation:
        first = transport.client.post(
            "/questions/submit",
            json=_body("resolve", operation_id="previous-resolution"),
            headers={"Authorization": f"Bearer {transport.token}"},
        )
        assert first.status_code == 200, first.text

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append((self.path, json.loads(body)))
                response = transport.client.post(
                    self.path,
                    content=body,
                    headers={"Authorization": self.headers["Authorization"]},
                )
                assert response.status_code == 200, response.text
                if response.json()["status"] == "committed":
                    receipts.append(response.json()["receipt_json"])
                if len(calls) == 1 and scenario.endswith("drop"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                self.send_error(500)

    home = tmp_path / "question-client-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"auth": {"user_id": USER_ID, "access_token": transport.token}})
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["NAURO_HOME"] = str(home)
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        command = [
            os.environ["NAURO_CLIENT_PYTHON"],
            "-c",
            _CLIENT,
            f"https://localhost:{server.server_port}",
            str(certificate),
            TEST_PROJECT_ID,
            USER_ID,
            "append" if scenario.startswith("append") else "resolve",
        ]

        def invoke(mode):
            result = subprocess.run(command + [mode], env=env, capture_output=True, timeout=30)
            assert failures == []
            return result

        try:
            sent = invoke("send")
            assert sent.returncode == (17 if scenario.endswith("drop") else 0), sent.stderr.decode()
            if not scenario.endswith("drop"):
                assert json.loads(sent.stdout)["status"] == "no_change_observed"
            rows = _scan_table()
            objects = _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/")
            recovered = invoke("recover")
            assert recovered.returncode == 0, recovered.stderr.decode()
            assert [path for path, _ in calls] == ["/questions/submit", "/questions/lookup"]
            assert _scan_table() == rows
            assert _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/") == objects
            if observation:
                assert json.loads(recovered.stdout)["status"] == "absent"
                assert json.loads(recovered.stdout)["unresolved"] is True
                retry = invoke("retry")
                assert retry.returncode == 0, retry.stderr.decode()
                assert json.loads(retry.stdout)["status"] == "no_change_observed"
                assert json.loads(retry.stdout)["unresolved"] is True
                assert [path for path, _ in calls] == [
                    "/questions/submit",
                    "/questions/lookup",
                    "/questions/lookup",
                    "/questions/submit",
                ]
                assert _scan_table() == rows
                assert _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/") == objects
            else:
                assert json.loads(recovered.stdout)["receipt_json"] == receipts[0]
                count = len(calls)
                cached = invoke("recover")
                assert cached.returncode == 0, cached.stderr.decode()
                assert json.loads(cached.stdout) == json.loads(recovered.stdout)
                assert len(calls) == count
            assert len({json.dumps(body, sort_keys=True) for _, body in calls}) == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)
