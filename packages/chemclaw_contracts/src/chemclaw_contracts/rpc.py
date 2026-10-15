"""The HTTP/JSON surface a backend serves beside MCP: where an operation lives and how it fails.

A backend operation is a tool of the same name. `POST /rpc/<tool>` takes the tool's request model
(`chemclaw_contracts.calc` / `.rxnlabel`, `.wire()` is the body) and answers with the JSON the
tool's response model reads. This module holds only what those models do not: the path and the
error envelope a non-200 answer carries.

Invariants: a 200 body is the tool's answer and nothing else; every error the route itself
produces carries an `RpcError`; 401 and 413 come from the transport layers below the route and
are plain text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from chemclaw_contracts._wire import WireRequest

__all__ = ["RPC_PREFIX", "RpcError", "RpcErrorKind", "WireRequest", "rpc_path"]

#: The path prefix of every operation, with no trailing slash.
RPC_PREFIX = "/rpc"

#: What went wrong, as a caller acts on it. Classify by `kind`; the status is its transport. The
#: server's markers (`[calc-at-capacity]`, `[calc-time-budget]`) still open `message`, for a caller
#: that has not moved off reading them.
#: `refused` (400) the arguments are wrong, resending the same call cannot succeed;
#: `unknown_op` (404) this server does not serve that operation;
#: `rejected` (400, 403, 421) the transport guard refused the Content-Type, Origin or Host;
#: `at_capacity` (503) the pod is full, the identical call may succeed shortly;
#: `time_budget` (422) the pod's clock stopped the call after it ran; the input was fine, and a
#: retry spends the same clock again;
#: `failed` (500) a fault in the server, named by the `error id` in `message`.
RpcErrorKind = Literal["refused", "unknown_op", "rejected", "at_capacity", "time_budget", "failed"]


class RpcError(BaseModel):
    """The body of every error answer the RPC route itself produces."""

    kind: RpcErrorKind
    message: str


def rpc_path(operation: str) -> str:
    """The path of `operation` (a tool name) on a backend, e.g. `/rpc/calculation_key`."""
    return f"{RPC_PREFIX}/{operation}"
