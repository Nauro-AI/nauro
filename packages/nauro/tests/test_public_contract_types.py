"""Builtin label normalization must retain real parameter contract differences."""

import copy

import click
import pytest
import typer

from tests.test_public_contract import _param_node


def parameter(annotation=str, **options):
    app = typer.Typer()
    option = typer.Option(..., **options)

    def probe(value=option):
        pass

    probe.__annotations__ = {"value": annotation}
    app.command()(probe)
    return next(p for p in typer.main.get_command(app).params if p.name == "value")


@pytest.mark.parametrize(
    "annotation,labels,expected",
    [(str, ("str", "text"), "text"), (int, ("int", "integer"), "integer")],
)
def test_known_builtin_labels_keep_exact_node(monkeypatch, annotation, labels, expected):
    param = parameter(annotation)
    baseline = {
        "name": "value",
        "kind": "option",
        "type": expected,
        "required": True,
        "flags": ["--value"],
    }
    for label in labels:
        monkeypatch.setattr(type(param.type), "name", label)
        assert _param_node(param) == baseline


@pytest.mark.parametrize("annotation", [int, float, bool])
def test_builtin_substitutions_change_node(annotation):
    assert _param_node(parameter(annotation)) != _param_node(parameter(str))


def test_choices_and_changed_members_remain_visible():
    param = parameter()
    plain = _param_node(param)
    param.type = click.Choice(["a", "b"])
    first = _param_node(param)
    assert first != plain and first["choices"] == ["a", "b"]
    param.type = click.Choice(["a", "c"])
    assert _param_node(param) != first


@pytest.mark.parametrize(
    "changed", [{"min": 2}, {"max": 8}, {"min_open": True}, {"max_open": True}, {"clamp": True}]
)
def test_integer_range_constraints_change_node(changed):
    param = parameter(int)
    plain = _param_node(param)
    param.type = click.IntRange(1, 9)
    bounded = _param_node(param)
    assert bounded != plain
    param.type = click.IntRange(**{"min": 1, "max": 9, **changed})
    assert _param_node(param) != bounded


@pytest.mark.parametrize("label", ["str", "text", "int", "integer", "unknown"])
@pytest.mark.parametrize("subclass", [False, True])
def test_custom_types_cannot_masquerade_as_builtins(label, subclass):
    param = parameter(int if label in {"int", "integer"} else str)
    plain = _param_node(param)
    base = type(param.type) if subclass else click.ParamType
    custom = type("Custom", (base,), {"name": label})
    param.type = custom()
    actual = _param_node(param)
    assert actual != plain and actual["type"] == label
    if label != "unknown":
        assert actual["type_implementation"] == f"{custom.__module__}.{custom.__qualname__}"


@pytest.mark.parametrize("override", ["configuration", "convert"])
@pytest.mark.parametrize("shared", [False, True])
def test_instance_overrides_do_not_match_plain_builtin(monkeypatch, override, shared):
    param = parameter()
    plain = _param_node(param)
    if not shared:
        param.type = copy.copy(param.type)
    monkeypatch.setattr(
        param.type,
        override,
        True if override == "configuration" else lambda value, *args: 1,
        raising=False,
    )
    assert _param_node(param) != plain


def test_unexpected_builtin_label_remains_visible(monkeypatch):
    param = parameter()
    plain = _param_node(param)
    monkeypatch.setattr(type(param.type), "name", "future-string")
    actual = _param_node(param)
    assert actual != plain and actual["type"] == "future-string"
