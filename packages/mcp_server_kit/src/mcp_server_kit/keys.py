"""The calculation key every compute server derives, and the hash it is built on, written once.

Chemclaw3 never recomputes a persisted result, so it needs a key naming everything a result
depends on before the result exists. The four field names, the bare `inputs` hash, the
`{"epoch", "params"}` envelope and the flat `calc_type@calc_version:input_hash:params_hash` form
are a contract with Chemclaw3's `science/calc/store.py` (`servers/calc/tests/test_key_contract.py`
pins them). `calc_version` for an engine run off the serving pod is composed by
`off_pod_calc_version`, so two builds of one engine never share a cache row.
Invariants: pure, no I/O; a server's epoch is passed in, never read from here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Self

from pydantic import BaseModel

__all__ = [
    "IMAGE_DIGEST_PATTERN",
    "NOT_A_COMPUTE_TOOL",
    "CalculationKey",
    "not_a_compute_tool",
    "off_pod_calc_version",
    "stable_hash",
]

# Digest width: 16 hex characters (64 bits). Part of the wire contract with Chemclaw3, not a knob.
_DEFAULT_CHARS = 16

#: An OCI content digest, the only form an image reference is keyed by (a tag can move).
IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

#: The phrase `calculation_key` refuses a non-compute tool with; `tests/test_fleet_compute_keys.py`
#: reads it to tell "not a calculation" from "a calculation it failed to key".
NOT_A_COMPUTE_TOOL = "is not a compute tool on this server"

# Separates the parts of a composed `calc_version`; only the method, which comes first, may hold it.
_PART_SEPARATOR = "+"


def stable_hash(payload: Any, *, chars: int = _DEFAULT_CHARS) -> str:
    """A short SHA-256 of the canonical JSON form of `payload`.

    Sorted keys and tight separators make it independent of ordering and whitespace; `default=str`
    serialises a non-JSON value deterministically. `chars` is how many hex characters to keep.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:chars]


class CalculationKey(BaseModel):
    """Content-addressed identity of a calculation, versioned by the calculator.

    Two calculations share a key iff they are the same calculator *version* on the same input with
    the same parameters under the same server epoch. `calc_version` names every program whose
    output survives into the result; the epoch names the server's own contribution, and is folded
    into `params_hash` only, so a bump invalidates the cache and leaves the ledger's key intact.
    """

    calc_type: str
    calc_version: str
    input_hash: str
    params_hash: str

    @classmethod
    def build(
        cls,
        calc_type: str,
        calc_version: str,
        inputs: Any,
        params: Any = None,
        *,
        epoch: str | None = None,
    ) -> Self:
        """The one way to assemble a key: `inputs` hashed bare, `params` inside the epoch envelope.

        Raises:
            ValueError: no `epoch` was given. A server passes its own; a subclass may default it.
        """
        if epoch is None:
            raise ValueError("a calculation key needs the serving side's epoch")
        return cls(
            calc_type=calc_type,
            calc_version=calc_version,
            input_hash=stable_hash(inputs),
            params_hash=stable_hash({"epoch": epoch, "params": params}),
        )

    def as_str(self) -> str:
        """The flat form a result carries to Chemclaw3: `calc_type@calc_version:in:params`."""
        return f"{self.calc_type}@{self.calc_version}:{self.input_hash}:{self.params_hash}"


def off_pod_calc_version(method: str, engine: str, engine_version: str, image_digest: str) -> str:
    """`calc_version` for an engine that runs off the serving pod: method, engine, build, image.

    The pod cannot see the build that will run, so the caller states it, and the backend image's
    digest is in the version so two clusters with different builds never share a cache row. Only
    `method` may contain `+`; the three parts after it may not, so the string parses from the right.

    Raises:
        ValueError: a part is empty or holds the separator, or the digest is not `sha256:<64 hex>`.
    """
    if not method.strip():
        raise ValueError("calc_version needs a method")
    for label, part in (("engine", engine), ("engine_version", engine_version)):
        if not part.strip() or _PART_SEPARATOR in part or part != part.strip():
            raise ValueError(
                f"{label} {part[:64]!r} must be non-empty, unpadded and free of "
                f"{_PART_SEPARATOR!r}, the separator a calc_version is read by"
            )
    if re.fullmatch(IMAGE_DIGEST_PATTERN, image_digest) is None:
        raise ValueError(
            f"image_digest {image_digest[:80]!r} is not sha256:<64 lowercase hex>; a tag can move "
            "under a key, so only a content digest names the build that ran"
        )
    return _PART_SEPARATOR.join((method, engine, engine_version, image_digest))


def not_a_compute_tool(tool: str, compute_tools: list[str]) -> ValueError:
    """The refusal `calculation_key` gives a tool it keys nothing for, in the fleet wording."""
    return ValueError(
        f"{tool[:64]!r} {NOT_A_COMPUTE_TOOL}; expected one of {', '.join(sorted(compute_tools))}"
    )
