# D-2026-10-14-the-fleet-deploys-as-one-chart — A server's deployment is a row in one Helm chart's values, and the deploy checks read what that chart renders

**Status:** accepted · **Date:** 2026-10-14

## Context

Each of the eleven servers carried a `deploy/` directory: a Deployment, Service, NetworkPolicy,
HorizontalPodAutoscaler, PodDisruptionBudget and ServiceMonitor, plus a KEDA ScaledObject for the
servers with an admission gate (commit `6100fc0`). Parsed and compared field by field, the eleven
copies differ only in the server's name, port, token variable, resources,
scratch size, two probe delays, grace period, autoscaler ceiling and scale-in window; everything else
(security context, probes, spread, budgets, policy peers, behaviour) is one text copied eleven times,
with its rationale comment copied alongside it. A change to one rule is eleven edits that a review
must see are the same edit, and `tests/test_deploy_shape.py` exists to catch the copy that was missed.

Chemclaw3 (`D-2026-10-07-the-architecture-programme`, W6) is moving to Helm for its own deployment and
needs the fleet's Service name, port and token Secret key from one declaration instead of re-typing
them in `connectors.<name>.url`.

## Options

1. **Keep the per-server files and add a lint that the copies agree.** The ratchet already exists and
   passes today; the cost is that every rule stays eleven texts and a new server is a copy-and-rename
   that the ratchet can only police after the fact.
2. **Kustomize: one base and a patch per server.** Removes the duplication without a templating
   language, but a base cannot derive the grace period from the manifest's `request_timeout`, the
   Service address from the port or the HPA floor from the replica count; those become patches that
   restate the number, which is the copy this removes. It also gives Chemclaw3 nothing to depend on.
3. **One Helm chart with a values table, one entry per server** (chosen). Rules are written once in
   templates; a server states only how it differs; derived numbers are derived; the chart is
   something Chemclaw3's chart can take as a subchart.
4. **A Helm library chart with a thin chart per server.** Same templates, eleven wrappers to keep in
   step, and a library chart cannot be rendered by `helm template`, which the checks below need.

## Decision

Option 3, as an application chart at `deploy/helm/chemclaw-fleet` (an application chart is renderable
and can still be a subchart). `values.yaml` holds `defaults` and one entry per server; object names
stay `chemclaw-mcp-<name>` whatever the release is called, because Chemclaw3 dials them. The pod
hardening, the default-deny policy, the budget, the probes and the two autoscalers are written once,
with their reasons, in `templates/`. KEDA stays opt-in: `autoscaling.mode=keda` swaps the HPA for a
ScaledObject on the gated servers, so each Deployment has exactly one autoscaler in either mode.

The deploy checks move with it. They no longer parse files a server owns; they parse
`helm template` output (the `fleet_render` fixture in the root `conftest.py`) with the assertions
they already had, over every server in the tree. The per-server `tests/test_deploy.py` files are one
parametrised `tests/test_deploy.py`, each assertion held against an independent source (the port
against the server's own `connector.yaml` and `Containerfile`).

A values file must not be able to write manifest. Two layers hold that: `values.schema.json` closes
every object and gives every string that reaches a manifest a pattern, and every template quotes what
it interpolates, so the egress and bound ratchets, which read the render, see every variable a pod
can have. A moving image tag is the template's refusal (`chemclaw-fleet.image`), not the schema's.
`tests/test_deploy_shape.py` renders newline-bearing values into each interpolated field, with the
schema and with it removed.

The Deployment renders no `replicas` while an autoscaler owns the count, because a rendered number
is applied again by every `helm upgrade` and would reset a scaled-out server to its floor;
`autoscaling.mode=none` is the fixed-replica form.

## Consequences

A new server is a row, not a directory of copies, and `tests/test_deploy_shape.py` requires the row. Rendering needs `helm`:
locally the deploy tests skip and name the skip, in CI `CHEMCLAW_REQUIRE_HELM=1` makes a missing
binary a failure, and `make chart-validate` adds a kubeconform pass over the render in both
autoscaling modes. Anything that consumed `servers/<name>/deploy/*.yaml` directly (Chemclaw3's
`deploy/kind/render-fleet.sh`) must render the chart instead.

The old files were compared with the chart's render once, before they were deleted, in both
autoscaling modes. The intended differences are that the ScaledObject's Prometheus `namespace`
defaults to the release's namespace where the file hard-coded `chemclaw`, and that the Deployment
renders no `replicas`. The change that introduced the chart carries the measurements.

The release path is not part of this decision. Chemclaw3's release tooling still sets images with
`oc set image`; `docs/delivery.md` says which of the two paths an environment uses and why they
must not be mixed.

**Revisit when:** a server needs a shape the table cannot express without a template branch of its
own (a second container, a volume that is not scratch). That is the point where the server stops
being a row and the chart is no longer the right unit for it.

## What keeps it true

`tests/test_deploy_shape.py::test_the_chart_names_exactly_the_servers_the_tree_has` holds the table
equal to the tree. `tests/test_deploy_shape.py::test_a_server_is_rendered_whole` and the fleet-wide
checks in that file hold each render to one shape; `tests/test_deploy.py::test_the_container_declares_the_port_the_manifest_dials`
holds each port against the manifest. `tests/test_fleet_deploy.py::test_a_deployment_directory_holds_only_shapes_the_ratchets_can_read`
holds that no server keeps a second description beside the chart and that the schema admits no
environment-injecting key; `tests/test_deploy_shape.py::test_the_templates_quote_what_the_schema_would_have_refused`
holds that no value, whatever its content, adds a field to any object.
