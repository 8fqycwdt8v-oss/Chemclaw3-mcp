"""The shared shape of every MCP server in this repository.

Importing this package **arms the egress guard**, because import is the only moment guaranteed to
precede a dependency opening a socket. `MCP_EGRESS_GUARD=off` opts out and is set in no shipped
deployment.
"""

# Arm the guard FIRST: a module that binds `socket.getaddrinfo`/`connect` into its namespace at
# import keeps the unguarded function, so every import below must see the patched socket.
from mcp_server_kit import egress as egress

egress.arm_from_env()

# E402 is deliberate here: these imports MUST follow arming, not precede it — that is the whole
# point of the block above. Moving them up (what the lint wants) reintroduces the bug.
from mcp_server_kit import degradation as degradation  # noqa: E402
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
    "degradation",
    "egress",
    "load_dataset",
    "read_records",
]
