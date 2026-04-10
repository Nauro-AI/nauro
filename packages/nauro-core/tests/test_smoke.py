"""Smoke test: verify nauro_core is importable."""


def test_nauro_core_importable():
    import nauro_core

    assert nauro_core is not None
