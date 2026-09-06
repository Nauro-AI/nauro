"""Emit real authenticated server history responses for paired client verification."""

from __future__ import annotations

import base64
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from tests.conftest import s3_bucket
from tests.test_generation_history_selection import LIMITS, publish_chain
from tests.test_generation_history_service import FILES, TIMES, request_for
from tests.test_generation_reads import USER_ID, _member, _table


def main():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mcp_server import auth
    from mcp_server.authz import Role
    from mcp_server.generation_history_transport import history_router
    from mcp_server.generation_projection import serve_projection

    days, role_name, root, request_body = json.loads(sys.stdin.read())
    with contextmanager(s3_bucket.__wrapped__)():
        pointers = publish_chain(TIMES[:1] if root else TIMES, FILES[:1] if root else FILES)
        pointer = pointers[-1]
        role = Role(role_name)
        _member(role_name)
        _table().put_item(
            Item={"pk": "IDENTITY#history-paired", "sk": "IDENTITY", "user_id": USER_ID}
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(
            {
                "sub": "history-paired",
                "scope": "read:context",
                "aud": auth.AUTH0_AUDIENCE,
                "iss": f"https://{auth.AUTH0_DOMAIN}/",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            key,
            algorithm="RS256",
        )
        jwks = SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
        )
        app = FastAPI()
        app.include_router(history_router(limits=LIMITS))
        with patch.object(auth, "get_jwks_client", return_value=jwks), TestClient(app) as client:
            response = client.post(
                "/generations/history",
                json=request_for(pointer, role, days).model_dump()
                if request_body is None
                else request_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, response.text
        served = serve_projection(pointer.project_id, pointer, user_id=USER_ID, role=role)
        print(
            json.dumps(
                {
                    "identity": served.identity,
                    "response": base64.b64encode(response.content).decode(),
                }
            )
        )


if __name__ == "__main__":
    main()
