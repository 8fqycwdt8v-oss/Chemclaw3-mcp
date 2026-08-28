"""Fetch the model checkpoints into the image, at build time, in a stage that is thrown away.

This is the one script in the fleet that is *meant* to reach a network, and it is worth being
precise about why that is not a hole in the no-egress rule. It runs in a builder stage, under
`MCP_EGRESS_ALLOW` naming the model hosts explicitly, and its output is a directory of files that
gets copied into a runtime image which sets `HF_HUB_OFFLINE=1` and arms the guard. No serving
process ever runs this, and nothing that runs it ever serves.

Deliberately *not* a lazy download on first use, which is what the predictor libraries do by
default. A model fetched at request time is a model nobody reviewed, arriving over a network the
production deployment does not have, at the moment a chemist is waiting for an answer.

Run by the Containerfile. To refresh a checkpoint, change the pinned revision below and rebuild —
the diff is then a reviewable line rather than a silent change in what the server predicts.
"""

from __future__ import annotations

import os
import sys

# Pinned by **commit SHA**, not by a branch or tag. `"main"` is a moving branch, so every rebuild
# fetched whatever it pointed at that day — the exact "the model changed under us, invisibly"
# failure this comment claimed to prevent, and worse because `snapshot_download` loads the T5
# checkpoint through `torch.load`, i.e. an unpickle of whoever last pushed to the branch. A 40-hex
# commit SHA is immutable: a rebuild fetches the reviewed bytes or fails. Update deliberately, in a
# pull request, alongside the trust priors that were calibrated against it.
#
# The SHA below is the HEAD of `sagawa/ReactionT5v2-forward`'s `main` observed on 2026-08-28.
MODELS: tuple[tuple[str, str], ...] = (
    ("sagawa/ReactionT5v2-forward", "933114058cb2604dc1bf536dbebdfcefbe83d4fc"),
)


def _is_pinned_sha(revision: str) -> bool:
    """Whether `revision` is an immutable 40-hex commit SHA rather than a moving branch or tag."""
    return len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)


def main() -> int:
    """Download every pinned model into `HF_HOME`, or explain why it could not."""
    if not os.environ.get("MCP_EGRESS_ALLOW"):
        print(
            "refusing to run without MCP_EGRESS_ALLOW: this script is a build step, and running "
            "it inside a serving image is the thing the egress guard exists to prevent",
            file=sys.stderr,
        )
        return 2

    unpinned = [f"{repo}@{rev}" for repo, rev in MODELS if not _is_pinned_sha(rev)]
    if unpinned:
        print(
            "refusing to fetch an unpinned revision (a branch or tag can move under a rebuild, and "
            f"the checkpoint is loaded via torch.load): {', '.join(unpinned)}. Pin a 40-hex commit "
            "SHA.",
            file=sys.stderr,
        )
        return 2

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "huggingface_hub is not installed; install the [reaction_t5] extra before building",
            file=sys.stderr,
        )
        return 2

    for repo_id, revision in MODELS:
        print(f"fetching {repo_id}@{revision}", flush=True)
        snapshot_download(repo_id=repo_id, revision=revision)
    print(f"fetched {len(MODELS)} model(s) into {os.environ.get('HF_HOME', '(default)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
