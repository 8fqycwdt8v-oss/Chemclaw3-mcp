# D-2026-10-04-a-deployment-reads-its-bearer-from-the-secret-its-caller-reads — A Deployment reads its bearer from the Secret its caller reads

**Status:** accepted · **Date:** 2026-10-04 · **Scope:** every `servers/*/deploy/deployment.yaml`.

## What was found

- **No shipped Deployment set the bearer, so every server as shipped refused every `/mcp` call.**
  Each one set `MCP_ALLOWED_HOSTS` and nothing else. The variable a server's manifest names as
  `auth.token_env` was left to an operator step (`oc set env --from=secret/...`). The server fails
  closed, which is the right direction, but `/healthz` stays 200 while every call is a 401, so a
  missed step looked like a working pod. `docs/BACKLOG.md` §5 queued this and left one question
  open: whether a site's Secret naming is the fleet's to fix.
- **The fleet runs in the release's namespace already.** The NetworkPolicy admits Chemclaw3's pods
  with a `podSelector` and no `namespaceSelector`, and Chemclaw3's deployment guide states the same
  constraint. Chemclaw3's chart reads one Secret, `secrets.name` (default `chemclaw-secrets`), keyed
  by variable name, and its `secrets.optionalKeys` already carries a slot for each fleet server's
  token under the same variable name the fleet's manifest declares.

## What was decided

- **Every Deployment reads exactly its manifest's `auth.token_env` from `chemclaw-secrets`, with
  the variable name as the key.** One Secret in one namespace holds both halves of each bearer, so
  the value Chemclaw3 sends and the value the server checks cannot drift apart.
- **The reference is not `optional`.** A missing key keeps the pod in
  `CreateContainerConfigError`, naming the key. That is louder than a pod that starts, reports
  ready and refuses every call, which is the failure this replaces.
- **A site that renamed `secrets.name` patches the `secretKeyRef` in its overlay.** The default is
  the chart's default, so the common case needs no patch.

## Alternatives weighed

- **One Secret per server (`chemclaw-mcp-<name>-token`).** This is what `docs/operations.md`
  described and what the backlog row proposed. It keeps the fleet independent of Chemclaw3's chart,
  but it makes every bearer two copies of one value in two Secrets that nothing compares. A
  mismatch is exactly the silent 401 this change exists to remove. Declined.
- **Leave the env unset and keep the operator step.** The shipped files would stay incomplete, and
  the missed step would still read as a healthy pod. Declined.
- **`optional: true`.** The pod would start without the key and serve refusals. That is the
  behaviour being removed, so it was declined too.

## Revisit when

- the fleet is deployed in a namespace of its own, so it cannot read the release's Secret. The
  NetworkPolicy peers and `MCP_ALLOWED_HOSTS` change at the same moment, and
  `D-2026-10-02-the-rebinding-guard-stays-on-and-is-told-the-service-name` names the same trigger;
- Chemclaw3's chart stops reading bearers from `secrets.name` (check
  `deploy/helm/chemclaw/values.yaml` `secrets:` there), or the fleet gains a chart of its own that
  owns its Secrets.

## What keeps it true

- `tests/test_deploy_shape.py::test_the_bearer_is_wired_from_the_secret_the_manifest_names`
