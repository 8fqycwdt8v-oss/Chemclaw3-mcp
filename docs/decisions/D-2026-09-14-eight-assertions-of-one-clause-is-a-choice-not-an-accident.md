# D-2026-09-14-eight-assertions-of-one-clause-is-a-choice-not-an-accident — Eight assertions of one clause is a choice, not an accident

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over `14a764b`
(PR #67), on top of `e8cf74a` (PR #68).

## Context

`D-2026-09-14-a-layer-nobody-reads-fleet-wide-is-a-convention` added
`tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload`, and its
argument for the third of that test's three clauses was a causal story about the seven per-server
files. It says, in bold:

> the `podSelector` matches the Deployment's own pod label. **This is the clause the per-server
> files mostly did not carry** — `props` held the pair and the others did not

**One `grep` settles it and it is false.** Measured against the parent the record was written on:

```
$ git grep -L test_the_pod_label_matches_the_networkpolicy_selector 24b50ec -- 'servers/*/tests/test_deploy.py'
$ echo $?
1                                  # nothing listed: all seven carry it
```

The bodies are identical, and `calc`'s docstring opens "The highest-value check here" — the opposite
of not carrying it. Driven at HEAD, mutating `servers/calc/deploy/networkpolicy.yaml` line 22 so the
selector reads `chemclaw-mcp-calcX` (numstat `1 1`) reds `calc`'s **own** file twice
(`test_the_policy_selects_this_server` and `test_the_pod_label_matches_the_networkpolicy_selector`)
as well as the fleet-wide parametrisation.

This is the failure that record's own neighbour in the same commit
(`D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance`) is about, and the failure
`D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing` names in its title: a claim about the
tree that nobody ran against the tree. The conclusion is untouched — the fleet-wide test is right
and stays — but it rested on a reason that reading seven files refutes.

The second half is what the story concealed: **that clause is now asserted eight times** — seven
per-server copies plus the parametrised one — and nothing anywhere said so, so a reader arrives at
the eighth with no way to tell whether it is the gate or a duplicate somebody forgot to delete.

## Decision

The reason stated for the fleet-wide test is the one that survives reading the seven files, and it
is the reason the record's *other* paragraphs already gave for the file list: **the per-server copies
are seven copies of one rule, and none of them is owed by an eighth server.** What
`test_a_server_ships_the_whole_set` requires of a new server is the *file*; a file copied and
trimmed still satisfies it. `server_dirs()` reads the filesystem, so the fleet-wide test binds the
eighth server the day its directory exists, which is the standing this repository already gave the
bearer check in `D-2026-09-12-a-shared-helper-is-not-a-proof-it-was-applied`.

**The eight-fold redundancy is kept and named.** The fleet-wide test is the gate for a new server.
The seven stay for two reasons that are not the gate's: each sits beside that server's own ports,
ingress peers and scrape port *name* — the numbers only that file can hold — which is what a
reviewer has open when changing them; and a per-server failure names the file to open where a
parametrised failure names an id. Deleting them would buy one assertion per server and cost the
locality; that trade is recorded here so the next reader does not have to guess it was one.

`D-2026-09-14-a-layer-nobody-reads-fleet-wide-is-a-convention` is not edited. Its decision stands
in full; this record replaces its stated reason for the third clause and adds the redundancy it did
not mention.

## What keeps it true

- `tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload` — the
  docstring now states the measurement above rather than the causal story, and the test is unchanged.
  Driven: the `calc` selector drift reds it (`1 failed, 62 passed`), and deleting
  `servers/calc/tests/test_deploy.py` does not, which is the standing the fleet-wide one exists for.
- `servers/calc/tests/test_deploy.py::test_the_pod_label_matches_the_networkpolicy_selector` — the
  per-server half, driven by the same mutation on the same commit: `2 failed, 5 passed`. All seven
  servers carry this test; that is the fact the superseded sentence denied.
- `tests/test_fleet.py::test_a_server_ships_the_whole_set` — what actually binds an eighth server to
  ship the per-server file at all, and what it cannot bind that file to contain.
