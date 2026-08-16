# Adding a server

Read [`CLAUDE.md`](../CLAUDE.md) first — it is the *why*. This is the checklist.

The reference server is `servers/props/`. Copy its structure rather than inventing one; the
variance between servers should be in what they compute, not in how they are shaped.

## Before you write anything

1. **Check it is not already a Chemclaw3 capability.** The exclusion table is in `CLAUDE.md`. A
   second implementation of a capability is a second answer to one question.
2. **Check it can work with no egress.** If the value depends on a live third-party API, the module
   is rejected or redesigned around a snapshot. There is no third option.
3. **Claim a name and a port in `MODULES.md`,** in this same pull request. The name is used four
   times and they must match: directory, package suffix, manifest `name:`, and the key Chemclaw3
   addresses it by.
4. **Decide whether the tool is request/response or orchestration.** This used to read "decide
   whether any tool can exceed ~20 s", and that was the wrong question — duration is not the
   property this fleet promises. `servers/calc` runs CREST searches that take hours.

   The property is **statelessness**: a tool takes its arguments, computes, and returns. It holds no
   job record, offers no resumption, and if it is interrupted the caller simply calls again. A
   *composite* — optimise, take a Hessian, displace along the imaginary mode, repeat — is a loop
   with state, and the giveaway is that **its key names its own output**, so a caller cannot ask
   "have I computed this?" before running it. That belongs in Chemclaw3 as a durable job; its
   *parts* belong here, each separately keyed.

   `compute_thermochemistry` is the worked example and it went both ways before it settled: it was
   ported, found to be underivable, nearly deleted outright, and finally **decomposed** —
   `relax_structure` + `compute_hessian` here, the RRHO partition functions and the refinement loop
   in Chemclaw3. The measurement that decided it: repeating thermochemistry in Chemclaw3 costs
   0.007 s against 0.816 s cold for ethanol and 0.012 s against 3.273 s for ethyl acetate, two
   orders of magnitude that come entirely from the *nested* caches. Shipping the composite would
   have converted every repeat into a full recompute; decomposed, every one of those hits still
   hits.

   So a slow tool is fine and a stateful one is not. What a slow tool owes the fleet:

   - a **bound on its input** so the cost cannot run away unpriced;
   - a `request_timeout` in its manifest that states the real budget rather than inheriting a
     habitual one and dying mid-calculation;
   - a docstring that tells the model what it is asking for;
   - and a `calculation_key`-style probe, if a caller is expected to cache it.

## The files

```
servers/<name>/
├── connector.yaml               # every tool classified read_only or state_changing, exactly once
├── pyproject.toml               # this server's dependency closure and nobody else's
├── Containerfile                # rootless, dataset baked in, no credential for anything external
├── README.md                    # what it serves, the exact artifact it reads, and who refreshes it
├── deploy/networkpolicy.yaml    # policyTypes includes Egress; egress: []
├── src/chemclaw_mcp_<name>/
│   ├── engine/                  # pure computation — no FastAPI, no MCP, no network
│   ├── tools.py                 # the FastMCP surface
│   ├── app.py                   # connector_app(server, name=..., token_env=...)
│   └── data/                    # records + dataset.json
└── tests/
    ├── test_dataset.py          # the corpus validated against itself
    ├── test_tools.py            # what the tools answer, and what they refuse
    ├── test_no_egress.py        # three lines; see props
    ├── test_deploy.py           # the NetworkPolicy, asserted in both directions
    └── test_server.py           # real socket, real handshake, real 401, manifest mirror
```

Then symlink the manifest — never copy it:

```sh
mkdir -p manifests/<name>
ln -s ../../servers/<name>/connector.yaml manifests/<name>/connector.yaml
```

## The dataset

**A server with no dataset is possible and `calc` is the first one** — every number it returns is
computed from its dependencies' own compiled parameters (tblite's GFN Hamiltonians, RDKit's Crippen
and QED tables) rather than read from a corpus, so there is no `data/` and no `test_dataset.py`. The
obligation does not disappear with the file; it moves. What replaces "validate the corpus against
itself" is **proving the computation needs nothing from outside the process**, which
`servers/calc/tests/test_no_egress.py` does by running one of each kind of calculation with the
egress guard armed. The failure being ruled out is the one a numerical library can produce: fetching
parameters, model weights or a licence check on first use.

`data/dataset.json` needs all six fields — `name`, `version`, `licence`, `retrieved_from`,
`description`, `sha256` — and `load_dataset` refuses without them. Compute the checksum with
`sha256sum` and paste it; the loader verifies it on every start.

**Then make the corpus validate itself.** This is the part most worth the effort, and `props`
shows the pattern: CAS check digits, molecular weight against formula, and Antoine constants against
the tabulated boiling point are all pairs of *independently written* numbers that must agree, so a
transposed digit fails a test instead of answering a question. Find the equivalent for your data
before falling back to "it was reviewed once".

## The tool docstrings

They are the prompt. For each tool:

- What is it for, in the words a chemist would use to ask for it.
- Every unit, on every argument and every returned field.
- What the answer is **not** evidence of.
- Where the number came from (`source`), and by which method when there is more than one.
- What it does when it does not know: refuse and name the corpus, never approximate.

## Before you open the pull request

```sh
make check          # lint + mypy --strict + the whole suite
make offline-run    # the same suite with the network taken away
```

Then wire it to a real Chemclaw3 checkout following [`integration.md`](integration.md) and ask the
agent a question only this server can answer. Confirm the tool was called — `source` in the answer
is the tell — and check `/readyz`, because an unreachable connector degrades silently rather than
erroring.

Finally, update `MODULES.md`'s status row and the port table in `CLAUDE.md` if a block changed.
