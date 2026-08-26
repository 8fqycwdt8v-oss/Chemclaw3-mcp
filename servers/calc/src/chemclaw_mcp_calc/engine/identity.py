"""The key of a calculation, **before** it is run — what makes a remote cache lookup possible.

## Why returning the key on the result was not enough

Chemclaw3's cache seam is `science/calc/store.py::cached_compute`:

```python
hit = await store.get(key)
if hit is not None:
    return hit.result, True
result = await compute()
```

**The key is needed to do the lookup, so it is needed before the compute.** A `calc_key` that only
arrives *on the result* is unusable there: on a hit there is no result to read it off. The only
remaining way for Chemclaw3 to fill that argument would be to derive the key locally — which is
precisely the silent divergence the split was supposed to remove, because `calc_version` is built
from `tblite`/`rdkit` distribution versions, a Hamiltonian revision, an `xtb --version` subprocess
and seven pKa calibration settings that a Chemclaw3 pod does not have, and
`xtb_cli.binary_version()` answers `"absent"` rather than raising.

So this module answers the question one round trip earlier, and cheaply: canonicalise, embed, hash,
read the versions. **No SCF** — `tests/test_calculation_key.py` proves it by making every path
through `Calculator` raise and asking for all nine identities anyway.

The wrapper on the Chemclaw3 side then reads:

    identity = await remote.calculation_key(tool, arguments)   # one cheap call
    hit = await store.get(identity.key)                        # the four parts, ready to use
    if hit is not None:
        return hit.result, True
    result = await remote.<tool>(**arguments)                  # only on a miss

One cheap round trip on a hit instead of an SCF; two calls on a miss, which is noise beside minutes
of CPU.

## `key` is the four parts, not a string to parse

`CalculationKey` is what `store.get` takes, so this returns it whole. That is not a convenience:
`calc_version` legitimately contains both `@` and `:` — `esol-delaney@2004/...` and
`cal-0.28733:-29.3116` — so splitting the flat form is fiddly enough to get wrong, and a
mis-parsed key is a lookup that misses forever rather than an error. The flat `calc_key` string is
returned beside it so a caller can assert it against the one the compute tool later puts on its
result, which is the cheapest possible check that the two paths agree.

**What this leaves as the only thing the two repositories must still keep in step**: the value of
`CALCULATION_EPOCH`. Not the config (only this server reads it), not the RDKit build (only this
server embeds), not the flat-string format (nobody parses it). One constant.

## What is covered, and the one tool that is not

Every **calculation** is here — the eight backing Chemclaw3's SMILES-in tools, and the six
structure-in primitives Chemclaw3's activities compose. Three tools are deliberately absent because
they are not calculations and nothing stores their output: `embed_structure` and
`combine_structures` build geometries (cheap, pure, and the *input* to a key rather than a keyed
thing), and `calculation_key` is this probe itself.

`predict_logd` is the one calculation with no key, and it says so in a `caveat` rather than by
omission. Chemclaw3 never cached logD, because its expensive half is already a cached pKa and
Crippen LogP is sub-millisecond, so there is no key derivation to port.

There was briefly a second: `compute_thermochemistry`, whose key named the geometry its refinement
loop settled on and was therefore an output rather than a function of its arguments. That
underivable key was the structural signal that a *composite* does not belong on this server at all,
and the tool was removed rather than shipped uncacheable — Chemclaw3 assembles the same answer from
`relax_structure` + `compute_hessian` + its own RRHO arithmetic, and every part of it caches. See
`servers/calc/README.md`.

**The CREST searches key like anything else, and refuse like nothing else.** `CrestSpec
.calc_version()` answers `crest-absent` when the binary is missing rather than raising, so a key
*is* derivable with no crest — and it would name a program that cannot run, addressing a row nothing
will ever write. So the derivation calls `crest_search.require_crest()` exactly as the compute path
does: the probe refuses precisely where the calculation would.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from chemclaw_mcp_calc.engine import (
    crest_search,
    descriptors,
    logd,
    pka,
    solubility,
    xtb,
    xtb_props,
)
from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.key import CalculationKey
from chemclaw_mcp_calc.engine.scan import scan_point_inputs
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimization_inputs
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = ["COMPUTE_TOOLS", "CalculationIdentity", "calculation_identity"]


class CalculationIdentity(BaseModel):
    """What one calculation *would* be stored under, answered without running it.

    `calc_version` is always present. `key` and `calc_key` are the same identity in the two shapes a
    caller needs — the four fields `store.get` takes, and the flat string the compute tool will
    later put on its result — and both are `None` for the two tools whose key is not derivable from
    their arguments, with `caveat` saying which case it is and what to do instead.
    """

    tool: str
    # The version string this calculation's results are keyed and calibrated under. Present even
    # when there is no key: it is what Chemclaw3's calibration ledger matches on, and the ledger is
    # keyed per prediction rather than per cache entry.
    calc_version: str
    key: CalculationKey | None = None
    calc_key: str | None = None
    # The content address of the geometry the calculation runs on, where it runs on one. Worth
    # returning on its own: it is the cheapest way for a caller to see that the two sides agree
    # about *which molecule* is being asked for, before any energy exists to compare.
    structure_id: str | None = None
    caveat: str | None = None


def _from_spec(tool: str, spec: XtbSpec, structure: Structure) -> CalculationIdentity:
    """The identity of running `spec` on `structure`, resolved backend and all.

    `XtbSpec.cache_key` applies `for_structure` itself, so the version reported here is the one that
    will actually run — tblite for an open shell even where the binary is configured.
    """
    key = spec.cache_key(structure)
    return CalculationIdentity(
        tool=tool,
        calc_version=key.calc_version,
        key=key,
        calc_key=key.as_str(),
        structure_id=structure.structure_id,
    )


def _xtb_energy(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_xtb_energy` — the spec and geometry come from `xtb.sp_inputs`, one definition."""
    job = xtb.XtbInput(smiles=str(arguments["smiles"]), charge=int(arguments.get("charge", 0)))
    return _from_spec("compute_xtb_energy", *xtb.sp_inputs(job))


def _electronic_properties(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_electronic_properties`."""
    return _from_spec(
        "compute_electronic_properties",
        *xtb_props.properties_inputs(str(arguments["smiles"]), _solvent(arguments)),
    )


def _site_reactivity(arguments: dict[str, Any]) -> CalculationIdentity:
    """`predict_site_reactivity` — `mode` and `top_n` are accepted and do not enter the key.

    Neither changes what is computed: the three single points are mode-independent and `top_n` only
    truncates the ranking. Accepting them anyway matters, because a caller passes the compute tool's
    arguments through unchanged and must not have to know which of them are keyed.
    """
    return _from_spec("predict_site_reactivity", *xtb_props.fukui_inputs(str(arguments["smiles"])))


def _optimize_geometry(arguments: dict[str, Any]) -> CalculationIdentity:
    """`optimize_geometry`."""
    return _from_spec(
        "optimize_geometry",
        *optimization_inputs(str(arguments["smiles"]), _solvent(arguments)),
    )


def _pka(arguments: dict[str, Any]) -> CalculationIdentity:
    """`predict_pka` — no geometry needed: the key is on the canonical SMILES.

    `pka_cache_key` expects the canonical form, exactly as `predict_pka` gives it, so this goes
    through the same canonicalisation rather than trusting the caller's spelling.
    """
    canonical = require_canonical_smiles(str(arguments["smiles"]))
    key = pka.pka_cache_key(pka.PkaInput(smiles=canonical))
    return CalculationIdentity(
        tool="predict_pka", calc_version=key.calc_version, key=key, calc_key=key.as_str()
    )


def _solubility(arguments: dict[str, Any]) -> CalculationIdentity:
    """`predict_solubility`."""
    key = solubility.cache_key(solubility.SolubilityInput(smiles=str(arguments["smiles"])))
    return CalculationIdentity(
        tool="predict_solubility", calc_version=key.calc_version, key=key, calc_key=key.as_str()
    )


def _developability(arguments: dict[str, Any]) -> CalculationIdentity:
    """`predict_developability_profile`."""
    key = descriptors.cache_key(descriptors.DescriptorInput(smiles=str(arguments["smiles"])))
    return CalculationIdentity(
        tool="predict_developability_profile",
        calc_version=key.calc_version,
        key=key,
        calc_key=key.as_str(),
    )


def _logd(arguments: dict[str, Any]) -> CalculationIdentity:
    """`predict_logd` — a version, and no key, because Chemclaw3 never gave logD one."""
    return CalculationIdentity(
        tool="predict_logd",
        calc_version=logd.calc_version(),
        caveat=(
            "logD has no cache key: Chemclaw3 never gave it one, because its expensive half is "
            "already a cached pKa and Crippen LogP is sub-millisecond. Its cost is one pKa, whose "
            "own key is available from calculation_key('predict_pka', {'smiles': ...})"
        ),
    )


def _structure(arguments: dict[str, Any]) -> Structure:
    """The `structure` argument, validated through the same model the compute path uses.

    A caller sends a geometry as JSON; `Structure` rounds and validates it on construction exactly
    as it did when this server produced it, so a payload that was truncated or edited in transit
    fails here rather than keying a geometry nobody computed.
    """
    return Structure.model_validate(arguments["structure"])


def _relax_structure(arguments: dict[str, Any]) -> CalculationIdentity:
    """`relax_structure` — an ordinary optimisation of a geometry the caller already holds."""
    structure = _structure(arguments)
    frozen = tuple(int(index) for index in arguments.get("frozen_atoms") or ())
    spec = OptSpec(solvent=_solvent(arguments), frozen_atoms=frozen)
    return _from_spec("relax_structure", spec, structure)


def _properties_at(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_properties_at` — the same `xtb.properties` calculation as the SMILES-in tool.

    Same `calc_type`, so a properties row computed from a SMILES and one computed at the identical
    geometry are one entry rather than two. That is the whole reason both tools exist without
    duplicating anything: they differ in how the caller names the subject, not in what is computed.
    """
    return _from_spec(
        "compute_properties_at",
        XtbSpec(task="properties", solvent=_solvent(arguments)),
        _structure(arguments),
    )


def _fukui_at(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_fukui_at` — the same `xtb.fukui` calculation as the SMILES-in tool.

    Same `calc_type` as `predict_site_reactivity`, for the reason `_properties_at` gives one
    function up: the two differ in how the caller names the subject, not in what is computed.

    **`mode` is absent from the key and `solvent` is present**, and that asymmetry against the
    SMILES-in twin is real rather than an oversight. `mode` only chooses the sort — the three
    single points are the same three whichever attack is asked about, which is why `ranked_for`
    exists — so keying on it would make a cache hit authoritative about an ordering it never chose.
    `solvent` is in because this tool takes one and `predict_site_reactivity` does not.
    """
    return _from_spec(
        "compute_fukui_at",
        XtbSpec(task="fukui", solvent=_solvent(arguments)),
        _structure(arguments),
    )


def _hessian(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_hessian` — keyed on the geometry and what moves the matrix, and nothing else.

    `HessianSpec` is deliberately narrower than the thermochemistry spec Chemclaw3 wraps it in:
    temperature, pressure, the symmetry number and the quasi-RRHO cutoff are absent because a
    Hessian does not depend on them. That absence is what lets a caller ask for a second temperature
    and pay only the partition functions.
    """
    return _from_spec(
        "compute_hessian", HessianSpec(solvent=_solvent(arguments)), _structure(arguments)
    )


def _scan_point(arguments: dict[str, Any]) -> CalculationIdentity:
    """`scan_point` — driving the coordinate is deterministic, so the key is derivable.

    The driven geometry is a pure function of `(structure, atoms, value)`, so this runs the same
    driver the compute path does and keys the result. It comes out as an `xtb.opt` key, which is
    correct: a scan point *is* a constrained optimisation, and it shares its row with one.
    """
    atoms = tuple(int(index) for index in arguments["atoms"])
    spec, driven = scan_point_inputs(
        _structure(arguments), atoms, float(arguments["value"]), _solvent(arguments)
    )
    return _from_spec("scan_point", spec, driven)


def _conformer_ensemble(arguments: dict[str, Any]) -> CalculationIdentity:
    """`search_conformer_ensemble` — refuses without the binary, exactly as the search does."""
    crest_search.require_crest()
    spec = crest_search.EnsembleSpec(
        search=arguments.get("search", "conformers"),
        effort=arguments.get("effort", "quick"),
        solvent=_solvent(arguments),
        temperature_k=(
            float(arguments.get("temperature_k", 0.0)) or crest_search.EnsembleSpec().temperature_k
        ),
    )
    return _from_spec("search_conformer_ensemble", spec, _structure(arguments))


def _binding_modes(arguments: dict[str, Any]) -> CalculationIdentity:
    """`search_binding_modes` — same refusal, and a version that also names the opt backend."""
    crest_search.require_crest()
    spec = crest_search.ComplexSpec(
        effort=arguments.get("effort", "quick"), solvent=_solvent(arguments)
    )
    return _from_spec("search_binding_modes", spec, _structure(arguments))


def _solvent(arguments: dict[str, Any]) -> str | None:
    """The `solvent` argument, or None for gas phase — never a silently-defaulted empty string."""
    value = arguments.get("solvent")
    return None if value is None else str(value)


# One derivation per compute tool, plus the argument names it accepts.
#
# **The `accepts` sets are not decoration.** A caller passes the compute tool's arguments through
# unchanged, and an argument this module quietly ignored would be the worst possible failure here: a
# misspelled `solvent` would produce the *gas-phase* key, the lookup would hit a real row, and the
# caller would be handed a solvated question's answer computed without solvent. So an unknown name
# is refused, and `tests/test_calculation_key.py` checks every set against the served tool's own
# input schema so the two cannot drift.
COMPUTE_TOOLS: dict[str, tuple[frozenset[str], Callable[[dict[str, Any]], CalculationIdentity]]] = {
    "compute_xtb_energy": (frozenset({"smiles", "charge"}), _xtb_energy),
    "compute_electronic_properties": (frozenset({"smiles", "solvent"}), _electronic_properties),
    "predict_site_reactivity": (frozenset({"smiles", "mode", "top_n"}), _site_reactivity),
    "optimize_geometry": (frozenset({"smiles", "solvent"}), _optimize_geometry),
    "predict_pka": (frozenset({"smiles"}), _pka),
    "predict_solubility": (frozenset({"smiles"}), _solubility),
    "predict_logd": (frozenset({"smiles", "ph"}), _logd),
    "predict_developability_profile": (frozenset({"smiles"}), _developability),
    # The structure-in primitives Chemclaw3's activities compose.
    "relax_structure": (frozenset({"structure", "solvent", "frozen_atoms"}), _relax_structure),
    "compute_properties_at": (frozenset({"structure", "solvent"}), _properties_at),
    "compute_fukui_at": (frozenset({"structure", "solvent", "mode", "top_n"}), _fukui_at),
    "compute_hessian": (frozenset({"structure", "solvent"}), _hessian),
    "scan_point": (frozenset({"structure", "atoms", "value", "solvent"}), _scan_point),
    "search_conformer_ensemble": (
        frozenset({"structure", "search", "effort", "solvent", "temperature_k"}),
        _conformer_ensemble,
    ),
    "search_binding_modes": (frozenset({"structure", "effort", "solvent"}), _binding_modes),
}


def calculation_identity(tool: str, arguments: dict[str, Any]) -> CalculationIdentity:
    """The identity of what `tool` would compute for `arguments`, without computing it.

    Args:
        tool: One of the nine compute tools' names.
        arguments: The arguments that would be passed to it. Every tool requires its subject —
            `smiles` for the eight SMILES-in tools, `structure` for the six primitives — and every
            other argument is optional and takes the compute tool's own default.

    Returns:
        The version, and the key in both shapes where one is derivable.

    Raises:
        ValueError: `tool` is not a compute tool, an argument name is not one that tool takes, or
            `smiles`/`solvent` is invalid — the same refusals, with the same messages, the compute
            tool itself would give, because the same canonicaliser and the same spec validator run.
    """
    known = COMPUTE_TOOLS.get(tool)
    if known is None:
        raise ValueError(
            f"{tool!r} is not a compute tool on this server; expected one of "
            f"{', '.join(sorted(COMPUTE_TOOLS))}"
        )
    accepts, derive = known
    unexpected = sorted(set(arguments) - accepts)
    if unexpected:
        raise ValueError(
            f"{tool} does not take {', '.join(repr(name) for name in unexpected)}; it takes "
            f"{', '.join(sorted(accepts))}. Refused rather than ignored: an ignored argument would "
            "produce the key of a different calculation, and the lookup would then hit a real row "
            "holding an answer to a question nobody asked"
        )
    subject = "structure" if "structure" in accepts else "smiles"
    if subject not in arguments:
        raise ValueError(f"{tool} requires a {subject!r} argument")
    return derive(arguments)
