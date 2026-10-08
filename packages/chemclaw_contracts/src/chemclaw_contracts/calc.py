"""The wire of the `calc` backend: one request model per tool, and the responses a caller reads.

`servers/calc` computes; Chemclaw3 keys, caches and orchestrates across this wire. A request model
is the exact argument dict its tool takes (`wire()`), so a caller cannot send a name or type the
server would refuse, and `servers/calc/tests/test_contract.py` fails when a tool's served input
schema drifts from the model here. Compute payloads other than the identity and the structure are
the caller's to model; they share only `KeyedResult`.

Invariants: requests forbid unknown arguments (the server refuses them too); `wire()` sends only
what the caller set, so a default is the server's, never restated here.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw_contracts._wire import WireRequest

__all__ = [
    "CALC_REQUESTS",
    "CALC_RESPONSES",
    "CalculationIdentity",
    "CalculationKeyParts",
    "CalculationKeyRequest",
    "CombineStructuresRequest",
    "ComputeAtomicDescriptorsRequest",
    "ComputeElectronicPropertiesRequest",
    "ComputeFukuiAtRequest",
    "ComputeHessianRequest",
    "ComputePropertiesAtRequest",
    "ComputeSurfacePotentialRequest",
    "ComputeXtbEnergyRequest",
    "CrestEffort",
    "EmbedStructureRequest",
    "EnsembleSearch",
    "FukuiMode",
    "KeyedResult",
    "OptimizeGeometryRequest",
    "PredictDevelopabilityProfileRequest",
    "PredictLogdRequest",
    "PredictPkaRequest",
    "PredictSiteReactivityRequest",
    "PredictSolubilityRequest",
    "RelaxStructureRequest",
    "ScanPointRequest",
    "SearchBindingModesRequest",
    "SearchConformerEnsembleRequest",
    "Structure",
]

FukuiMode = Literal["electrophilic", "nucleophilic", "radical"]
EnsembleSearch = Literal["conformers", "tautomers", "protomers", "deprotomers"]
CrestEffort = Literal["quick", "normal", "extensive"]


class Structure(BaseModel):
    """A 3D structure: parallel `elements` (atomic numbers) and `positions` (Angstrom).

    `structure_id` is output-only: the server recomputes it from the coordinates and ignores a
    value sent in. The physical-consistency checks (electron count against multiplicity, the atom
    ceiling) are the server's, so a structure this model accepts may still be refused there.
    """

    elements: list[int] = Field(min_length=1)
    positions: list[list[float]] = Field(min_length=1)
    charge: int = 0
    multiplicity: int = Field(default=1, ge=1, description="Spin multiplicity 2S+1.")
    smiles: str | None = None
    origin: str | None = None
    structure_id: str | None = None


class CalculationKeyRequest(WireRequest):
    """`calculation_key`: the identity `tool` would be stored under for `arguments`."""

    tool_name: ClassVar[str] = "calculation_key"
    tool: str
    arguments: dict[str, Any]


class ComputeXtbEnergyRequest(WireRequest):
    """`compute_xtb_energy`: GFN2-xTB single point of a SMILES."""

    tool_name: ClassVar[str] = "compute_xtb_energy"
    smiles: str
    charge: int = 0


class PredictSolubilityRequest(WireRequest):
    """`predict_solubility`."""

    tool_name: ClassVar[str] = "predict_solubility"
    smiles: str


class PredictPkaRequest(WireRequest):
    """`predict_pka`."""

    tool_name: ClassVar[str] = "predict_pka"
    smiles: str


class ComputeElectronicPropertiesRequest(WireRequest):
    """`compute_electronic_properties`."""

    tool_name: ClassVar[str] = "compute_electronic_properties"
    smiles: str
    solvent: str | None = None


class PredictSiteReactivityRequest(WireRequest):
    """`predict_site_reactivity`: `mode` orders the answer and does not enter the key."""

    tool_name: ClassVar[str] = "predict_site_reactivity"
    smiles: str
    mode: FukuiMode = "electrophilic"


class ComputeAtomicDescriptorsRequest(WireRequest):
    """`compute_atomic_descriptors`."""

    tool_name: ClassVar[str] = "compute_atomic_descriptors"
    smiles: str
    solvent: str | None = None


class ComputeSurfacePotentialRequest(WireRequest):
    """`compute_surface_potential`."""

    tool_name: ClassVar[str] = "compute_surface_potential"
    smiles: str
    solvent: str | None = None


class OptimizeGeometryRequest(WireRequest):
    """`optimize_geometry`: the SMILES-in twin of `relax_structure`."""

    tool_name: ClassVar[str] = "optimize_geometry"
    smiles: str
    solvent: str | None = None


class PredictDevelopabilityProfileRequest(WireRequest):
    """`predict_developability_profile`."""

    tool_name: ClassVar[str] = "predict_developability_profile"
    smiles: str


class PredictLogdRequest(WireRequest):
    """`predict_logd`: the one compute tool `calculation_key` does not key."""

    tool_name: ClassVar[str] = "predict_logd"
    smiles: str
    ph: float | None = None


class EmbedStructureRequest(WireRequest):
    """`embed_structure`: `multiplicity=0` asks the server to derive it from radical electrons."""

    tool_name: ClassVar[str] = "embed_structure"
    smiles: str
    charge: int | None = None
    multiplicity: int | None = None
    relax_with_force_field: bool = True


class CombineStructuresRequest(WireRequest):
    """`combine_structures`: two closed-shell monomers placed side by side."""

    tool_name: ClassVar[str] = "combine_structures"
    first: Structure
    second: Structure
    separation_angstrom: float = 3.5


class RelaxStructureRequest(WireRequest):
    """`relax_structure`: `frozen_atoms` are indices held at their input positions."""

    tool_name: ClassVar[str] = "relax_structure"
    structure: Structure
    solvent: str | None = None
    frozen_atoms: list[int] | None = None


class ComputePropertiesAtRequest(WireRequest):
    """`compute_properties_at`."""

    tool_name: ClassVar[str] = "compute_properties_at"
    structure: Structure
    solvent: str | None = None


class ComputeFukuiAtRequest(WireRequest):
    """`compute_fukui_at`."""

    tool_name: ClassVar[str] = "compute_fukui_at"
    structure: Structure
    mode: FukuiMode = "electrophilic"
    solvent: str | None = None


class ComputeHessianRequest(WireRequest):
    """`compute_hessian`."""

    tool_name: ClassVar[str] = "compute_hessian"
    structure: Structure
    solvent: str | None = None


class ScanPointRequest(WireRequest):
    """`scan_point`: drive the internal coordinate `atoms` to `value`, relax the rest."""

    tool_name: ClassVar[str] = "scan_point"
    structure: Structure
    atoms: list[int]
    value: float
    solvent: str | None = None


class SearchConformerEnsembleRequest(WireRequest):
    """`search_conformer_ensemble`: a CREST search; minutes to hours."""

    tool_name: ClassVar[str] = "search_conformer_ensemble"
    structure: Structure
    search: EnsembleSearch = "conformers"
    effort: CrestEffort = "quick"
    solvent: str | None = None
    temperature_k: float = 0.0


class SearchBindingModesRequest(WireRequest):
    """`search_binding_modes`: CREST's non-covalent mode over a combined pair."""

    tool_name: ClassVar[str] = "search_binding_modes"
    structure: Structure
    effort: CrestEffort = "quick"
    solvent: str | None = None


class CalculationKeyParts(BaseModel):
    """The four fields a store lookup takes. `calc_version` may contain `@` and `:`."""

    calc_type: str
    calc_version: str
    input_hash: str
    params_hash: str


class CalculationIdentity(BaseModel):
    """`calculation_key`'s answer: what a calculation would be stored under, unrun.

    `key` and `calc_key` are `None` exactly where the key is not derivable, and `caveat` says why.
    """

    tool: str
    calc_version: str
    key: CalculationKeyParts | None = None
    calc_key: str | None = None
    structure_id: str | None = None
    caveat: str | None = None


class KeyedResult(BaseModel):
    """What every compute tool's answer carries, whatever else it holds."""

    model_config = ConfigDict(extra="allow")

    calc_version: str
    calc_key: str | None = None


CALC_REQUESTS: dict[str, type[WireRequest]] = {
    request.tool_name: request
    for request in (
        CalculationKeyRequest,
        ComputeXtbEnergyRequest,
        PredictSolubilityRequest,
        PredictPkaRequest,
        ComputeElectronicPropertiesRequest,
        PredictSiteReactivityRequest,
        ComputeAtomicDescriptorsRequest,
        ComputeSurfacePotentialRequest,
        OptimizeGeometryRequest,
        PredictDevelopabilityProfileRequest,
        PredictLogdRequest,
        EmbedStructureRequest,
        CombineStructuresRequest,
        RelaxStructureRequest,
        ComputePropertiesAtRequest,
        ComputeFukuiAtRequest,
        ComputeHessianRequest,
        ScanPointRequest,
        SearchConformerEnsembleRequest,
        SearchBindingModesRequest,
    )
}

#: The model each tool's answer is read through: the identity, the structure, or the keyed core.
CALC_RESPONSES: dict[str, type[BaseModel]] = {
    tool: (
        CalculationIdentity
        if tool == "calculation_key"
        else Structure
        if tool in ("embed_structure", "combine_structures")
        else KeyedResult
    )
    for tool in CALC_REQUESTS
}
