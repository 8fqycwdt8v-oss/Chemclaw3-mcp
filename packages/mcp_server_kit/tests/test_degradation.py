"""The degradation vocabulary, and the two properties the rest of the fleet rests on it having.

`degradation.classify` decides whether a broken component takes a pod out of rotation (readiness
reads `PERMANENT_CAUSES`) and what label reaches an unauthenticated `/metrics`.

The ordering test matters most: `EgressForbidden` subclasses `OSError`, so a classifier in the
obvious order would file a refusal as `failed` beside a connection reset. The refusal here is a
real one, raised by the armed guard from a real `getaddrinfo`.
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

# How many `degradation.record` call sites this fleet has; a count, so a deleted site fails. Four
# in `rxnlabel`, three in `rxnpredict`, and the readiness funnel in `mcp_server_kit.app`.
RECORD_CALL_SITES = 8


def _refusal() -> OSError:
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
    """Torch's real OOM class name matches the string `_RESOURCE_TYPE_NAMES` looks for.

    The match is by type name because torch rewords its message and this package must not import
    torch; an upstream rename would otherwise move every CUDA OOM into the permanent bucket. Skipped
    where torch is absent.
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


def test_an_installed_module_that_will_not_load_is_broken_not_absent() -> None:
    """An installed module that will not load is `broken`, not `not_installed`.

    `not_installed` keeps a pod in service, so a missing shared library must not read as a
    deployment's choice. `ModuleNotFoundError` means not located; a plain `ImportError` means
    located and failed to load.
    """
    broken = ImportError("libcudart.so.11.0: cannot open shared object file: No such file")
    assert degradation.classify(broken) == degradation.CAUSE_FAILED
    assert degradation.classify(broken, optional=("torch",)) == degradation.CAUSE_FAILED
    assert degradation.CAUSE_FAILED in degradation.PERMANENT_CAUSES
    # The OSError a `ctypes.CDLL` of the same library raises was already `failed`; asserted so the
    # two spellings of one fault cannot drift into different buckets.
    dlopen = OSError("libcudart.so.11.0: cannot open shared object file: No such file or directory")
    assert degradation.classify(dlopen) == degradation.CAUSE_FAILED


def test_a_missing_dependency_of_an_installed_extra_is_broken_not_absent() -> None:
    """A missing dependency of an installed extra is broken, told apart by `exc.name`.

    It raises the same `ModuleNotFoundError` as an absent extra; `optional` declares which names the
    caller tolerates the absence of.
    """
    absent = ModuleNotFoundError("No module named 'rxnmapper'", name="rxnmapper")
    submodule = ModuleNotFoundError("No module named 'rxn_insight.x'", name="rxn_insight.x")
    transitive = ModuleNotFoundError("No module named 'tokenizers'", name="tokenizers")
    nameless = ModuleNotFoundError("raised by hand")

    assert degradation.classify(absent, optional=("rxnmapper",)) == degradation.CAUSE_NOT_INSTALLED
    # A missing *submodule* means the package is there and is not the version this code expects.
    assert degradation.classify(submodule, optional=("rxn_insight",)) == degradation.CAUSE_FAILED
    assert degradation.classify(transitive, optional=("rxnmapper",)) == degradation.CAUSE_FAILED
    assert degradation.classify(nameless, optional=("rxnmapper",)) == degradation.CAUSE_FAILED
    # And without a declaration the type is taken at its word, which is the old behaviour for the
    # one shape it was right about.
    assert degradation.classify(transitive) == degradation.CAUSE_NOT_INSTALLED
    assert not degradation.is_not_installed(ImportError("x", name="rxnmapper"), ("rxnmapper",))


def test_a_resource_errno_is_not_a_broken_checkpoint() -> None:
    """The four errno values that mean the same thing `MemoryError` does.

    `ENOMEM`, `EMFILE`, `ENFILE` and `EAGAIN` are transient resource exhaustion, not permanent
    failure. `ENOSPC` stays permanent on purpose: a full volume is not reclaimed by waiting.
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

    `record` runs inside `except` blocks that must answer, and a `ValueError` would reach the model
    verbatim. So an unclamped cause logs at ERROR, books `failed`, and mints no series for the
    unclamped string.
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
    """An unregistered component is clamped, as a caller-supplied tool name is.

    Otherwise a crafted component string could inject a series into the unauthenticated `/metrics`.
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
    """`classify` answers inside `CAUSES` for every exception shape.

    `record` is lenient at runtime, so the hard assertion lives here. The battery spans every branch
    and its edges so a new branch without a `CAUSES` member is caught.
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
    """Every `record` call site passes `exc=` through `classify`, read as source.

    The sites that matter run at import and cannot be driven into different failures. AST rather
    than grep, since spellings differ as text and agree as a tree. The count is asserted because an
    emptiness check over a collected list passes when the sites disappear.
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

    `auth.py` has an unrelated `record`, so matching the bare name would count it.
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
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
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
