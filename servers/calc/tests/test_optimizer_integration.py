"""How geomeTRIC is driven, held as properties rather than as a paragraph.

`engine/xtb_opt.py` uses `geometric.optimize.Optimize`, not `run_optimizer`: the latter writes
files into the working directory and replaces the root logger's handlers. `connector_app` owns
the log configuration and the server must stay stateless. So a relaxation must leave the working
directory and the root logger alone, and that is driven.
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

    A source assertion is right here: the entry points differ only in side effects, which a driven
    test would not notice until the disk filled.
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
    """The process's log configuration is `connector_app`'s, not geomeTRIC's.

    geomeTRIC's per-cycle records stop at the `geometric` logger, and the root's handler list is the
    same object after a relaxation as before.
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

    A repeated evaluation also runs outside the `Deadline.check`, so it could overrun a margin sized
    for one single point. Held as "no two consecutive evaluations are the same point", not a count.
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
