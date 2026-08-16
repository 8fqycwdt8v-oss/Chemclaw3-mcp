"""The ALPB solvent table is re-derived against the installed tblite, not trusted.

`engine/solvents.py` is a hand-written constant describing somebody else's compiled data — every
name `Calculator.add("alpb-solvation", ...)` accepts. A constant like that is exactly the thing that
rots: a tblite upgrade adds or drops a solvent, the table does not move, and the consequence is a
*wrong refusal* — a chemist told GFN2-xTB has no parameters for a solvent it now supports, or an
accepted name that fails minutes later inside the SCF.

So the set is probed against a live `Calculator` here rather than reviewed. This is the one test in
this server that exists to fail on a dependency bump, and failing is the correct behaviour: the fix
is to update the constant to whatever the new build actually accepts.
"""

from __future__ import annotations

import numpy as np
import pytest
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.solvents import (
    ALPB_SOLVENTS,
    SUGGESTED_SOLVENTS,
    is_supported,
    require_supported_solvent,
)
from chemclaw_mcp_calc.engine.xtb_engine import ANGSTROM_TO_BOHR, Calculator

# Water, because the probe has to be a real system tblite will actually set up, and three atoms is
# the cheapest one. The geometry does not matter: `add("alpb-solvation", …)` either finds the
# solvent's dielectric *and* its Born parameters for this Hamiltonian, or it raises.
_NUMBERS = np.array([8, 1, 1])
_POSITIONS = np.array([[0.0, 0.0, 0.0], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]]) * ANGSTROM_TO_BOHR


def _accepts(name: str) -> bool:
    """Whether the installed tblite really has ALPB parameters for `name`, for this method.

    tblite has *two* tables and rejects a name from each differently: absent from the dielectric
    database gives "String value for epsilon was not found", present there but lacking Born
    parameters gives "No ALPB/GBSA parameters found for the method/solvent". Only the intersection
    works, which is why this probes rather than reading either list.
    """
    calc = Calculator(settings.xtb_method, _NUMBERS, _POSITIONS)
    calc.set("verbosity", 0)
    try:
        calc.add("alpb-solvation", name)
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize("name", sorted(ALPB_SOLVENTS))
def test_every_declared_solvent_is_one_tblite_actually_accepts(name: str) -> None:
    """No entry in the table may be a name the method rejects — that is a promise it cannot keep."""
    assert _accepts(name), (
        f"{name!r} is in ALPB_SOLVENTS but the installed tblite refuses it; the table has drifted "
        "from the build and this server would accept a solvent its SCF then fails on"
    )


def test_the_shortlist_a_refusal_quotes_is_a_subset_of_what_works() -> None:
    """The message must never advertise a name the method rejects.

    A shortlist that is a subset of a probed set cannot drift that way. The reverse drift — omitting
    supported solvents — is what happened to the curated tuple this replaced: it left out `dmf`,
    `dioxane`, `benzene` and `nitromethane`, all valid and all ordinary process solvents, while its
    comment claimed to name "the solvents process chemistry actually asks about".
    """
    assert set(SUGGESTED_SOLVENTS) <= ALPB_SOLVENTS
    assert len(set(SUGGESTED_SOLVENTS)) == len(SUGGESTED_SOLVENTS)


def test_matching_is_case_and_whitespace_insensitive_because_tblite_is() -> None:
    """A membership test stricter than the thing it guards would refuse names that work."""
    assert is_supported("Water") and is_supported("  THF  ") and is_supported("DMSO")
    assert not is_supported("2-MeTHF")


def test_the_refusal_names_the_solvent_the_suggestion_and_the_menu() -> None:
    """Three things a chemist needs to fix the call in the same turn, and the measured case."""
    with pytest.raises(ValueError) as raised:
        require_supported_solvent("2-methyltetrahydrofuran")
    message = str(raised.value)
    assert "2-methyltetrahydrofuran" in message
    assert "did you mean" in message and "tetrahydrofuran" in message
    assert "Commonly used supported solvents" in message


def test_a_name_with_nothing_close_gets_no_guess() -> None:
    """Proposing `phenol` for `mtbe` would be worse than proposing nothing."""
    with pytest.raises(ValueError) as raised:
        require_supported_solvent("mtbe")
    assert "did you mean" not in str(raised.value)


def test_gas_phase_passes_untouched() -> None:
    """`None` is not a solvent name that failed to match; it is the absence of solvation."""
    require_supported_solvent(None)
