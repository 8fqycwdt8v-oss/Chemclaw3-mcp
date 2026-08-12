"""The shared shape of every MCP server in this repository.

Importing any part of a server imports this package, and this package **arms the egress guard**.
That is the point: the guard has to be on before a dependency gets a chance to open a socket, and
the only moment guaranteed to precede that is import. `MCP_EGRESS_GUARD=off` opts out, and is set
in no shipped deployment (`tests/test_egress.py` pins the default).
"""

from mcp_server_kit import egress as egress
from mcp_server_kit.app import DEFAULT_MAX_REQUEST_BYTES as DEFAULT_MAX_REQUEST_BYTES
from mcp_server_kit.app import connector_app as connector_app
from mcp_server_kit.datasets import Dataset as Dataset
from mcp_server_kit.datasets import DatasetError as DatasetError
from mcp_server_kit.datasets import load_dataset as load_dataset
from mcp_server_kit.datasets import read_records as read_records
from mcp_server_kit.egress import EgressForbidden as EgressForbidden
from mcp_server_kit.identity import Caller as Caller
from mcp_server_kit.identity import current_caller as current_caller

egress.arm_from_env()

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
