"""The labelling engine: roles, representations, names, and the version that identifies them.

* `agents` — structure rules for catalyst / ligand / base / solvent / additive.
* `roles` — one reaction's species, each given a role from its slot, the atom map and those rules.
* `mapping` — RXNMapper where installed, `None` where not.
* `naming` — Rxn-INSIGHT's SMIRKS where installed, nothing where not.
* `species` — canonical form, scaffold, and the functional-group vocabulary (a wire contract).
* `version` — the string a stored label is stamped with.
"""
