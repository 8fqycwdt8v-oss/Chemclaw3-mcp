# Delivery

`Jenkinsfile` at the repository root builds every server's image, verifies each **running** image,
publishes them by digest, and reports those digests. It does not deploy anything.

## Why the pipeline exists at all

`.github/workflows/ci.yml` checks the source: the suite, the same suite with the network taken away,
and the fleet invariants. All seven `Containerfile`s were exercised by **nothing** — which, for a
repository whose central promise is "every server answers from data baked into its image", was the
least checked thing in the tree.

## What it does

| Stage | What it establishes |
| --- | --- |
| Preflight | The server list, **derived from `servers/*/Containerfile`**. Never written down — `tests/test_delivery.py` fails a pipeline that enumerates instead. |
| Gate (opt-in) | `make check` and `make offline-run`. Off by default: GitHub Actions is the gate, and a second implementation of it would be a second answer. |
| Build and publish | One image per server, via Chemclaw3's shared `build_and_push` (buildah, podman, kaniko or docker), tagged `chemclaw-mcp-<name>` and published **by digest**. |
| Verify every image answers | Starts each image and asks it two questions no source file can answer. |
| Report the digests | `mcp-digests.txt`, one `server=sha256:…` per line, for the Chemclaw3 release job's `MCP_DIGESTS` parameter. |

## The two things only a running image can prove

1. **`/healthz` reports this build's revision.** `tests/test_fleet.py` asserts the `ARG`/`ENV` pair
   exists in each `Containerfile`; only a build shows the value arrived. Chemclaw3's own revision
   field read `unknown` in every image for eight months with its test green, because nothing ever
   passed the build argument.
2. **`/mcp` refuses an unauthenticated call.** This one cannot be read off the source at all: the
   MCP surface is *mounted*, and a mount bypasses the enclosing app's dependencies. `CLAUDE.md` says
   to verify bearer enforcement against a running server for exactly this reason, and this is that
   check, run on the artifact that ships.

Both the port and the credential's env var name come out of `connector.yaml`, because the manifest
is the contract — a server whose `token_env` did not follow the usual pattern would otherwise be
started with no credential and pass by refusing everything.

## What the image does *not* carry, and the operator has to

**The `HEALTHCHECK` line in every `Containerfile` is Docker-only.** Kubernetes and OpenShift ignore
it entirely — they read `readinessProbe` and `livenessProbe` off the Pod spec and nothing else — so
a `docker run` locally is the only place that line has ever executed. It is kept because it is right
for the local case and costs nothing; it must not be read as the cluster's probe.

**The Pod spec is not the operator's to write any more, and these four lines said it was.** Every
server ships `deploy/deployment.yaml`, `deploy/hpa.yaml` and `deploy/pdb.yaml` beside the `Service`
and the `ServiceMonitor` — `docs/adding-a-server.md` lists all six as required and
`tests/test_fleet.py::test_a_server_ships_the_whole_set` is what requires them. This section
previously said the images ship "no readiness probe and no liveness probe at all" and told an
operator to wire `livenessProbe` "on the same route, on a longer period", which is the exact shape
`tests/test_deploy_shape.py::test_liveness_and_readiness_do_not_share_a_route` forbids: on
`/healthz`, a broken *optional* component is a kill after `periodSeconds x failureThreshold` rather
than a pod leaving its Service, and a restart cannot recreate a missing checkpoint. So the
instruction recreated the `CrashLoopBackOff` that
`D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe` was written for.

What the shipped Deployment wires, per server, and what an operator therefore does not:

- `readinessProbe` on `GET /healthz`. A real check rather than a constant 200: a server with a
  `readiness` callable answers **503 with the reason** when its corpus, rule table or backend will
  not load, and the body lists the corpora it did verify as `name@version`. A pod that is not ready
  must not be sent traffic: an unready `safety` pod fails every screen it is asked for, and a screen
  that errors is a control the answer gets written without. Only a
  `mcp_server_kit.degradation.PERMANENT_CAUSES` failure answers unready; a transient one answers 200
  with `degraded` naming it.
- `livenessProbe` on **`GET /livez`**, which is a different route because the two answers differ. It
  consults nothing and proves only that the process still serves HTTP, which is the one fault a
  restart fixes.
- the `Service` and the `ServiceMonitor`, which are what tells Prometheus to scrape `/metrics`, and
  the `HorizontalPodAutoscaler` and `PodDisruptionBudget` that make a rollout or a node drain
  something other than a total outage of that capability.

What is still an operator's is applying those manifests and driving the *release*: see below. Six
near-identical files per server is what a chart would replace, and this document does not say what
the `BACKLOG.md` row over in Chemclaw3 currently reads, because nothing here can re-read it
(`D-2026-09-19-a-claim-about-another-repository-is-checked-by-re-reading-it`).

## Where the rollout is

Not here. No server in this repository ships a chart, so a release is `oc set image` against a
Deployment an operator created, driven from the Chemclaw3 checkout by
`deploy/jenkins/targets/openshift.sh` reading a release descriptor. See that repository's
`deploy/jenkins/README.md`, and `D-2026-08-26-a-release-is-a-descriptor-and-a-target` for why the
fleet is rolled out **before** the core that dials it.

A chart for the fleet — the servers differ only in name, port and token env — is a
`BACKLOG.md` row over there. Until it exists, a release can change a server's bytes and nothing else.
