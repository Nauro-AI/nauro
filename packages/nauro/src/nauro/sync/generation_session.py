"""Normal credentials for one resolved generation replica operation."""

from __future__ import annotations

import httpx

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI, ActiveCredentials, PartialAuthConfigError
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import ResolvedProjectBinding, resolve_project_binding
from nauro.sync.generation_connection import connection_for
from nauro.sync.generation_credentials import generation_credentials
from nauro.sync.remote import TransferSession


class GenerationConnectionError(GenerationAuthorityError):
    code = "generation_connection_unavailable"


class GenerationTransferSession(TransferSession):
    def __init__(self, binding: ResolvedProjectBinding, client: httpx.Client | None = None) -> None:
        self.binding = binding
        try:
            self.marker = observe_generation_marker(binding)
            self.connection = connection_for(binding, DEFAULT_AUTH_REDIRECT_URI)
            store = self.connection.store()
            with store.locked():
                record = store.read()
                if record is None:
                    raise ValueError("Generation login required")
                self.actor = record.user_id
            self.credentials()
        except (ValueError, OSError, PartialAuthConfigError) as exc:
            raise GenerationConnectionError(
                "Check generation login and project connection."
            ) from exc
        super().__init__(client or httpx.Client(trust_env=False))
        self._owned = client is None

    def __enter__(self) -> GenerationTransferSession:
        return self

    @property
    def api_url(self) -> str:
        return self.connection.endpoint.removesuffix("/mcp")

    def require_binding(self, binding: ResolvedProjectBinding) -> None:
        if binding != self.binding:
            raise GenerationConnectionError("The replica operation changed project binding.")
        self.credentials()

    def credentials(self) -> ActiveCredentials:
        try:
            binding = resolve_project_binding(self.binding.project_id, None, use_cwd=False)
            if (
                binding != self.binding
                or observe_generation_marker(binding) != self.marker
                or connection_for(binding, DEFAULT_AUTH_REDIRECT_URI) != self.connection
            ):
                raise ValueError("Generation connection changed")
            return generation_credentials(self.connection, self.actor)
        except (ValueError, OSError, PartialAuthConfigError) as exc:
            raise GenerationConnectionError(
                "Generation login or connection recovery required."
            ) from exc

    def require_actor(self, actor: str) -> None:
        if self.credentials().user_id != actor:
            raise GenerationConnectionError("The replica belongs to another account.")
