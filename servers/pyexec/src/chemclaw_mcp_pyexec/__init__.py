"""`pyexec`: run a short Python analysis in a bounded, offline child process.

One tool. The agent sends a program and a JSON payload; the program runs with numpy, pandas, scipy,
matplotlib, sympy, scikit-learn, RDKit and OpenBabel importable, in a process that is killed by
process group on a wall clock, holds no credential in its environment, and has no route off the pod.
A jailed `open()` lets it read and write files under that one call's own scratch directory; nothing
written there survives past the call.

`engine/` is the whole capability and imports no transport. See `README.md` for the control list and
— more importantly — for which of those controls are the security boundary and which are not.
"""
