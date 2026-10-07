"""`/healthz` on this server proves the child process works, not that the app imported.

Everything that decides whether `pyexec` can answer (interpreter, runner module, `prctl` seal,
child resource limits) lives outside the process and is untouched at import, so a broken sandbox
would otherwise look healthy while every `run_python` fails.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_pyexec.engine import readiness


def test_readiness_runs_a_program_in_the_child_and_verifies_what_came_back() -> None:
    """The probe is a real fork, not an import check: a broken sandbox must fail it."""
    readiness.verify_sandbox.cache_clear()
    assert readiness.verify_sandbox() == ()


def test_readiness_refuses_when_the_sandbox_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that starts and returns the wrong thing is a pod that must not take traffic."""
    from chemclaw_mcp_pyexec.engine import sandbox

    def wrong(*_args: object, **_kwargs: object) -> sandbox.Outcome:
        return sandbox.Outcome(
            stdout="", result_json="null", error=None, truncated=False, timed_out=False
        )

    readiness.verify_sandbox.cache_clear()
    monkeypatch.setattr(readiness, "run", wrong)
    with pytest.raises(RuntimeError, match="sandbox"):
        readiness.verify_sandbox()
    readiness.verify_sandbox.cache_clear()


def test_readiness_refuses_when_the_child_could_not_be_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that will not launch raises, and the raise is what becomes the 503."""

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the runner could not be started")

    readiness.verify_sandbox.cache_clear()
    monkeypatch.setattr(readiness, "run", explode)
    with pytest.raises(RuntimeError):
        readiness.verify_sandbox()
    readiness.verify_sandbox.cache_clear()


def test_the_app_wires_the_probe_in() -> None:
    """A readiness callable nothing passes to `connector_app` is a control that does not exist."""
    from chemclaw_mcp_pyexec import app as app_module

    assert app_module._readiness is not None
    assert app_module._readiness() == []
