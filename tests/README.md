# `tests/` — the fleet-level checks

Each server tests itself under `servers/<name>/tests/`. This directory holds the invariants no
single server can see about itself: two servers claiming one port, a manifest copied into
`manifests/` instead of symlinked and since drifted, a server the catalogue has never heard of.

Written in both directions, like Chemclaw3's `test_repo_map.py`: a one-way check passes happily
while the tree grows things the documentation does not know about.

The fleet checks are split by concern: `test_fleet_layout.py` (file set, names, ports, maps, type
gate), `test_fleet_manifests.py` (registration, classification, readiness, admission),
`test_fleet_auth.py`, `test_fleet_egress.py`, `test_fleet_deploy.py` (what shipped deployment files
may set), `test_fleet_images.py` (Containerfiles against the lock), `test_fleet_source.py`
(serving-code discipline) and `test_fleet_data.py` (data two servers both hold).
