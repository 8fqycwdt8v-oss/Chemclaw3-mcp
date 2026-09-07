"""The shape of every payload this server writes — the writer's half of a guard Chemclaw3 has.

## The gap this closes

`calc_version` answers one question: *would the program we shell out to produce a different number
now?* `CALCULATION_EPOCH` answers the other one — *did our own contribution to a stored result
change?* — and it is a hand-bumped constant, so nothing fails on the commit that should bump it.

That is a whole class of change this repository can make with no key moving and no test going red:
a field added to `ElectronicProperties`, a field removed from `SiteReactivityResult`, `_valences`
learning to report something new, the ESOL arithmetic returning one more number. The programs are
unchanged, so `calc_version` is *correctly* unchanged; the payload's shape is not.

Chemclaw3 has the reader's half of this (`tests/test_calc_payload_schemas.py`) and it cannot see
this half: it fingerprints *its own* copies of these models, so a field added here moves nothing
there. Neither side can see the other's schema, which is exactly why each needs its own.

The measured precedent is on the record in `engine/key.py`: `CALCULATION_EPOCH` went to `"2"`
because `SiteReactivityResult` gained the conceptual-DFT panel and `AtomCharge` gained its Wiberg
and free valence. No number that was already stored moved — the same three SCFs, the same geometry —
but every row written under epoch 1 became *incomplete*, and an incomplete row that still validates
is served as an answer. That bump happened because a person noticed. This file is what notices.

## What the digest covers, and what it deliberately does not

It is taken over the model's JSON schema with `title`/`description` stripped, so it moves for a
field added, removed, renamed or retyped — anywhere in the model, including inside a nested one such
as `BondOrder` or `Estimate` — and does not move for a reworded docstring. Prose is stripped only
where a JSON-Schema keyword is expected: the keys inside `properties`/`$defs` are the model's own
field names, and a model with a field literally named `title` must not be fingerprinted as one
without it.

The one thing a digest cannot see is our arithmetic being wrong and then fixed — a corrected
linear-rotor term changes every entropy in a payload without moving its shape. That half stays a
judgement, which is why the epoch is a constant rather than a derived value.

## The model list is derived, not typed

`Keyed` is the base every compute result on this server carries, so `__subclasses__` over the
imported package *is* the list — a new result model is guarded the day it is written rather than the
day somebody remembers to add it here. The recorded digests are the data; the set of models they
have to cover is measured.
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

    Imports the whole package first: a model in a module nothing else imports would otherwise be
    invisible to `__subclasses__`, which is the failure mode a hand-written list has by
    construction. Filtered to this package so that a `Keyed` subclass defined inside another test
    module cannot make this file's answer depend on import order.
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
    "HessianPayload": "bcfacc8dbc1af311",
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

    This is not a request to keep the models still. It is the moment to answer one question — can a
    row written before this change still be read as what it claims to be? An added *required* field
    makes every stored row fail validation on the reader's side; an added optional one is worse,
    because it validates back as `None`, which reads as "we do not know" when the truth is "we never
    asked".
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
    """The control, and the exact shape of the change that slips through everything else.

    An optional field appended to a result model moves no `calc_version` — the programs did not
    change — and validates back as `None` on every row written before it existed. Without this
    assertion the two tests above could be measuring a digest that never moves, and would pass
    forever while guarding nothing.
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
