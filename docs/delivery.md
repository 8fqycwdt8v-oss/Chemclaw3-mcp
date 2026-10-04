# Delivery

`Jenkinsfile` at the repository root builds every server's image, verifies each **running** image,
publishes them by digest, and reports those digests. It does not deploy anything.

## Why the pipeline exists at all

`.github/workflows/ci.yml` checks the source: the suite, the same suite with the network taken away,
and the fleet invariants. It never builds a `Containerfile` — and for a repository whose central
promise is "every server answers from data baked into its image", the image is the thing to check.
This pipeline is what does.

## What it does

| Stage | What it establishes |
| --- | --- |
| Preflight | The server list, **derived from `servers/*/Containerfile`**. Never written down — `tests/test_delivery.py` fails a pipeline that enumerates instead. |
| Gate (opt-in) | `make check` and `make offline-run`. Off by default: GitHub Actions is the gate, and a second implementation of it would be a second answer. |
| Build and publish | One image per server, via Chemclaw3's shared `build_and_push` (buildah, podman, kaniko or docker), tagged `chemclaw-mcp-<name>` and published **by digest**. |
| Verify every image answers | Starts each image and asks it two questions no source file can answer. |
| Report the digests | `mcp-digests.txt`, one `server=sha256:…` per line, for the Chemclaw3 release job's `MCP_DIGESTS` parameter. |

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `IMAGE_REGISTRY` | empty | Registry and org, e.g. `image-registry.openshift-image-registry.svc:5000/chemclaw`. Empty builds only. |
| `SERVERS` | empty | Space-separated subset. Empty means every `servers/*/Containerfile`. |
| `IMAGE_BUILDER` | `autodetect` | `buildah`, `podman`, `kaniko` or `docker`. OpenShift agents have no Docker socket. `kaniko` cannot build without pushing, so it skips the verify stage. |
| `DRY_RUN` | `true` | Build and verify without publishing. |
| `RUN_GATE` | `false` | Run `make check` and `make offline-run` here. **Required to publish**: Preflight refuses `DRY_RUN=false` with a registry and the gate off, because this pipeline cannot see whether GitHub Actions passed on that revision. |
| `REGISTRY_CREDENTIALS_ID` | `chemclaw-registry` | Jenkins username/password credential for the registry. |
| `CHEMCLAW3_REPO` / `CHEMCLAW3_BRANCH` | the Chemclaw3 repository / `main` | Where the shared `deploy/jenkins/lib` build library comes from. |

Images are named `chemclaw-mcp-<name>` and tagged with the first twelve characters of the commit;
each build passes `--build-arg CHEMCLAW_REVISION=<full sha>`. The same build by hand is in
[`operations.md`](operations.md#1-build-an-image).

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

**The Pod spec ships with each server.** Every server has `deploy/deployment.yaml`, `deploy/hpa.yaml`
and `deploy/pdb.yaml` beside the `Service`, the `NetworkPolicy` and the `ServiceMonitor` —
`docs/adding-a-server.md` lists them as required and
`tests/test_fleet.py::test_a_server_ships_the_whole_set` is what requires them. Applying them is
[`operations.md`](operations.md) §2.

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
- the bearer: the variable the manifest names as `auth.token_env`, from a non-optional
  `secretKeyRef` into `chemclaw-secrets` (key = the variable name) — the Secret Chemclaw3's chart
  reads, so both halves hold one value.
- a security context `restricted-v2` admits: `runAsNonRoot` with no pinned UID, GID or fsGroup, so
  OpenShift assigns them. The image is built for that — a numeric `USER` in group 0 and
  `HOME=/tmp`, the one writable mount.

`tests/test_deploy_shape.py` holds the last two against every server's manifest and `Containerfile`.

What is still an operator's: applying those manifests, **putting the bearer's key into
`chemclaw-secrets`** (a missing key keeps the pod from starting), replacing the placeholder
`registry.invalid/chemclaw-mcp-<name>:unset` image — unresolvable by design, so it fails to pull
rather than resolving to somebody else's — with a published digest
(`operations.md` §2 does it in a kustomize overlay), and driving the *release*: see below.

## Where the rollout is

Not here. No server in this repository ships a chart, so a release is `oc set image` against a
Deployment an operator created, driven from the Chemclaw3 checkout by
`deploy/jenkins/targets/openshift.sh` reading a release descriptor. See that repository's
`deploy/jenkins/README.md`, and `D-2026-08-26-a-release-is-a-descriptor-and-a-target` for why the
fleet is rolled out **before** the core that dials it.

Until the fleet has a chart — the servers differ only in name, port and token env — a release can
change a server's bytes and nothing else; a change to a Deployment's env or resources is an
`oc apply` of `servers/<name>/deploy/` (then `oc set image` again, because the applied manifest
carries a placeholder image).
