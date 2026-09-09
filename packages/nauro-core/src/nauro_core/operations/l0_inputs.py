"""Capture the immutable inputs used by the existing L0 renderer."""

from __future__ import annotations

from collections.abc import Mapping

from nauro_core.operations.get_context import get_context


class _CapturedStore:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)
        self.captured: dict[str, str] = {}

    def read_file(self, path: str) -> str | None:
        body = self.files.get(path)
        if body is not None:
            self.captured[path] = body
        return body

    def list_decisions(self) -> list[str]:
        return sorted(
            path[10:-3]
            for path in self.files
            if path.startswith("decisions/") and path.endswith(".md")
        )

    def write_file(self, path: str, content: str) -> None:
        raise TypeError("L0 capture is read-only")

    def delete_file(self, path: str) -> None:
        raise TypeError("L0 capture is read-only")

    def read_decision(self, stem: str) -> str | None:
        return self.read_file(f"decisions/{stem}.md")

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        return {stem: self.read_decision(stem) for stem in stems}


def capture_l0_inputs(files: Mapping[str, bytes]) -> dict[str, str]:
    store = _CapturedStore({path: body.decode("utf-8") for path, body in files.items()})
    get_context(store, 0)
    return dict(sorted(store.captured.items()))
