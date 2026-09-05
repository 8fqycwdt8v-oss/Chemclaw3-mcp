"""`/healthz` on this server has to prove the child process works, not that the app imported.

`pyexec` is the one server in the fleet that answers by forking. Everything that decides whether it
can answer anything at all lives outside this process — the interpreter on the image, the runner
module beside it, the `prctl` seal the parent applies before it forks, and the resource limits the
child sets on itself — and none of it is touched at import. So a broken sandbox looked exactly like
a healthy one from the outside: a constant `{"status": "ok"}`, a kubelet probe satisfied, traffic
routed, and every `run_python` failing. Meanwhile every server's `deploy/deployment.yaml` carries
the same comment saying `/healthz` here is "real readiness (503 until the corpus/model/backend
loads), not a constant 200".
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
