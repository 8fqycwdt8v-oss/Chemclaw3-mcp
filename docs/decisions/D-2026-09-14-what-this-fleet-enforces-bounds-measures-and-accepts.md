# D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts — What this fleet enforces, bounds, measures and accepts

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** Wave 30.8, on top of `eb58363`.

## Context

This is the sign-off a deployment team reads before putting this fleet in front of chemists. It is
the document in this repository **most likely to be believed without checking**, which is the
reason for the one rule it is written under:

> **Every clause names the test that holds it.** A clause with no test is written down as an
> accepted risk, in the section for accepted risks, or it is not written down at all.

`tests/test_decision_log.py::test_every_test_a_record_names_still_exists` resolves every `test_*`
named below against the suite, so a rename cannot retire a citation here in silence. That is the
only reason this format is worth the ceremony: several of this repository's own records exist
because a sentence outran its commit — a port table publishing two taken ports as free, a docstring
naming a DNS guard that only covered `connect`, a Makefile comment calling image drift unaudited
in the commit that fixed it, a `BLE001` remedy unavailable on two of the shapes it was prescribed
for. Each was persuasive. None was true.

**Numbers here are dated measurements of a named commit, never claims about `HEAD`** — the same
rule `CLAUDE.md` and `docs/decisions/README.md` apply to themselves.

---

## 1 · Enforced — a request cannot get past this

### 1.1 No egress, four layers

Every server answers from data on disk. The claim is not "no server calls out"; it is that four
independent mechanisms refuse one, because a rule in one place rots.

| Layer | What it refuses | Held by |
| --- | --- | --- |
| Runtime guard, armed on import | a non-loopback `connect`/`connect_ex`, a UDP `sendto`/`sendmsg`, a forward or reverse DNS lookup — including a `bytes` or `bytearray` host, which walked past it in pure Python until it was measured | `test_a_remote_address_is_refused`, `test_connect_ex_is_guarded_too`, `test_a_dns_lookup_is_refused`, `test_a_reverse_lookup_is_refused`, `test_udp_payload_cannot_leave`, `test_udp_sendmsg_cannot_leave_either`, `test_a_bytes_host_is_refused_like_a_str_one`, `test_a_bytearray_host_is_refused_too` |
| Static scan (AST, not grep) | a networking import in any serving module, however spelled — `import httpx as h` and `from requests import get` read identically as a tree; a host split across string literals; `importlib.import_module` with a literal name | `test_no_module_can_reach_the_network`, `test_a_host_split_across_string_literals_is_still_a_host`, `test_a_dynamic_import_with_a_literal_name_is_flagged`, and one `test_no_egress` module per server |
| The suite itself, guard armed | a test that only passes by reaching the internet — which is what makes a vendored corpus *proven* sufficient rather than assumed | `test_the_guard_is_armed_by_default`, `conftest.py`'s autouse `egress_guard_is_armed` fixture |
| Default-deny `NetworkPolicy` | the pod's egress at the cluster, asserted in both directions (`Egress` in `policyTypes` **and** an empty `egress:`) | one `test_deploy` module per server; `test_no_shipped_deployment_widens_the_egress_allowlist`, `test_the_allowlist_check_bites`, `test_no_shape_that_hides_a_value_from_this_ratchet_reads_as_clean` (the four shapes three fresh-context reviewers walked that ratchet past — an `ARG`→`ENV` indirection that already ships, a leading space, a `valueFrom:` and a `command:` assignment) |

`MCP_EGRESS_ALLOW` is empty by default and empty in every shipped deployment
(`test_the_default_allowlist_is_empty`, `test_no_shipped_deployment_widens_the_egress_allowlist`),
and a deployment that widened it or shipped `MCP_EGRESS_GUARD=off` is visible from a Prometheus
scrape rather than from a docstring (`test_a_widened_allowlist_is_visible_from_a_scrape`,
`test_a_disabled_guard_still_publishes_the_widening_it_was_configured_with`,
`test_every_value_that_disarms_the_guard_is_in_the_set_the_ratchet_reads`).

**Four channels are outside the runtime guard by construction**, and which layer reaches which is
stated exactly, because this paragraph has been wrong in both directions before:

- a **child process** — no static reader can help, because `subprocess` is how `pyexec` and `calc`
  do their work;
- a **`ctypes` call into `libc`** — deliberately off the static scan's list, because `pyexec`'s
  sandbox needs it for `prctl(PR_SET_DUMPABLE, 0)`
  (`test_ctypes_is_outside_both_in_repo_layers_and_has_exactly_one_caller`);
- the private C type **`_socket.socket`**, which the guard's method rebinding does not reach
  (`test_the_private_c_socket_type_is_flagged` — layer 2 does refuse the import);
- any syscall from a **compiled extension**, whose reachable instance in this lockfile is `grpc`
  (`test_a_grpc_channel_is_flagged_however_it_is_spelled` — again layer 2, not layer 1).

`test_claude_md_and_the_guard_name_the_same_channels_as_outside_it` holds the prose and the guard
to the same list. What covers the two layer 2 cannot see is `make offline-run`, and **that target
is not in `make check`** — §4.1.

### 1.2 Bearer auth on `/mcp`, and fail-closed

Bearer is enforced on the MCP surface itself; `/healthz`, `/livez` and `/metrics` stay open,
because a kubelet probe and a Prometheus scrape have no identity
(`test_every_probe_route_is_open`, `test_a_probe_path_with_a_trailing_slash_is_not_refused`,
`test_the_open_paths_are_not_a_prefix_rule`).

The credential itself: refused anonymous, refused wrong, refused right-secret-wrong-scheme, served
correct, and **refused when the declared `token_env` is unset or holds only whitespace**
(`test_the_mcp_surface_refuses_without_a_token`, `test_a_wrong_token_is_refused`,
`test_the_right_token_is_served`, `test_a_missing_env_var_fails_closed`,
`test_a_non_ascii_header_is_refused_not_crashed`). Chemclaw3 once mounted a secret, recorded the
control as enabled, and served every tool to anything that could reach the pod, because the serving
side never checked.

**`connector_app` being shared is not the proof.** A mounted MCP surface is exactly the route an
enclosing app's declared credential does not reach, so every server drives its own running listener
through all seven arms, and a fleet test makes an eighth server owe the same proof:
`test_every_server_proves_its_bearer_check_against_a_running_server`. A manifest that names a
non-loopback address without a credential is refused outright
(`test_a_networked_manifest_carries_a_credential`).

The `X-Chemclaw-*` identity headers are **logged, never trusted** — authorization happened in
Chemclaw3 before the call was made — and their spellings are transcribed from the sender rather
than imported from this repository's own constants, because the two constants agreed with each
other and one was wrong about the sender for as long as it existed
(`test_each_constant_names_the_header_that_is_sent`,
`test_the_constants_are_exactly_the_headers_chemclaw3_stamps`,
`test_a_tool_body_reads_every_header_chemclaw3_sends`,
`test_two_concurrent_calls_on_one_session_each_read_their_own_caller`).

### 1.3 Readiness is not liveness, and only a permanent cause may unready a pod

`/healthz` is readiness: a server whose corpus, rule table or backend will not load answers 503
naming the reason and listing what it did verify (`test_readiness_failure_is_a_503_naming_the_reason`,
`test_readiness_success_names_the_verified_datasets`). Its body is redacted, because the route is
unauthenticated (`test_the_503_body_is_redacted_because_the_route_is_unauthenticated`).

**Only `degradation.PERMANENT_CAUSES` — an egress refusal and a generic failure — may produce an
unready answer.** A transient resource exhaustion is counted and left alone, and that rule is
`connector_app`'s rather than each callable's: stated per callable it was honoured in two places
across seven servers, so every other raise went out as an unconditional 503 and a pod could kill
itself over a passing memory spike (`test_only_a_permanent_cause_answers_unready`,
`test_every_cause_reaches_this_funnel`, `test_the_transient_verdict_is_counted_for_a_scrape`).

`/livez` is a **different route** and consults nothing: a readiness 503 sheds traffic and is undone
by the next passing probe, a liveness failure kills the container, and every Deployment here
pointed both at `/healthz` until it was measured — one missing checkpoint among eleven optional
`rxnpredict` predictors was a kill after ~90 s, and a restart cannot recreate a missing file
(`test_livez_answers_while_healthz_refuses`,
`test_liveness_and_readiness_do_not_share_a_route`).

Every server hands `connector_app` a readiness callable rather than leaving `/healthz` a constant
200 (`test_every_server_hands_connector_app_a_readiness_check`, added by this record's commit —
every server already did, and nothing said so).

### 1.4 The manifest contract

`connector.yaml` is the surface Chemclaw3 advertises, and it is checked against a **running**
server: every served tool declared, every declared tool served, every tool classified exactly once
as `read_only` or `state_changing`, and every argument name, type and requiredness equal to the
recorded `tool-surface.json` (`assert_manifest_matches`; `test_every_tool_is_classified_exactly_once`;
`test_manifest_surface` for the helper's own behaviour). Getting the classification wrong by
omission fails **open** at Chemclaw3's plan gate.

That check is now owed by every server rather than followed by convention
(`test_every_server_proves_its_manifest_against_a_running_server`, added by this record's commit).
The name is one string used four times and they must match
(`test_the_name_is_one_string_used_four_times`); a manifest is a symlink into the bucket it
declares, and a **backend** declares itself in a `mount:` key Chemclaw3's `extra="forbid"` model
refuses outright, which is what keeps `calc` and `rxnlabel` off `CHEMCLAW_CONNECTORS_DIR`
(`test_the_manifest_is_registered_by_symlink_in_the_bucket_it_declares`,
`test_a_backend_declares_itself_in_a_key_chemclaw3_refuses`,
`test_the_directory_the_export_line_names_holds_only_connectors`).

**Across the repository boundary**, the three manifests both trees ship (`chem`, `rxnpredict`,
`safety`) and this fleet's recorded `calc` surface are compared against the repository that
consumes them — from this side as well as that one, since the gate that catches a change has to be
the gate of the tree the change is made in
(`test_the_consumer_still_agrees_with_the_surface_this_tree_declares`). It is opt-in on a checkout
— §4.6.

### 1.5 No `assert` in serving code, no unclassified blind handler

`python -O` deletes every `assert`, so an invariant enforced by one is a control conditional on how
somebody started the process — and an `AssertionError` reaches the model as an opaque `error_id`
rather than as something it can act on. Two modules are exempt because their *product* is an
assertion failure (`test_no_serving_module_enforces_an_invariant_with_assert`,
`test_the_assert_scan_reads_a_tree_and_not_the_text`).

Every path that catches an exception and **answers with a component's contribution missing**
classifies it through `mcp_server_kit.degradation` and counts it; anything else carries a
`# noqa: BLE001` with its reason at the site, or is argued in an allowlist held against the tree in
both directions. The lint rule alone was not the control it was read as — measured, `BLE001` is
silent on a blind handler that logs with `logging.exception` and returns, and on one that re-raises
conditionally (`test_every_blind_handler_that_answers_anyway_is_argued`,
`test_the_argued_blind_handlers_are_still_there`,
`test_every_call_site_derives_its_cause_rather_than_writing_one`).

An unexpected exception never reaches the model verbatim: `ValueError` is the family for
deliberately worded, caller-safe messages and passes through; anything else is replaced and logged
with a short `error_id` in both halves (`test_an_internal_fault_is_still_replaced`,
`test_a_domain_refusal_still_passes_through`,
`test_a_caller_safe_message_is_redacted_before_the_model_sees_it`).

---

## 2 · Bounded — it can run away only this far

| Bound | Where | Held by |
| --- | --- | --- |
| **MCP sessions per pod.** A session is 57,900 bytes measured on the real `chem` app, growing with no ceiling at an arrival rate an idle timeout cannot see. `MCP_MAX_SESSIONS` defaults to 1,024 — derived from an eighth of the smallest pod memory limit this fleet actually ships, not chosen — and a full pod refuses the *handshake* with 503 + `Retry-After` rather than queueing | `mcp_server_kit/sessions.py` | `test_the_default_is_derived_from_the_smallest_pod_and_the_measured_session_cost`, `test_a_session_costs_about_what_the_ceiling_was_derived_from`, `test_a_full_pod_refuses_the_next_handshakes_promptly_and_counts_every_one`, `test_a_simultaneous_burst_against_an_empty_pod_cannot_admit_past_the_ceiling`, `test_the_session_ceiling_is_derived_from_the_smallest_pod_this_fleet_actually_ships` |
| **Concurrent work per pod**, in the five servers that own heavy tools: `CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS`, `CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS`, `CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS`, `CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES`, `CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS`. A full pod refuses promptly — a queued minute-long calculation returns after `request_timeout` has expired, computed at the expense of one somebody is still waiting for. The ceiling counts what the pod **spends**, not calls: CREST gets a scrubbed environment and four threads, so a call-counting four admitted sixteen runnable threads on a four-core pod | `servers/*/engine/admission.py` | one `test_admission` module per owning server, plus `test_capacity` (pyexec) |
| **Request body size**, declared or chunked, refused with 413 rather than a 500 | `connector_app` | `test_a_declared_oversize_body_is_refused`, `test_a_chunked_oversize_body_is_refused_with_413_and_not_a_500`, `test_a_body_under_the_cap_is_served_chunked_too` |
| **Input size**: `MCP_MAX_SMILES_CHARS`, `MCP_MAX_MOLECULE_ATOMS`, and per-server depiction and component bounds. A refusal names the subject and never echoes the megastring | `mcp_server_kit/limits.py` | `test_an_over_length_string_is_refused`, `test_the_refusal_never_echoes_the_megastring`, `test_an_over_large_molecule_is_refused`, `test_the_subject_is_named_in_the_message` |
| **Wall clock on a subprocess, killed by process group.** A per-tool-call timeout in the transport is not the control it looks like: every heavy tool offloads with `asyncio.to_thread`, and cancelling the coroutine does not stop the worker thread. `xtb_cli.run_isolated` gives the run its own session and kills the whole group, because `xtb` forks workers that `subprocess.run(timeout=…)` leaves orphaned, still burning CPU and still writing into a tempdir the caller has removed | `servers/calc` | `test_process_isolation`, `test_cost_bounds` |
| **The sandbox `pyexec` executes in**: module denylist including the `__import__` route, a filesystem jail proof against relative traversal, absolute paths, a planted symlink, a file descriptor, a `chdir` and a custom `opener` | `servers/pyexec` | `test_dangerous_modules_are_refused_by_name`, `test_the_dunder_import_route_is_refused_too`, `test_a_relative_traversal_out_of_the_jail_is_refused`, `test_a_symlink_planted_inside_the_jail_cannot_point_out_of_it`, `test_a_file_descriptor_is_refused_rather_than_resolved`, `test_open_does_not_accept_a_custom_opener` |
| **No shipped deployment may move any of these from outside the code.** A `deploy/*.yaml` `env:`, a Containerfile `ENV` in either casing, an `envFrom`, a `valueFrom:` and a `command:` assignment are all refused by a ratchet that reads the tree — and the ratchet is itself driven against shapes built to slip past it | `tests/test_fleet.py` | `test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`, `test_the_bound_check_bites`, `test_no_spelling_that_moved_a_bound_past_this_ratchet_reads_as_clean` (the lowercase spelling, measured moving `calc`'s ceiling for real, and the non-spelling that changes nothing) |
| **Deployment shape**: never one pod for a capability, replicas spread across nodes, a PodDisruptionBudget, bounded scratch space, and a termination grace period derived from the `request_timeout` the manifest declares | `servers/*/deploy` | `test_a_capability_is_never_one_pod`, `test_replicas_are_spread_across_nodes`, `test_a_voluntary_disruption_cannot_take_the_whole_capability`, `test_scratch_space_is_bounded`, `test_the_grace_period_is_derived_from_the_budget_the_manifest_declares` |

---

## 3 · Measured — an operator can see it from a scrape

`/metrics` is the default registry plus this fleet's own per-tool counters and latencies, and it
carries **nothing about a caller**: no actor, no session, no correlation id, no tool argument. A
tool *name* is none of those and is allowed on one condition — it is clamped to the served surface,
because the name in a `tools/call` is caller-supplied and an unclamped label mints a series per
string anything that can reach the pod invents. A destination host is not clampable and is
therefore not a label at all (`chemclaw_mcp_egress_refused_total` is bare). Asserted over the live
exposition in both directions — the forbidden words absent **and** the metrics present:
`test_metrics_is_open_and_carries_no_identity`, `test_an_unknown_tool_name_cannot_mint_a_metric_series`,
`test_the_egress_counter_is_unlabelled`, `test_metrics_publishes_what_a_tool_call_did`,
`test_metrics_counts_the_requests_it_refused`.

| Series | What it makes visible |
| --- | --- |
| `chemclaw_mcp_egress_refused_total` | a refusal that a library's own `except OSError: retry` would otherwise swallow whole — `EgressForbidden` subclasses `OSError`, so what a refusal looks like from outside depends on who catches it (`test_a_real_egress_refusal_is_not_sorted_as_a_generic_failure`) |
| `chemclaw_mcp_egress_guard_armed` | a deployment that shipped `MCP_EGRESS_GUARD=off` (`test_a_disabled_guard_still_publishes_the_widening_it_was_configured_with`) |
| `chemclaw_mcp_degraded_total{server,component,cause}` | an answer delivered with a component's contribution missing — `rxnpredict` used to gather an egress refusal into a *silently* degraded ensemble. Causes are clamped, components are clamped, and an unregistered one is refused rather than published (`test_recording_moves_the_series_an_operator_scrapes`, `test_an_unclamped_cause_is_refused_rather_than_published`, `test_an_unregistered_component_is_clamped_the_way_a_tool_name_is`, `test_classify_cannot_answer_outside_the_clamped_set`) |
| the transient-readiness counter | a resource exhaustion the probe deliberately did **not** act on (`test_the_transient_verdict_is_counted_for_a_scrape`) |
| session refusals | a pod at its handshake ceiling, reported once an interval rather than once a refusal (`test_a_pod_that_is_refusing_says_so_once_an_interval_and_not_once_a_refusal`, `test_the_refusals_status_and_retry_interval_are_what_a_client_is_told`) |
| the request log | the whole call rather than the SSE headers, which is the measurement that used to end at the first byte (`test_the_request_log_measures_the_whole_call_and_not_the_sse_headers`) |

**Tracing is inside the no-egress rule, not an exception to it.** `mcp_server_kit/tracing.py`
continues the W3C trace context Chemclaw3 sends and constructs no provider, no processor and no
exporter; without a configured SDK the tracer produces non-recording spans and no I/O at all,
`MCP_TRACING_ENABLED` is off by default, and a span carries the server and the tool and nothing
about the caller — not even a fault's text or stack
(`test_tracing_is_off_unless_a_deployment_turns_it_on`,
`test_the_span_names_the_server_and_the_tool_and_nothing_about_the_caller`,
`test_a_fault_puts_neither_its_text_nor_its_stack_on_the_span`,
`test_a_hostile_tool_name_cannot_become_a_span_name`). Exporting spans means a deployment
installing an SDK and opening a destination, which is a decision to argue for in exactly the way
`MCP_EGRESS_ALLOW` is.

### 3.1 The vendored-data contract

Every corpus ships a `dataset.json` with `name`, `version`, `licence`, `retrieved_from`,
`description` and `sha256`. All six are required and `load_dataset` refuses without them: a corpus
with no recorded licence is a legal question nobody can answer a year later, one with no checksum
cannot be shown to be what the review approved, and `retrieved_from` is the only record of where a
human obtained the file — **nothing reads it as an address**, and the guard would refuse if
anything did (`test_every_provenance_field_is_required`, `test_a_changed_file_is_refused`,
`test_a_missing_dataset_is_named`, `test_a_manifest_that_is_not_a_mapping_is_named`).

A corpus is also validated **against itself**, because the realistic failure is not a bad decision
but a transposed digit in a row nobody looks at again: CAS check digits, molecular weight against
formula, Antoine constants against the tabulated boiling point — independently written numbers that
must agree (one `test_dataset` module per server carrying a corpus). Where two servers hold the
same table, it is one file and the densities are compared
(`test_the_reagent_table_two_servers_carry_is_one_file`,
`test_the_two_tables_that_both_hold_densities_agree`). Every server's wheel is built and opened to
confirm it carries its own data (`test_every_server_builds_a_wheel_that_carries_its_data`).

### 3.2 Image, lock and what `--require-hashes` does and does not prove

Every Containerfile copies `uv.lock` and installs what `uv export --frozen` produces, with
`--require-hashes` (`test_every_image_installs_the_closure_the_audit_read`,
`test_the_image_install_check_reads_instructions_and_not_the_text`). Measured on `props` at the
commit that introduced it: the re-resolving form had shipped **11 of 37 packages the audit had
never seen**, `mcp` 1.29.0 → 1.30.0 among them; after, 0 version differences in both directions on
every server whose image was built.

**What that proves:** the bytes installed are the bytes `uv.lock` names, so an index that serves a
different artefact under the same version fails the build rather than shipping.

**What it does not prove**, stated plainly because this is the clause most likely to be read as
more than it is:

- not that the locked packages are free of known vulnerabilities — that is `make deps-audit`, with
  the suppressions in §4.4;
- not that the **base image** is audited; nothing here reads it;
- not that a package's own build step is safe;
- not that the running container installs the same thing as the file — see §4.5;
- and not `servers/rxnlabel`'s runtime stage, which installs `rxnmapper` and `rxn-insight` from
  PyPI's CPU-torch index outside the export. That pair is held to the lock **by version rather than
  by hash** (`test_an_image_that_installs_from_the_index_pins_what_the_audit_read`) and its
  transitive closure — torch included — re-resolves on every build. `docs/BACKLOG.md` carries it.

An image can name the build it is, and that revision reaches both the MCP handshake and the probe
(`test_the_image_can_name_the_build_it_is`, `test_the_revision_reaches_the_handshake_and_the_probe`).
The delivery pipeline verifies the **running image** rather than the source, reports digests rather
than tags, defaults to a dry run, has one answer to whether there is a registry, and **refuses a
publishing run with the gate off** — because Jenkins cannot see GitHub Actions, and claiming a
control in another system is not a control (`test_the_running_image_is_verified_rather_than_the_source`,
`test_the_publish_path_reports_digests_rather_than_tags`, `test_dry_run_is_the_default`,
`test_the_pipeline_has_one_answer_to_whether_there_is_a_registry`,
`test_a_publishing_run_cannot_skip_the_gate`,
`test_every_server_with_a_containerfile_can_be_addressed_by_the_pipeline`).

---

## 4 · Accepted — unbounded, unproven, or proven somewhere this repository cannot see

Each of these is a real gap. None has a test that would close it, which is why it is here and not
above.

### 4.1 Two of the four egress channels are covered only by a target `make check` does not run

The two are outside layer 2 for *different* reasons, and saying "both are off its list" would be the
kind of compression this record exists to avoid: a **child process** is not an import at all, so no
static reader can see it — and `subprocess` is how `pyexec` and `calc` do their work; a **`ctypes`**
call *is* an import and is off the list deliberately, because `pyexec`'s sandbox needs it for
`prctl(PR_SET_DUMPABLE, 0)`.

What covers both is `make offline-run`, which takes the network namespace away rather than asking
Python nicely. It needs `unshare`, so it runs as its own job in GitHub Actions
(`.github/workflows/ci.yml`'s `offline`) and its own stage in `Jenkinsfile`, and **a local
`make check` is green without it**. **Accepted:** a local gate does not exercise the two channels a
determined dependency would use. Queued in `docs/BACKLOG.md` §1 with the decision to take (fold it
in on detection, or make `make check` say which layer it did not run).

### 4.2 A dynamic import of a computed name is outside the static scan, and always will be

`importlib.import_module(name)` cannot be resolved by any static reader, and `rxnpredict` loads its
optional predictors exactly that way — so flagging the shape would fail correct code and teach the
next reader to reach for an exemption
(`test_a_dynamic_import_of_a_computed_name_is_deliberately_not_flagged` pins that this is a
decision rather than an oversight). **Accepted:** covered by the runtime guard for anything going
through Python, and by §4.1's target for anything that is not.

### 4.3 Nothing derives *which* servers need an admission ceiling

Five of seven have one. `props` and `safety` do not, and that is a judgement — a dict lookup and a
bisection, an RDKit substructure screen under a component bound — **not a derivation**. It cannot
be derived from the manifest: `read_only`/`state_changing` does not carry, and `render_structure`
is `read_only`, correctly, and is the one `chem` tool that needs a ceiling. **Accepted:** an eighth
server with a heavy tool and no ceiling would pass every test in this repository. The five that
have one are held by their own `test_admission` modules; the absence of a sixth is held by
nobody. Queued in `docs/BACKLOG.md` §2 with the smallest thing that would close it.

### 4.4 The supply-chain suppressions, and their expiry

`make deps-audit` does not fail on eight advisories, declared once in `pyproject.toml` with the
package and the version `uv.lock` resolved when the argument was written, and argued in the
`Makefile` beside the target that reads them. Every one turns on a **malicious artefact on disk** —
an unpickled cache, a crafted `config.json`, a checkpoint conversion — which is the class this
fleet's posture already answers, and none is reachable from a request. All eight live in the
optional ML extras of two servers and none appears in the closure without them.

They **expire**: `--ignore-vuln` matches by id, so a fixed dependency merely stops being reported
and a suppression outlives its reason in silence. When the lock moves one of these packages the
entry goes red and somebody re-derives it against the version that will actually ship; an entry
whose package the lock no longer resolves goes red too
(`test_a_suppression_names_a_package_the_lock_still_resolves`,
`test_a_suppression_expires_when_its_package_moves`,
`test_the_makefile_argues_exactly_the_suppressions_that_are_declared`).

**Accepted:** an unreachable advisory database is a *skip* locally and a **failure** in CI, because
a silent skip where the network is a given is a supply-chain hole that reads as a green build
forever. A local `make check` can therefore be green with the lockfile unaudited, and says so in
as many words when it happens.

### 4.5 What the bearer proof does not cover

`assert_bearer_is_enforced` runs **this repository's `app` object under this repository's uvicorn**
on loopback. A Containerfile that starts a different entrypoint, or an ingress in front of the pod,
is outside it. **Accepted:** the control is proven for the application; the deployment's own
composition is not this suite's to see.

Likewise, `test_every_server_hands_connector_app_a_readiness_check` says a probe is *wired*, not
that it is any good — three of seven were measured passing a component that builds and then fails
on every call, which is why the rule is that a probe runs the thing on a fixture. That property is
per callable and is held by each server's own readiness tests, not fleet-wide.

### 4.6 The cross-repository agreement runs nowhere automated

`test_the_consumer_still_agrees_with_the_surface_this_tree_declares` needs a `Chemclaw3` checkout
with a built `.venv`; CI here clones neither, and the consumer's mirror-image check has the same
problem. Without one it **skips**, and `conftest.py::pytest_terminal_summary` says so at the end of
the run, because a skip is not a pass. **Accepted**, with the row in `docs/BACKLOG.md` §4.

Two further limits of that arrangement, both queued rather than implied: this side runs the
consumer's module rather than reproducing it, so a check *deleted* there is silently deleted here;
and eight of the twenty `calc` tools are hardcoded in a third `Chemclaw3` module its own
`_CALLERS` tuple does not list — sound when measured on 2026-09-14, and watched by nothing.

### 4.7 `mypy --strict` does not read the test tree

`$(SRC)` lists eight `src/` roots and no test directory, so the files driving every ratchet in this
repository are not type-checked, and there is a pre-existing error waiting in
`servers/calc/tests/test_admission.py`. **Accepted**, queued in `docs/BACKLOG.md` §4 with the
second half that makes it more than a one-liner: `mypy --strict` over the test roots aborts on
`Duplicate module named "test_no_egress"`, because every server ships one.

### 4.8 A cluster operator's environment is invisible to every ratchet here

Both bound ratchets read the tree — `servers/*/deploy/*.yaml` and `servers/*/Containerfile`. A
bound moved by a kustomize overlay, a Helm value in a deploying repository, or `kubectl set env` is
invisible and always will be. **Accepted**, with the open question queued: whether the serving side
should *report* its live bounds on `/healthz` so the running value is observable from a probe
rather than inferred from an image.

### 4.9 A server hosted in another repository inherits the contract and not the kit

`retro` is not in this workspace, so it cannot inherit `mcp_server_kit`; what it owes is the same
contract checked the same way, and the one thing that cannot be read off its source is whether its
bearer dependency covers its *mounted* MCP surface. **Accepted:** nothing here can check it.
`docs/BACKLOG.md` §6 names the six things that must be true before it is consumed, and no manifest
for it exists yet.

### 4.10 No mirrored corpus has a named refresh owner or cadence

What a corpus *is* can be checked — `sha256`, in both directions. When it was last true of the
upstream cannot. A stale patent index nobody knows is stale is worse than none, and the same is
true of a hazard table. **Accepted**, queued in `docs/BACKLOG.md` §5.

---

## 5 · Controls this programme found wanting, and fixed

This section is the evidence the rest is real. Each was a control that existed, was documented, and
did not do what its documentation said.

| The claim | What was measured | What it is now |
| --- | --- | --- |
| "The egress guard refuses DNS" | it covered only `connect`; two of the three examples in its own docstring walked past it, and a `bytes` host walked past it in pure Python | lookups, reverse lookups, UDP and both host types, each with its own test |
| "Bearer is enforced, `connector_app` is shared" | a mount bypasses the enclosing app's dependencies; a shared helper is not a proof it was applied | every server drives its own listener through seven arms, and a fleet test makes an eighth owe it |
| "`/healthz` is readiness" | three of seven probes checked a component *constructed* or a version string *derivable*, and passed a component that fails on every call; and the permanence rule, stated per callable, was honoured in two places out of seven | a probe runs the thing on a fixture; `PERMANENT_CAUSES` is enforced in `connector_app` |
| both probes on `/healthz` | one missing checkpoint among eleven optional predictors killed the pod after ~90 s, and a restart could not recreate the file | `/livez` consults nothing; every Deployment held to both paths *and* their inequality |
| "a session is cheap" | 56.6 kB each, linear, with no ceiling and an arrival rate an idle timeout cannot see | `MCP_MAX_SESSIONS`, derived from the smallest pod, refused at the handshake |
| "one tool call is one thread" | `rxnpredict`'s consensus tools fan out over six predictors at once; CREST takes four threads in a scrubbed subprocess, so a call-counting ceiling of four admitted sixteen | ceilings count what the pod spends |
| "the audit covers what ships" | 11 of 37 packages in the `props` image were versions the audit had never seen | every image installs the exported lock with `--require-hashes` |
| "a suppression is argued" | `--ignore-vuln` matches by id, so a fixed dependency stops being reported and the suppression outlives its argument | package + locked version declared once, red when the lock moves |
| "every blind handler classifies" | one did not, and `BLE001` is silent on two shapes this fleet writes — while `RUF100` makes the prescribed `# noqa` an error where ruff did not fire | a lint rule *and* a test that reads the tree, with an allowlist held in both directions |
| "the manifests agree across the repositories" | true, and checked only in the consumer's tree: a rename done completely here left `servers/safety/tests` (261) and all other fleet tests (212) green | checked from this side too |
| a second port table in `CLAUDE.md`, and "Chemclaw3's connectors are 8810–8815" | that table listed five servers when seven were built and advertised two ports `rxnlabel` and `pyexec` already held as free; `bo` had sat on 8816 since it was written, outside the range published as that repository's | `MODULES.md` is the one registry and `test_ports_are_unique_and_inside_this_repository_s_block` checks it against every manifest in both directions, with `test_claude_md_holds_no_second_port_registry` keeping the deleted table deleted; the clearance from the rest of the family is a dated observation rather than a boundary, because it belongs to a checkout this suite cannot read |

---

## What keeps it true

This record is a map of other tests, so what keeps *it* true is that the map cannot rot silently:

- `tests/test_decision_log.py::test_every_test_a_record_names_still_exists` resolves every `test_*`
  named above against the three test roots. A rename anywhere in the suite fails this record.
- `tests/test_decision_log.py::test_every_record_says_what_keeps_it_true` is why this section
  exists at all rather than being a persuasive document with nothing behind it.
- `tests/test_backlog_register.py::test_every_anchor_a_row_names_exists` opens the anchors of every
  `docs/BACKLOG.md` row §4 sends a reader to, and reports the rows about another repository that it
  had to skip.
- `tests/test_fleet.py::test_every_path_claude_md_cites_under_a_real_directory_resolves` holds the
  sibling document this one leans on.

What it deliberately does **not** claim: that the clauses in §1–§3 are complete. They are the
posture a deployment team asks about, each traced to a test that runs; a control this fleet has and
this record omits is an omission, not a denial.
