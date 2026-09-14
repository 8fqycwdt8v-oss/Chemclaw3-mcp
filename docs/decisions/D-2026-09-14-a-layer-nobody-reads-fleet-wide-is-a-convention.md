# D-2026-09-14-a-layer-nobody-reads-fleet-wide-is-a-convention — A layer nobody reads fleet-wide is a convention

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over Wave 30, on top
of `24b50ec` (PR #66).

## Context

`CLAUDE.md` states the no-egress posture as **four independent layers**, "because a rule that lives
in one place rots". The fourth is the default-deny NetworkPolicy, and it was listed under *Enforced*
in `D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` with no caveat.

It was not enforced. `tests/test_fleet.py::test_a_server_ships_the_whole_set` required
`deploy/networkpolicy.yaml`, `tests/test_no_egress.py` and `tests/test_server.py` — and **not**
`tests/test_deploy.py`. Nothing fleet-wide read a policy's *content*; every assertion about it lived
in seven copies, one per server, in files no eighth server was obliged to have.

### Measured, at `24b50ec`

```
$ sed -i '/^    - Egress$/d' servers/props/deploy/networkpolicy.yaml   # numstat 0 1
$ pytest servers/props/tests/test_deploy.py -q
FAILED …::test_egress_is_denied                     # the per-server test does work

$ mv servers/props/tests/test_deploy.py /tmp/       # an 8th server simply never has one
$ pytest tests/ servers/ -q
(only the pre-existing failures)                     # policy permits all egress, suite green
```

A server whose NetworkPolicy silently permits all outbound traffic, shipping no `test_deploy.py`,
passed the whole fleet suite.

This is the standing the bearer check had before
`D-2026-09-12-a-shared-helper-is-not-a-proof-it-was-applied`, one layer over. The same wave that
made an eighth server owe the bearer proof, the manifest proof and a readiness callable did not make
the same argument here — and the record presented the layer as enforced. A reader of that record and
a reader of the suite would have disagreed about what this fleet does.

## Decision

**The content is read fleet-wide, and the per-server file is owed.** Two changes, and they are not
redundant with each other.

`tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload` is parametrised
over `server_dirs()` — read off the filesystem, so a server added next year is covered the day its
directory exists — and asserts **three** clauses, because "default-deny" has three independent ways
to be false and each looks unchanged in review:

- `Egress` in `policyTypes`. Absent, the direction is not governed at all and the file still reads
  as a network policy. This is the regression the per-server tests were written for.
- `egress == []`. An empty list rather than an absent key, because only the list says "deny all"
  where a reader can see it.
- the `podSelector` matches the Deployment's own pod label. **This is the clause the per-server
  files mostly did not carry** — `props` held the pair and the others did not — and it is the one a
  reviewer is least likely to catch: a policy is bound to workloads by label, so a one-character
  drift exempts the workload entirely and the object is a deny-all against nothing.

`tests/test_fleet.py::test_a_server_ships_the_whole_set` now also requires `tests/test_deploy.py`,
for the same reason `deploy/deployment.yaml`, `hpa.yaml` and `pdb.yaml` are on that list: the failure
is a *new* server copying a directory that predates the file, whose own tests then cannot notice
what it does not have. What only that file can hold is the server's own numbers and strings — its
port, its ingress peers, and the Service-to-ServiceMonitor port *name*, which resolves to no targets
and **no error** when it misses.

`CLAUDE.md`'s layer-4 paragraph is rewritten to describe what is now checked, and says in as many
words that its previous wording described the gap without naming it as one.

### What this does not do

It reads the files this repository ships. Whether the cluster *applies* them — whether a
NetworkPolicy controller is installed at all, which is the usual way a default-deny policy is inert
— is outside any test here and stays where the sign-off record puts that class of claim.

## What keeps it true

- `tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload` — driven,
  each mutation applied and verified by `git diff --numstat`: dropping `- Egress` from `props`
  (`0 1`) reds `[props]`; a `0.0.0.0/0` egress rule on `safety` (`4 1`) reds `[safety]`; renaming
  `calc`'s `podSelector` label by two characters (`1 1`) reds `[calc]`. Each failure names only its
  own server, and the other 62 stay green.
- `tests/test_fleet.py::test_a_server_ships_the_whole_set` — driven: moving
  `servers/props/tests/test_deploy.py` away reds `[props]`.
- The two together, driven as the review's exact scenario — permissive policy **and** no
  `test_deploy.py` — now red twice where they were green: `2 failed, 232 passed`.
- `tests/test_fleet.py::test_every_path_claude_md_cites_under_a_real_directory_resolves` — which
  caught the first draft of the rewritten paragraph citing a root `tests/test_deploy.py` that has
  never existed, the same mis-citation its own docstring records from last time.
