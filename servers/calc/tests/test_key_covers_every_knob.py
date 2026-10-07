"""Every setting that can move a number moves a key — derived from `CalcSettings.model_fields`.

Spec fields are keyed by construction via `model_dump()`, but a setting a compute path reads
directly from `settings` is not. Missing one makes a cache hit serve a result computed under a
different configuration, and `structure_id` propagates the fork down the chain.

`test_every_setting_that_can_move_a_key_does` perturbs each setting and re-derives every key the
server can derive: every tool identity, and every `XtbSpec` subclass on both backends over every
task. A setting that moves no key must be named in `UNKEYED_BY_DESIGN` with its reason. New
settings, spec classes and tools are covered the day they are added.

**A refusal is not a key**: only probes answering a real key on both sides are compared.

The directional tests below say which key a knob moves and which it must leave alone: a false hit
is a wrong answer, a false miss is a needless minutes-long recompute. Which applies depends on the
resolved backend.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Literal, get_args, get_origin

import annotated_types
import pytest
from chemclaw_mcp_calc.engine import identity, xtb_props
from chemclaw_mcp_calc.engine.config import CalcSettings, settings
from chemclaw_mcp_calc.engine.crest_search import EnsembleSpec
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec
from chemclaw_mcp_calc.engine.xtb_props import PropertiesSpec
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec, XtbTask
from pydantic.fields import FieldInfo

# A geometry cheap enough to build in a loop: no RDKit embedding, no SCF, and every key derivation
# below is pure hashing over it.
WATER = Structure(
    elements=[8, 1, 1],
    positions=[[0.0, 0.0, 0.0], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]],
    smiles="O",
)

# **Settings that reach no key, each with the reason it cannot.** This is the list the measurement
# is allowed to find empty-handed; everything else must move a key or fail. Each entry is a claim
# about the code rather than a note, so it says which *kind* of claim it is.
UNKEYED_BY_DESIGN: dict[str, str] = {
    # Keyed through `calc_version` (`binary_version()` resolves this path), but unmeasurable here:
    # neither binary is installed and both readers are cached, so every name answers `"absent"`.
    "xtb_binary": "selects the program `backend_version` reads a version from: keyed through "
    "calc_version wherever the binary exists, unmeasurable where it does not",
    "crest_binary": "selects the program `CrestSpec.calc_version` reads a version from; the same "
    "case as xtb_binary",
    # Like `xtb_binary`: selecting `xtb` routes to the CLI, whose version enters `calc_version`, so
    # it is keyed where the binary exists. This suite ships no `xtb`, so that choice refuses, and a
    # refusal is not a key; `tblite` and `auto` key normally.
    "xtb_engine": "selects the backend whose version `calc_version` reads: keyed through it "
    "wherever the binary exists, and refused rather than keyed where it does not, which "
    "`test_no_tool_keys_a_program_this_image_lacks_under_an_explicit_engine_setting` holds",
    # Budgets. `config.py` states this rule itself: they decide whether an answer comes back, not
    # what it is, and keying on one would fork the cache every time a deployment gave itself more
    # time.
    "xtb_cli_timeout_seconds": "a budget: it decides whether an answer comes back, not what it is",
    "xtb_inline_timeout_seconds": "a budget, as above",
    "crest_timeout_seconds": "a budget, as above",
    # Resource knobs — how much of the pod one run may use, which prices the work rather than
    # defining it. (A CREST ensemble is genuinely not a function of its key, and that has nothing to
    # do with `-T`; see `CrestSpec` in `engine/xtb_spec.py`.)
    "xtb_cli_threads": "a resource knob: how much of the pod one run may use",
    "crest_threads": "a resource knob, as above",
    # Refusal bounds. They decide whether the calculation runs at all, and a refused call writes no
    # row — so there is nothing for a second configuration to be served.
    "xtb_max_atoms": "a refusal bound: it decides whether the calculation runs at all",
    "xtb_hessian_max_atoms": "a refusal bound, as above",
    "calc_max_concurrent_requests": "an admission bound: it decides whether the call is accepted",
    # Not a refusal bound: above it a member travels with `smiles=None`, which moves the payload. It
    # is unkeyed because a CREST ensemble is already not a function of its key (unseeded and
    # stochastic, first writer wins), so keying it would re-address the most expensive rows for
    # nothing.
    "crest_perceive_max_atoms": "moves an ensemble member's label, on a payload that is already "
    "not key-determined because the search is stochastic — see CrestSpec",
    # logD is the one calculation on this server with no key at all, and says so in a `caveat`
    # rather than by omission: Chemclaw3 never cached it, because its expensive half is a cached pKa
    # and Crippen LogP is sub-millisecond. With no row to address there is no row to fork.
    "logd_default_ph": "logD has no cache key, so there is no row to fork",
    "logd_negligible_ionised_fraction": "logD has no cache key, as above",
}

# `pka_solvent` is validated against ALPB's table, so a made-up value would be refused rather than
# keyed; it needs an explicit alternative.
PERTURBATIONS: dict[str, Any] = {"pka_solvent": "methanol"}

_REFUSED = "refused"


def _import_every_engine_module() -> None:
    """Import the whole engine package, so `__subclasses__` sees every spec that exists.

    A spec class in a module nothing else imports would otherwise be invisible to the probe — which
    is the same "a list of what the tree looked like" failure one level up.
    """
    from chemclaw_mcp_calc import engine

    for module in pkgutil.walk_packages(engine.__path__, f"{engine.__name__}."):
        importlib.import_module(module.name)


def _spec_probes() -> list[XtbSpec]:
    """Every concrete spec this server can build, on both backends.

    Tool identities use the configured engine, so without both engines a binary-only knob such as
    `opt_level` would never be probed.
    """
    _import_every_engine_module()
    classes: list[type[XtbSpec]] = [XtbSpec]
    pending: list[type[XtbSpec]] = [XtbSpec]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if subclass not in classes:
                classes.append(subclass)
                pending.append(subclass)
    probes: list[XtbSpec] = []
    for cls in classes:
        field = cls.model_fields["task"]
        tasks = get_args(XtbTask) if field.is_required() else [field.default]
        for task in tasks:
            for engine in ("tblite", "xtb"):
                probes.append(cls(task=task, engine=engine))
    return probes


# Built once: the RDKit embedding is the only part of a snapshot that is not pure hashing.
_STRUCTURE = structure_from_smiles("O", optimize=True)
_ARGUMENTS: dict[str, Any] = {
    "smiles": "O",
    "structure": _STRUCTURE.model_dump(),
    "charge": 0,
    "atoms": [0, 1],
    "value": 1.0,
}


def _snapshot() -> dict[str, str]:
    """Every key this server can derive right now, labelled by what derived it."""
    keys: dict[str, str] = {}
    for tool, (accepts, _) in identity.COMPUTE_TOOLS.items():
        arguments = {name: value for name, value in _ARGUMENTS.items() if name in accepts}
        try:
            answer = identity.calculation_identity(tool, arguments)
        except ValueError:
            keys[f"tool:{tool}"] = _REFUSED
        else:
            keys[f"tool:{tool}"] = f"{answer.calc_key}|{answer.calc_version}|{answer.structure_id}"
    for spec in _spec_probes():
        label = f"spec:{type(spec).__name__}:{spec.task}:{spec.engine}"
        try:
            keys[label] = spec.cache_key(_STRUCTURE).as_str()
        except ValueError:
            keys[label] = _REFUSED
    return keys


def _within_bounds(field: FieldInfo, value: Any) -> bool:
    """Whether `value` satisfies the field's own constraints, read off its metadata."""
    for bound in field.metadata:
        if isinstance(bound, annotated_types.Gt) and not value > bound.gt:
            return False
        if isinstance(bound, annotated_types.Ge) and not value >= bound.ge:
            return False
        if isinstance(bound, annotated_types.Lt) and not value < bound.lt:
            return False
        if isinstance(bound, annotated_types.Le) and not value <= bound.le:
            return False
    return True


def _candidates(name: str, field: FieldInfo) -> list[Any]:
    """Values to try for `name`, derived from its declared type and its own constraints.

    Several, because one guess can hide the answer; a setting counts as keyed if any valid
    alternative moves a key.
    """
    if name in PERTURBATIONS:
        return [PERTURBATIONS[name]]
    current = getattr(settings, name)
    if get_origin(field.annotation) is Literal:
        return [member for member in get_args(field.annotation) if member != current]
    if isinstance(current, str):
        return [f"{current}-perturbed"]
    tried: list[Any] = []
    for raw in (current + 1, current * 2 + 1, current / 2, current - 1, 1, 3):
        value = type(current)(raw)
        if value != current and value not in tried and _within_bounds(field, value):
            tried.append(value)
    assert tried, f"no valid alternative could be derived for {name!r}"
    return tried


def _moves_a_key(name: str, field: FieldInfo, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Whether any valid alternative value for `name` changes a key this server derives."""
    before = _snapshot()
    for value in _candidates(name, field):
        with monkeypatch.context() as patched:
            patched.setattr(settings, name, value)
            after = _snapshot()
        moved = [
            label
            for label, key in before.items()
            if key != after.get(label) and _REFUSED not in (key, after.get(label))
        ]
        if moved:
            return True
    return False


def test_every_setting_that_can_move_a_key_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """The completeness guard: derived from `model_fields`, so a new knob is covered on arrival."""
    unkeyed = [
        name
        for name, field in CalcSettings.model_fields.items()
        if not _moves_a_key(name, field, monkeypatch)
    ]
    unexplained = sorted(set(unkeyed) - set(UNKEYED_BY_DESIGN))
    assert not unexplained, (
        f"these settings move no key: {unexplained}.\n"
        "If one of them can change a stored payload, thread it into the spec of the calculation "
        "that reads it, exactly as `PropertiesSpec.bond_order_threshold` was — `cache_key` derives "
        "from `model_dump()`, so a spec field is keyed by construction. If it genuinely cannot "
        "reach a number, add it to UNKEYED_BY_DESIGN with the reason, which is a claim a reviewer "
        "can check."
    )


def test_the_unkeyed_allowlist_names_only_real_settings() -> None:
    """A stale exemption is an exemption for a setting that no longer exists — and a live hole.

    The name it once excused is gone; the entry stays, and the next setting that lands with a
    similar name inherits an argument nobody made for it.
    """
    stale = sorted(set(UNKEYED_BY_DESIGN) - set(CalcSettings.model_fields))
    assert not stale, f"UNKEYED_BY_DESIGN names settings that do not exist: {stale}"
    unused = sorted(set(PERTURBATIONS) - set(CalcSettings.model_fields))
    assert not unused, f"PERTURBATIONS names settings that do not exist: {unused}"


def test_the_probe_answers_in_both_directions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the probe can answer both "keyed" and "unkeyed".

    The trust radius reaches every optimisation's key and the inline budget reaches none.
    """
    fields = CalcSettings.model_fields
    assert _moves_a_key("xtb_opt_trust_radius", fields["xtb_opt_trust_radius"], monkeypatch)
    assert not _moves_a_key(
        "xtb_inline_timeout_seconds", fields["xtb_inline_timeout_seconds"], monkeypatch
    )


def test_the_trust_radius_moves_the_optimisation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling on one step decides which stationary point is reached, so it is in the key.

    Different trust radii relax ethanol to different geometries, and `structure_id` derives every
    downstream key from that geometry.
    """
    monkeypatch.setattr(settings, "xtb_opt_trust_radius", 0.35)
    default = OptSpec(engine="tblite").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_opt_trust_radius", 0.05)
    tuned = OptSpec(engine="tblite").cache_key(WATER)
    assert default.params_hash != tuned.params_hash


def test_the_ancopt_convergence_level_moves_the_binary_optimisation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--opt <level>` is xtb's own convergence criterion, so it decides where the run stops."""
    monkeypatch.setattr(settings, "xtb_cli_opt_level", "vtight")
    tight = OptSpec(engine="xtb").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_cli_opt_level", "crude")
    crude = OptSpec(engine="xtb").cache_key(WATER)
    assert tight.params_hash != crude.params_hash


@pytest.mark.parametrize("task", ["atomic", "surface"])
def test_the_cli_accuracy_moves_the_key_of_every_calculation_the_binary_runs(
    task: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--acc` scales xtb's SCF and integral thresholds, so it produces the numbers being stored.

    Both tasks are binary-only (`_FIXED_BACKEND`), so the knob is never inert for them.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    loose = XtbSpec(task=task, engine="xtb").cache_key(WATER)  # type: ignore[arg-type]
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    tight = XtbSpec(task=task, engine="xtb").cache_key(WATER)  # type: ignore[arg-type]
    assert loose.params_hash != tight.params_hash


def test_the_cli_accuracy_moves_a_hessian_key_only_where_the_binary_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Hessian dispatches, so `--acc` is keyed on the binary backend and inert in process.

    The key names what runs; tblite has no such knob, so keying it there would recompute for
    nothing.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    binary_loose = HessianSpec(engine="xtb").cache_key(WATER)
    library_loose = HessianSpec(engine="tblite").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    assert HessianSpec(engine="xtb").cache_key(WATER).params_hash != binary_loose.params_hash
    assert HessianSpec(engine="tblite").cache_key(WATER).params_hash == library_loose.params_hash


def test_a_knob_no_backend_of_this_calculation_reads_stays_out_of_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sp`, `properties` and `fukui` are in-process whatever is configured, and crest reads none.

    `_FIXED_BACKEND` pins the three to tblite, which has no `--acc`, and crest gets no accuracy
    flag. Over-keying costs a minutes-long recompute.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    before = [XtbSpec(task=task).cache_key(WATER) for task in ("sp", "properties", "fukui")]
    before.append(EnsembleSpec(engine="xtb").cache_key(WATER))
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    after = [XtbSpec(task=task).cache_key(WATER) for task in ("sp", "properties", "fukui")]
    after.append(EnsembleSpec(engine="xtb").cache_key(WATER))
    assert [key.as_str() for key in before] == [key.as_str() for key in after]


def test_a_frozen_atom_optimisation_is_keyed_as_the_backend_that_really_runs_it() -> None:
    """The binary cannot hold an atom fixed, so a constrained spec resolves in-process.

    The fallback must be decided before the key is derived, or a scan point would be keyed under a
    program that did not run. `for_structure` answers what will actually run.
    """
    free = OptSpec(engine="xtb")
    constrained = OptSpec(engine="xtb", frozen_atoms=(0,))
    assert free.for_structure(WATER).engine == "xtb"
    assert constrained.for_structure(WATER).engine == "tblite"
    assert "tblite" in constrained.cache_key(WATER).calc_version
    assert "+xtb+" not in constrained.cache_key(WATER).calc_version


def test_one_solvent_spelled_five_ways_is_one_calculation() -> None:
    """One solvent spelled five ways is one calculation.

    The name is matched case- and whitespace-insensitively, with aliases such as `"h2o"`, so it is
    hashed in canonical form; otherwise each spelling is a separate expensive row.
    """
    keys = {
        PropertiesSpec(solvent=name).cache_key(WATER).params_hash
        for name in ("water", "Water", "WATER", " water", "h2o")
    }
    assert len(keys) == 1


def test_the_bond_order_threshold_moves_the_electronic_properties_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bond-order threshold filters the payload, so it must move the properties key.

    A shared key would be a wrong answer: rows would be missing bonds with nothing saying so. An
    argument outside the key may permute the answer but not remove from it.
    """
    monkeypatch.setattr(settings, "xtb_bond_order_threshold", 0.5)
    default = PropertiesSpec().cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_bond_order_threshold", 0.05)
    permissive = PropertiesSpec().cache_key(WATER)
    assert default.params_hash != permissive.params_hash


def test_two_thresholds_are_two_payloads_and_therefore_two_keys() -> None:
    """Two thresholds are two payloads and therefore two keys, driven through a real SCF.

    Payloads are compared before keys, so a field keyed but ignored by the calculator also fails.
    Acetic acid gives a different bond count at 0.5 and 0.05.
    """
    spec, structure = xtb_props.properties_inputs("CC(=O)O")
    default = xtb_props.compute_properties(spec, structure)
    permissive = xtb_props.compute_properties(
        spec.model_copy(update={"bond_order_threshold": 0.05}), structure
    )
    assert len(permissive.bond_orders) > len(default.bond_orders), (
        "the threshold no longer reaches the payload, so this test proves nothing about the key"
    )
    assert default.calc_key != permissive.calc_key
