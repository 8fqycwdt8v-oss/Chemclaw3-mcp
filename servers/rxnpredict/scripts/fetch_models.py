"""Fetch the model checkpoints into the image, at build time, in a stage that is thrown away.

The one script in the fleet meant to reach a network. It runs only in a builder stage, under an
`MCP_EGRESS_ALLOW` naming the model hosts; its output is copied into a runtime image that sets
`HF_HUB_OFFLINE=1` and arms the guard. No serving process runs it, and no model is ever fetched at
request time. To refresh a checkpoint, change its pinned revision below and rebuild.
"""

from __future__ import annotations

import os
import sys

# Pinned by 40-hex commit SHA, never a branch or tag: a rebuild fetches the reviewed bytes or fails.
# This matters doubly because the T5 checkpoint is loaded through `torch.load` (an unpickle). Update
# in a pull request, together with the trust priors calibrated against it.
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
