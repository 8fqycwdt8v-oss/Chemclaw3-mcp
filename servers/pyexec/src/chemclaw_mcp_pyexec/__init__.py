"""`pyexec`: run a short Python analysis in a bounded, offline child process.

One tool. The program runs with the scientific stack importable, in a child killed by process group
on a wall clock, with no credential in its environment and no route off the pod. A jailed `open()`
confines file access to the call's own scratch directory, which does not outlive the call.
`engine/` imports no transport. `README.md` says which controls are the security boundary.
"""
