# D-2026-09-18-a-gate-that-omits-a-layer-reads-like-one-that-ran-it — `make check` runs the offline lane where the kernel allows it, and names it where it does not

## Status

Accepted. Closes the `docs/BACKLOG.md` row that posed the choice; it does not revise
`D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see`, whose subject is a gate in
Jenkins rather than a layer in this one.

## Context

`egress.py` names four channels the runtime guard cannot reach by construction: a **child process**,
a **`ctypes` call into libc**, the private C type **`_socket.socket`**, and any syscall from a
**compiled extension**. The static scan reads imports, so it refuses two of them — `_socket`, and a
named compiled extension. The other two it cannot help with at all, and both for stated reasons:
`ctypes` is off its list deliberately because `servers/pyexec`'s sandbox needs it for
`prctl(PR_SET_DUMPABLE, 0)`, and `subprocess` is not a smell here but *how* `pyexec` and `calc` do
their work.

So exactly one thing covers those two: `make offline-run`, which takes the network namespace away
instead of asking Python nicely. **And it was outside `make check`.** A contributor running the
local gate saw a green line with two of the four layers unverified and nothing on screen saying so —
which is the failure this repository states as a rule elsewhere: a check that quietly shrinks is
worse than one that says what it did not look at.

## Decision

`make check` gains `offline-guarded`, which runs `offline-run` when an unprivileged network
namespace is available and otherwise prints a notice **naming the two channels it therefore did not
verify**.

**Folding it in costs CI nothing**, which is what makes this cheap rather than a trade.
`.github/workflows/ci.yml` invokes `make lint`, `make type` and `make cov` as separate steps and
runs `offline-run` as its own job; it never invokes `make check`. Nothing runs twice.

**Guarded rather than unconditional.** `unshare --net` needs unprivileged user namespaces and a
container can be configured without them. A gate that *failed* there would tell a contributor their
change is broken when it is their kernel. A gate that silently omitted the step is the defect being
fixed. Naming it is the third option, and it is the shape `deps-audit` in the same file already uses
for an audit it could not reach over the network.

Ordered before `deps-audit`, which stays last for the reason its own comment gives.

## Consequences

- A local `make check` is slower by one full suite run in a namespace, on machines that can do it.
  That is the cost of the gate meaning what it says.
- Three documents that described `make check`'s scope are corrected in the same commit — `README.md`,
  `CLAUDE.md`'s command list, and the four-channels paragraph in `CLAUDE.md` that named
  `offline-run` as what is "left" without saying that nothing in the default gate ran it.
- The wiring is asserted rather than trusted, and the first draft of this record argued it need not
  be — that the prerequisite list is "three words on one line in a file every contributor reads".
  That is the argument every unenforced invariant in this tree was written with, and this repository
  has a reader for the Makefile already (`tests/test_fleet.py` reads `SRC` and the `run-*` targets),
  so the cheap control was built instead of talked out of.

## What keeps it true

- `tests/test_fleet.py::test_the_gate_runs_the_only_layer_that_covers_two_of_the_four_egress_channels`
  reads `check`'s own prerequisite list for `offline-guarded`, and reads that target for the
  `unshare` probe, the `offline-run` call and the notice naming the lane it did not run. It asserts
  the *wiring*, not the lane's result: whether the suite passes with the network taken away is
  `offline-run`'s business and CI's separate `offline` job, which this decision leaves unchanged.
