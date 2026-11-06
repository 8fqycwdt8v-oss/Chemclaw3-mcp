"""The identity and the result core every compute server answers with, whatever it computes.

A server whose manifest declares `compute: true` serves `calculation_key` over `/rpc`: for each of
its compute tools, the `ComputeIdentity` a result *would* be stored under, without running it. A
caller's cache needs that before it computes, and only the server can derive the version string.
`KeyedResult` is what every compute answer carries beside its own payload.

Invariants: `key` and `calc_key` are both set or both `None`, and where `None`, `caveat` says why;
the four `CalculationKeyParts` names are the ones Chemclaw3's `remote_key` reads.
"""

from __future__ import annotations

from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, model_validator

from chemclaw_contracts._wire import WireRequest

__all__ = ["CalculationKeyParts", "CalculationKeyRequest", "ComputeIdentity", "KeyedResult"]


class CalculationKeyRequest(WireRequest):
    """`calculation_key`: the identity `tool` would be stored under for `arguments`."""

    tool_name: ClassVar[str] = "calculation_key"
    tool: str
    arguments: dict[str, Any]


class CalculationKeyParts(BaseModel):
    """The four fields a store lookup takes. `calc_version` may contain `@` and `:`."""

    calc_type: str
    calc_version: str
    input_hash: str
    params_hash: str


class ComputeIdentity(BaseModel):
    """`calculation_key`'s answer: what a calculation would be stored under, unrun.

    `key` and `calc_key` are `None` exactly where the key is not derivable, and `caveat` says why;
    a `None` key with no caveat is refused here, since a caller would read it as "nothing to look
    up" and recompute forever without knowing why.
    """

    tool: str
    calc_version: str
    key: CalculationKeyParts | None = None
    calc_key: str | None = None
    structure_id: str | None = None
    caveat: str | None = None

    @model_validator(mode="after")
    def _a_missing_key_is_explained(self) -> Self:
        """Both shapes of the key or neither, and an unkeyed answer says why."""
        if (self.key is None) != (self.calc_key is None):
            raise ValueError("key and calc_key are the same identity: both set or both None")
        if self.key is None and not (self.caveat or "").strip():
            raise ValueError(f"{self.tool}: no key and no caveat saying why there is none")
        return self


class KeyedResult(BaseModel):
    """What every compute tool's answer carries, whatever else it holds."""

    model_config = ConfigDict(extra="allow")

    calc_version: str
    calc_key: str | None = None
