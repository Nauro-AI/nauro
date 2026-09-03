"""Private result types for sync path admission."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path


class _PathClass(Enum):
    ORDINARY = auto()
    RESERVED_CONTROL = auto()
    UNSAFE = auto()


class _UnsafeReason(Enum):
    RAW_ABSOLUTE = auto()
    RAW_ROOTED = auto()
    RAW_DRIVE = auto()
    RAW_UNC = auto()
    RAW_DEVICE = auto()
    RAW_PARENT = auto()
    EMPTY_PATH = auto()
    FOLDED_EMPTY = auto()
    FOLDED_DOT = auto()
    FOLDED_PARENT = auto()
    OUTSIDE_STORE = auto()
    OBSERVATION_LOST = auto()
    WINDOWS_NAME_LOOKUP_FAILED = auto()
    METADATA_UNAVAILABLE = auto()
    LINK_TARGET_UNREADABLE = auto()
    UNSUPPORTED_REPARSE = auto()
    LINK_LOOP = auto()
    LINK_HOP_LIMIT = auto()
    NON_DIRECTORY_PARENT = auto()


class _MissingPathPolicy(Enum):
    OBSERVED = auto()
    OPTIONAL_FIXED_LEAF = auto()
    CREATE_DESTINATION = auto()


class _NativeKind(Enum):
    DIRECTORY = auto()
    REGULAR_FILE = auto()
    IRREGULAR = auto()


@dataclass(frozen=True)
class _SemanticPathView:
    raw_identity: str
    exact_components: tuple[str, ...]
    semantic_components: tuple[str, ...]


@dataclass(frozen=True)
class _PathAdmission:
    path_class: _PathClass
    raw_identity: str
    exists: bool | None = None
    missing_policy: _MissingPathPolicy | None = None
    native_kind: _NativeKind | None = None
    reason: _UnsafeReason | None = None

    def __post_init__(self) -> None:
        if self.path_class is _PathClass.UNSAFE:
            if self.reason is None:
                raise ValueError("An unsafe path requires a reason.")
        elif self.reason is not None:
            raise ValueError("Only an unsafe path can have a reason.")
        if self.native_kind is not None and not (
            self.path_class is _PathClass.ORDINARY and self.exists is True
        ):
            raise ValueError("Only an existing ordinary path can have a native kind.")
        if self.exists is False and self.missing_policy is None:
            raise ValueError("An absent path requires a missing policy.")


@dataclass(frozen=True)
class _PreparedStoreRoot:
    configured_root: Path
    canonical_root: Path
    native_anchor: str
    canonical_parts: tuple[str, ...]


@dataclass(frozen=True)
class _SafeWalkEntry:
    native_path: Path
    raw_relative_path: str
    admission: _PathAdmission


class _StoreRootPreparationError(Exception):
    def __init__(self) -> None:
        super().__init__("The Store root is unavailable.")


_REASON_TEXT = {
    _UnsafeReason.RAW_ABSOLUTE: "The path is absolute.",
    _UnsafeReason.RAW_ROOTED: "The path is rooted.",
    _UnsafeReason.RAW_DRIVE: "The path uses drive syntax.",
    _UnsafeReason.RAW_UNC: "The path uses UNC syntax.",
    _UnsafeReason.RAW_DEVICE: "The path uses device syntax.",
    _UnsafeReason.RAW_PARENT: "The path contains a parent component.",
    _UnsafeReason.EMPTY_PATH: "The path has no usable component.",
    _UnsafeReason.FOLDED_EMPTY: "A path component normalizes to empty.",
    _UnsafeReason.FOLDED_DOT: "A path component normalizes to dot.",
    _UnsafeReason.FOLDED_PARENT: "A path component normalizes to parent.",
    _UnsafeReason.OUTSIDE_STORE: "The path resolves outside the Store root.",
    _UnsafeReason.OBSERVATION_LOST: "The path changed after it was observed.",
    _UnsafeReason.WINDOWS_NAME_LOOKUP_FAILED: "The Windows long-name lookup failed.",
    _UnsafeReason.METADATA_UNAVAILABLE: "Path metadata is unavailable.",
    _UnsafeReason.LINK_TARGET_UNREADABLE: "The link target is unavailable.",
    _UnsafeReason.UNSUPPORTED_REPARSE: "The path uses an unsupported reparse point.",
    _UnsafeReason.LINK_LOOP: "The path contains a link loop.",
    _UnsafeReason.LINK_HOP_LIMIT: "The path exceeds the link hop limit.",
    _UnsafeReason.NON_DIRECTORY_PARENT: ("A non-directory path has a remaining component."),
}


def _unsafe_reason_text(reason: _UnsafeReason) -> str:
    return _REASON_TEXT[reason]


def _escape_path_for_display(raw_path: str) -> str:
    escaped: list[str] = []
    for character in raw_path:
        value = ord(character)
        if character == " ":
            escaped.append(r"\x20")
        elif character == "\n":
            escaped.append(r"\n")
        elif character == "\r":
            escaped.append(r"\r")
        elif character == "\\":
            escaped.append(r"\\")
        elif character == "'":
            escaped.append(r"\x27")
        elif character == '"':
            escaped.append(r"\x22")
        elif value < 0x20 or value == 0x7F:
            escaped.append(f"\\x{value:02X}")
        elif 0xDC80 <= value <= 0xDCFF:
            escaped.append(f"\\x{value - 0xDC00:02X}")
        elif 0xD800 <= value <= 0xDFFF:
            escaped.append(f"\\u{value:04X}")
        elif value > 0x7F:
            escaped.extend(f"\\x{byte:02X}" for byte in character.encode("utf-8"))
        else:
            escaped.append(character)
    return "".join(escaped)
