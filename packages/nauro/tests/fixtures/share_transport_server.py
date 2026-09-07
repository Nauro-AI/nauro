"""Run with the paired server interpreter and its synthetic storage fixtures."""

import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from tests.test_authenticated_client_recovery import _tls_context
from tests.test_judgment_staging import _object_inventory, _scan_table
from tests.test_share_transport import USER_ID, _body, _post, transport

from tests.conftest import TEST_PROJECT_ID

__all__ = ["transport"]

_CLIENT = """
import json, os, ssl, sys
import httpx
from nauro.store.share_contract import ShareTransportError, share_payload
from nauro.store.share_records import prepare_share_submission, list_share_submissions
from nauro.sync.share_transport import HttpShareTransport
from nauro.sync.share_submission import submit_share, recover_share, retry_share
endpoint, certificate, project, user, mode = sys.argv[1:]
with httpx.Client(verify=ssl.create_default_context(cafile=certificate), trust_env=False) as client:
    transport = HttpShareTransport(endpoint,client)
    if mode == 'send':
        payload=share_payload('brief','Frozen share','brief','Exact context')
        record=prepare_share_submission(project,user,payload)
        try:
            result=submit_share(record.scope,transport)
        except ShareTransportError:
            os._exit(17)
    else:
        record,=list_share_submissions(project,user)
        result=(retry_share if mode == 'retry' else recover_share)(record.scope,transport)
    print(result.model_dump_json())
"""


@pytest.mark.parametrize(
    "scenario", ["commit_drop", "absent_drop", "conflict_drop", "conflict_saved"]
)
def test_share_client_restart_over_tls(transport, s3_bucket, tmp_path, scenario):
    context, certificate = _tls_context(transport.key, tmp_path)
    calls, receipts, failures = [], [], []
    if scenario.startswith("conflict"):
        changed = _post(
            transport, "submit", _body(content="Intervening share", operation_id="intervening")
        )
        assert changed.status_code == 200
        assert changed.json()["status"] == "committed"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append((self.path, json.loads(body)))
                if len(calls) == 1 and scenario == "absent_drop":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                response = transport.client.post(
                    self.path,
                    content=body,
                    headers={"Authorization": self.headers["Authorization"]},
                )
                assert response.status_code == 200, response.text
                value = response.json()
                if value["status"] == "committed":
                    receipts.append(value["receipt_json"])
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

    home = tmp_path / "share-client-home"
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
        ]

        def invoke(mode):
            result = subprocess.run(command + [mode], env=env, capture_output=True, timeout=30)
            assert failures == []
            return result

        try:
            sent = invoke("send")
            assert sent.returncode == (17 if scenario.endswith("drop") else 0), sent.stderr.decode()
            if scenario == "conflict_saved":
                assert json.loads(sent.stdout)["status"] == "slug_conflict_observed"
                assert json.loads(sent.stdout)["unresolved"] is True
            before_rows = _scan_table()
            before_objects = _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/")
            recovered = invoke("recover")
            assert recovered.returncode == 0, recovered.stderr.decode()
            assert _scan_table() == before_rows
            assert _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/") == before_objects
            assert [path for path, _ in calls] == ["/share/submit", "/share/lookup"]
            if scenario.startswith("conflict"):
                assert json.loads(recovered.stdout)["status"] == "slug_conflict_observed"
                assert json.loads(recovered.stdout)["unresolved"] is True
                retried = invoke("retry")
                assert retried.returncode == 0, retried.stderr.decode()
                assert json.loads(retried.stdout) == json.loads(recovered.stdout)
                assert [path for path, _ in calls] == [
                    "/share/submit",
                    "/share/lookup",
                    "/share/lookup",
                ]
                assert _scan_table() == before_rows
                assert (
                    _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/")
                    == before_objects
                )
                return
            if scenario == "absent_drop":
                assert json.loads(recovered.stdout)["status"] == "absent"
                assert json.loads(recovered.stdout)["unresolved"] is True
                recovered = invoke("retry")
                assert recovered.returncode == 0, recovered.stderr.decode()
                assert [path for path, _ in calls] == [
                    "/share/submit",
                    "/share/lookup",
                    "/share/lookup",
                    "/share/submit",
                ]
            assert len({json.dumps(body, sort_keys=True) for _, body in calls}) == 1
            assert json.loads(recovered.stdout)["receipt_json"] == receipts[0]
            rows = _scan_table()
            objects = _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/")
            count = len(calls)
            cached = invoke("recover")
            assert cached.returncode == 0, cached.stderr.decode()
            assert json.loads(cached.stdout) == json.loads(recovered.stdout)
            assert len(calls) == count
            assert _scan_table() == rows
            assert _object_inventory(s3_bucket, f"generations/{TEST_PROJECT_ID}/") == objects
        finally:
            server.shutdown()
            thread.join(timeout=5)
