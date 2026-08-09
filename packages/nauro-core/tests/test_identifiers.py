"""Tests for nauro_core.identifiers - the shared identifier validators."""

from __future__ import annotations

import pytest

from nauro_core.identifiers import (
    IdentifierKind,
    InvalidIdentifier,
    is_identifier,
    validate_identifier,
)

VALID_ULID = "01KQ6AZGNA0B3QBF67NBXP3S45"
ULID_BODY = VALID_ULID[1:]

VALID_BY_KIND = {
    IdentifierKind.ulid: VALID_ULID,
    IdentifierKind.operation_id: "op-1",
    IdentifierKind.server_token: "update_stack",
    IdentifierKind.audit_target_id: "target-1",
    IdentifierKind.brief_slug: "auth-cutover-2",
    IdentifierKind.stack_revision: "0" * 64,
}


class TestUlid:
    @pytest.mark.parametrize(
        "value",
        [
            VALID_ULID,
            "0" + ULID_BODY,
            "7" + ULID_BODY,
            "0" + "0" * 25,
            "7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
        ],
    )
    def test_accepts(self, value: str) -> None:
        assert is_identifier(IdentifierKind.ulid, value)

    @pytest.mark.parametrize(
        "value",
        [
            VALID_ULID.lower(),
            "8" + ULID_BODY,
            "9" + ULID_BODY,
            "A" + ULID_BODY,
            "0I" + ULID_BODY[1:],
            "0L" + ULID_BODY[1:],
            "0O" + ULID_BODY[1:],
            "0U" + ULID_BODY[1:],
            VALID_ULID[:25],
            VALID_ULID + "Z",
            "",
        ],
    )
    def test_rejects(self, value: str) -> None:
        assert not is_identifier(IdentifierKind.ulid, value)

    @pytest.mark.parametrize("value", [None, 1, b"01KQ6AZGNA0B3QBF67NBXP3S45", ["x"]])
    def test_rejects_non_str(self, value: object) -> None:
        assert not is_identifier(IdentifierKind.ulid, value)


class TestPrintableAsciiKinds:
    """operation_id and audit_target_id share the 1 to 128 printable ASCII shape."""

    KINDS = [IdentifierKind.operation_id, IdentifierKind.audit_target_id]

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize(
        "value",
        [
            "x",
            "x" * 128,
            " ",
            "~",
            "01KQ6AZGNA0B3QBF67NBXP3S45",
            "a b~c/d.e",
        ],
    )
    def test_accepts(self, kind: IdentifierKind, value: str) -> None:
        assert is_identifier(kind, value)

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "x" * 129,
            "a\tb",
            "a\nb",
            "a\x7fb",
            "\x7f",
            "café",
            "é",
        ],
    )
    def test_rejects(self, kind: IdentifierKind, value: str) -> None:
        assert not is_identifier(kind, value)

    @pytest.mark.parametrize("kind", KINDS)
    def test_rejects_non_str(self, kind: IdentifierKind) -> None:
        assert not is_identifier(kind, None)


class TestServerToken:
    @pytest.mark.parametrize(
        "value",
        [
            "a",
            "a" * 64,
            "update_stack",
            "stack_",
            "sha256_v2",
            "a1",
        ],
    )
    def test_accepts(self, value: str) -> None:
        assert is_identifier(IdentifierKind.server_token, value)

    @pytest.mark.parametrize(
        "value",
        [
            "1stack",
            "_stack",
            "Stack",
            "update_Stack",
            "update-stack",
            "a" * 65,
            "",
            "update stack",
        ],
    )
    def test_rejects(self, value: str) -> None:
        assert not is_identifier(IdentifierKind.server_token, value)

    def test_rejects_non_str(self) -> None:
        assert not is_identifier(IdentifierKind.server_token, None)


class TestBriefSlug:
    @pytest.mark.parametrize(
        "value",
        [
            "a",
            "1",
            "auth-cutover-2",
            "a-b",
            "a--b",
            "a" * 120,
            "a" + "-" * 118 + "b",
        ],
    )
    def test_accepts(self, value: str) -> None:
        assert is_identifier(IdentifierKind.brief_slug, value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "-",
            "-a",
            "a-",
            "-a-",
            "A",
            "Auth-Cutover",
            "a_b",
            "a b",
            "a.b",
            "a/b",
            "café",
            "a" * 121,
        ],
    )
    def test_rejects(self, value: str) -> None:
        assert not is_identifier(IdentifierKind.brief_slug, value)

    def test_rejects_non_str(self) -> None:
        assert not is_identifier(IdentifierKind.brief_slug, None)


class TestStackRevision:
    @pytest.mark.parametrize(
        "value",
        [
            "0" * 64,
            "f" * 64,
            "0123456789abcdef" * 4,
            "absent",
        ],
    )
    def test_accepts(self, value: str) -> None:
        assert is_identifier(IdentifierKind.stack_revision, value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "0" * 63,
            "0" * 65,
            "A" * 64,
            "0123456789ABCDEF" * 4,
            "g" * 64,
            "ABSENT",
            "Absent",
            "absent ",
            "present",
        ],
    )
    def test_rejects(self, value: str) -> None:
        assert not is_identifier(IdentifierKind.stack_revision, value)

    def test_rejects_non_str(self) -> None:
        assert not is_identifier(IdentifierKind.stack_revision, None)


class TestTrailingNewline:
    """Every kind matches whole values: a trailing newline never slips through."""

    @pytest.mark.parametrize("kind", list(IdentifierKind))
    def test_trailing_newline_rejects(self, kind: IdentifierKind) -> None:
        assert is_identifier(kind, VALID_BY_KIND[kind])
        assert not is_identifier(kind, VALID_BY_KIND[kind] + "\n")


class TestValidateIdentifier:
    @pytest.mark.parametrize("kind", list(IdentifierKind))
    def test_returns_the_value_unchanged(self, kind: IdentifierKind) -> None:
        value = VALID_BY_KIND[kind]
        assert validate_identifier(kind, value, field="target_id") == value

    def test_raises_value_error_subclass(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_identifier(IdentifierKind.ulid, "nope", field="event_id")
        assert isinstance(excinfo.value, InvalidIdentifier)

    def test_error_exposes_kind_field_and_length(self) -> None:
        with pytest.raises(InvalidIdentifier) as excinfo:
            validate_identifier(IdentifierKind.server_token, "Stack", field="operation_kind")
        error = excinfo.value
        assert error.kind is IdentifierKind.server_token
        assert error.field == "operation_kind"
        assert error.observed_length == 5

    def test_error_on_non_str_reports_no_length(self) -> None:
        with pytest.raises(InvalidIdentifier) as excinfo:
            validate_identifier(IdentifierKind.ulid, None, field="event_id")
        assert excinfo.value.observed_length is None

    @pytest.mark.parametrize(
        "kind, value",
        [
            (IdentifierKind.ulid, "0iiiiSECRETiiii"),
            (IdentifierKind.operation_id, "SECRET\n"),
            (IdentifierKind.server_token, "SECRET"),
            (IdentifierKind.audit_target_id, "SECRET\t"),
        ],
    )
    def test_message_never_echoes_the_rejected_value(
        self, kind: IdentifierKind, value: str
    ) -> None:
        with pytest.raises(InvalidIdentifier) as excinfo:
            validate_identifier(kind, value, field="secret_field")
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert "secret_field" in message
