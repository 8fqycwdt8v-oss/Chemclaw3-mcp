# D-2026-09-19-a-claim-about-another-repository-is-checked-by-re-reading-it — A claim about another repository is checked by re-reading it, and a count in prose is the same defect wherever it appears

**Status:** accepted · **Date:** 2026-09-19 · **Builds on:**
`D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing` (prose that describes a control is not
the control), and the argument `CLAUDE.md`'s `Ports` section and `docs/BACKLOG.md`'s rule 3 already
make about a number nobody re-derives.

## What was measured

Every constraint this repository *enforces* was audited and found sound. What was wrong was the
prose that is not enforced, and it failed in two shapes that turn out to be one shape.

**Shape one — a census of the neighbour, never re-read.** Four sentences in `CLAUDE.md` describe
Chemclaw3, and each was written once from a reading of that repository and then left. Re-read on
2026-09-19 against a full checkout of that repository:

| the sentence said | what the repository says |
| --- | --- |
| "Knowledge graph read/write, **the PR-gate**" is a Chemclaw3 capability | there is no PR-gate: `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted it and every module behind it, and `src/chemclaw/kg/git_writer.py` opens with "ended the PR-gate" |
| the exclusion table lists every Chemclaw3 capability worth refusing a server over | it omitted `results`, the one bundle with **no `endpoint:`** — jobs only, because a write is not an agent-facing tool there — so nothing in this fleet dials it and nothing here noticed it existed |
| "**Two rows** of that table have left it outright" | two rows *left*, but `src/chemclaw/connectors/rxnpredict/connector.yaml` calls itself "the third bundle of that shape"; the shape and the row are different things and the sentence collapsed them |
| "Observed on 2026-08-27 … everything observed is below 8850" | **three** of that repository's endpoints are inside this fleet's 8850–8899 block, because they address `chem`, `rxnpredict` and `safety` — this fleet's own servers |

The last is the one worth dwelling on. It was not merely stale: it was *reassuring*. A reader
checking for a port collision would have read "everything observed is below 8850", stopped, and
never learned that the overlap is the seam working rather than a clash. And the sentence had already
been wrong once before, in the other direction — the `Ports` section records that it published
Chemclaw3's range as 8810–8815 while `bo` sat on 8816 — so this is the **second** time one paragraph
about one neighbouring repository went wrong in the only way it can.

**Shape two — the no-count rule applied everywhere except to ourselves.** This repository refuses a
count of its own ports (the deleted port table), of its own backlog rows (`docs/BACKLOG.md` rule 3,
held by `test_nowhere_in_the_file_states_a_live_row_count`) and of its own decision records
(`test_the_decision_ledger_states_no_count_of_its_own_records`). It then wrote "seven servers" in
the present tense once in `CLAUDE.md`, three times in `docs/adding-a-server.md` and twice in
`docs/delivery.md`, over a fleet of **eleven** — plus "the gap was three of seven" in `CLAUDE.md`
and "19 servers" in `README.md` about a catalogue nothing reconciles.
Same defect, different subject: the rule had been applied to the subjects somebody had been burned
by, and not to the subject the burn came from.

Two more of the same kind, found in the same sweep:

- `CLAUDE.md`'s canonical `servers/<name>/` tree listed seven files where
  `test_a_server_ships_the_whole_set` requires thirteen. Every server on disk ships thirteen. A
  contributor copying the tree from the document they are told to read *first* fails their first
  `make check`, with two documents disagreeing and neither wrong about itself.
- `CLAUDE.md`'s egress-guard paragraph enumerated six intercepted calls where
  `mcp_server_kit/egress.py` rebinds nine — the reverse-lookup pair and `gethostbyname_ex` were
  added to the guard and never carried back. Here the drift was *under*-stating a control, which is
  the harmless direction; the same mechanism over-stating one is a reader believing in an
  interception nobody makes.

And one instruction that told a contributor to do what a test forbids:
`docs/adding-a-server.md`'s last line said to update "the port table in `CLAUDE.md`". That table was
deleted for publishing two taken ports as free, and
`test_claude_md_holds_no_second_port_registry` reds the moment anybody follows the instruction.

**And two rows of `docs/BACKLOG.md` that its own rule 1 already forbids.** That file says a closed
row is *deleted* in the commit that closes it, and it carried an empty `## 7 — The coverage floor`
heading whose rows had been closed out from under it — a section that reads as live state and holds
nothing. The Rxn-INSIGHT row is the append-don't-delete shape one level in: a blocking reason, a
later paragraph headed "That third reason is false, measured 2026-09-18" refuting it, and a
counter-indication, stacked in one row that a reader has to reconstruct in order to find out what is
actually true. The dead reason is removed rather than annotated; the measurement that matters —
including the one that points *against* the change — stays, because it is what the next session
needs. Neither is a new decision; both are that file's rule applied to itself, which is this
record's shape everywhere else too.

## The decision

**1. A claim about another repository is checked by re-reading that repository, and carries the date
it was read.** Not by remembering, not by citing the record that was true when it was written. No
test here can open that checkout, which is exactly why the date is part of the claim: a dated
observation is falsifiable by one command, an undated one is an assertion. Where the fact is
*already* checkable in a file this repository holds — a port, a tool surface — the claim names that
file instead of restating the fact, because a restatement goes stale on somebody else's merge
schedule.

**2. A count in prose is the same defect wherever it appears — ports, backlog rows, ADRs, servers,
files in a tree, or a neighbour's manifests.** The rule is not "never write a number". It is that a
number must be either *self-verifying in the sentence that holds it* — "`calc` and `rxnlabel` are
the two servers Chemclaw3 must not discover" is checked by reading two names in the same breath — or
it must not be written. A count that stands *for* a set, with nothing beside it to check it against,
rots at the next merge and is believed until it is not.

**3. A set declared in two documents is declared in one, and a test holds the other end.** The file
tree moves to `docs/adding-a-server.md` alone; the intercepted-call list moves to `egress.py` alone.
`CLAUDE.md` links to both and keeps the *why*, which is what it is for.

**4. `docs/decisions/` is out of scope for rule 2, deliberately.** A merged record is never edited
and is a claim about the day it was written, which is the one place a figure is correct by being
historical. A ratchet that policed counts there would be asking authors to falsify their own
measurements.

## What this does not settle

The fourth sentence in the census table was only *found* because someone re-read the neighbour. The
rule above says to do that; nothing enforces it, and nothing here can. What is enforced is the
half that lives in this tree — that this repository does not state a count of itself, and does not
carry a second copy of a set it declares elsewhere. The neighbour-census half remains a review
obligation with a date attached, and saying so is the point: an unenforced rule presented as an
enforced one is the defect this record is about.

## What keeps it true

- `tests/test_fleet.py::test_no_prose_here_counts_this_fleet_s_servers_without_naming_them` — every
  `<number> servers` phrase in `CLAUDE.md`, `README.md` and `docs/*.md` names at least that many of
  this fleet's servers in the same paragraph, or fails. `docs/decisions/` is excluded per rule 4.
- `tests/test_fleet.py::test_the_required_file_set_is_declared_once` — `docs/adding-a-server.md`'s
  tree lists every entry of `REQUIRED_SERVER_FILES`, and `CLAUDE.md` does not draw a second tree.
  The requirement and the checklist can no longer disagree.
- `tests/test_fleet.py::test_claude_md_claims_no_interception_the_guard_does_not_make` — every
  socket call `CLAUDE.md` §1 names is one `arm()` actually rebinds, matched against the live
  `socket` surface. The document no longer holds the list; this stops it growing a phantom.
- `tests/test_fleet.py::test_claude_md_holds_no_second_port_registry` — already existed, and is what
  the corrected `docs/adding-a-server.md` final step now agrees with instead of contradicting.
- `tests/test_backlog_register.py::test_nowhere_in_the_file_states_a_live_row_count` and
  `tests/test_backlog_register.py::test_the_decision_ledger_states_no_count_of_its_own_records` — the
  two places rule 2 was already enforced, and the precedent it is generalised from.
- Nothing keeps the census in `CLAUDE.md`'s `Ports` and `Never duplicate a Chemclaw3 capability`
  sections true, by construction: they are claims about a checkout this suite cannot open. They
  carry the date they were read, which is the whole of what is available.
