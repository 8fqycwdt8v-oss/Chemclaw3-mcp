"""The two bounds this server had on *one* run and not on the pod: memory, and how many at once.

Every per-call bound here was already right — 20 s of wall clock enforced with `killpg`, 15 CPU
seconds, 16 MB of writes, 128 descriptors, no fork headroom. What was missing is the pair of bounds
that are about the pod rather than the call, and both failed in the same direction: they looked
present and could not fire.

- **`RLIMIT_AS` shipped at a flat 2 GiB inside a pod limited to 512Mi** — four times the container's
  own ceiling. So the sandbox's memory guard was unreachable, and what enforced memory instead was
  the container OOMKiller, which kills the *pod* and every other in-flight MCP session with it
  rather than refusing the one offending call. Two independently written numbers cannot be kept
  consistent by review, so `default_memory_bytes` derives one from the other.
- **Nothing bounded concurrency at all.** The effective ceiling was CPython's default executor,
  `min(32, os.cpu_count() + 4)` — and `os.cpu_count()` is not cgroup-aware, so a pod limited to two
  cores on a 64-core node would still admit 32 child processes. That does not merely slow runs down;
  it breaks the wall clock, because 15 CPU seconds on a thirty-second share of a core cannot finish
  inside 20 s of wall clock, and every caller is then told their *program* timed out.

These test the mechanism against a real cgroup file and a real gate, not a mock of either — and
against the *shipped* Deployment rather than a transcription of it. Both numbers below used to be
literals in this file, under a docstring claiming they were "the numbers `deploy/deployment.yaml`
actually ships": lowering `limits.cpu` to `"1"` put two runs on one core — the breakage the gate
exists to end — and this suite stayed green. They are now read from the file, following
`servers/chem/tests/test_depiction_bound.py`.
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
    """The container's `env:` as a mapping — empty today, and that is exactly the drift to catch."""
    return {entry["name"]: str(entry["value"]) for entry in _container().get("env", [])}


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

    `tools.py` reads the same environment variable with the same default, so a Deployment that
    grows a `CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS` is picked up here instead of silently parting
    company with the number these assertions are about.
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
    """The whole arithmetic, run against the numbers `deploy/deployment.yaml` actually ships.

    This is the test that would have caught the original defect, and it is written against the pod
    limit rather than against `Limits.memory_bytes` for that reason: the old value was correct in
    isolation and wrong about the container it ran in, which no test of the constant alone can see.
    The pod limit is *read* rather than transcribed, because a copy of a number is not a check on
    it: this assertion passed unchanged while the Deployment's `limits.memory` said anything at all.
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
    """The other half of the coupling `engine/admission.py` asserts, and nothing used to check.

    "A slot is a core, and `deploy/deployment.yaml` sets `limits.cpu` to the same number" is a claim
    about a file this package cannot import, and it was made in a docstring beside a test that
    transcribed the number instead of reading it. Measured on the transcribed version: setting
    `limits.cpu: "1"` gave two runs one core — a 15-CPU-second program then cannot finish inside its
    20 s wall clock, so every caller is told their *program* timed out — and the suite stayed green.

    Equality rather than "at least", in both directions: a ceiling above the cores breaks the wall
    clock, and a ceiling below them leaves a core idle while a caller is refused. Whichever moves
    first, the other one and the paragraph arguing for it have to move with it.
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
    """The invariant, across pod sizes: one run can never be allowed more than the container has.

    `RLIMIT_AS` bounds address space and address space is at least resident set, so a bound under
    the container's limit guarantees the guard fires first — a refusal of one call instead of an
    OOMKill of the pod. Checked over a range because a deployment may size this pod differently and
    the property must not depend on the shipped number.
    """
    for gigabytes in (1, 2, 4, 8):
        _with_cgroup(monkeypatch, tmp_path, str(gigabytes * 1024**3))
        for ceiling in (1, 2, 4):
            assert default_memory_bytes(ceiling) < gigabytes * 1024**3


def test_an_undersized_pod_is_reported_rather_than_silently_clamped_back_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Clamping up would restore the original defect; the deployment is what has to change.

    A pod too small for its run ceiling produces a bound below what a real analysis needs, and the
    honest answer is to say so at WARNING and let legitimate work be refused — because raising the
    bound back over the container's limit is precisely the state where nothing can fire.
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
    """A dev box and a container started without `--memory` must behave as this file always did.

    Both cgroup generations write "no limit" differently and neither writes a number a division
    would survive: v1's sentinel is exabytes, so dividing it would hand one run more address space
    than the machine has and call it a bound.
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
    """The whole point of admitting rather than queueing, expressed as the message.

    Without a gate a full pod produced a *timeout*, because a queued program spends its wall clock
    waiting and is then killed for exceeding it — so the caller was told its analysis was too slow
    when what happened is that the pod had no core to give it. `connector_app` passes `ValueError`
    to the model verbatim, so this wording is what the agent reads and reasons from.
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
