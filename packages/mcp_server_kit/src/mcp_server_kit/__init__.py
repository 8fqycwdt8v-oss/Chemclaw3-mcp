"""The shared shape of every MCP server in this repository.

Importing any part of a server imports this package, and this package **arms the egress guard**.
That is the point: the guard has to be on before a dependency gets a chance to open a socket, and
the only moment guaranteed to precede that is import. `MCP_EGRESS_GUARD=off` opts out, and is set
in no shipped deployment (`tests/test_egress.py` pins the default).
"""

# Arm the guard FIRST, before any other import pulls in a dependency graph. A module that binds
# `socket.getaddrinfo` (or `connect`) into its own namespace at import time keeps whatever function
# was current then — so if FastAPI/Starlette/MCP/prometheus are imported before the guard is armed,
# any such binding is of the *unguarded* call and outlives arming. Arming here means every import
# below, and every dependency they pull, sees the patched socket.
from mcp_server_kit import egress as egress

egress.arm_from_env()

# E402 is deliberate here: these imports MUST follow arming, not precede it — that is the whole
# point of the block above. Moving them up (what the lint wants) reintroduces the bug.
from mcp_server_kit.app import DEFAULT_MAX_REQUEST_BYTES as DEFAULT_MAX_REQUEST_BYTES  # noqa: E402
from mcp_server_kit.app import connector_app as connector_app  # noqa: E402
from mcp_server_kit.datasets import Dataset as Dataset  # noqa: E402
from mcp_server_kit.datasets import DatasetError as DatasetError  # noqa: E402
from mcp_server_kit.datasets import load_dataset as load_dataset  # noqa: E402
from mcp_server_kit.datasets import read_records as read_records  # noqa: E402
from mcp_server_kit.egress import EgressForbidden as EgressForbidden  # noqa: E402
from mcp_server_kit.identity import Caller as Caller  # noqa: E402
from mcp_server_kit.identity import current_caller as current_caller  # noqa: E402

__all__ = [
    "DEFAULT_MAX_REQUEST_BYTES",
    "Caller",
    "Dataset",
    "DatasetError",
    "EgressForbidden",
    "connector_app",
    "current_caller",
    "egress",
    "load_dataset",
    "read_records",
]
