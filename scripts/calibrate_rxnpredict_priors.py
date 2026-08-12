"""Calibrate `rxnpredict`'s per-class trust priors against a labelled reaction set.

Runs every registered forward predictor over a labelled dataset, partitions the results by the
coarse reaction class, and writes the smoothed per-class top-1 accuracy as the weight each model's
votes will carry for that class.

**An operator's script, run outside the serving image, and never by the server.** It needs every
predictor installed and it takes as long as inference does — neither of which belongs in a pod
answering a chemist. Its output is data with provenance: write it over
`servers/rxnpredict/src/chemclaw_mcp_rxnpredict/data/trust_priors.json`, recompute the `sha256` in
`dataset.json` beside it, bump the version, and let a person review the diff. The priors decide
every ranking this server produces, so "where did these numbers come from" has to have an answer.

Run:
    python scripts/calibrate_rxnpredict_priors.py --dataset labelled_reactions.jsonl --top-k 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from chemclaw_mcp_rxnpredict.engine.config import DATA_DIR
from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_OTHER, classify_reaction
from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import save_priors_file
from chemclaw_mcp_rxnpredict.engine.predictors import discover_predictors, list_forward
from chemclaw_mcp_rxnpredict.engine.preprocessing import canonical_smiles

logger = logging.getLogger(__name__)

# Laplace smoothing: avoids a class with one sample dominating with a 1.0 prior
# when it's just lucky.
SMOOTH_ALPHA = 1.0
SMOOTH_BETA = 1.0


def _load_records(path: Path) -> list[dict[str, Any]]:
    """Read the labelled set, as JSON or JSONL, without caring which."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    loaded: list[dict[str, Any]] = json.loads(text)
    return loaded


async def _evaluate(records: list[dict[str, Any]], top_k: int) -> dict[str, dict[str, float]]:
    discover_predictors()
    predictors = list_forward()
    if not predictors:
        logger.error("No forward predictors registered; nothing to calibrate.")
        return {}

    # hits[class][model] = (correct, total)
    hits: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    for rec in records:
        try:
            expected = canonical_smiles(rec["expected_product"])
        except (KeyError, ValueError):
            continue
        klass = classify_reaction(rec["reactants"], product=rec["expected_product"])
        for p in predictors:
            try:
                preds = await p.predict(rec["reactants"], top_k)
            except Exception as exc:
                logger.warning("[%s] failed on '%s': %r", p.name, rec["reactants"], exc)
                continue
            top1 = preds[0].product_smiles if preds else None
            hits[klass][p.name][1] += 1
            if top1 == expected:
                hits[klass][p.name][0] += 1

    # Convert (correct, total) -> smoothed accuracy
    priors: dict[str, dict[str, float]] = {}
    for klass, model_hits in hits.items():
        if klass == CLASS_OTHER:
            continue  # don't pollute "other" — it falls back to global priors anyway
        priors[klass] = {}
        for model, (correct, total) in model_hits.items():
            acc = (correct + SMOOTH_ALPHA) / (total + SMOOTH_ALPHA + SMOOTH_BETA)
            priors[klass][model] = round(acc, 4)
    return priors


def main() -> int:
    # Calibration must reflect each model's *current* behaviour, so disable the
    # prediction cache before any Settings/cache singleton is constructed —
    # otherwise stale cached predictions (from a prior model version) would skew
    # the per-class priors. Set before the cache singleton is built below.
    os.environ["CHEMCLAW_RXNPREDICT_CACHE_ENABLED"] = "false"

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "sample_reactions.json",
        help="JSON list or JSONL file with {reactants, expected_product} records.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to the server's vendored data/trust_priors.json.",
    )
    args = parser.parse_args()

    records = _load_records(args.dataset)
    if not records:
        logger.error("Dataset %s is empty.", args.dataset)
        return 1

    # The vendored priors file this server actually reads. Writing it is only half the job: its
    # `sha256` in `dataset.json` has to be recomputed and the version bumped, or `load_dataset`
    # will refuse the corpus at startup — which is the check doing exactly what it is for.
    output_path = args.output or (DATA_DIR / "trust_priors.json")

    priors = asyncio.run(_evaluate(records, args.top_k))
    if not priors:
        logger.error("Calibration produced no priors; aborting.")
        return 1

    save_priors_file(output_path, priors)
    logger.info("Wrote per-class priors for %d classes -> %s", len(priors), output_path)
    print(json.dumps(priors, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
