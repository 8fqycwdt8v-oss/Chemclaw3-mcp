# `tests/` — the fleet-level checks

Each server tests itself under `servers/<name>/tests/`. This directory holds the invariants no
single server can see about itself: two servers claiming one port, a manifest copied into
`manifests/` instead of symlinked and since drifted, a server the catalogue has never heard of.

Written in both directions, like Chemclaw3's `test_repo_map.py`: a one-way check passes happily
while the tree grows things the documentation does not know about.
