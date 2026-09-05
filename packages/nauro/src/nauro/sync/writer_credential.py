from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path


def writer_headers(project_id: str, api_url: str) -> dict[str, str]:
    filename = os.environ.get("NAURO_WRITER_CREDENTIAL_FILE")
    if not filename:
        return {}
    try:
        with Path(filename).open() as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError
            credential = json.load(handle)
        if (
            not isinstance(credential, dict)
            or set(credential) != {"project_id", "api_url", "token"}
            or not all(isinstance(value, str) for value in credential.values())
            or not re.fullmatch(r"[0-9a-f]{64}", credential["token"])
        ):
            raise ValueError
        if credential["project_id"] != project_id:
            return {}
        if credential["api_url"].rstrip("/") != api_url.rstrip("/"):
            raise ValueError
        return {"X-Nauro-Writer-Token": credential["token"]}
    except (OSError, ValueError, TypeError, KeyError):
        from nauro.sync.remote import PresignError

        raise PresignError("Writer credential is unavailable or invalid.") from None
