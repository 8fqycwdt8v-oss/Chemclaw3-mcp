"""The forward model is pinned to an immutable commit, and loaded offline from exactly that commit.

`"main"` is a moving branch: every rebuild fetched whatever it pointed at, and the checkpoint is
loaded through `torch.load` (an unpickle), so a branch push was an unreviewed code-execution change
in what the server predicts. These pin the fix — a 40-hex SHA in the fetch script, the same SHA in
the predictor, `local_files_only=True` so load makes no hub call — without importing torch or
transformers (a `[reaction_t5]` extra a plain checkout does not carry).
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_FETCH = _ROOT / "scripts" / "fetch_models.py"
_PREDICTOR = (
    _ROOT
    / "src"
    / "chemclaw_mcp_rxnpredict"
    / "engine"
    / "predictors"
    / "forward"
    / ("reaction_t5.py")
)


def _load_fetch_module() -> object:
    """Import `fetch_models.py` in isolation — it is a script, not part of the package."""
    spec = importlib.util.spec_from_file_location("_fetch_models_under_test", _FETCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _string_assignment(source: str, name: str) -> str:
    """The value of a top-level `name = "..."` assignment, read from the AST (no imports run)."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            value = node.value
            assert isinstance(value, ast.Constant) and isinstance(value.value, str)
            return value.value
    raise AssertionError(f"no top-level string assignment {name!r} found")


def _is_sha(revision: str) -> bool:
    return len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)


def test_every_fetched_model_is_pinned_to_a_commit_sha() -> None:
    """No entry in `MODELS` may be a branch or tag — only an immutable 40-hex commit."""
    fetch = _load_fetch_module()
    models = fetch.MODELS  # type: ignore[attr-defined]
    assert models, "the fetch list is empty"
    for repo_id, revision in models:
        assert _is_sha(revision), f"{repo_id} is pinned to {revision!r}, which is not a commit SHA"
    # The helper the script guards itself with agrees.
    assert fetch._is_pinned_sha("0" * 40) is True  # type: ignore[attr-defined]
    assert fetch._is_pinned_sha("main") is False  # type: ignore[attr-defined]


def test_the_predictor_loads_the_same_pinned_commit_offline() -> None:
    """The predictor's revision matches the fetched SHA and it loads with `local_files_only=True`.

    Read as source, not imported: the predictor imports torch/transformers, which the plain test
    environment does not carry. A drift between the two SHAs would fetch one commit and load a
    different one.
    """
    fetch = _load_fetch_module()
    fetched = {repo: rev for repo, rev in fetch.MODELS}  # type: ignore[attr-defined]
    predictor_source = _PREDICTOR.read_text(encoding="utf-8")
    model_id = _string_assignment(predictor_source, "_MODEL_ID")
    revision = _string_assignment(predictor_source, "_MODEL_REVISION")
    assert fetched[model_id] == revision, "the predictor and the fetch script pin different commits"
    assert _is_sha(revision)
    # Both `from_pretrained` calls must forbid a hub round trip on load.
    assert predictor_source.count("local_files_only=True") >= 2
    assert "revision=_MODEL_REVISION" in predictor_source
