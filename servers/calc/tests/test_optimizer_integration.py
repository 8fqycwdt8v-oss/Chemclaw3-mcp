"""How geomeTRIC is driven, held as properties rather than as a paragraph.

`engine/xtb_opt.py` uses `geometric.optimize.Optimize` and **not**
`geometric.optimize.run_optimizer`. That is not a style preference and it is not obvious from the
library's documentation, which presents the driver as the entry point. Measured on a trivial engine
at the commit that made this change, one `run_optimizer` call:

- writes `<prefix>.log`, `<prefix>.tmp/` and `<prefix>_optim.xyz` into the process's **working
  directory**, and
- replaces the **root logger's** handlers with geomeTRIC's own stream handler *and* a file handler,
  permanently, from inside what is otherwise an ordinary function call.

Both are disqualifying here. `connector_app` owns this process's log configuration — it is why
`CLAUDE.md` says not to call `basicConfig` in a server — and a tool that writes unbounded files into
its pod's working directory on every call is not the stateless thing this fleet promises.

The two tests that matter are therefore about *this* module rather than about geomeTRIC: a
relaxation must leave the working directory and the root logger alone. They are driven, because the
whole point is that the difference is invisible in the source.
"""

from __future__ import annotations

import logging
import os
from itertools import pairwise
from pathlib import Path
from typing import Any

import chemclaw_mcp_calc.engine.xtb_opt as xtb_opt
import numpy as np
import pytest
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import evaluate_point
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure

#: A water with one bond stretched — strained enough that the optimizer certainly runs, small enough
#: that it costs a second.
STRAINED = Structure(
    elements=[8, 1, 1],
    positions=[[0.0, 0.0, 0.0], [1.3, 0.0, 0.0], [-0.3, 0.9, 0.0]],
)


def test_the_driver_that_writes_files_and_seizes_the_root_logger_is_not_the_one_used() -> None:
    """`run_optimizer` must not appear in this module, and the optimizer under it must.

    A source assertion, deliberately, and it is the one case where that is the right instrument: the
    two entry points differ in their *side effects*, so a test that only drove the module would pass
    just as happily on the day somebody swapped one for the other and the pod started writing log
    files — until the disk filled.
    """
    source = Path(xtb_opt.__file__).read_text(encoding="utf-8")
    assert "run_optimizer" not in source.replace("`run_optimizer`", ""), (
        "engine/xtb_opt.py calls geomeTRIC's driver; it writes three files into the working "
        "directory and replaces the root logger's handlers"
    )
    assert "from geometric.optimize import Optimize" in source


def test_a_relaxation_writes_nothing_into_the_working_directory(tmp_path: Path) -> None:
    """The half of the finding a source check cannot make.

    Run from an empty directory: after a real relaxation it must still be empty. `run_optimizer`
    would have left three entries in it.
    """
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        result = optimize_structure(OptSpec(engine="tblite"), STRAINED)
    finally:
        os.chdir(previous)
    assert result.steps > 0, "the optimizer did not run, so this proves nothing"
    assert sorted(tmp_path.iterdir()) == [], (
        f"a relaxation left {[p.name for p in tmp_path.iterdir()]} in the working directory"
    )


def test_geometric_logging_does_not_reach_the_root_logger() -> None:
    """The other half: the process's log configuration is `connector_app`'s, not geomeTRIC's.

    Two properties, and the second is the one that was measured rather than assumed. geomeTRIC logs
    one line per optimizer cycle at INFO on `geometric.optimize`; those records are stopped at the
    `geometric` logger rather than reformatted at the root. And the root logger's **handler list**
    must be the same object after a relaxation as before — which is what `run_optimizer` changes.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    collector = _Collect()
    root.addHandler(collector)
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        optimize_structure(OptSpec(engine="tblite"), STRAINED)
    finally:
        root.removeHandler(collector)
        root.setLevel(previous_level)

    assert list(root.handlers) == before, "a relaxation changed the root logger's handlers"
    from_geometric = [record for record in records if record.name.startswith("geometric")]
    assert not from_geometric, (
        f"{len(from_geometric)} geomeTRIC record(s) reached the root logger, e.g. "
        f"{from_geometric[0].getMessage()!r}"
    )


def test_a_relaxation_evaluates_no_geometry_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The input and the final frame are each one SCF, not two.

    Measured before the fix on ethanol: 22 single points for 19 steps, because geomeTRIC's first
    request re-evaluated the input the caller had just evaluated (identical to 3e-17 Angstrom) and
    the convergence re-check re-evaluated geomeTRIC's last request (1.1e-9 Angstrom apart, its
    Bohr round trip). The second of those ran outside every `Deadline.check`, so the overrun past
    the budget could be two uninterruptible single points against a margin sized for one.

    Held as "no two consecutive evaluations are the same point", which is the property, rather than
    as a count, which would move with every geomeTRIC release.
    """
    seen: list[np.ndarray] = []
    real = evaluate_point

    def spy(calculator: Any, positions: np.ndarray) -> Any:
        seen.append(np.array(positions, dtype=float))
        return real(calculator, positions)

    monkeypatch.setattr(xtb_opt, "evaluate_point", spy)
    result = optimize_structure(OptSpec(engine="tblite"), STRAINED)
    assert result.steps > 0, "the optimizer did not run, so this proves nothing"
    repeats = [
        index
        for index, (before, after) in enumerate(pairwise(seen))
        if np.max(np.abs(before - after)) <= 1e-6
    ]
    assert repeats == [], f"evaluations {repeats} repeated the geometry before them"
    # The returned geometry is the one the last SCF was run at, not a round-tripped copy of it —
    # compared after `Structure`'s own rounding, which is what every stored geometry goes through.
    verified = Structure(elements=STRAINED.elements, positions=seen[-1].tolist())
    assert result.structure.positions == verified.positions
