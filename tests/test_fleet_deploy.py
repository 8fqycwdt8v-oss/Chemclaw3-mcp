"""Shipped deployment files: no file widens the egress allowlist or moves a bound the code reads
from the environment, every such bound refuses at import and is reported on the probe, and the
session ceiling is derived from the smallest pod.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import shlex
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml
from mcp_server_kit.egress import GUARD_DISABLED_VALUES
from mcp_server_kit.sessions import (
    DEFAULT_MAX_SESSIONS,
    SESSION_BACKLOG_BUDGET_BYTES,
    SESSION_COST_BYTES,
    SMALLEST_POD_MEMORY_LIMIT_BYTES,
)
from mcp_server_kit.testing import reimported

ROOT = Path(__file__).resolve().parents[1]


SERVERS = ROOT / "servers"


# A variable a shipped file sets, and the value it sets it to — or `None` where the file *names* the
# variable and does not hold the value: a `valueFrom:` reference into a ConfigMap or Secret, or a
# Containerfile `ARG` the build supplies. Both ratchets below read `None` as unprovable rather than
# as absent, which is the only safe reading of a file that cannot answer the question.
EnvSetting = tuple[str, str | None]


# A `NAME=value` assignment at the head of a command line, which is how a value is set *past*
# every `env:` block and every `ENV` instruction:
# `command: ["sh", "-c", "MCP_EGRESS_GUARD=off exec …"]`.
_SHELL_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def _inline_assignments(command: object) -> list[EnvSetting]:
    """Every `NAME=value` a command line sets, whether it is a JSON array or one shell string.

    A container that sets a variable in its own `command:` sets it for the server process exactly
    as an `env:` entry would, and a parser reading only `env:` reports clean. Both shapes reduce to
    the same thing: split every string with `shlex` and keep the tokens that are assignments —
    over-inclusive on purpose, since a flag (`--port=8850`) cannot match and a real assignment must.
    """
    parts = command if isinstance(command, list) else [command]
    found: list[EnvSetting] = []
    for part in parts:
        if not isinstance(part, str):
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            tokens = part.split()
        for token in tokens:
            match = _SHELL_ASSIGNMENT.match(token)
            if match:
                found.append((match.group(1), match.group(2)))
    return found


def _env_pairs(node: object) -> list[EnvSetting]:
    """Every environment variable a parsed manifest sets, however nested and however spelled."""
    found: list[EnvSetting] = []
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str):
            if "value" in node:
                found.append((name, str(node["value"])))
            elif "valueFrom" in node:
                found.append((name, None))
        for key in ("command", "args"):
            if key in node:
                found.extend(_inline_assignments(node[key]))
        for value in node.values():
            found.extend(_env_pairs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_env_pairs(item))
    return found


def _instructions(text: str) -> list[str]:
    r"""A Containerfile's instructions, one per entry, with backslash continuations joined."""
    joined: list[str] = []
    buffer = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        joined.append(buffer + stripped)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


def _containerfile_env(label: str, text: str) -> list[EnvSetting]:
    r"""Every variable a Containerfile's `ENV` instructions set, however it is spelled."""
    found: list[EnvSetting] = []
    for instruction in _instructions(text):
        words = re.split(r"\s+", instruction, maxsplit=1)
        verb, remainder = words[0], words[1] if len(words) > 1 else ""
        if verb.upper() in {"CMD", "ENTRYPOINT"}:
            found.extend(_inline_assignments(_command_words(remainder)))
            continue
        if verb.upper() != "ENV":
            continue
        try:
            tokens = shlex.split(remainder)
        except ValueError as exc:  # pragma: no cover - a malformed Containerfile
            raise AssertionError(f"{label}: cannot parse {instruction!r}: {exc}") from exc
        if not tokens:
            continue
        if "=" not in tokens[0]:
            found.append((tokens[0], " ".join(tokens[1:])))
            continue
        for token in tokens:
            name, _, value = token.partition("=")
            if name:
                found.append((name, value))
    return found


def _command_words(remainder: str) -> list[str] | str:
    """A `CMD`/`ENTRYPOINT` argument as its words — the JSON exec form, or the shell form verbatim.

    Docker's two forms differ in quoting, not in effect: `["sh", "-c", "X=1 exec …"]` and
    `sh -c "X=1 exec …"` both run the same process with the same environment.
    """
    if remainder.lstrip().startswith("["):
        try:
            parsed = json.loads(remainder)
        except ValueError:
            return remainder
        if not isinstance(parsed, list):
            return []
        return [word for word in parsed if isinstance(word, str)]
    return remainder


def _env_settings(label: str, text: str) -> list[EnvSetting]:
    """Every environment variable one shipped file sets, whichever kind of file it is.

    `label` decides how the text is read — a deployment manifest is YAML with `env:` entries, a
    Containerfile is `ENV` instructions. Shared by both ratchets below, so the two cannot disagree
    about what a file sets.
    """
    if label.endswith(".yaml"):
        return _env_pairs(list(yaml.safe_load_all(text)))
    return _containerfile_env(label, text)


def _shown(value: str | None) -> str:
    """A value as an offence reads it — `None` is a file naming the variable but not its value."""
    return repr(value) if value is not None else "a value this file does not hold"


def _provably_arms(value: str | None) -> bool:
    """Whether this value, as written in the file, *provably* leaves the egress guard armed."""
    if value is None or "$" in value:
        return False
    return value.strip().lower() not in GUARD_DISABLED_VALUES


def _egress_offences(label: str, text: str) -> list[str]:
    """Every way one shipped file departs from the posture: guard on, allowlist empty.

    Split out from the test so the ratchet can be shown to bite on a widened manifest without one
    existing in the tree.
    """
    offences: list[str] = []
    if label.endswith(".yaml") and "envFrom" in text:
        offences.append(f"{label}: uses envFrom, which can carry MCP_EGRESS_* unseen")
    for name, value in _env_settings(label, text):
        if name == "MCP_EGRESS_ALLOW":
            offences.append(f"{label}: sets MCP_EGRESS_ALLOW={_shown(value)}")
        if name == "MCP_EGRESS_GUARD" and not _provably_arms(value):
            if value is not None and value.strip().lower() in GUARD_DISABLED_VALUES:
                offences.append(f"{label}: disables the egress guard ({value!r})")
            else:
                offences.append(
                    f"{label}: sets MCP_EGRESS_GUARD to {_shown(value)}, so this file cannot show "
                    "the guard is armed in the image it builds"
                )
    return offences


def shipped_deployment_files() -> list[Path]:
    """Every file a deployment of this fleet ships — the set both deployment ratchets read."""
    files = [path for path in SERVERS.glob("*/deploy/**/*") if path.is_file()]
    files += [path for path in SERVERS.glob("*/Containerfile*") if path.is_file()]
    return sorted(files)


def test_a_deployment_directory_holds_only_shapes_the_ratchets_can_read() -> None:
    """`deploy/` is YAML and nothing else, because the readers of it dispatch on that suffix.

    The two ratchets now glob every file under `deploy/`, which closes the "a third filename is
    invisible" hole — but reading a file is not understanding it. `_env_settings` treats anything
    not ending `.yaml` as a Containerfile, so a `deployment.yml`, a `kustomization.yaml.tpl` or a
    JSON patch would be scanned for `ENV` instructions, find none, and be reported clean. This is
    the half that stops a file arriving in a shape the parser answers wrongly rather than not at
    all.
    """
    unreadable = sorted(
        str(path.relative_to(ROOT))
        for path in SERVERS.glob("*/deploy/**/*")
        if path.is_file() and path.suffix != ".yaml"
    )
    assert not unreadable, (
        f"{unreadable} sit under a server's deploy/ and are not `.yaml`. The egress and bound "
        "ratchets read every file there, but `_env_settings` parses anything else as a "
        "Containerfile — it would find no `ENV` and report the file clean. Rename it, or teach "
        "`_env_settings` the shape in the same commit."
    )
    mislabelled = sorted(
        str(path.relative_to(ROOT))
        for path in SERVERS.glob("*/Containerfile*")
        if path.is_file() and path.suffix == ".yaml"
    )
    assert not mislabelled, f"{mislabelled} would be parsed as YAML by its name; rename it"


def test_no_shipped_deployment_widens_the_egress_allowlist() -> None:
    """`MCP_EGRESS_ALLOW` is empty in every shipped deployment — asserted, not asserted *about*."""
    shipped = shipped_deployment_files()
    assert shipped, "no deployment manifests found; has the layout changed?"
    offences = [
        offence
        for manifest in shipped
        for offence in _egress_offences(
            str(manifest.relative_to(ROOT)), manifest.read_text(encoding="utf-8")
        )
    ]
    assert not offences, "the shipped posture is no egress and no allowlist:\n  " + "\n  ".join(
        offences
    )


def test_the_allowlist_check_bites() -> None:
    """A ratchet that passes on everything is not a ratchet, so it is shown failing on purpose."""
    widened = _egress_offences(
        "servers/x/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: MCP_EGRESS_ALLOW\n          value: weights.example.org\n",
    )
    assert widened == [
        "servers/x/deploy/deployment.yaml: sets MCP_EGRESS_ALLOW='weights.example.org'"
    ]
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=off\n") == [
        "servers/x/Containerfile: disables the egress guard ('off')"
    ]
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=on\n") == []

    # The shape the two ML servers actually ship: one `ENV` spanning lines, the guard not first and
    # not last, a comment paragraph above it and a comment line inside the continuation.
    continued = (
        "# The three switches that keep inference local.\n"
        "ENV HF_HOME=/opt/models/hf \\\n"
        "    HF_HUB_OFFLINE=1 \\\n"
        "# a comment inside a continuation does not end it\n"
        "    MCP_EGRESS_GUARD=off \\\n"
        '    MCP_EGRESS_ALLOW="evil.example.com" \\\n'
        "    CHEMCLAW_RXNPREDICT_MODEL_DIR=/opt/models\n"
    )
    assert _egress_offences("servers/x/Containerfile", continued) == [
        "servers/x/Containerfile: disables the egress guard ('off')",
        "servers/x/Containerfile: sets MCP_EGRESS_ALLOW='evil.example.com'",
    ]
    assert _egress_offences(
        "servers/x/Containerfile", continued.replace("GUARD=off", "GUARD=on")
    ) == ["servers/x/Containerfile: sets MCP_EGRESS_ALLOW='evil.example.com'"]

    # The guard set on as a continuation is what every shipped file that uses the form does, and it
    # must read as clean — a parser that flagged it would be noticed, a parser that cannot see it
    # at all is what shipped.
    assert _containerfile_env(
        "servers/x/Containerfile", continued.replace("GUARD=off", "GUARD=on")
    ) == [
        ("HF_HOME", "/opt/models/hf"),
        ("HF_HUB_OFFLINE", "1"),
        ("MCP_EGRESS_GUARD", "on"),
        ("MCP_EGRESS_ALLOW", "evil.example.com"),
        ("CHEMCLAW_RXNPREDICT_MODEL_DIR", "/opt/models"),
    ]

    # A comment line ending in a backslash must not swallow the instruction under it, which is why
    # comments are dropped before the join rather than after.
    assert _containerfile_env(
        "servers/x/Containerfile", "# a trailing backslash in prose \\\nENV MCP_EGRESS_GUARD=off\n"
    ) == [("MCP_EGRESS_GUARD", "off")]

    # Docker's legacy space-separated form sets one variable to the rest of the line.
    assert _containerfile_env(
        "servers/x/Containerfile", "ENV MCP_EGRESS_ALLOW evil.example.com"
    ) == [("MCP_EGRESS_ALLOW", "evil.example.com")]


def test_no_shape_that_hides_a_value_from_this_ratchet_reads_as_clean() -> None:
    """The four shapes three fresh-context reviewers walked the ratchet past, as the ratchet's own
    data.
    """
    indirected = _egress_offences(
        "servers/x/Containerfile", "FROM x\nARG GUARD=off\nENV MCP_EGRESS_GUARD=${GUARD}\n"
    )
    assert indirected == [
        "servers/x/Containerfile: sets MCP_EGRESS_GUARD to '${GUARD}', so this file cannot show "
        "the guard is armed in the image it builds"
    ]
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=\n") == []
    assert _egress_offences("servers/x/Containerfile", "ENV MCP_EGRESS_GUARD=on\n") == []

    assert _egress_offences("servers/x/Containerfile", "   ENV MCP_EGRESS_GUARD=off\n") == [
        "servers/x/Containerfile: disables the egress guard ('off')"
    ]

    assert _egress_offences(
        "servers/x/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: MCP_EGRESS_ALLOW\n          valueFrom:\n"
        "            configMapKeyRef: {name: egress, key: hosts}\n",
    ) == ["servers/x/deploy/deployment.yaml: sets MCP_EGRESS_ALLOW=a value this file does not hold"]

    assert _egress_offences(
        "servers/x/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n"
        '      command: ["sh", "-c", "MCP_EGRESS_GUARD=off exec uvicorn app"]\n',
    ) == ["servers/x/deploy/deployment.yaml: disables the egress guard ('off')"]
    assert _egress_offences(
        "servers/x/Containerfile", 'CMD ["sh", "-c", "MCP_EGRESS_GUARD=off exec uvicorn app"]\n'
    ) == ["servers/x/Containerfile: disables the egress guard ('off')"]
    assert _egress_offences(
        "servers/x/Containerfile", "ENTRYPOINT MCP_EGRESS_ALLOW=evil.example.com uvicorn app\n"
    ) == ["servers/x/Containerfile: sets MCP_EGRESS_ALLOW='evil.example.com'"]


# Every environment variable a first-party module turns into a number is something a deployment can
# move, and `_BOUND_ANCHORS` is the floor under the derivation below: a scan that silently stopped
# finding variables — a renamed `env_prefix`, a read through a helper — would agree with an empty
# tree forever. These five are the ones whose loss would matter most, one per
# mechanism and one per server that owns an admission ceiling, which `CLAUDE.md` calls the bound a
# slow tool owes the fleet. They are named here rather than in prose for the reason this repository
# keeps relearning: a count or a list in a document goes stale on somebody else's merge.
#
# **Five of these six now arrive through `_BOUND_HELPERS` rather than through a bare `os.environ`
# read**, which makes this floor load-bearing in a way it was not before: renaming
# `mcp_server_kit.limits.env_bound` without telling the scan takes them out of the inventory, and
# this is the assertion that says so instead of the set silently shrinking.
#
# `CHEMCLAW_PROPS_MAX_TB_RATIO` is anchored for a sharper version of the same reason: it is the one
# bound read through `env_ratio`, so it is the *whole* of what a rename of that second helper would
# cost. Measured when it moved behind the helper, the derived set went 44 -> 43 until the helper's
# name was added to `_BOUND_HELPERS` — a bound made safe at import and invisible to the deployment
# ratchet in the same commit, which is exactly the trade this floor exists to make loud.
_BOUND_ANCHORS = frozenset(
    {
        "MCP_MAX_SMILES_CHARS",
        "MCP_MAX_MOLECULE_ATOMS",
        "CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS",
        "CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS",
        "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS",
        "CHEMCLAW_PROPS_MAX_TB_RATIO",
    }
)


# A shipped deployment file that sets a derived numeric setting, and the argument for it. One row,
# and it is the one this check found on its first run over the real tree — which is also the
# counter-example to the widening check this one replaced. `crest_threads` defaults to `0`, meaning
# "let CREST's OpenMP size itself from `/proc/cpuinfo`", which is the *node's* core count and not
# something a container CPU limit changes; `servers/calc/Containerfile` sets `4`, and a comparison
# against the default would have read that narrowing as `4 > 0` and called it a widening.
#
# Everything else derived below is either a resource bound (`MCP_MAX_*`, the admission ceilings, the
# thread pool) or — in `servers/calc` — a *scientific* constant that enters `calc_version`, the
# primary key of Chemclaw3's calibration ledger; that server's own config docstring says changing
# one "is a scientific decision, not a deployment tweak". The two classes fail differently and need
# the same gate: one lets a pod be exhausted, the other writes rows nothing reconciles against.
_ARGUED_DEPLOYMENT_SETTINGS: frozenset[tuple[str, str]] = frozenset(
    {
        # CREST is the one thing in this image that should use more than one core, and the scrubbed
        # child environment means it has to be told so here rather than inherit `OMP_NUM_THREADS`.
        ("servers/calc/Containerfile", "CHEMCLAW_CREST_THREADS"),
    }
)


_NUMERIC_CASTS = frozenset({"int", "float"})


# The one first-party helper the derivation below follows into, by name.
#
# **Following a helper at all is a decision this scan spent a while refusing**, and the docstring of
# `numeric_env_bounds` named "a read through a helper" as a shape it does not parse. What changed is
# that eleven of this fleet's bounds moved behind exactly one such helper —
# `mcp_server_kit.limits.env_bound`, which reads the variable, refuses a value that would stop the
# server working, and returns an `int` — so not following it would have taken eleven variables out
# of the inventory in a single commit, four of the five `_BOUND_ANCHORS` rows among them. Measured
# on 2026-09-16 before this constant existed, the derived set went from 45 bounds to 34.
#
# It is a *name*, which is a coupling: renaming the helper stops the scan seeing its call sites.
# That is survivable only because `_BOUND_ANCHORS` fails loudly when it happens instead of letting
# the set quietly shrink, which is the floor the rest of this derivation already rests on. An
# arbitrary helper is still not followed, and
# `test_the_derivation_reads_the_two_spellings_it_used_to_miss` asserts both halves.
#
# **`env_ratio` joined it the day it existed, and adding the helper without adding the name would
# have been a silent shrink of exactly the kind this comment is about.** Measured: moving
# `CHEMCLAW_PROPS_MAX_TB_RATIO` off its bare `float(os.environ.get(...))` and behind that reader
# took the derived set from 44 bounds to 43 — the bound became safe at import and invisible to the
# ratchet that keeps a Containerfile or a ConfigMap from setting it outside its floor, which is a
# worse trade than the defect it fixed. The two readers are one entry per parsed type, not a
# general capability: `env_ratio` is followed because it is `env_bound` for a `float`.
_BOUND_HELPERS = frozenset({"env_bound", "env_ratio"})


def _is_environ(node: ast.AST) -> bool:
    """Whether `node` is `os.environ` (or a bare `environ` imported from it)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _called_name(node: ast.Call) -> str:
    """The bare name a call names, whatever it hangs off — `os.getenv` and `getenv` read the
    same."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _env_read(node: ast.AST) -> str | None:
    """The variable name `node` reads from the environment, when it is a literal.

    Three spellings: `os.environ["X"]`, `os.environ.get("X", …)` and `os.getenv("X", …)`. The third
    was missed until 2026-09-12 — nothing in `src/` uses it today, so the gap was latent, and a
    derivation that silently omits the most ordinary spelling of an environment read is the failure
    this whole ratchet is about.
    """
    if isinstance(node, ast.Call):
        func = node.func
        called = _called_name(node)
        if (
            isinstance(func, ast.Attribute)
            and called in {"get", "setdefault"}
            and _is_environ(func.value)
            and node.args
        ) or (called == "getenv" and node.args):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def _env_read_within(node: ast.AST) -> str | None:
    """The first literal environment read anywhere inside `node`.

    A subtree rather than the node itself, because the read is rarely the bare argument: the fleet
    writes `int(os.environ.get("X", "4"))` and `os.environ.get("X", "").strip()`.
    """
    for child in ast.walk(node):
        found = _env_read(child)
        if found is not None:
            return found
    return None


def _bound_helper_variable(node: ast.AST) -> tuple[str, int] | None:
    """The variable a `_BOUND_HELPERS` call names and the line it is named on, or `None`."""
    if not (isinstance(node, ast.Call) and _called_name(node) in _BOUND_HELPERS and node.args):
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value, node.lineno
    return None


def _numeric_environ_reads(tree: ast.Module) -> dict[str, int]:
    """Every environment variable this module turns into a number, and the line it happens on."""
    from_var: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            read = _env_read_within(node.value)
            if read is not None:
                from_var[node.targets[0].id] = (read, node.lineno)
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _NUMERIC_CASTS
            and node.args
        ):
            continue
        argument = node.args[0]
        read, line = _env_read_within(argument), node.lineno
        if read is None and isinstance(argument, ast.Name) and argument.id in from_var:
            read, line = from_var[argument.id]
        if read is not None:
            found.setdefault(read, line)
    for node in ast.walk(tree):
        bound = _bound_helper_variable(node)
        if bound is not None:
            found.setdefault(bound[0], bound[1])
    return found


class _Declared(NamedTuple):
    """A class statement and the module label it was declared in."""

    label: str
    node: ast.ClassDef


class _SettingsBound(NamedTuple):
    """One environment name a `pydantic-settings` class reads a number from."""

    where: str
    case_sensitive: bool


# The `SettingsConfigDict` keys the derivation reads, and what each is when a class sets none.
_SETTINGS_CONFIG_DEFAULTS: dict[str, object] = {
    "env_prefix": "",
    "env_nested_delimiter": None,
    "case_sensitive": False,
}


def _base_name(base: ast.expr) -> str:
    """The bare name a base-class expression names — `BaseSettings`, `x.BaseSettings`, `G[T]`."""
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    if isinstance(base, ast.Attribute):
        return base.attr
    return base.id if isinstance(base, ast.Name) else ""


def _resolve_class(name: str, label: str, index: dict[str, list[_Declared]]) -> _Declared | None:
    """The first-party class `name` means from inside `label`, or `None` for a library class.

    The same module first, then the whole tree when exactly one class has the name. **Two
    candidates is refused rather than guessed**: the parent is where an inherited `env_prefix` comes
    from, so picking the wrong one would derive a set of names nothing reads — the defect the
    `validation_alias` arm below exists for, reached a different way.
    """
    candidates = index.get(name, [])
    local = [declared for declared in candidates if declared.label == label]
    if len(local) == 1:
        return local[0]
    if len(candidates) == 1:
        return candidates[0]
    assert not candidates, (
        f"{label}: `{name}` is declared in {sorted(d.label for d in candidates)}, and the bound "
        "derivation cannot tell which one a class there inherits from. Rename one of them, or "
        "teach `_resolve_class` the import"
    )
    return None


def _lineage(declared: _Declared, index: dict[str, list[_Declared]]) -> list[_Declared]:
    """`declared` and every first-party ancestor, root first, so a subclass's statements win."""
    ordered: list[_Declared] = []
    for base in declared.node.bases:
        parent = _resolve_class(_base_name(base), declared.label, index)
        if parent is not None and parent.node is not declared.node:
            ordered.extend(a for a in _lineage(parent, index) if a not in ordered)
    ordered.append(declared)
    return ordered


def _derives_from(lineage: list[_Declared], root: str) -> bool:
    """Whether any class in `lineage` names the library class `root` among its bases."""
    return any(_base_name(base) == root for declared in lineage for base in declared.node.bases)


def _settings_config(lineage: list[_Declared]) -> dict[str, object]:
    """The `SettingsConfigDict` keys this derivation reads, merged down `lineage` as pydantic does.

    **Inherited, which is the first of the three shapes the derivation used to miss.** pydantic
    merges `model_config` along the class hierarchy, so a subclass of a settings class that sets
    `env_prefix` reads its fields under the *parent's* prefix — and this read only the class's own
    body, so it derived `MAX_RUNS` where the environment reads `CHEMCLAW_MAX_RUNS`. A value it
    cannot read as a literal is refused rather than skipped, because a skipped prefix is a wrong
    name rather than a missing one.
    """
    config = dict(_SETTINGS_CONFIG_DEFAULTS)
    for declared in lineage:
        for statement in declared.node.body:
            if not (
                isinstance(statement, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "model_config"
                    for target in statement.targets
                )
            ):
                continue
            value = statement.value
            pairs: list[tuple[str | None, ast.expr]]
            if isinstance(value, ast.Call):
                pairs = [(keyword.arg, keyword.value) for keyword in value.keywords]
            elif isinstance(value, ast.Dict):
                pairs = [
                    (
                        key.value
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                        else None,
                        item,
                    )
                    for key, item in zip(value.keys, value.values, strict=True)
                    if key is not None
                ]
            else:
                raise AssertionError(
                    f"{declared.label}:{statement.lineno}: `model_config` is not a literal "
                    "`SettingsConfigDict(...)` or dict, so the bound derivation cannot read its "
                    "env_prefix"
                )
            for key, item in pairs:
                assert key is not None, (
                    f"{declared.label}:{statement.lineno}: `model_config` unpacks a mapping, so "
                    "the bound derivation cannot read its env_prefix"
                )
                if key not in config:
                    continue
                assert isinstance(item, ast.Constant), (
                    f"{declared.label}:{statement.lineno}: `{key}` is not a literal, so the bound "
                    "derivation cannot say which environment names this class reads"
                )
                config[key] = item.value
    return config


def _field_aliases(value: ast.expr | None, where: str) -> list[str] | None:
    """The environment names a `Field(validation_alias=...)` or `Field(alias=...)` reads, if any.

    **The third shape, and the one worse than absent**: pydantic-settings reads an aliased field
    from the alias *as written*, with no `env_prefix` — so deriving the prefixed field name
    reported a variable nothing reads, and a ratchet built on it would refuse the harmless name and
    wave the real one through. `AliasChoices` of literals is every one of them; an `AliasPath` or a
    computed alias is refused, because this cannot name what it reads.
    """
    if not (isinstance(value, ast.Call) and _called_name(value) == "Field"):
        return None
    chosen = {keyword.arg: keyword.value for keyword in value.keywords}
    alias = chosen.get("validation_alias", chosen.get("alias"))
    if alias is None:
        return None
    if isinstance(alias, ast.Constant) and isinstance(alias.value, str):
        return [alias.value]
    if (
        isinstance(alias, ast.Call)
        and _called_name(alias) == "AliasChoices"
        and alias.args
        and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in alias.args)
    ):
        return [str(a.value) for a in alias.args if isinstance(a, ast.Constant)]
    raise AssertionError(
        f"{where}: a field alias the bound derivation cannot read as a literal name "
        f"({ast.unparse(alias)}); it would derive a variable nothing reads"
    )


def _annotation_class(annotation: ast.expr) -> str | None:
    """The one class an annotation is about — `M`, `M | None`, `Annotated[M, ...]` — or None."""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        names = {
            n
            for n in (_annotation_class(annotation.left), _annotation_class(annotation.right))
            if n
        }
        names.discard("None")
        return names.pop() if len(names) == 1 else None
    if isinstance(annotation, ast.Subscript) and _annotation_name(annotation.value) == "Annotated":
        inner = annotation.slice
        first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return _annotation_class(first)
    return None


def _model_numbers(
    declared: _Declared,
    index: dict[str, list[_Declared]],
    prefix: str,
    delimiter: str | None,
    seen: frozenset[int] = frozenset(),
) -> dict[str, str]:
    """Every environment name a settings class — or a model nested in one — reads a number from.

    **Nested models are the second shape.** A field annotated with a first-party `BaseModel` is one
    environment variable carrying JSON — which moves every number inside it — and, where the
    settings class sets `env_nested_delimiter`, one variable per nested field as well. Both are
    derived; before this the field was skipped as "not numeric" and its numbers were invisible.
    """
    fields: dict[str, tuple[str, ast.AnnAssign]] = {}
    for ancestor in _lineage(declared, index):
        for statement in ancestor.node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                head = statement.annotation
                if isinstance(head, ast.Subscript):
                    head = head.value
                if _annotation_name(head) == "ClassVar":
                    continue
                fields[statement.target.id] = (ancestor.label, statement)
    found: dict[str, str] = {}
    for field, (label, statement) in fields.items():
        where = f"{label}:{statement.lineno}"
        aliases = _field_aliases(statement.value, where)
        names = aliases if aliases is not None else [prefix + field]
        if _annotation_is_numeric(statement.annotation):
            found.update(dict.fromkeys(names, where))
            continue
        nested_name = _annotation_class(statement.annotation)
        nested = _resolve_class(nested_name, label, index) if nested_name else None
        if nested is None or id(nested.node) in seen:
            continue
        lineage = _lineage(nested, index)
        if not _derives_from(lineage, "BaseModel") or _derives_from(lineage, "BaseSettings"):
            continue
        inner_seen = seen | {id(nested.node)}
        whole = _model_numbers(nested, index, "", None, inner_seen)
        if not whole:
            continue
        found.update(dict.fromkeys(names, where))
        if delimiter:
            assert aliases is None, (
                f"{where}: an aliased nested model under `env_nested_delimiter`, which the bound "
                "derivation does not follow"
            )
            found.update(
                _model_numbers(nested, index, f"{prefix}{field}{delimiter}", delimiter, inner_seen)
            )
    return found


def _numeric_settings_fields(modules: dict[str, ast.Module]) -> dict[str, _SettingsBound]:
    """Every numeric `pydantic-settings` field across `modules`, as its environment name."""
    index: dict[str, list[_Declared]] = {}
    for label, tree in modules.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                index.setdefault(node.name, []).append(_Declared(label, node))
    found: dict[str, _SettingsBound] = {}
    for declarations in index.values():
        for declared in declarations:
            lineage = _lineage(declared, index)
            if not _derives_from(lineage, "BaseSettings"):
                continue
            config = _settings_config(lineage)
            sensitive = bool(config["case_sensitive"])
            delimiter = config["env_nested_delimiter"]
            numbers = _model_numbers(
                declared,
                index,
                str(config["env_prefix"]),
                str(delimiter) if delimiter else None,
            )
            for name, where in numbers.items():
                found[name if sensitive else name.upper()] = _SettingsBound(where, sensitive)
    return found


def _annotation_is_numeric(annotation: ast.expr) -> bool:
    """Whether `annotation` is `int`, `float`, one of those unioned with `None`, or wrapped.

    `Annotated[int, Field(ge=1)]` is the spelling a field grows the moment somebody wants a
    constraint on it, and it was invisible here until 2026-09-12 — so a bound could have left the
    ratchet's set by acquiring a validator. No field in the tree is written that way today;
    `Optional[int]` is covered as the `|` form only, which is the form this repository writes.
    """
    if isinstance(annotation, ast.Name):
        return annotation.id in _NUMERIC_CASTS
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_numeric(annotation.left) or _annotation_is_numeric(annotation.right)
    if isinstance(annotation, ast.Subscript) and _annotation_name(annotation.value) == "Annotated":
        inner = annotation.slice
        first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return _annotation_is_numeric(first)
    return False


def _annotation_name(node: ast.expr) -> str:
    """The bare name of an annotation's head — `Annotated` and `typing.Annotated` read the same."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


class Bound(NamedTuple):
    """A number a deployment can move: where the code reads it, and how its name is matched."""

    where: str
    case_sensitive: bool


def numeric_env_bounds() -> dict[str, Bound]:
    """Every environment variable first-party code turns into a number, and where it is read."""
    found: dict[str, Bound] = {}
    roots = sorted(ROOT.glob("packages/*/src")) + sorted(ROOT.glob("servers/*/src"))
    assert roots, "no first-party source roots found; has the layout changed?"
    modules: dict[str, ast.Module] = {}
    for root in roots:
        for source in sorted(root.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            where = str(source.relative_to(ROOT))
            modules[where] = tree
            for name, line in _numeric_environ_reads(tree).items():
                found[name] = Bound(f"{where}:{line}", case_sensitive=True)
    for name, field in _numeric_settings_fields(modules).items():
        found[name] = Bound(field.where, case_sensitive=field.case_sensitive)
    return found


def _matching_bound(name: str, bounds: dict[str, Bound]) -> tuple[str, Bound] | None:
    """The bound a shipped spelling of `name` moves, under that bound's own matching rule.

    A lowercase `ENV chemclaw_calc_max_concurrent_requests=99` moved `servers/calc`'s admission
    ceiling to 99 with both ratchets silent, because the derivation uppercases a settings field's
    name and matching was `name in bounds`.
    """
    exact = bounds.get(name)
    if exact is not None:
        return name, exact
    canonical = name.upper()
    insensitive = bounds.get(canonical)
    if insensitive is not None and not insensitive.case_sensitive:
        return canonical, insensitive
    return None


def _bound_offences(label: str, text: str, bounds: dict[str, Bound]) -> list[str]:
    """Every numeric setting one shipped file moves without an argued row."""
    offences: list[str] = []
    if label.endswith(".yaml") and "envFrom" in text:
        offences.append(f"{label}: uses envFrom, which can carry a resource bound unseen")
    for name, value in _env_settings(label, text):
        matched = _matching_bound(name, bounds)
        if matched is None:
            continue
        canonical, bound = matched
        if (label, canonical) in _ARGUED_DEPLOYMENT_SETTINGS:
            continue
        spelling = (
            ""
            if name == canonical
            else f" (written as {name}, which a case-insensitive settings field honours)"
        )
        offences.append(
            f"{label}: sets {canonical}={_shown(value)}{spelling}, which {bound.where} reads as a "
            "number"
        )
    return offences


def test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment() -> None:
    """Nothing in `deploy/` or a Containerfile moves a number first-party code reads as a bound."""
    bounds = numeric_env_bounds()
    assert set(bounds) >= _BOUND_ANCHORS, (
        f"the bound scan lost {sorted(_BOUND_ANCHORS - set(bounds))!r}; a derivation that stops "
        "finding variables agrees with an empty tree forever"
    )
    shipped = shipped_deployment_files()
    assert shipped, "no deployment manifests found; has the layout changed?"
    offences = [
        offence
        for manifest in shipped
        for offence in _bound_offences(
            str(manifest.relative_to(ROOT)), manifest.read_text(encoding="utf-8"), bounds
        )
    ]
    assert not offences, (
        "a bound is moved in a shipped file with no argued row in "
        "`_ARGUED_DEPLOYMENT_SETTINGS`:\n  " + "\n  ".join(offences)
    )


def test_the_bound_check_bites() -> None:
    """The tree is clean, so the check is shown failing on purpose — in both file shapes.

    Including the continuation `ENV`, because that is the shape the sibling egress ratchet was blind
    to for two of seven servers, and this check reads the same files through the same parser.
    """
    bounds = {
        "CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS": Bound(
            "servers/chem/x.py:1", case_sensitive=False
        )
    }
    assert _bound_offences(
        "servers/chem/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS\n          value: '64'\n",
        bounds,
    ) == [
        "servers/chem/deploy/deployment.yaml: sets CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS='64', "
        "which servers/chem/x.py:1 reads as a number"
    ]
    assert _bound_offences(
        "servers/chem/Containerfile",
        "ENV PYTHONUNBUFFERED=1 \\\n    CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS=64\n",
        bounds,
    ) == [
        "servers/chem/Containerfile: sets CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS='64', "
        "which servers/chem/x.py:1 reads as a number"
    ]
    # A setting nothing reads as a number is not this check's business.
    assert _bound_offences("servers/chem/Containerfile", "ENV HF_HUB_OFFLINE=1\n", bounds) == []
    # An argued row is the one way through, and it is a *pair*: the real row exempts
    # `CHEMCLAW_CREST_THREADS` in calc's Containerfile and nowhere else, so the same variable set
    # from another file is still an offence.
    threads = {
        "CHEMCLAW_CREST_THREADS": Bound("servers/calc/.../config.py:215", case_sensitive=False)
    }
    assert (
        _bound_offences("servers/calc/Containerfile", "ENV CHEMCLAW_CREST_THREADS=4\n", threads)
        == []
    )
    assert _bound_offences(
        "servers/chem/Containerfile", "ENV CHEMCLAW_CREST_THREADS=4\n", threads
    ) == [
        "servers/chem/Containerfile: sets CHEMCLAW_CREST_THREADS='4', which "
        "servers/calc/.../config.py:215 reads as a number"
    ]
    # A ConfigMap this repository does not hold can carry any of them.
    assert _bound_offences(
        "servers/chem/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      envFrom:\n"
        "        - configMapRef:\n            name: tuning\n",
        bounds,
    ) == [
        "servers/chem/deploy/deployment.yaml: uses envFrom, which can carry a resource bound unseen"
    ]


def test_no_spelling_that_moved_a_bound_past_this_ratchet_reads_as_clean() -> None:
    """The lowercase spelling, and the two hiding places, as this ratchet's own data."""
    ceiling = {
        "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS": Bound(
            "servers/calc/.../config.py:194", case_sensitive=False
        ),
        "MCP_MAX_SMILES_CHARS": Bound("packages/.../limits.py:40", case_sensitive=True),
    }
    assert _bound_offences(
        "servers/calc/Containerfile", "ENV chemclaw_calc_max_concurrent_requests=99\n", ceiling
    ) == [
        "servers/calc/Containerfile: sets CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS='99' (written as "
        "chemclaw_calc_max_concurrent_requests, which a case-insensitive settings field honours), "
        "which servers/calc/.../config.py:194 reads as a number"
    ]
    assert (
        _bound_offences("servers/chem/Containerfile", "ENV mcp_max_smiles_chars=7\n", ceiling) == []
    )
    assert _bound_offences(
        "servers/chem/Containerfile", "ENV MCP_MAX_SMILES_CHARS=7\n", ceiling
    ) == [
        "servers/chem/Containerfile: sets MCP_MAX_SMILES_CHARS='7', which "
        "packages/.../limits.py:40 reads as a number"
    ]

    # The *derived* set carries the same distinction, which the fixtures above cannot show: a
    # mutation flipping every bound to case-insensitive left this test green until these two lines
    # existed, because a hand-built fixture asserts the matching rule and not the derivation.
    derived = numeric_env_bounds()
    # Named rather than indexed, because the two assertions below are about a bound's *case* rule
    # and a bare `KeyError` from the subscript would say only that a dict lacked a key — in a file
    # whose whole standard is that a failure names what broke. A bound disappearing from the
    # derivation is the more serious of the two failures and has to read as the more serious one.
    for variable in ("MCP_MAX_SMILES_CHARS", "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS"):
        assert variable in derived, (
            f"{variable} is no longer in `numeric_env_bounds()`, so the ratchet that stops a "
            f"Containerfile or a ConfigMap setting it outside the bound no longer covers it at "
            f"all — which is a wider failure than the case rule these two lines assert"
        )
    assert derived["MCP_MAX_SMILES_CHARS"].case_sensitive, (
        "an `os.environ` read is case-sensitive; marking it otherwise makes the ratchet flag an "
        "`ENV` that changes nothing"
    )
    assert not derived["CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS"].case_sensitive, (
        "a `pydantic-settings` field is honoured in any case; marking it sensitive puts the "
        "lowercase spelling of an admission ceiling back outside the ratchet"
    )

    # The same two hiding places the egress ratchet had, over the same parser: a bound pulled from a
    # ConfigMap, and a bound assigned in the container's own command line.
    assert _bound_offences(
        "servers/calc/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n      env:\n"
        "        - name: CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS\n          valueFrom:\n"
        "            configMapKeyRef: {name: tuning, key: ceiling}\n",
        ceiling,
    ) == [
        "servers/calc/deploy/deployment.yaml: sets CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS=a value "
        "this file does not hold, which servers/calc/.../config.py:194 reads as a number"
    ]
    assert _bound_offences(
        "servers/calc/deploy/deployment.yaml",
        "spec:\n  containers:\n    - name: server\n"
        '      command: ["sh", "-c", "MCP_MAX_SMILES_CHARS=9 exec uvicorn app"]\n',
        ceiling,
    ) == [
        "servers/calc/deploy/deployment.yaml: sets MCP_MAX_SMILES_CHARS='9', which "
        "packages/.../limits.py:40 reads as a number"
    ]


_MEMORY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "K": 10**3, "M": 10**6, "G": 10**9}


def _memory_bytes(quantity: str) -> int:
    """A Kubernetes memory quantity as bytes — `512Mi`, `3Gi`, or a bare byte count."""
    for suffix, factor in _MEMORY_SUFFIXES.items():
        if quantity.endswith(suffix):
            return int(float(quantity[: -len(suffix)]) * factor)
    return int(quantity)


def test_the_session_ceiling_is_derived_from_the_smallest_pod_this_fleet_actually_ships() -> None:
    """`mcp_server_kit` bounds sessions against a memory limit it cannot see; this sees it."""
    limits = {}
    for deployment in sorted(SERVERS.glob("*/deploy/deployment.yaml")):
        manifest = yaml.safe_load(deployment.read_text(encoding="utf-8"))
        for container in manifest["spec"]["template"]["spec"]["containers"]:
            memory = container.get("resources", {}).get("limits", {}).get("memory")
            if memory is not None:
                limits[f"{deployment.parent.parent.name}/{container['name']}"] = _memory_bytes(
                    str(memory)
                )
    assert limits, "no shipped Deployment declares a memory limit; has the layout changed?"

    smallest = min(limits.values())
    assert smallest == SMALLEST_POD_MEMORY_LIMIT_BYTES, (
        f"the smallest shipped memory limit is now {smallest} B "
        f"({min(limits, key=lambda name: limits[name])}), and "
        "`mcp_server_kit.sessions.SMALLEST_POD_MEMORY_LIMIT_BYTES` still says "
        f"{SMALLEST_POD_MEMORY_LIMIT_BYTES} B. The fleet-wide session ceiling is derived from that "
        "number, so it has to be re-derived — the paragraph beside it in `sessions.py` is the "
        "argument, not just the value."
    )
    assert DEFAULT_MAX_SESSIONS * SESSION_COST_BYTES <= SESSION_BACKLOG_BUDGET_BYTES <= smallest


def test_the_derivation_reads_the_two_spellings_it_used_to_miss() -> None:
    """`os.getenv` and `Annotated[int, …]`, neither of which exists in `src/` today.

    That is the point: a derivation is a claim about shapes rather than about this tree, and both of
    these would have entered it as an ordinary line of code with the ratchet silent. Measured on
    2026-09-12 before the fix, each of these modules contributed **nothing** to the bound set.

    The shapes still outside it are named in `numeric_env_bounds`' docstring and queued with an
    anchor, rather than left for the next reviewer to discover by writing one.
    """
    getenv = ast.parse('import os\n\nLIMIT = int(os.getenv("MCP_MAX_THINGS", "4"))\n')
    assert set(_numeric_environ_reads(getenv)) == {"MCP_MAX_THINGS"}
    bare = ast.parse('from os import getenv\n\nLIMIT = float(getenv("MCP_MAX_SECONDS", "1.5"))\n')
    assert set(_numeric_environ_reads(bare)) == {"MCP_MAX_SECONDS"}

    annotated = ast.parse(
        "from typing import Annotated\n\n"
        "class S(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_")\n'
        "    max_runs: Annotated[int, Field(ge=1)] = 4\n"
    )
    assert set(_numeric_settings_fields({"m.py": annotated})) == {"CHEMCLAW_MAX_RUNS"}

    # And the boundary, asserted so the docstring naming it cannot quietly become false. It moved
    # on 2026-09-16 and is now a *pair*: one named helper is followed, every other is not.
    known = ast.parse(
        'LIMIT = env_bound("MCP_MAX_THINGS", default=4, minimum=1, consequence="x")\n'
    )
    assert set(_numeric_environ_reads(known)) == {"MCP_MAX_THINGS"}
    helper = ast.parse('LIMIT = _env_int("MCP_MAX_THINGS", 4)\n')
    assert _numeric_environ_reads(helper) == {}


def _settings_names(**sources: str) -> dict[str, _SettingsBound]:
    """`_numeric_settings_fields` over synthetic modules, keyed by the label each is given."""
    return _numeric_settings_fields(
        {f"{label}.py": ast.parse(source) for label, source in sources.items()}
    )


def test_the_derivation_follows_inheritance_nesting_and_aliases() -> None:
    """The three shapes `numeric_env_bounds` used to name as invisible, each one driven.

    Measured on 2026-09-12 against synthetic modules and queued since: none exists in `src/` today,
    and each would have entered it as an ordinary line. Every assertion below was red against the
    one-module derivation this replaced — the parent's prefix was lost, the nested model's numbers
    were skipped as "not numeric", and the aliased field came back under `CHEMCLAW_MAX_RUNS`, a
    name pydantic-settings does not read at all.
    """
    # 1. The `env_prefix` is on the parent, and the parent is in another module.
    inherited = _settings_names(
        base=(
            "class ChemclawSettings(BaseSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_")\n'
            "    max_batch: int = 8\n"
        ),
        child="class RunSettings(ChemclawSettings):\n    max_runs: int = 4\n",
    )
    assert set(inherited) == {"CHEMCLAW_MAX_BATCH", "CHEMCLAW_MAX_RUNS"}
    assert inherited["CHEMCLAW_MAX_RUNS"].where == "child.py:2"
    overridden = _settings_names(
        base=(
            "class ChemclawSettings(BaseSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_")\n'
        ),
        child=(
            "class RunSettings(ChemclawSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_RUN_")\n'
            "    max_runs: int = 4\n"
        ),
    )
    assert set(overridden) == {"CHEMCLAW_RUN_MAX_RUNS"}

    # 2. A nested model: one JSON variable always, one per field under a delimiter.
    nested = (
        "class Limits(BaseModel):\n    max_runs: int = 4\n    label: str = 'x'\n\n"
        "class S(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_"{delimiter})\n'
        "    limits: Limits = Limits()\n"
    )
    assert set(_settings_names(m=nested.format(delimiter=""))) == {"CHEMCLAW_LIMITS"}
    assert set(_settings_names(m=nested.format(delimiter=', env_nested_delimiter="__"'))) == {
        "CHEMCLAW_LIMITS",
        "CHEMCLAW_LIMITS__MAX_RUNS",
    }
    wordy = "class Tags(BaseModel):\n    label: str = 'x'\n\nclass S(BaseSettings):\n    t: Tags\n"
    assert _settings_names(m=wordy) == {}, "a nested model with no number in it is not a bound"

    # 3. An alias is the name the environment reads, prefix or no prefix.
    aliased = _settings_names(
        m=(
            "class S(BaseSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="CHEMCLAW_")\n'
            '    max_runs: int = Field(4, validation_alias="REAL_NAME")\n'
            '    max_jobs: int = Field(2, validation_alias=AliasChoices("JOBS", "OLD_JOBS"))\n'
        )
    )
    assert set(aliased) == {"REAL_NAME", "JOBS", "OLD_JOBS"}
    assert "CHEMCLAW_MAX_RUNS" not in aliased, (
        "the one shape worse than absent: a name nothing reads"
    )

    # `case_sensitive=True` is read too, since it changes which spelling a shipped file must match.
    sensitive = _settings_names(
        m=(
            "class S(BaseSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="chemclaw_", case_sensitive=True)\n'
            "    max_runs: int = 4\n"
        )
    )
    assert sensitive == {"chemclaw_max_runs": _SettingsBound("m.py:3", True)}


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "a path alias",
            "class S(BaseSettings):\n"
            '    n: int = Field(4, validation_alias=AliasPath("limits", 0))\n',
        ),
        (
            "a computed prefix",
            "class S(BaseSettings):\n    model_config = SettingsConfigDict(env_prefix=PREFIX)\n",
        ),
        (
            "an unpacked config",
            "class S(BaseSettings):\n    model_config = SettingsConfigDict(**SHARED)\n",
        ),
    ],
)
def test_a_settings_shape_the_derivation_cannot_read_fails_loudly(label: str, source: str) -> None:
    """Where a shape cannot be followed cleanly, the suite goes red rather than deriving a guess.

    Each of these would otherwise produce a *wrong* name — the field's own, where the environment
    reads something else — and a wrong name is the failure the alias arm above is about.
    """
    with pytest.raises(AssertionError, match="bound derivation"):
        _settings_names(m=source)


def test_an_ambiguous_parent_fails_loudly_rather_than_picking_a_prefix() -> None:
    """Two first-party classes with the parent's name, and the child in neither module."""
    parent = (
        "class Base(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="{}")\n'
        "    n: int = 1\n"
    )
    with pytest.raises(AssertionError, match="cannot tell which one"):
        _settings_names(
            a=parent.format("CHEMCLAW_A_"),
            b=parent.format("CHEMCLAW_B_"),
            c="class Child(Base):\n    m: int = 2\n",
        )


def test_the_bound_scan_sees_both_configuration_mechanisms() -> None:
    """A scan that knew only `os.environ` would find `servers/calc`'s whole config absent."""
    bounds = numeric_env_bounds()
    environ_read = {
        name
        for name, bound in bounds.items()
        if bound.where.startswith(("packages/", "servers/chem/", "servers/pyexec/"))
    }
    assert "MCP_MAX_SMILES_CHARS" in environ_read
    through_helper = {
        "CHEMCLAW_CHEM_MAX_DEPICTION_CHARS",
        "CHEMCLAW_CHEM_RENDER_SIZE_PX",
        "CHEMCLAW_SAFETY_MAX_COMPONENTS",
        "MCP_MAX_MOLECULE_ATOMS",
        "MCP_MAX_SMILES_CHARS",
    }
    assert through_helper <= set(bounds), (
        f"the scan lost {sorted(through_helper - set(bounds))!r}, which are read through "
        "`env_bound`; a derivation that stops following the helper this fleet reads its bounds "
        "with covers the shape nothing here uses and misses the one it does"
    )
    calc = {name for name, bound in bounds.items() if bound.where.startswith("servers/calc/")}
    assert "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS" in calc, (
        "calc's admission ceiling is a settings field, not a constant; if the scan cannot see it "
        "the ratchet does not cover the one server whose calls take minutes"
    )
    assert len(calc) > 20, (
        f"the settings mechanism contributes {len(calc)} of calc's numbers; a collapse here is a "
        "ratchet that has quietly stopped covering the server with the most to move"
    )


# The names a module reports to `/healthz` by writing them down, rather than by reading them through
# `env_bound`/`env_ratio` (which report themselves) or by handing a whole settings object to
# `report_settings`. Named here, not imported, for the reason `_BOUND_HELPERS` is: the scan reads
# source, and a rename of either reporter has to fail this file rather than shrink what it sees.
_BOUND_REPORTERS = frozenset({"report_bound"})


_SETTINGS_REPORTERS = frozenset({"report_settings"})


def _unreported_bounds(modules: dict[str, ast.Module], bounds: dict[str, Bound]) -> list[str]:
    """Every bound in `bounds` that no code path in `modules` reports on `/healthz`."""
    through_helper: set[str] = set()
    written_down: set[str] = set()
    settings_modules: set[str] = set()
    for label, tree in modules.items():
        for node in ast.walk(tree):
            helper = _bound_helper_variable(node)
            if helper is not None:
                through_helper.add(helper[0])
            if not isinstance(node, ast.Call):
                continue
            called = _called_name(node)
            if called in _SETTINGS_REPORTERS:
                settings_modules.add(label)
            if called in _BOUND_REPORTERS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    written_down.add(first.value)
    settings_fields = _numeric_settings_fields(modules)
    offences = []
    for name, bound in sorted(bounds.items()):
        module = bound.where.rsplit(":", 1)[0]
        from_settings = name in settings_fields and module in settings_modules
        if name in through_helper or name in written_down or from_settings:
            continue
        offences.append(
            f"{name} (read at {bound.where}) is a bound a deployment can move and `/healthz` never "
            "reports it: read it through `env_bound`/`env_ratio`, hand its settings object to "
            "`report_settings` in the module that declares it, or call "
            f'`report_bound("{name}", value)` where it is resolved'
        )
    return offences


def _first_party_modules() -> dict[str, ast.Module]:
    """Every first-party source module, parsed and keyed the way `numeric_env_bounds` keys it."""
    roots = sorted(ROOT.glob("packages/*/src")) + sorted(ROOT.glob("servers/*/src"))
    return {
        str(source.relative_to(ROOT)): ast.parse(
            source.read_text(encoding="utf-8"), filename=str(source)
        )
        for root in roots
        for source in sorted(root.rglob("*.py"))
    }


def test_every_bound_a_deployment_can_move_is_reported_on_the_probe() -> None:
    """The ratchets above read the shipped files; this makes the pod say what it actually runs.

    `test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment` holds every
    file this repository ships, and cannot see a bound moved by an overlay applied elsewhere, a
    Helm value in a deploying repository or an operator's `kubectl set env` — by construction, and
    for good. The serving side can: `connector_app`'s `/healthz` reports
    `mcp_server_kit.limits.effective_bounds()`, and this holds every bound in the derived inventory
    to reaching that record, so a new knob is observable from a probe the day it is added
    (`D-2026-09-26-a-pod-reports-the-bounds-it-is-running-with`).
    """
    offences = _unreported_bounds(_first_party_modules(), numeric_env_bounds())
    assert not offences, "\n".join(offences)


def test_the_reporting_check_bites() -> None:
    """A check that passes on everything is not a check, so it is shown failing on purpose.

    Four synthetic modules, one per shape: a bare `int(os.environ[...])` nobody reports (flagged),
    the same read with a literal `report_bound` (clean), a settings class whose module hands an
    instance to `report_settings` (clean) and one whose module does not (flagged).
    """
    source = {
        "a.py": 'import os\nX = int(os.environ["PROBE_BARE"])\n',
        "b.py": (
            "import os\nfrom mcp_server_kit.limits import report_bound\n"
            'Y = int(os.environ["PROBE_REPORTED"])\nreport_bound("PROBE_REPORTED", Y)\n'
        ),
        "c.py": (
            "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
            "from mcp_server_kit.limits import report_settings\n"
            "class S(BaseSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="PROBE_")\n'
            "    good: int = 1\n"
            "report_settings(S())\n"
        ),
        "d.py": (
            "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
            "class T(BaseSettings):\n"
            '    model_config = SettingsConfigDict(env_prefix="PROBE_")\n'
            "    silent: int = 1\n"
        ),
    }
    modules = {label: ast.parse(text) for label, text in source.items()}
    bounds = {
        "PROBE_BARE": Bound("a.py:2", case_sensitive=True),
        "PROBE_REPORTED": Bound("b.py:3", case_sensitive=True),
        "PROBE_GOOD": Bound("c.py:5", case_sensitive=False),
        "PROBE_SILENT": Bound("d.py:4", case_sensitive=False),
    }
    flagged = {line.split(" ", 1)[0] for line in _unreported_bounds(modules, bounds)}
    assert flagged == {"PROBE_BARE", "PROBE_SILENT"}


class BoundSite(NamedTuple):
    """One `env_bound` call in the tree: the variable, and the module whose import reads it.

    `module` is the dotted name, derived from the path under a `src/` root rather than transcribed,
    because the whole point of this collection is that nobody keeps a list of it by hand.
    """

    variable: str
    module: str
    where: str


def env_bound_sites() -> list[BoundSite]:
    """Every `mcp_server_kit.limits.env_bound` call in first-party source, derived from the tree.

    The first positional argument is the variable's name, which is the only shape this repository
    writes and the only one `_numeric_environ_reads` follows — so a site spelled any other way is
    absent from both this and the deployment ratchet, and that is one failure rather than two.
    """
    sites: list[BoundSite] = []
    for root in sorted(ROOT.glob("packages/*/src")) + sorted(ROOT.glob("servers/*/src")):
        for source in sorted(root.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                bound = _bound_helper_variable(node)
                if bound is None:
                    continue
                variable, line = bound
                sites.append(
                    BoundSite(
                        variable=variable,
                        module=str(source.relative_to(root).with_suffix("")).replace("/", "."),
                        where=f"{source.relative_to(ROOT)}:{line}",
                    )
                )
    return sites


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_every_environment_bound_refuses_at_import_and_names_its_own_variable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Each bound is driven against its own defect: set it to nothing, watch the import refuse."""
    sites = env_bound_sites()
    assert len(sites) >= 11, (
        f"only {len(sites)} `env_bound` call sites found; a derivation that stops finding them "
        "agrees with an empty tree forever"
    )
    for site in sites:
        module = importlib.import_module(site.module)
        monkeypatch.setenv(site.variable, value)
        with pytest.raises(ValueError) as refusal:
            reimported(module)
        message = str(refusal.value)
        assert site.variable in message, (
            f"{site.where}: {site.variable}={value} refused with a message that does not name the "
            f"variable — {message!r}. A CrashLoopBackOff plus a number whose source an operator "
            "has to guess is the failure this guard replaced."
        )
        assert value.lstrip("-") in message, (
            f"{site.where}: {site.variable}={value} refused without quoting the value seen — "
            f"{message!r}. An operator cannot tell a typo from a policy without it."
        )
        monkeypatch.delenv(site.variable)


def test_a_bound_at_its_own_floor_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, so the test above cannot be passed by refusing everything."""
    for site in env_bound_sites():
        module = importlib.import_module(site.module)
        monkeypatch.setenv(site.variable, str(_declared_minimum(site)))
        reimported(module)
        monkeypatch.delenv(site.variable)


def _declared_minimum(site: BoundSite) -> float:
    """The floor one call site declares, resolved through the module when it is a named constant."""
    source = ROOT / site.where.rsplit(":", 1)[0]
    line = int(site.where.rsplit(":", 1)[1])
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.lineno == line):
            continue
        for keyword in node.keywords:
            if keyword.arg != "minimum":
                continue
            if isinstance(keyword.value, ast.Constant):
                literal = keyword.value.value
                if isinstance(literal, int | float) and not isinstance(literal, bool):
                    return literal
            if isinstance(keyword.value, ast.Name):
                resolved = getattr(importlib.import_module(site.module), keyword.value.id)
                assert isinstance(resolved, int | float) and not isinstance(resolved, bool)
                return resolved
    raise AssertionError(f"{site.where}: no `minimum=` on this bound's call")
