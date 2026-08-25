"""The labelling engine: roles, representations, names, and the version that identifies them.

Five modules, split by what each one needs rather than by what it computes:

* `agents` — the structure rules that decide catalyst / ligand / base / solvent / additive, and the
  two that need the rest of the flask to decide at all.
* `roles` — one reaction's species, each given a role, from the slot it was written in, the atom
  map where there is one, and those rules.
* `mapping` — RXNMapper, where installed, and a truthful `None` where not.
* `naming` — Rxn-INSIGHT's SMIRKS, where installed, and a truthful nothing where not.
* `species` — canonical form, Bemis-Murcko scaffold, and the first-party functional-group
  vocabulary that is a wire contract rather than a convenience.
* `version` — what all of the above amounted to, as the string a stored label is stamped with.
"""
