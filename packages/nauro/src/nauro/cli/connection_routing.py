"""Route ``nauro attach PROJECT`` by local evidence and the hosted project's authority.

Runs after the attach refusals and before the legacy block, only without
``--generation``. Local states the legacy block already owns, and every discovery
answer other than a positive generation owner proof, leave the legacy block to run
unchanged. Once generation authority is proven the legacy block never runs: a saved
conversion goes to the guided upgrade, retained initial-attachment evidence goes to
initial installation, a bound legacy copy goes to the guided upgrade and every other
destination goes to initial installation, which refuses what it cannot install.
Generation imports stay inside those branches so plain legacy attach loads nothing
POSIX-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from nauro.cli.auth_presentation import present_login_url
from nauro.store.migration_admission import MigrationAdmissionError, inspect_migration
from nauro.store.resolution import DisconnectedProject, RepoResolution
from nauro.sync.generation_discovery import discover_project_authority

if TYPE_CHECKING:
    from nauro.store.registry import RegistryEntryV2
    from nauro.sync.generation_credentials import GenerationConnection

_UNREPORTED = (
    "A saved conversion exists for this project but the server does not report "
    "generation authority; owner recovery is required before this project can be attached."
)
_OTHER_SERVER = (
    "The registered connection for this project uses another server; owner recovery is required."
)


def _refuse(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _has_replica(store: Path) -> bool:
    try:
        (store / ".replica").lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _has_content(store: Path) -> bool:
    try:
        return store.exists() and (not store.is_dir() or any(store.iterdir()))
    except OSError:
        _refuse("Error: Cannot verify project write authority.")


def _has_retained_attachment(project_id: str) -> bool:
    from nauro.store.generation_authority import GenerationAuthorityError
    from nauro.sync.generation_attachment_record import read_record

    try:
        return read_record(project_id) is not None
    except (GenerationAuthorityError, OSError):
        return True


def _trusted_registration(
    project_id: str,
) -> tuple[GenerationConnection, str, RegistryEntryV2 | None]:
    from nauro.auth import DEFAULT_AUTH_REDIRECT_URI
    from nauro.store.registry import get_project_entry_v2
    from nauro.sync.generation_connection import attachment_connection

    connection = attachment_connection(DEFAULT_AUTH_REDIRECT_URI)
    endpoint = connection.endpoint.removesuffix("/mcp")
    entry = get_project_entry_v2(project_id)
    if entry is not None and (entry.mode != "cloud" or entry.server_url != endpoint):
        _refuse(_OTHER_SERVER)
    return connection, endpoint, entry


def route_attach(
    project_id: str,
    repo: Path,
    connection: RepoResolution | DisconnectedProject | None,
    store: Path,
) -> bool:
    """Return True when the connection flow completed the attach; False runs the legacy block.

    Every refusal and incomplete generation step exits with code 1.
    """
    try:
        record = inspect_migration(store)
    except MigrationAdmissionError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if isinstance(connection, DisconnectedProject):
        if record is not None:
            _refuse(f"Error: {connection.guidance}")
        return False
    if _has_replica(store):
        return False
    if discover_project_authority(project_id).authority != "generation":
        if record is not None:
            _refuse(_UNREPORTED)
        return False
    if record is not None:
        _upgrade(project_id, repo)
    elif _has_retained_attachment(project_id):
        _install(project_id, repo)
    elif connection is not None and _has_content(store):
        _upgrade(project_id, repo)
    else:
        _install(project_id, repo)
    return True


def _install(project_id: str, repo: Path) -> None:
    from filelock import Timeout

    from nauro.auth import PartialAuthConfigError
    from nauro.store.generation_authority import GenerationAuthorityError
    from nauro.sync.generation_attachment import attach_generation
    from nauro.sync.reference_auth import AUTH_ERRORS

    try:
        _trusted_registration(project_id)
        binding = attach_generation(project_id, repo, present_login_url)
    except (*AUTH_ERRORS, GenerationAuthorityError, PartialAuthConfigError, Timeout) as exc:
        typer.echo(f"Attachment incomplete: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Attached generation project '{binding.display_name}' to {repo.resolve()}")


def _upgrade(project_id: str, repo: Path) -> None:
    from functools import partial

    import httpx
    from filelock import Timeout

    from nauro.auth import PartialAuthConfigError
    from nauro.cli.generation_upgrade import guided_existing_hosted_upgrade
    from nauro.store.generation_authority import GenerationAuthorityError
    from nauro.store.registry import bind_project_store_v2, get_project_entry_v2
    from nauro.store.repo_config import repo_config_path, save_repo_config
    from nauro.store.resolution import resolve_project_binding
    from nauro.sync.generation_attachment import InitialAttachmentSession
    from nauro.sync.generation_credentials import GenerationAuth
    from nauro.sync.generation_session import GenerationConnectionError
    from nauro.sync.reference_auth import AUTH_ERRORS
    from nauro.sync.remote import TransferBoundaryError

    try:
        connection, endpoint, entry = _trusted_registration(project_id)
        binding = resolve_project_binding(project_id, None, use_cwd=False)
        config_path = repo_config_path(repo)
        prior_config = config_path.read_bytes() if config_path.exists() else None
        with httpx.Client(trust_env=False) as client:
            auth = GenerationAuth(connection, project_id, client)
            if auth.status() != "active":
                auth.login(present_login_url)
            if (
                get_project_entry_v2(project_id) != entry
                or (config_path.read_bytes() if config_path.exists() else None) != prior_config
            ):
                raise GenerationConnectionError("The project association changed during login.")
            session = InitialAttachmentSession(binding, repo, connection, client)
            with session:
                result = guided_existing_hosted_upgrade(
                    session, emit=typer.echo, confirm=partial(typer.confirm, default=False)
                )
                if result is None or result.phase != "completed":
                    raise typer.Exit(code=1)
                bind_project_store_v2(
                    project_id=project_id,
                    name=binding.display_name,
                    mode="cloud",
                    repo_path=repo,
                    store_path=binding.store_path,
                    server_url=endpoint,
                )
                save_repo_config(
                    repo,
                    {
                        "mode": "cloud",
                        "id": project_id,
                        "name": binding.display_name,
                        "server_url": endpoint,
                    },
                )
    except (
        *AUTH_ERRORS,
        GenerationAuthorityError,
        PartialAuthConfigError,
        TransferBoundaryError,
        Timeout,
    ) as exc:
        typer.echo(f"Upgrade incomplete: {exc}", err=True)
        raise typer.Exit(code=1) from exc
