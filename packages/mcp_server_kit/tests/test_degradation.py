"""The degradation vocabulary, and the two properties the rest of the fleet rests on it having.

`degradation.classify` is what decides whether a broken component takes a pod out of rotation
(`rxnlabel`'s and `rxnpredict`'s readiness checks read `PERMANENT_CAUSES`) and what label reaches an
unauthenticated `/metrics`. Both consequences are one function, so both are checked here rather than
only where they are consumed.

The ordering test is the one that matters. `EgressForbidden` subclasses `OSError` deliberately —
`egress.py` says so — which is exactly why a classifier written in the obvious order buries it: a
refusal and a connection reset would both come back `failed`, and the fleet's most important
degradation would be the one indistinguishable from a network blip. So the refusal here is a
**real** one, raised by the armed guard from a real `getaddrinfo`, not a constructed object.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import logging
import os
import socket
from pathlib import Path

import pytest
from mcp_server_kit import degradation
from prometheus_client import REGISTRY

ROOT = Path(__file__).resolve().parents[3]

# How many `degradation.record` call sites this fleet has. A number rather than an emptiness check,
# for the reason `test_every_call_site_derives_its_cause_rather_than_writing_one` gives. Five in
# `rxnlabel` (`mapping.map_reaction`, `mapping._mapper`, `naming.name`, `naming._namer`, and
# `readiness._probe`'s transient arm), two in `rxnpredict` (`predictors.mark_unavailable` at import,
# `tools._survivors` at request time), and the readiness funnel in `mcp_server_kit.app`.
RECORD_CALL_SITES = 8


def _refusal() -> BaseException:
    """The exception the armed guard actually raises, obtained by tripping it."""
    try:
        socket.getaddrinfo("example.invalid", 443)
    except OSError as exc:
        return exc
    raise AssertionError("the guard did not refuse; the root conftest should keep it armed")


def test_a_real_egress_refusal_is_not_sorted_as_a_generic_failure() -> None:
    """The refusal and a plain `OSError` must not land in the same bucket."""
    refusal = _refusal()
    assert isinstance(refusal, OSError), "the premise: it is an OSError, which is why order matters"
    assert degradation.classify(refusal) == degradation.CAUSE_EGRESS_REFUSED
    assert degradation.classify(ConnectionResetError(104, "reset")) == degradation.CAUSE_FAILED


def test_an_out_of_memory_is_separated_from_a_broken_checkpoint() -> None:
    """The split a readiness check acts on: one is transient, the other is a pod to replace.

    `OutOfMemoryError` is matched by *type name* because torch's is a `RuntimeError` subclass and
    this package must not import torch to see it; the double below is that shape exactly.
    """

    class OutOfMemoryError(RuntimeError):
        """Named as torch names both `torch.OutOfMemoryError` and `torch.cuda.OutOfMemoryError`."""

    # A double, not the class — see `test_torch_really_names_its_oom_the_way_this_matches_it` for
    # the half a double cannot assert.
    assert degradation.classify(MemoryError()) == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert degradation.classify(OutOfMemoryError("CUDA")) == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert degradation.classify(RuntimeError("weights")) == degradation.CAUSE_FAILED
    assert degradation.CAUSE_RESOURCE_EXHAUSTED not in degradation.PERMANENT_CAUSES
    assert degradation.CAUSE_FAILED in degradation.PERMANENT_CAUSES


def test_torch_really_names_its_oom_the_way_this_matches_it() -> None:
    """The half the double above cannot assert: that the name being matched is torch's real one.

    `_RESOURCE_TYPE_NAMES` is a *string* match, chosen over a message match because torch rewords
    its OOM text across releases and over an `isinstance` because this package must not import
    torch. The cost of that choice is that nothing anywhere checked the string against torch, and
    the test that looked like it did defined its own `OutOfMemoryError`. A rename upstream would
    move every CUDA OOM into the permanent bucket in silence.

    Skipped with the reason where torch is absent, which is every developer checkout and this CI —
    so this is a check that bites on an image carrying the `models` extras, and says what it did not
    look at everywhere else.
    """
    torch = pytest.importorskip("torch", reason="torch is not installed in this checkout")
    names = {
        torch.cuda.OutOfMemoryError.__name__,
        getattr(torch, "OutOfMemoryError", type).__name__,
    }
    assert names <= degradation._RESOURCE_TYPE_NAMES, (
        f"torch names its OOM {names}; the resource branch matches "
        f"{sorted(degradation._RESOURCE_TYPE_NAMES)}, so those spellings would classify as `failed`"
    )


def test_a_missing_distribution_is_not_a_fault() -> None:
    """An optional extra nobody installed is a deployment's decision, not a broken image."""
    assert degradation.classify(ModuleNotFoundError("torch")) == degradation.CAUSE_NOT_INSTALLED
    assert degradation.CAUSE_NOT_INSTALLED not in degradation.PERMANENT_CAUSES


def test_a_resource_errno_is_not_a_broken_checkpoint() -> None:
    """The four errno values that mean the same thing `MemoryError` does.

    `CAUSE_RESOURCE_EXHAUSTED`'s own comment said "memory, a device allocation" while
    `OSError(ENOMEM, "Cannot allocate memory")` — the kernel saying exactly that — classified
    `failed`, which is *permanent*, alongside a pod at its descriptor ceiling
    (`EMFILE`/`ENFILE`) and
    a pod at its thread ceiling (`EAGAIN`). Driven before the branch existed: all four permanent.

    `ENOSPC` is the counterfactual and it stays permanent on purpose: a full volume is not something
    the process gets back by waiting, and a probe that kept a pod in service over it would be
    reporting on a disk nobody is freeing.
    """
    for code in (errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.EAGAIN):
        exc = OSError(code, os.strerror(code))
        cause = degradation.classify(exc)
        assert cause == degradation.CAUSE_RESOURCE_EXHAUSTED, f"{errno.errorcode[code]} -> {cause}"
        assert cause not in degradation.PERMANENT_CAUSES
    full = degradation.classify(OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)))
    assert full == degradation.CAUSE_FAILED, "a full volume is not a resource the process gets back"


def test_a_refusal_is_still_sorted_before_the_errno_branch() -> None:
    """The new branch is an `OSError` branch, which is the shape `classify`'s order exists for."""
    refusal = _refusal()
    assert refusal.errno not in {errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.EAGAIN}, (
        "the premise: today a refusal's errno is not one of these, so only the ordering keeps "
        "them apart if that ever changes"
    )
    assert degradation.classify(refusal) == degradation.CAUSE_EGRESS_REFUSED


def test_an_unclamped_cause_is_refused_rather_than_published(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reporter must not become the error, and the clamp must still be a clamp.

    **The name is the one the degradation record cites, and it is still accurate about what
    matters**: what is refused is the unclamped *label*, which never reaches `/metrics`. What
    changed is that the refusal is no longer a raise — see below. A merged record is never edited,
    so the citation keeps resolving and the new record says what moved.

    `record` used to raise `ValueError` for a cause outside `CAUSES`, from inside the `except` block
    whose whole job is to degrade gracefully — and `connector_app` passes `ValueError` to the model
    verbatim. Driven with `classify` patched to return a fifth cause: `map_reaction` raised instead
    of degrading and a chemist's answer became a sentence about Prometheus labels. So it logs at
    ERROR, books `failed`, and **mints no series for the unclamped string** — which is the half that
    makes this a clamp rather than a shrug.
    """
    degradation.register_components("probe")
    labels = {"server": "kit", "component": "probe", "cause": degradation.CAUSE_FAILED}
    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0
    with caplog.at_level(logging.ERROR):
        degradation.record(server="kit", component="probe", cause="whatever-just-happened")
    assert "whatever-just-happened" in caplog.text
    assert REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) == before + 1.0
    assert (
        REGISTRY.get_sample_value(
            "chemclaw_mcp_degraded_total",
            {"server": "kit", "component": "probe", "cause": "whatever-just-happened"},
        )
        is None
    ), "the unclamped cause must not have minted a series on the way out"


def test_an_unregistered_component_is_clamped_the_way_a_tool_name_is(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`cause` was a closed set and `component` was a convention, on the same unauthenticated route.

    Driven before `register_components` existed:
    `record(component='hostile"}\n fake_metric 99', ...)` minted that series. No caller could reach
    it — all four call sites pass a source constant — so this closes a gap rather than a breach, and
    it closes it the way `app._served_tool_name` closes the same gap for a caller-supplied tool
    name.
    """
    hostile = 'hostile"}\n fake_metric 99'
    assert hostile not in degradation.registered_components(), "the premise"
    sentinel = {
        "server": "kit",
        "component": degradation.UNKNOWN_COMPONENT,
        "cause": degradation.CAUSE_FAILED,
    }
    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", sentinel) or 0.0
    with caplog.at_level(logging.ERROR):
        degradation.record(server="kit", component=hostile, cause=degradation.CAUSE_FAILED)
    assert REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", sentinel) == before + 1.0
    assert (
        REGISTRY.get_sample_value(
            "chemclaw_mcp_degraded_total",
            {"server": "kit", "component": hostile, "cause": degradation.CAUSE_FAILED},
        )
        is None
    ), "an unregistered component minted its own series"


def test_classify_cannot_answer_outside_the_clamped_set() -> None:
    """The hard assertion `record` gave up, kept where a failure costs a red build.

    `record` is lenient at runtime because its callers are `except` blocks that must answer anyway —
    raising there made the error reporter the error. That leniency would be a hole if nothing
    asserted the composition, so this is that assertion: every call site passes `classify(...)`, and
    `classify` over a battery of exception shapes answers inside `CAUSES` and nothing else.

    The battery spans the three branches and their edges — a refusal, both resource spellings, four
    errno values, a broken `.so`, a `BaseException` that is not an `Exception` — because a fifth
    branch added without a matching `CAUSES` member is what this is here to catch.
    """
    battery: list[BaseException] = [
        _refusal(),
        MemoryError("Unable to allocate 48.0 MiB"),
        RuntimeError("CUDA out of memory"),
        OSError(errno.ENOMEM, "Cannot allocate memory"),
        OSError(errno.EMFILE, "Too many open files"),
        OSError(errno.ENFILE, "Too many open files in system"),
        OSError(errno.EAGAIN, "Resource temporarily unavailable"),
        OSError(errno.ENOSPC, "No space left on device"),
        OSError("no errno at all"),
        FileNotFoundError(errno.ENOENT, "No such file", "/mnt/models/megan/model.ckpt"),
        ImportError("libcudart.so.11: cannot open shared object file"),
        ModuleNotFoundError("No module named 'dgl'"),
        TimeoutError("timed out"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
        ValueError("unparseable SMILES"),
    ]
    for exc in battery:
        assert degradation.classify(exc) in degradation.CAUSES, f"{exc!r} classified outside CAUSES"


def test_every_call_site_derives_its_cause_rather_than_writing_one() -> None:
    """Read as source, because the sites that matter run at *import*.

    `rxnpredict`'s eleven `mark_unavailable` calls and `rxnlabel`'s two constructors fire while the
    module is being imported, in a checkout where every guarded import fails the same way, so no
    behavioural test can drive one of them into a *different* failure. AST rather than grep, for the
    reason `no_egress.py` gives: spellings differ as text and agree as a tree.

    **The count is asserted, and that is not decoration.** The sibling test this file's ADR cited as
    keeping the `exc=exc` invariant true asserted `not missing` over a collected list — which is
    satisfied by *zero* matching call sites, so deleting the call it was written about left it
    green.
    A number fails when a site disappears, which is the case that matters: a degradation nobody
    counts is the defect this whole vocabulary exists for.
    """
    roots = (
        ROOT / "packages" / "mcp_server_kit" / "src",
        ROOT / "servers" / "rxnlabel" / "src",
        ROOT / "servers" / "rxnpredict" / "src",
    )
    sites: list[str] = []
    bad: list[str] = []
    for root in roots:
        for module in sorted(root.rglob("*.py")):
            tree = ast.parse(module.read_text())
            if not _imports_record(tree):
                continue
            for function in ast.walk(tree):
                if not isinstance(function, ast.FunctionDef):
                    continue
                for node in ast.walk(function):
                    if not (isinstance(node, ast.Call) and _is_record(node)):
                        continue
                    where = f"{module.relative_to(ROOT)}:{node.lineno}"
                    sites.append(where)
                    cause = next((kw.value for kw in node.keywords if kw.arg == "cause"), None)
                    if not _is_derived_cause(cause, function):
                        bad.append(where)
    assert not bad, (
        "a `cause=` that is neither a CAUSE_* constant, a `classify(...)` call, nor a local bound "
        f"from one: {bad}"
    )
    assert len(sites) == RECORD_CALL_SITES, (
        f"found {len(sites)} `degradation.record` call sites {sites}, expected "
        f"{RECORD_CALL_SITES}; update the constant deliberately"
    )


def _imports_record(tree: ast.Module) -> bool:
    """Whether this module binds `degradation.record` — by import, or by the module alias.

    Needed because `auth.py` has its own unrelated `record` callable, and a test that matched the
    *name* counted it. One false positive is enough to make a reviewer widen the rule until it
    asserts nothing.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("degradation")
            and any(alias.name in {"record", "*"} for alias in node.names)
        ):
            return True
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "degradation" for alias in node.names
        ):
            return True
    return False


def _is_record(call: ast.Call) -> bool:
    """Whether this call is `record(...)` or `degradation.record(...)`."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == "record"
    return isinstance(func, ast.Name) and func.id == "record"


def _is_derived_cause(cause: ast.expr | None, function: ast.FunctionDef) -> bool:
    """Whether `cause` is a clamped constant, a `classify(...)`, or a local bound from one."""
    if cause is None:
        return False
    if isinstance(cause, ast.Attribute) and cause.attr.startswith("CAUSE_"):
        return True
    if isinstance(cause, ast.Call) and _called(cause) == "classify":
        return True
    if isinstance(cause, ast.IfExp):
        # `classify(exc) if exc is not None else CAUSE_NOT_INSTALLED` — `mark_unavailable`'s shape.
        return _is_derived_cause(cause.body, function) and _is_derived_cause(cause.orelse, function)
    if not isinstance(cause, ast.Name):
        return False
    for node in ast.walk(function):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == cause.id for t in targets):
            continue
        if _is_derived_cause(node.value, function):
            return True
    return False


def _called(call: ast.Call) -> str:
    """The bare name of whatever `call` invokes."""
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_recording_moves_the_series_an_operator_scrapes() -> None:
    """The whole point: a degradation is visible from a scrape rather than from a log line."""
    degradation.register_components("probe")
    labels = {"server": "kit", "component": "probe", "cause": degradation.CAUSE_FAILED}
    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0
    degradation.record(server="kit", component="probe", cause=degradation.CAUSE_FAILED)
    after = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels)
    assert after == before + 1.0
