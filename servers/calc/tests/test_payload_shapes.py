"""The shape of every payload this server writes — the writer's half of a guard Chemclaw3 has.

`calc_version` tracks the programs; `CALCULATION_EPOCH` is hand-bumped for our own contribution.
A field added to or removed from a result model moves neither, yet makes stored rows incomplete,
and an incomplete row that validates is served as an answer. Chemclaw3 fingerprints its own copies
and cannot see this side.

The digest is over each model's JSON schema with `title`/`description` stripped where a schema
keyword is expected (field names are kept), so it moves for any added, removed, renamed or retyped
field, nested models included, and not for reworded prose. A fixed arithmetic error does not
change shape; that stays a judgement, which is why the epoch is a constant.

The model list is derived from `Keyed.__subclasses__`, so a new result model is guarded the day
it is written.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from chemclaw_mcp_calc.engine.ids import stable_hash
from chemclaw_mcp_calc.engine.key import Keyed
from pydantic import BaseModel

# JSON-Schema keywords whose *keys* are names the model chose rather than schema vocabulary. The
# prose filter must not be applied inside them — see the module docstring.
_NAME_MAPS = frozenset({"properties", "$defs", "patternProperties"})
# Prose, not structure. Rewording a docstring must never invalidate a cache.
_PROSE = frozenset({"title", "description"})


def _shape_only(node: Any, *, in_name_map: bool = False) -> Any:
    """`node` with every prose annotation removed, so only its structure remains."""
    if isinstance(node, dict):
        return {
            key: _shape_only(value, in_name_map=not in_name_map and key in _NAME_MAPS)
            for key, value in node.items()
            if in_name_map or key not in _PROSE
        }
    if isinstance(node, list):
        return [_shape_only(item) for item in node]
    return node


def shape_digest(model: type[BaseModel]) -> str:
    """A digest of what `model` persists, stable against prose and sensitive to structure."""
    return stable_hash(_shape_only(model.model_json_schema()))


def payload_models() -> list[type[Keyed]]:
    """Every result model this server hands back, found rather than listed.

    Imports the whole package so no model is invisible to `__subclasses__`, and filters to this
    package so test-defined subclasses cannot make the answer depend on import order.
    """
    from chemclaw_mcp_calc import engine, tools

    for module in pkgutil.walk_packages(engine.__path__, f"{engine.__name__}."):
        importlib.import_module(module.name)
    assert tools.server is not None, "tools imported for its two wire payloads"

    found: list[type[Keyed]] = []
    pending: list[type[Keyed]] = [Keyed]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if subclass not in found:
                found.append(subclass)
                pending.append(subclass)
    return [model for model in found if model.__module__.startswith("chemclaw_mcp_calc")]


# The recorded shape of each. Updating an entry is half of the answer to a failure here; the other
# half is deciding whether rows already written on the Chemclaw3 side are now wrong or incomplete.
RECORDED_SHAPES: dict[str, str] = {
    "AtomicDescriptorResult": "79fd969ecc995b5b",
    "DescriptorProfile": "8c6509010beceba0",
    "ElectronicProperties": "f49622cadc8cb1be",
    "EnsemblePayload": "296e072f46a1b8ac",
    # `ir_wavenumbers_cm` was added without an epoch bump: an older row is still complete (the
    # intensities are the same numbers in the same order), and bumping would recompute every
    # Hessian.
    "HessianPayload": "b524b88cf6dfdde4",
    "LogdResult": "80cc4e8b9cd7c31d",
    "OptimizationResult": "68b7012d0fb3b3ad",
    "OptimizationSummary": "8d9d706dc1c890e9",
    "PkaResult": "5e26bdb9b5ed9d95",
    "SiteReactivityResult": "ba3af8446392079f",
    "SolubilityResult": "31b62fa9275aa620",
    "SurfacePotentialResult": "a1f1708cfa8d4797",
    "XtbResult": "9b0c8a3f4fbdd3a8",
}


def test_every_result_model_is_recorded() -> None:
    """The snapshot and the measured set describe the same models.

    Both directions: a new `Keyed` subclass with no recorded digest is simply unguarded, and a
    recorded name with no model behind it is an entry that will never fail again.
    """
    assert {model.__name__ for model in payload_models()} == set(RECORDED_SHAPES)


def test_persisted_payload_shapes_have_not_changed() -> None:
    """A payload model changed shape: decide what that does to the rows already on disk.

    Can a row written before the change still be read as what it claims to be? A new required field
    fails validation on read; a new optional one validates as `None`, reading as "unknown" when it
    was never asked.
    """
    current = {model.__name__: shape_digest(model) for model in payload_models()}
    changed = {
        name: digest for name, digest in current.items() if RECORDED_SHAPES.get(name) != digest
    }
    assert not changed, (
        f"result payload shape(s) changed: {sorted(changed)}.\n"
        "If a row already in Chemclaw3's `calculation_results` is now wrong or incomplete, bump "
        "`engine.key.CALCULATION_EPOCH` and log the reason beside it — it is folded into every "
        "`params_hash` by `CalculationKey.build`, and Chemclaw3's `remote_key` folds its own epoch "
        "over that, so a bump on either side alone misses every stored row.\n"
        "Then record the new digest(s) in RECORDED_SHAPES: "
        + ", ".join(f'"{name}": "{digest}"' for name, digest in sorted(changed.items()))
    )


def test_the_digest_notices_an_added_optional_field() -> None:
    """The control: the digest notices an added optional field.

    That change moves no `calc_version` and validates as `None` on old rows; without this, the tests
    above could be measuring a digest that never moves.
    """

    class Before(BaseModel):
        log_s_mol_per_l: float

    class After(BaseModel):
        log_s_mol_per_l: float
        estimate: float | None = None

    assert shape_digest(Before) != shape_digest(After)


def test_the_digest_notices_a_field_added_to_a_nested_model() -> None:
    """Nesting is not a hiding place: `BondOrder` and `AtomCharge` are where the numbers live."""

    class Inner(BaseModel):
        order: float

    class WiderInner(BaseModel):
        order: float
        kind: str | None = None

    class Outer(BaseModel):
        bonds: list[Inner]

    class WiderOuter(BaseModel):
        bonds: list[WiderInner]

    assert shape_digest(Outer) != shape_digest(WiderOuter)


def test_the_digest_ignores_a_reworded_docstring() -> None:
    """The other direction, and it is what makes a failure above worth acting on.

    A fingerprint that moved on prose would be re-recorded on sight, which is how a guard becomes a
    line people edit to make the suite green.
    """

    class Documented(BaseModel):
        """One sentence."""

        order: float

    class Rewritten(BaseModel):
        """A different sentence entirely, saying rather more about the same field."""

        order: float

    assert shape_digest(Documented) == shape_digest(Rewritten)
