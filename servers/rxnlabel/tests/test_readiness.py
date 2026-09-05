"""`/healthz` here has to separate "this deployment chose not to install a model" from "it broke".

`rxnlabel`'s two heavy components are optional *by design*: without them a reaction is labelled
without an atom map and without a name, `engine/version.py` records that in `labeller_version`, and
the corpus re-labels itself the day they arrive. That design is what made the readiness gap easy to
miss — "optional" was read as "nothing to be unready about", so this server passed no `readiness=`
at all and answered a constant `{"status": "ok"}`. A pod whose `rxnmapper` checkpoint failed to load
therefore passed its probe, took traffic, and quietly wrote coarse labels under a version string
that claimed no mapper was ever installed.

The distinction this module has to make is the one `version._installed` already knows how to make:
a distribution that is *not there* is a deployment's choice, and a distribution that is there and
will not construct is a broken image.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnlabel.engine import readiness


def test_readiness_labels_a_fixture_reaction_through_the_engine() -> None:
    """The probe exercises the labelling path, not an import — a broken rule table must fail it."""
    readiness.verify_labeller.cache_clear()
    assert readiness.verify_labeller() == ()


def test_an_uninstalled_component_is_ready_and_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mapper installed is this deployment's decision, and the server answers with it."""
    monkeypatch.setattr(readiness.mapping, "available", lambda: False)
    monkeypatch.setattr(readiness.naming, "available", lambda: False)
    monkeypatch.setattr(readiness.version, "_installed", lambda _name: "absent")
    readiness.verify_labeller.cache_clear()
    assert readiness.verify_labeller() == ()
    readiness.verify_labeller.cache_clear()


def test_an_installed_component_that_will_not_construct_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint that failed to load is a broken image, and it must not take traffic."""
    monkeypatch.setattr(readiness.mapping, "available", lambda: False)
    monkeypatch.setattr(readiness.version, "_installed", lambda name: "9.9.9")
    readiness.verify_labeller.cache_clear()
    with pytest.raises(RuntimeError) as unready:
        readiness.verify_labeller()
    assert "rxnmapper" in str(unready.value)
    readiness.verify_labeller.cache_clear()


def test_the_app_wires_the_probe_in() -> None:
    """A readiness callable nothing passes to `connector_app` is a control that does not exist."""
    from chemclaw_mcp_rxnlabel import app as app_module

    assert app_module._readiness is not None
    assert app_module._readiness() == []
