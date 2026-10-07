"""The two pod-level bounds: memory per run, and how many runs at once.

`RLIMIT_AS` must sit below the container's memory limit, or the OOMKiller takes the whole pod
instead of refusing one call, so `default_memory_bytes` derives one from the other. Concurrency
is gated to the pod's cores, because `os.cpu_count()` is not cgroup-aware and an oversubscribed
core turns a CPU-bound run into a spurious wall-clock timeout. Tested against a real cgroup
file, a real gate, and the shipped Deployment read from disk rather than transcribed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from chemclaw_mcp_pyexec.engine import limits as limits_module
from chemclaw_mcp_pyexec.engine.admission import DEFAULT_MAX_CONCURRENT_RUNS, Admission
from chemclaw_mcp_pyexec.engine.limits import (
    MINIMUM_VIABLE_MEMORY_BYTES,
    SERVER_HEADROOM_BYTES,
    UNCONSTRAINED_MEMORY_BYTES,
    Limits,
    container_memory_limit,
    default_memory_bytes,
)

#: The file the pod's two limits and the ceiling's own override live in, read rather than copied.
DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"

#: Kubernetes' quantity suffixes, for the two fields this file reads.
_MEMORY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def _container() -> dict[str, Any]:
    """The shipped Deployment's one container, where the limits and any `env:` live."""
    loaded = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    container = loaded["spec"]["template"]["spec"]["containers"][0]
    assert isinstance(container, dict)
    return container


def _declared_env() -> dict[str, str]:
    """The container's literal `env:` values — none a sizing knob today, the drift to catch."""
    # Literal values only: a `valueFrom` entry (the bearer, from a Secret) is not a sizing knob.
    return {
        entry["name"]: str(entry["value"])
        for entry in _container().get("env", [])
        if "value" in entry
    }


def pod_memory_limit_bytes() -> int:
    """`limits.memory` in bytes. This is what `default_memory_bytes` divides by the ceiling."""
    declared = str(_container()["resources"]["limits"]["memory"])
    for suffix, multiplier in _MEMORY_SUFFIXES.items():
        if declared.endswith(suffix):
            return int(float(declared[: -len(suffix)]) * multiplier)
    return int(declared)


def pod_cpu_limit_cores() -> float:
    """`limits.cpu` in cores. Kubernetes accepts `"500m"`, `"1"` and `1` for the same field."""
    declared = str(_container()["resources"]["limits"]["cpu"])
    return float(declared[:-1]) / 1000 if declared.endswith("m") else float(declared)


def shipped_max_concurrent_runs() -> int:
    """The ceiling this pod actually runs with: the default, unless its `env:` overrides it.

    Reads the same variable and default as `tools.py`, so a Deployment override is picked up here.
    """
    declared = _declared_env().get("CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS")
    return int(declared) if declared else DEFAULT_MAX_CONCURRENT_RUNS


#: The address space a program importing numpy, pandas, scipy, sklearn, sympy, matplotlib, RDKit
#: and OpenBabel and drawing a plot actually reached, measured in the child: 629 MiB of VSZ against
#: 292 MiB of RSS. `RLIMIT_AS` bounds the first, so this is the number the derivation must clear.
HEAVY_PROGRAM_ADDRESS_SPACE_BYTES = 629 * 1024**2


def _with_cgroup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str) -> None:
    """Point the reader at a file holding `contents`, as a cgroup v2 `memory.max` would."""
    fake = tmp_path / "memory.max"
    fake.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(limits_module, "_CGROUP_V2_MAX", fake)
    monkeypatch.setattr(limits_module, "_CGROUP_V1_MAX", tmp_path / "absent")


def test_the_shipped_pod_gives_each_run_more_than_a_heavy_program_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The memory arithmetic, run against the numbers `deploy/deployment.yaml` actually ships.

    Written against the pod limit rather than `Limits.memory_bytes`, because a value correct in
    isolation can be wrong about the container it runs in; the limit is read, not transcribed.
    """
    pod_memory = pod_memory_limit_bytes()
    ceiling = shipped_max_concurrent_runs()
    _with_cgroup(monkeypatch, tmp_path, str(pod_memory))
    per_run = default_memory_bytes(ceiling)
    assert per_run > HEAVY_PROGRAM_ADDRESS_SPACE_BYTES, (
        f"each run gets {per_run // 1024**2} MiB of address space, and a legitimate analysis "
        f"importing the scientific stack reached {HEAVY_PROGRAM_ADDRESS_SPACE_BYTES // 1024**2} "
        f"MiB — the bound would refuse real work. {DEPLOYMENT.name} limits this pod to "
        f"{pod_memory // 1024**2} MiB over a ceiling of {ceiling}"
    )
    admitted_together = ceiling * per_run + SERVER_HEADROOM_BYTES
    assert admitted_together <= pod_memory, (
        "all the runs the gate admits can together exceed the pod's own memory limit, so the "
        "OOMKiller is still what fires — which is the defect this derivation exists to end"
    )


def test_a_slot_is_a_core_the_shipped_pod_actually_has() -> None:
    """A slot is a core: the run ceiling equals the shipped pod's `limits.cpu`.

    Equality in both directions: a ceiling above the cores breaks the wall clock (a CPU-bound run
    cannot finish and the caller is told their program timed out), one below leaves a core idle
    while a caller is refused. Read from the Deployment, not transcribed.
    """
    cores = pod_cpu_limit_cores()
    ceiling = shipped_max_concurrent_runs()
    assert cores == ceiling, (
        f"{DEPLOYMENT.name} limits this pod to {cores} cores while the gate admits {ceiling} runs, "
        "and a run is a single-threaded child pinned to one core. Move both, and re-derive "
        "engine/admission.py's argument — the per-run memory bound is the same number's divisor"
    )


def test_the_guard_is_always_below_the_limit_the_kernel_kills_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Across pod sizes, one run can never be allowed more than the container has.

    `RLIMIT_AS` bounds address space, which is at least resident set, so a bound under the container
    limit guarantees the guard fires first: one refused call instead of an OOMKilled pod.
    """
    for gigabytes in (1, 2, 4, 8):
        _with_cgroup(monkeypatch, tmp_path, str(gigabytes * 1024**3))
        for ceiling in (1, 2, 4):
            assert default_memory_bytes(ceiling) < gigabytes * 1024**3


def test_an_undersized_pod_is_reported_rather_than_silently_clamped_back_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An undersized pod is reported at WARNING, not clamped back up.

    Raising the bound above the container's limit is exactly the state where the guard cannot fire;
    the deployment is what has to change.
    """
    _with_cgroup(monkeypatch, tmp_path, str(512 * 1024**2))
    with caplog.at_level("WARNING"):
        derived = default_memory_bytes(4)
    assert derived < MINIMUM_VIABLE_MEMORY_BYTES
    assert derived < 512 * 1024**2, "clamped above the pod's limit — the guard could never fire"
    assert "CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS" in caplog.text, (
        "the warning must name the knob; an operator reading it has to know which of the two "
        "numbers to move"
    )


@pytest.mark.parametrize(
    "contents",
    [
        "max",  # cgroup v2's "unbounded".
        "9223372036854771712",  # cgroup v1's PAGE_COUNTER_MAX sentinel.
    ],
)
def test_an_unbounded_cgroup_falls_back_instead_of_deriving_nonsense(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str
) -> None:
    """An unbounded cgroup (dev box, container without `--memory`) falls back to the fixed default.

    Neither cgroup generation writes "no limit" as a usable number; v1's sentinel is exabytes, and
    dividing it would hand one run more address space than the machine has.
    """
    _with_cgroup(monkeypatch, tmp_path, contents)
    assert container_memory_limit() is None
    assert default_memory_bytes(2) == UNCONSTRAINED_MEMORY_BYTES
    assert Limits().memory_bytes == UNCONSTRAINED_MEMORY_BYTES


def test_an_unreadable_cgroup_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither file exists on a Mac, in a plain venv, or under some CI runners."""
    monkeypatch.setattr(limits_module, "_CGROUP_V2_MAX", tmp_path / "absent-v2")
    monkeypatch.setattr(limits_module, "_CGROUP_V1_MAX", tmp_path / "absent-v1")
    assert container_memory_limit() is None
    assert default_memory_bytes(2) == UNCONSTRAINED_MEMORY_BYTES


def test_the_run_ceiling_refuses_rather_than_queues() -> None:
    """A prompt refusal in terms the caller can act on, and slots that come back."""
    gate = Admission(limit=1)
    gate.acquire("run_python")
    assert gate.in_flight == 1
    with pytest.raises(ValueError, match=r"already running 1 analyses"):
        gate.acquire("run_python")
    gate.release()
    assert gate.in_flight == 0
    gate.acquire("run_python")
    gate.release()


def test_the_refusal_says_the_pod_is_full_rather_than_blaming_the_program() -> None:
    """A full pod refuses with a message saying so, rather than blaming the program.

    A queued run spends its wall clock waiting and is killed as "too slow". `connector_app` passes
    `ValueError` to the model verbatim, so this wording is what the agent reasons from.
    """
    gate = Admission(limit=1)
    gate.acquire("run_python")
    with pytest.raises(ValueError) as caught:
        gate.acquire("run_python")
    message = str(caught.value)
    assert "refused rather than queued" in message
    assert "CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS" in message
    assert "whole core" in message


def test_a_ceiling_below_one_is_refused_at_construction() -> None:
    """A ceiling of zero would refuse every run, which is the tool deleted by configuration."""
    with pytest.raises(ValueError):
        Admission(limit=0)
