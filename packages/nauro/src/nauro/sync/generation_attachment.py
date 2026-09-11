"""Explicit initial attachment through generation authority and credentials."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
from filelock import FileLock
from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI, ActiveCredentials
from nauro.store.generation_authority import RefreshRequiredError
from nauro.store.generation_installation import install_generation_root, publish_generation_control
from nauro.store.home import ensure_nauro_home, nauro_home
from nauro.store.registry import bind_project_store_v2, get_project_entry_v2, get_store_path_v2
from nauro.store.replica_control import _validate_managed_path
from nauro.store.repo_config import load_repo_config, repo_config_path, save_repo_config
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_acquisition import acquire_generation_projection
from nauro.sync.generation_connection import attachment_connection
from nauro.sync.generation_credentials import (
    GenerationAuth,
    GenerationConnection,
    generation_credentials,
)
from nauro.sync.generation_refresh import (
    commit_generation_refresh,
    prepare_initial_generation_refresh,
)
from nauro.sync.generation_session import GenerationConnectionError, GenerationTransferSession
from nauro.sync.reference_oauth import response_json
from nauro.sync.remote import TransferSession


class InitialAttachmentSession(GenerationTransferSession):
    def __init__(
        self,
        binding: ResolvedProjectBinding,
        repo: Path,
        connection: GenerationConnection,
        client: httpx.Client,
    ) -> None:
        self.binding, self.connection, self.repo = binding, connection, repo
        self.entry = get_project_entry_v2(binding.project_id)
        self.repo_config = self._repo_config()
        if self.repo_config is not None:
            config = load_repo_config(repo)
            if (
                config.get("id") != binding.project_id
                or config.get("server_url") != binding.server_url
            ):
                raise GenerationConnectionError("Repository association conflicts with attachment.")
        store = connection.store()
        with store.locked():
            record = store.read()
            if record is None:
                raise GenerationConnectionError("Generation login required.")
            self.actor = record.user_id
            self.revision = record.revision
        self.credentials()
        TransferSession.__init__(self, client)

    def _repo_config(self) -> bytes | None:
        path = repo_config_path(self.repo)
        _validate_managed_path(self.repo, path)
        return path.read_bytes() if path.exists() else None

    def require_binding(self, binding: ResolvedProjectBinding) -> None:
        super().require_binding(binding)
        response = response_json(
            self.client,
            "GET",
            self.api_url + "/projects?project_id=" + binding.project_id,
            headers={"Authorization": "Bearer " + self.credentials().access_token},
        )
        self.credentials()
        projects = response.get("projects")
        if (
            response.get("authority") != "generation_owner"
            or not isinstance(projects, list)
            or not any(
                isinstance(item, dict)
                and item.get("project_id") == binding.project_id
                and item.get("role") == "owner"
                for item in projects
            )
        ):
            raise GenerationConnectionError("Initial attachment requires current owner access.")

    def credentials(self) -> ActiveCredentials:
        if (
            attachment_connection(DEFAULT_AUTH_REDIRECT_URI) != self.connection
            or get_project_entry_v2(self.binding.project_id) != self.entry
            or self._repo_config() != self.repo_config
        ):
            raise GenerationConnectionError("The attachment connection changed.")
        try:
            credentials = generation_credentials(self.connection, self.actor)
            store = self.connection.store()
            with store.locked():
                record = store.read()
                if record is None or record.revision != self.revision:
                    raise ValueError("The captured credentials changed")
            return credentials
        except (ValueError, OSError) as exc:
            raise GenerationConnectionError("Generation login or renewal required.") from exc


def _empty_destination(path: Path) -> None:
    _validate_managed_path(nauro_home(), path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise RefreshRequiredError(
            "Initial attachment requires an empty destination. Existing stores and interrupted "
            "replica evidence are preserved. Use explicit sync for an installed replica; "
            "incomplete attachment requires recovery before another attach."
        )


def attach_generation(
    project: str, repo: Path, present_url: Callable[[str], None]
) -> ResolvedProjectBinding:
    validate_identifier(IdentifierKind.ulid, project, field="project")
    _validate_managed_path(repo, repo_config_path(repo))
    ensure_nauro_home()
    lock = nauro_home() / f"generation-attachment-{project}.lock"
    _validate_managed_path(nauro_home(), lock)
    with FileLock(lock, timeout=0):
        return _attach(project, repo, present_url)


def _attach(project: str, repo: Path, present_url: Callable[[str], None]) -> ResolvedProjectBinding:
    connection = attachment_connection(DEFAULT_AUTH_REDIRECT_URI)
    endpoint = connection.endpoint.removesuffix("/mcp")
    path = get_store_path_v2(project)
    entry = get_project_entry_v2(project)
    if entry and (entry.mode != "cloud" or entry.server_url != endpoint or entry.has_store_path):
        raise GenerationConnectionError("The registered project conflicts with initial attachment.")
    _empty_destination(path)
    binding = ResolvedProjectBinding(
        path, project, entry.name if entry else project, "cloud", endpoint
    )
    config_path = repo_config_path(repo)
    prior_config = config_path.read_bytes() if config_path.exists() else None
    with httpx.Client(trust_env=False) as client:
        auth = GenerationAuth(connection, project, client)
        if auth.status() != "active":
            auth.login(present_url)
        if (
            get_project_entry_v2(project) != entry
            or (config_path.read_bytes() if config_path.exists() else None) != prior_config
        ):
            raise GenerationConnectionError("The project association changed during login.")
        with InitialAttachmentSession(binding, repo, connection, client) as session:
            projection = acquire_generation_projection(
                binding, active_user_id=session.actor, session=session
            )
            session.require_binding(binding)
            _empty_destination(path)
            path.mkdir(parents=True, exist_ok=True)
            installed = install_generation_root(projection, timeout=0)
            publish_generation_control(installed, timeout=0, session=session)
            prepared = prepare_initial_generation_refresh(
                binding, actor=session.actor, session=session
            )
            if prepared.projection.target != projection.target:
                raise RefreshRequiredError("The generation changed during initial attachment.")
            commit_generation_refresh(prepared, session=session)
            session.require_binding(binding)
            bind_project_store_v2(
                project_id=project,
                name=binding.display_name,
                mode="cloud",
                repo_path=repo,
                store_path=path,
                server_url=endpoint,
            )
            save_repo_config(
                repo,
                {
                    "mode": "cloud",
                    "id": project,
                    "name": binding.display_name,
                    "server_url": endpoint,
                },
            )
    return binding
