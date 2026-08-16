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
4. **Decide whether any tool can exceed ~20 s.** If so it is a Chemclaw3 durable job, declared as a
   `jobs:` entry, and the pull request touches both repositories.

   `calc` is the one server that answers this with "sometimes, deliberately", and what makes that
   allowed is the shape of the exception rather than an appeal to convenience: the expensive tool
   (`compute_thermochemistry`) has a **hard input bound** that refuses anything past it and names the
   durable path as the alternative, the manifest's `request_timeout` states the real budget instead
   of the fleet's habitual 30 s, and the tool docstring tells the model what it is asking for. A slow
   tool with none of those three is still a durable job. See `servers/calc/README.md`.

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
