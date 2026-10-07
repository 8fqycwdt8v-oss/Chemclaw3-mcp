"""The key of a calculation, **before** it is run — what makes a remote cache lookup possible.

Chemclaw3's `cached_compute` needs the key to look up, so it needs it before computing, and it
cannot derive it locally (the version string comes from this server's backends and settings). So
the caller asks `calculation_key` first — canonicalise, embed, hash, read versions, **no SCF** —
and computes only on a miss:

    identity = await remote.calculation_key(tool, arguments)
    hit = await store.get(identity.key)
    if hit is None:
        result = await remote.<tool>(**arguments)

`key` is returned as the four parts `store.get` takes (`calc_version` may contain `@` and `:`),
with the flat `calc_key` beside it for comparison with the compute result.

Every calculation tool is covered except `predict_logd`, which says so in a `caveat`.
`embed_structure`, `combine_structures` and `calculation_key` are not calculations. **The probe
refuses precisely where the calculation would**: a version naming a program this image lacks
(`crest-absent`, `xtb-absent`) is refused rather than returned as a key nothing will ever write.
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
    xtb_atomic,
    xtb_cli,
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
    # The version this calculation is keyed and calibrated under; present even without a key,
    # because the calibration ledger matches on it.
    calc_version: str
    key: CalculationKey | None = None
    calc_key: str | None = None
    # The content address of the geometry, where there is one: the cheapest check that both sides
    # agree which molecule is meant.
    structure_id: str | None = None
    caveat: str | None = None


def _from_spec(tool: str, spec: XtbSpec, structure: Structure) -> CalculationIdentity:
    """The identity of running `spec` on `structure`, resolved backend and all.

    `cache_key` applies `for_structure`, so the reported version is the one that will run.
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
    """`predict_site_reactivity` — `mode` is accepted and does not enter the key.

    `mode` only chooses the sort. An unkeyed argument may permute the stored answer but never remove
    from it, which is why `top_n` is not accepted here.
    """
    return _from_spec("predict_site_reactivity", *xtb_props.fukui_inputs(str(arguments["smiles"])))


def _atomic_descriptors(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_atomic_descriptors` — refuses without the binary, exactly as the panel does.

    `require_binary_backend` is the compute path's own refusal, covering both the absent binary and
    the open-shell fallback, so probe and computation cannot diverge.
    """
    spec, structure = xtb_atomic.atomic_inputs(str(arguments["smiles"]), _solvent(arguments))
    return _from_spec(
        "compute_atomic_descriptors",
        xtb_atomic.require_binary_backend(spec, structure),
        structure,
    )


def _surface_potential(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_surface_potential` — a second xtb run, so a second key, and the same refusal.

    Keyed apart from the atomic panel because the payloads differ.
    """
    spec, structure = xtb_atomic.surface_inputs(str(arguments["smiles"]), _solvent(arguments))
    return _from_spec(
        "compute_surface_potential",
        xtb_atomic.require_binary_backend(spec, structure),
        structure,
    )


def _optimize_geometry(arguments: dict[str, Any]) -> CalculationIdentity:
    """`optimize_geometry`."""
    return _from_spec(
        "optimize_geometry",
        *optimization_inputs(str(arguments["smiles"]), _solvent(arguments)),
    )


def _pka(arguments: dict[str, Any]) -> CalculationIdentity:
    """`predict_pka` — no geometry needed: the key is on the canonical SMILES."""
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

    A geometry truncated or edited in transit fails here rather than keying something never
    computed.
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

    Same `calc_type`, so both ways of naming the subject share one row.
    """
    return _from_spec(
        "compute_properties_at",
        xtb_props.PropertiesSpec(solvent=_solvent(arguments)),
        _structure(arguments),
    )


def _fukui_at(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_fukui_at` — the same `xtb.fukui` calculation as the SMILES-in tool.

    `mode` is unkeyed (it only chooses the sort); `solvent` is keyed, since this tool takes one.
    """
    return _from_spec(
        "compute_fukui_at",
        XtbSpec(task="fukui", solvent=_solvent(arguments)),
        _structure(arguments),
    )


def _hessian(arguments: dict[str, Any]) -> CalculationIdentity:
    """`compute_hessian` — keyed on the geometry and what moves the matrix, and nothing else.

    Temperature, pressure, symmetry number and quasi-RRHO cutoff are not keyed, so a second
    temperature costs only the partition functions.
    """
    return _from_spec(
        "compute_hessian", HessianSpec(solvent=_solvent(arguments)), _structure(arguments)
    )


def _scan_point(arguments: dict[str, Any]) -> CalculationIdentity:
    """`scan_point` — driving the coordinate is deterministic, so the key is derivable.

    The driven geometry is a pure function of `(structure, atoms, value)`; the result is an
    `xtb.opt` key, since a scan point is a constrained optimisation.
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


# One derivation per compute tool, plus the argument names it accepts. An unknown name is refused:
# silently ignoring a misspelt `solvent` would hit the gas-phase row.
# `tests/test_calculation_key.py` holds these sets against each tool's input schema.
COMPUTE_TOOLS: dict[str, tuple[frozenset[str], Callable[[dict[str, Any]], CalculationIdentity]]] = {
    "compute_xtb_energy": (frozenset({"smiles", "charge"}), _xtb_energy),
    "compute_electronic_properties": (frozenset({"smiles", "solvent"}), _electronic_properties),
    "predict_site_reactivity": (frozenset({"smiles", "mode"}), _site_reactivity),
    "compute_atomic_descriptors": (frozenset({"smiles", "solvent"}), _atomic_descriptors),
    "compute_surface_potential": (frozenset({"smiles", "solvent"}), _surface_potential),
    "optimize_geometry": (frozenset({"smiles", "solvent"}), _optimize_geometry),
    "predict_pka": (frozenset({"smiles"}), _pka),
    "predict_solubility": (frozenset({"smiles"}), _solubility),
    "predict_logd": (frozenset({"smiles", "ph"}), _logd),
    "predict_developability_profile": (frozenset({"smiles"}), _developability),
    # The structure-in primitives Chemclaw3's activities compose.
    "relax_structure": (frozenset({"structure", "solvent", "frozen_atoms"}), _relax_structure),
    "compute_properties_at": (frozenset({"structure", "solvent"}), _properties_at),
    "compute_fukui_at": (frozenset({"structure", "solvent", "mode"}), _fukui_at),
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
        tool: One of the seventeen compute tools' names.
        arguments: The arguments that would be passed to it. Every tool requires its subject —
            `smiles` for the ten SMILES-in tools, `structure` for the seven primitives — and every
            other argument is optional and takes the compute tool's own default.

    Returns:
        The version, and the key in both shapes where one is derivable.

    Raises:
        ValueError: `tool` is not a compute tool, an argument name is not one that tool takes, or
            `smiles`/`solvent` is invalid — the same refusals the compute tool itself would give.
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
    return _refuse_a_key_naming_a_program_this_image_lacks(tool, derive(arguments))


def _refuse_a_key_naming_a_program_this_image_lacks(
    tool: str, derived: CalculationIdentity
) -> CalculationIdentity:
    """Return `derived` unless its version names the xtb binary this image lacks; then raise.

    Checked once on the result rather than per derivation, so every compute tool — including those
    that inherit an optimisation's version, like pKa and logD — is covered with nothing to write.
    `tool` names the tool in the `ValueError`.
    """
    if xtb_cli.ABSENT_XTB_VERSION in (derived.calc_version or ""):
        raise ValueError(
            f"{tool} cannot be keyed on this pod: its calculation resolves to the xtb binary and "
            f"this image has none on PATH, so the version would read "
            f"{derived.calc_version!r} — a Chemclaw3 cache and calibration-ledger key naming a "
            "program that never ran, and one that becomes unreachable the moment the binary "
            "arrives and the key moves. A well-formed key naming a program the pod lacks is worse "
            "than no key. Install xtb in the image, or unset CHEMCLAW_XTB_ENGINE to fall back to "
            "tblite; this pod is already answering 503 on /healthz for the same reason."
        )
    return derived
