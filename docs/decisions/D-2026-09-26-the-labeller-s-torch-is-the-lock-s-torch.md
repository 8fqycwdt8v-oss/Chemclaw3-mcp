# D-2026-09-26-the-labeller-s-torch-is-the-lock-s-torch — rxnlabel's models install from the hashed lock, and ship the lock's torch

**Status:** accepted · **Date:** 2026-09-26

## What was found

`D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` put every Containerfile on
`uv export --frozen ... --require-hashes`, with one exception it named: `servers/rxnlabel`'s
runtime stage ran a third `pip install` of `"rxnmapper==0.4.3" "rxn-insight==0.1.3"` straight from
PyPI through the CPU-torch `--extra-index-url`. The two named packages were version-pinned to the
lock by a test; their transitive closure — torch, transformers, tokenizers and the rest — was
resolved by pip on the day of each build, with no hashes. So it was the one image whose closure
`make deps-audit` did not describe, and nothing in the suite refused a second install of that shape
in any image: `test_every_image_installs_the_closure_the_audit_read` required the hashed export to
exist and never asked whether it was the only way in.

Folding the pair into the export decides which torch ships. `uv.lock` resolves PyPI's torch 2.13.0,
whose linux wheel depends on the `nvidia-*`, `cuda-*` and `triton` wheels; the old install took the
CPU index's. Summed from `uv.lock`'s recorded wheel sizes for linux x86_64 / cp311, the exported
`models` closure is ~2.9 GB of wheels, ~2.2 GB of them the CUDA runtime a CPU-only pod never loads.

## What was decided

- **The models are the `models` extra, exported from the lock and fetched `--require-hashes`**, in
  the same build-stage pass as every other dependency, and the runtime stage installs
  `chemclaw-mcp-rxnlabel[models]` `--no-index` from that wheelhouse. Nothing in the image resolves.
- **The torch that ships is the lock's**, CUDA closure and all. That is what `servers/rxnpredict`'s
  image, installing its extras from the same lock, already ships, so the fleet has one answer
  rather than two; and the alternative — a CPU torch source in `pyproject.toml` — re-locks both servers and
  raises a question this change should not answer in passing: whether `pip-audit` audits a `+cpu`
  local version or skips it. That is queued in `docs/BACKLOG.md` with the figures above. The price
  accepted here is image size; the price refused was an unaudited, unhashed closure.
- **Any image installing outside a hashed pass fails the suite.** `unhashed_installs` accepts a pip
  command in exactly three shapes — `--require-hashes -r <export>`, `--no-index` from the wheelhouse,
  `--no-deps` over local paths — plus the build-stage bootstrap of pip and `uv`, verbatim; any
  `--index-url`, `--extra-index-url` or `--trusted-host` is refused whatever else the line carries.

## What the first full build found

The first version of this change was verified only as far as the build stage; the runtime stage
ran out of disk locally, and CI built no image. A full build of that commit (4e196ab) then failed
at `chmod -R a+rX /opt/models: No such file or directory`, and the cause was not the disk:

- **RXNMapper 0.4.3 ships its checkpoint inside its wheel.** `rxnmapper/models/transformers/
  albert_heads_8_uspto_all_1310k/` (a 3.2 MB `pytorch_model.bin`, its config and vocabulary) is in
  the locked `py3-none-any` wheel, and `RXNMapper()` resolves `model_path` with
  `pkg_resources.resource_filename("rxnmapper", ...)` and calls `from_pretrained` on that local
  directory. It never touches the HuggingFace cache, so `HF_HOME=/opt/models/hf` was never created
  and the `chmod` over it had nothing to act on. The Containerfile's account — "a transformer whose
  library downloads its checkpoint on first use", "constructing an `RXNMapper` is what pulls the
  checkpoint" — described a mechanism this version does not have. The weights are therefore
  already covered by the wheel's hash in `uv.lock`, which is a stronger property than a bake had.
- **Behind it, a second failure no build had reached:** importing `rxn_insight.reaction` failed with
  `ImportError: libXrender.so.1`, then `libexpat.so.1`. `rxn_insight.utils` imports
  `rdkit.Chem.Draw.rdMolDraw2D`, which the locked RDKit wheel links against `libXrender`,
  `libXext`, `libX11` and `libexpat`; `python:3.11-slim` has none of them.

So the bake became a **load check** and the image gained three Debian packages:

- `HF_HOME` and the `chmod` are gone. `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are set
  *before* the check, which constructs the mapper, requires that `model_path` lies inside the
  installed `rxnmapper` package, and maps one reaction. A release that moved its weights back to
  the hub fails that build, not a pod's startup.
- `libxrender1 libxext6 libexpat1` are installed at the top of the runtime stage (the set taken
  from `ldd` over every shared object in `rdkit/` and `rdkit.libs/`), and the existing
  `rxn_insight` import check now passes. `servers/rxnpredict` installs the same `rxn-insight` from
  the same lock and had no import check at all, so it gained the same packages and the same check;
  it did not share the `chmod` defect, because its `fetch_models.py` really does write `HF_HOME`.
- CI now builds every image on pull requests (`images` in `.github/workflows/ci.yml`, servers
  discovered from the tree), because the suite reads Containerfiles as text and only a build runs
  them.

## What keeps it true

- `tests/test_fleet.py::test_no_image_installs_what_the_lock_did_not_hash`
- `tests/test_fleet.py::test_the_unhashed_install_check_refuses_the_shapes_it_was_written_for`
- `tests/test_fleet.py::test_every_image_installs_the_closure_the_audit_read`
- `tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read`
- `tests/test_delivery.py::test_ci_builds_every_image_from_a_list_it_discovers`
