"""The base every request model of a backend shares."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict


class WireRequest(BaseModel):
    """The arguments of one backend tool. Subclasses name the tool they are for.

    Unknown arguments are refused, as the servers refuse them. `wire()` is what goes on the wire:
    only the fields the caller set, so a default stays the server's.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: ClassVar[str]

    def wire(self) -> dict[str, Any]:
        """The JSON argument dict to send."""
        return self.model_dump(mode="json", exclude_unset=True)
