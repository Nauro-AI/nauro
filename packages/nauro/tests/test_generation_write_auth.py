import importlib

import pytest


@pytest.mark.parametrize(
    "family,payload_args",
    [
        ("state", ("Current work",)),
        ("question", ("Next step?",)),
    ],
)
def test_records_use_explicit_actor_and_keep_connection(
    tmp_path, monkeypatch, family, payload_args
):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    records = importlib.import_module(f"nauro.store.{family}_records")
    contract = importlib.import_module(f"nauro.store.{family}_contract")
    calls = []
    actor = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    project = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    payload = getattr(contract, f"{family}_payload")(*payload_args)
    record = getattr(records, f"prepare_{family}_submission")(
        project,
        actor,
        payload,
        require_actor=calls.append,
        connection="endpoint-binding",
    )
    loaded = getattr(records, f"read_{family}_submission")(record.scope, require_actor=calls.append)
    assert loaded == record
    assert loaded.connection == "endpoint-binding"
    assert set(calls) == {actor}
