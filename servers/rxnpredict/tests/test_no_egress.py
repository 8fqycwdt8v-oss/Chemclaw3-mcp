"""This server's own code holds no way to call out — the same three lines every server ships.

Worth more here than in `props`. This one carries model adapters whose upstream documentation is
full of download URLs, and those were moved into the README precisely because this scan refuses a
host literal in a module. The runtime guard covers what the adapters' *libraries* might do.
"""

from __future__ import annotations

import ast
from pathlib import Path

import chemclaw_mcp_rxnpredict
from mcp_server_kit.no_egress import FORBIDDEN_MODULES, assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_rxnpredict.__file__).parent

LOADER = PACKAGE / "engine" / "predictors" / "__init__.py"

# The plug-in maps `discover_predictors` iterates. Named here because they are what the
# justification below rests on, and `test_the_plug_in_loader_imports_only_its_own_map` holds the
# loader to reading nothing else.
PLUGIN_MAPS = frozenset({"_FORWARD_MODULES", "_CONDITIONS_MODULES"})


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep.

    One computed import is argued rather than exempted: `discover_predictors` loads each optional
    predictor with `importlib.import_module(modname)`, and the scan cannot read `modname`. What it
    can read is the map the name comes from, which the two tests below hold to first-party modules
    this package ships (`D-2026-09-26-a-computed-import-is-argued-at-its-site`).
    """
    assert_no_egress_sources(
        PACKAGE,
        justified_imports={
            (LOADER, "discover_predictors"): (
                "imports only the keys of _FORWARD_MODULES and _CONDITIONS_MODULES, literal maps "
                "of this package's own predictor modules — held by the two tests below"
            ),
        },
    )


def test_the_plug_in_loader_imports_only_its_own_map() -> None:
    """The justification's first half: the name `import_module` receives comes from the maps.

    Read as a tree, so a loader that grew a second source of names — an environment variable, a
    settings list, an entry point — fails here rather than inheriting an argument written about a
    literal map.
    """
    tree = ast.parse(LOADER.read_text(encoding="utf-8"))
    loader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "discover_predictors"
    )
    loops = [node for node in ast.walk(loader) if isinstance(node, ast.For)]
    assert len(loops) == 1, "discover_predictors should iterate exactly one set of plug-ins"
    (loop,) = loops
    sources = {node.id for node in ast.walk(loop.iter) if isinstance(node, ast.Name)}
    assert sources == PLUGIN_MAPS, f"the loader iterates {sorted(sources)}, not only the two maps"
    target = loop.target
    assert isinstance(target, ast.Tuple) and isinstance(target.elts[0], ast.Name)
    bound = target.elts[0].id
    calls = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "import_module"
    ]
    assert [ast.unparse(call.args[0]) for call in calls] == [bound], (
        "import_module must receive the loop's module name and nothing else"
    )


def test_every_plug_in_the_loader_may_import_is_a_module_this_package_ships() -> None:
    """The justification's second half: every name in the maps is first-party and on disk.

    This is the statically checkable manifest the computed call itself cannot be: a name outside
    this package, or one under a forbidden root, is refused here before anything imports it.
    """
    from chemclaw_mcp_rxnpredict.engine import predictors

    names = [*predictors._FORWARD_MODULES, *predictors._CONDITIONS_MODULES]
    assert names, "the plug-in maps are empty — the loader has nothing to justify"
    prefix = "chemclaw_mcp_rxnpredict.engine.predictors."
    for name in names:
        assert name.startswith(prefix), f"{name} is not one of this package's own predictors"
        parts = name.split(".")
        assert not any(".".join(parts[: i + 1]) in FORBIDDEN_MODULES for i in range(len(parts)))
        relative = Path(*name.removeprefix("chemclaw_mcp_rxnpredict.").split("."))
        assert (PACKAGE / relative).with_suffix(".py").is_file(), f"{name} is not shipped"


def test_the_priors_come_from_the_vendored_corpus() -> None:
    """The weights behind every ranking are on disk, checksummed, and licensed."""
    from chemclaw_mcp_rxnpredict.engine.config import DATA_DIR
    from mcp_server_kit import load_dataset

    dataset = load_dataset(DATA_DIR, records_file="trust_priors.json")
    assert dataset.records_path.is_relative_to(PACKAGE)
    assert dataset.licence and dataset.retrieved_from
