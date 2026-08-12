"""The suite runs with the egress guard armed. That is the point, not a precaution.

Importing `mcp_server_kit` arms it already; this fixture asserts the state rather than establishing
it, so a change that quietly disarms the guard fails here instead of in a deployment. The one place
that legitimately turns it off is `packages/mcp_server_kit/tests/test_egress.py`, which restores it.

The consequence worth naming: a test that only passes because it reached the internet fails. So the
vendored datasets are *proven* sufficient rather than assumed to be — which is the property the
whole no-egress design is trying to buy.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from mcp_server_kit import egress


@pytest.fixture(autouse=True)
def egress_guard_is_armed() -> Iterator[None]:
    """Fail any test that runs without the guard, and leave it armed for the next one."""
    if not egress.armed():
        egress.arm()
    yield
    if not egress.armed():
        egress.arm()
