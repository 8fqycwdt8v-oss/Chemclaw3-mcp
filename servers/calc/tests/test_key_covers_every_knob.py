"""Every setting that can move a number moves a key — derived from `CalcSettings.model_fields`.

`xtb_spec.py`'s module docstring states the rule this file enforces: "someone adds a knob and
forgets to key on it, and the next run silently serves a result computed under the old setting". The
`model_dump()` derivation makes a *spec field* keyed by construction; it does nothing at all for a
setting a compute path reads out of `settings` directly.

**Measured before the first fix**, same input structure, same key, different geometry and different
energy:

    floor=1.0    key=xtb.opt@…:389b625b3220108a:5e9dada5819590e9  E=-11.394329102251229  steps=9
    floor=0.005  key=xtb.opt@…:389b625b3220108a:5e9dada5819590e9  E=-11.394339129461754  steps=25

That is a wrong-answer cache on the caller's side, which is the worst thing this seam can produce: a
`relax_structure` row written by one pod is served to another whose configuration would never have
produced that geometry, and `structure_id` is the `input_hash` of every downstream Hessian,
properties and scan key — so the fork propagates through the whole chain.

## Why this file is a measurement and no longer a list

It used to open by saying "three did", name those three, and stop. That sentence describes what the
tree looked like the week somebody wrote it, and it was wrong: `xtb_bond_order_threshold` was the
fourth, read inside `compute_properties` and in no key at all — measured on acetic acid with a real
tblite SCF, 7 bonds at 0.5 and 9 at 0.05 under a byte-identical
`xtb.properties@…:e67f316106051ef5:74c818075e77fec2`. A hand-written enumeration cannot fail on the
commit that adds a fifth, because the enumeration *is* the thing that would have to change.

So `test_every_setting_that_can_move_a_key_does` takes the setting names from
`CalcSettings.model_fields` and, for each one, perturbs it and re-derives **every** key this server
can derive: the identity of every tool in `identity.COMPUTE_TOOLS`, and the cache key of every
concrete `XtbSpec` subclass on both backends over every task it accepts. A setting whose
perturbation moves no key must be named in `UNKEYED_BY_DESIGN` with the reason it cannot; anything
else fails. A new setting is therefore covered the day it is added, and so is a new spec class or a
new tool.

**A refusal is not a key.** Perturbing an admission bound to 1 makes every probe refuse, and a
refusal compared against a key looks exactly like a key that moved — which reported `xtb_max_atoms`
as keyed while measuring nothing. Only probes that answer a real key on *both* sides of the
perturbation are compared.

## The directional tests below are not the completeness guard

They stay because they say something the measurement cannot: **which** key a knob moves, and — the
half that is a false-*miss* rather than a false hit — which key it must leave alone. A knob that
reaches the calculation must move the key (a false hit is a wrong answer); a knob that cannot reach
it must not (a false miss is CPU spent for nothing, and on this server a repeat is minutes to
hours). Which of the two a knob is depends on the resolved backend, because that is what decides
whether the binary or the in-process library runs — so `unkeyed_fields` reads the resolved engine
and these tests read it with it.
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
    # Keyed through `calc_version`, and unmeasurable from here. `backend_version` reads
    # `xtb_cli.binary_version()` / `crest_cli.binary_version()`, which resolve *this* setting to a
    # path — so naming a different build is a different version string on a deployment that has one.
    # Neither binary is installed in this suite's environment and both readers are `lru_cache`d, so
    # every name answers `"absent"` here and no perturbation can move anything.
    "xtb_binary": "selects the program `backend_version` reads a version from: keyed through "
    "calc_version wherever the binary exists, unmeasurable where it does not",
    "crest_binary": "selects the program `CrestSpec.calc_version` reads a version from; the same "
    "case as xtb_binary",
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
    # **Not a refusal bound, and worth reading before this entry is copied.** Above this atom count
    # an ensemble member travels with `smiles=None` instead of a perceived label, so it does move
    # the payload — the shape `xtb_bond_order_threshold` was fixed for. It is not keyed because a
    # CREST ensemble is already not a function of its key: the search is stochastic and no seed is
    # set, so `search_conformer_ensemble` is reproducible only through the cache, first writer wins
    # (`CrestSpec`). Adding a deterministic field to the key of a payload that is not deterministic
    # buys nothing and re-addresses the most expensive rows in the system.
    "crest_perceive_max_atoms": "moves an ensemble member's label, on a payload that is already "
    "not key-determined because the search is stochastic — see CrestSpec",
    # logD is the one calculation on this server with no key at all, and says so in a `caveat`
    # rather than by omission: Chemclaw3 never cached it, because its expensive half is a cached pKa
    # and Crippen LogP is sub-millisecond. With no row to address there is no row to fork.
    "logd_default_ph": "logD has no cache key, so there is no row to fork",
    "logd_negligible_ionised_fraction": "logD has no cache key, as above",
}

# The one setting whose alternative value cannot be derived from its type. Every other string here
# is free text that lands in a version string, so `<value>-perturbed` is a valid perturbation;
# `pka_solvent` is validated against ALPB's own table, so a made-up name is *refused* rather than
# keyed differently, and a refusal is not a measurement.
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

    The tool identities alone are not enough: `identity` builds each spec with the *configured*
    engine, so on an image without the xtb binary nothing would ever probe a binary-only knob such
    as `opt_level`. Naming both engines explicitly is what keeps those measurable everywhere.
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

    More than one, because a single guess can be wrong in the direction that hides the answer:
    perturbing `xtb_engine` from `"auto"` to `"tblite"` moves nothing on an image with no xtb
    binary, while `"xtb"` moves every dispatching task's key. So a setting counts as keyed if *any*
    valid alternative moves a key.
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
    """The control: a detector that only ever said "keyed" would pass the guard above forever.

    Two settings whose answers are settled by the directional tests below — the ANC curvature floor
    reaches every optimisation's key, and the inline budget reaches nothing — so this asserts the
    measurement can produce both answers rather than one.
    """
    fields = CalcSettings.model_fields
    assert _moves_a_key("xtb_anc_curvature_floor", fields["xtb_anc_curvature_floor"], monkeypatch)
    assert not _moves_a_key(
        "xtb_inline_timeout_seconds", fields["xtb_inline_timeout_seconds"], monkeypatch
    )


def test_the_anc_curvature_floor_moves_the_optimisation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is the stand-in for the terms the pairwise model cannot see — it moves the answer.

    `anc.basis` used to read it from `settings` inside the optimizer loop, which put it in no key at
    all. Measured on ethanol, 1.0 and 0.005 relax to different geometries and different energies,
    so it is exactly the case `OptSpec.trust_radius`'s own comment describes: "it moves the answer
    and a setting that moves the answer belongs in the key".
    """
    monkeypatch.setattr(settings, "xtb_anc_curvature_floor", 1.0)
    default = OptSpec(engine="tblite").cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_anc_curvature_floor", 0.005)
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

    The per-atom panel *is* those numbers — charges, coordination numbers, C6 coefficients and
    polarisabilities — and the surface potential is a grid computed under the same threshold. Both
    tasks are binary-only (`_FIXED_BACKEND`), so there is no configuration in which the knob is
    inert here.
    """
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 1.0)
    loose = XtbSpec(task=task, engine="xtb").cache_key(WATER)  # type: ignore[arg-type]
    monkeypatch.setattr(settings, "xtb_cli_accuracy", 0.05)
    tight = XtbSpec(task=task, engine="xtb").cache_key(WATER)  # type: ignore[arg-type]
    assert loose.params_hash != tight.params_hash


def test_the_cli_accuracy_moves_a_hessian_key_only_where_the_binary_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Hessian dispatches, so the same knob is keyed on one backend and inert on the other.

    Both halves in one test because they are one statement: the key names what runs. The in-process
    finite-difference path never passes `--acc` to anything — tblite has no such knob — so keying on
    it there would recompute every stored Hessian for a setting that could not have touched it.
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

    The first three are pinned to tblite by `_FIXED_BACKEND`, so `--acc` cannot reach them; a CREST
    search shells out to `crest`, which this server hands no accuracy flag. Over-keying costs a
    recompute rather than a wrong answer, and on this server a recompute is minutes to hours.

    **The reason this test used to give was the pre-correction sentence of the file it cites.** It
    said `xtb.sp`'s key is "pinned byte-for-byte against Chemclaw3's own derivation" in
    `test_key_contract.py`. That test now says the opposite in as many words: the strings are
    *this* repository's, measured against its own installed tblite and RDKit, and no cross-repo
    agreement is asserted anywhere — `remote_key` deliberately re-derives nothing on that side. The
    exclusion stands on its own merits, which are `_FIXED_BACKEND` pinning these three to tblite,
    a library with no `--acc`.
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

    `_optimize_with_binary` falls back to the Cartesian path whenever `frozen_atoms` is set — that
    is the only way frozen atoms work at all — but the fallback happened *after* the key was
    derived, so a scan point on a deployment with the binary was stored under a `calc_version`
    naming a program that had not run. `for_structure`'s whole job is to answer "what will actually
    run", and this is the third thing it has to answer it about, beside the fixed-backend tasks and
    the open-shell fallback.
    """
    free = OptSpec(engine="xtb")
    constrained = OptSpec(engine="xtb", frozen_atoms=(0,))
    assert free.for_structure(WATER).engine == "xtb"
    assert constrained.for_structure(WATER).engine == "tblite"
    assert "tblite" in constrained.cache_key(WATER).calc_version
    assert "+xtb+" not in constrained.cache_key(WATER).calc_version


def test_one_solvent_spelled_five_ways_is_one_calculation() -> None:
    """The name is matched case- and whitespace-insensitively, then hashed *as written*.

    So `"water"`, `"Water"` and `" water"` were three cache rows for one ALPB calculation, and
    `"h2o"` — the same entry in tblite's own table — was a fourth. On this server's cost profile
    that is the expensive kind of waste: a solvated Hessian or CREST search is minutes to hours,
    paid again for a number already on disk.
    """
    keys = {
        PropertiesSpec(solvent=name).cache_key(WATER).params_hash
        for name in ("water", "Water", "WATER", " water", "h2o")
    }
    assert len(keys) == 1


def test_the_bond_order_threshold_moves_the_electronic_properties_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It **filters the payload**, so a shared key is a wrong answer rather than a stale one.

    Measured on acetic acid with a real tblite SCF, before `PropertiesSpec` existed:

        threshold=0.5   bonds=7  key=xtb.properties@…:e67f316106051ef5:74c818075e77fec2
        threshold=0.05  bonds=9  key=xtb.properties@…:e67f316106051ef5:74c818075e77fec2

    `_bond_orders` read the setting inside `compute_properties`, outside every spec, so
    `model_dump()` never saw it. Two bonds are missing from the first pod's answer and nothing on
    the row says so — which is the case `identity._site_reactivity` already forbids in as many
    words: an argument outside the key may permute the answer and may not remove from it.
    """
    monkeypatch.setattr(settings, "xtb_bond_order_threshold", 0.5)
    default = PropertiesSpec().cache_key(WATER)
    monkeypatch.setattr(settings, "xtb_bond_order_threshold", 0.05)
    permissive = PropertiesSpec().cache_key(WATER)
    assert default.params_hash != permissive.params_hash


def test_two_thresholds_are_two_payloads_and_therefore_two_keys() -> None:
    """The whole defect in one assertion, driven through a real SCF rather than through a hash.

    Keying the field is only half of it: a spec field defaulted from `settings` and then ignored by
    the calculator would key one threshold and apply another, which is the same fork wearing a
    passing test. So this runs both, compares the *payloads*, and only then compares the keys —
    which is the order the failure happened in. Acetic acid because that is what the finding was
    measured on: 7 bonds at 0.5, 9 at 0.05.
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
