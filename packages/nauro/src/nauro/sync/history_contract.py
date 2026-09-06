"""Closed hosted history response verified against an installed projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.provenance import validate_utc_timestamp
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from nauro.store.generation_projection import GenerationProjectionIdentity, _digest

SelectionKind = Literal["root", "latest_two", "cutoff", "oldest_fallback"]

MAX_DIFF_CHARS = 12000
MAX_TEXT_CHARS = 13000
MAX_RESPONSE_BYTES = 170000


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    @field_validator("*", mode="before")
    @classmethod
    def _scalar(cls, value: object, info: ValidationInfo) -> object:
        name = info.field_name
        if name == "version" and type(value) is not int:
            raise ValueError("Version must be an integer.")
        if name in {
            "project_id",
            "authenticated_user_id",
            "generation_id",
        }:
            return validate_identifier(IdentifierKind.ulid, value, field=name)
        if name in {
            "manifest_digest",
            "projection_scope_id",
        }:
            return _digest(value) if isinstance(value, str) else _bad_digest()
        if name == "committed_at":
            return validate_utc_timestamp(value, field=name)
        return value


class HistoryStamp(_ClosedModel):
    generation_id: str
    committed_at: str


class HistoryAuthority(HistoryStamp):
    kind: Literal["generation"]
    project_id: str
    manifest_digest: str
    freshness: Literal["authorized_at_read"]


class HistoryResponse(_ClosedModel):
    version: int = Field(ge=1, le=1)
    store: Literal["remote"]
    authenticated_user_id: str
    projection_class: Literal["viewer", "contributor_plus"]
    projection_scope_id: str
    read_authority: HistoryAuthority
    baseline: HistoryStamp | None
    selection: SelectionKind
    requested_days: int | None
    cutoff_date_used: str | None
    diff: str = Field(max_length=MAX_DIFF_CHARS)
    text: str = Field(max_length=MAX_TEXT_CHARS)

    @field_validator("cutoff_date_used")
    @classmethod
    def _cutoff(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = datetime.fromisoformat(value)
            if len(value) > 40 or parsed.tzinfo != timezone.utc or parsed.isoformat() != value:
                raise ValueError("History cutoff is not canonical UTC.")
        return value

    @model_validator(mode="after")
    def _selection(self) -> HistoryResponse:
        if (self.selection == "root") != (self.baseline is None):
            raise ValueError("History baseline and selection disagree.")
        if (self.selection in {"cutoff", "oldest_fallback"}) != (self.cutoff_date_used is not None):
            raise ValueError("History cutoff and selection disagree.")
        if (self.requested_days is None) != (self.cutoff_date_used is None):
            raise ValueError("History days and cutoff disagree.")
        if (
            self.selection == "latest_two"
            and self.baseline is not None
            and self.baseline.generation_id == self.read_authority.generation_id
        ):
            raise ValueError("Latest-two history needs distinct generations.")
        if (
            self.selection == "cutoff"
            and self.baseline is not None
            and self.cutoff_date_used is not None
            and datetime.fromisoformat(self.baseline.committed_at.replace("Z", "+00:00"))
            > datetime.fromisoformat(self.cutoff_date_used)
        ):
            raise ValueError("History baseline exceeds the cutoff.")
        return self

    def matches(self, identity: GenerationProjectionIdentity, days: int | None) -> bool:
        return (
            self.authenticated_user_id == identity.installed_for_user_id
            and self.read_authority.project_id == identity.project_id
            and self.read_authority.generation_id == identity.generation_id
            and self.read_authority.manifest_digest == identity.manifest_digest
            and self.read_authority.committed_at == identity.committed_at
            and self.projection_class == identity.projection_class
            and self.projection_scope_id == identity.projection_scope_id
            and self.requested_days == days
        )


def _bad_digest() -> str:
    raise ValueError("History digest must be text.")
