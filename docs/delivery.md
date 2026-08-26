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

## Where the rollout is

Not here. No server in this repository ships a chart, so a release is `oc set image` against a
Deployment an operator created, driven from the Chemclaw3 checkout by
`deploy/jenkins/targets/openshift.sh` reading a release descriptor. See that repository's
`deploy/jenkins/README.md`, and `D-2026-08-26-a-release-is-a-descriptor-and-a-target` for why the
fleet is rolled out **before** the core that dials it.

A chart for the fleet — the seven servers differ only in name, port and token env — is a
`BACKLOG.md` row over there. Until it exists, a release can change a server's bytes and nothing else.
