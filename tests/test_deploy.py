"""The deployment says what the code says, for every server, read off what the chart renders.

Ports, ingress peers, scrape wiring and pod hardening are facts of one server that no
fleet-wide invariant derives, so each is held against an independent source: the port from the
server's own `connector.yaml`, the image facts from its `Containerfile`. The NetworkPolicy is the
layer that enforces no-egress and is checked in both directions, since the likely regression is
`Egress` quietly dropped from `policyTypes`. Fleet-wide shape (replicas, spread, budgets,
autoscalers) is `tests/test_deploy_shape.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "servers"

Render = Callable[..., list[dict[str, Any]]]

# The labels the agent's front door carries: the one caller a server admits besides the scraper.
CALLER_LABELS = {"app.kubernetes.io/name": "chemclaw"}


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def _port(server: Path) -> int:
    """The port the server's own manifest publishes, the independent source for every port."""
    manifest = yaml.safe_load((server / "connector.yaml").read_text(encoding="utf-8"))
    found = re.search(r":(\d+)/mcp", str(manifest["endpoint"]["url"]))
    assert found, f"{server.name}: cannot read a port out of connector.yaml's endpoint url"
    return int(found.group(1))


def _one(render: Render, server: Path, kind: str) -> dict[str, Any]:
    """The single `kind` object the chart renders for this server."""
    found = [
        doc
        for doc in render()
        if doc["kind"] == kind and doc["metadata"]["name"] == f"chemclaw-mcp-{server.name}"
    ]
    assert len(found) == 1, f"{server.name}: the chart renders {len(found)} {kind}, expected one"
    return found[0]


def _spec(render: Render, server: Path, kind: str) -> dict[str, Any]:
    """The `spec` of the single `kind` object rendered for this server."""
    spec = _one(render, server, kind)["spec"]
    assert isinstance(spec, dict)
    return spec


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_egress_is_denied(server: Path, fleet_render: Render) -> None:
    """`Egress` is governed and the rule list is empty — the two halves of "deny all"."""
    spec = _spec(fleet_render, server, "NetworkPolicy")
    assert "Egress" in spec["policyTypes"], (
        "dropping Egress from policyTypes restores unrestricted outbound traffic while leaving "
        "the object looking like a network policy"
    )
    assert spec["egress"] == [], f"{server.name} must reach nothing; found {spec['egress']!r}"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_ingress_admits_only_the_agent_and_the_scraper(server: Path, fleet_render: Render) -> None:
    """A tool server open to the whole namespace is a tool surface open to the whole namespace."""
    spec = _spec(fleet_render, server, "NetworkPolicy")
    assert "Ingress" in spec["policyTypes"]
    rules = spec["ingress"]
    assert isinstance(rules, list) and len(rules) == 1
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": _port(server)}]
    peers = rules[0]["from"]
    assert [next(iter(peer)) for peer in peers] == ["podSelector", "namespaceSelector"]
    assert peers[0]["podSelector"] == {"matchLabels": CALLER_LABELS}


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_policy_selects_this_server(server: Path, fleet_render: Render) -> None:
    """A policy whose selector matches nothing is a policy that protects nothing."""
    spec = _spec(fleet_render, server, "NetworkPolicy")
    selected = spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
    assert selected == f"chemclaw-mcp-{server.name}"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_container_declares_the_port_the_manifest_dials(
    server: Path, fleet_render: Render
) -> None:
    """Containerfile, container, Service, NetworkPolicy and connector.yaml agree on one number."""
    port = _port(server)
    containerfile = (server / "Containerfile").read_text(encoding="utf-8")
    assert str(port) in containerfile
    pod = _spec(fleet_render, server, "Deployment")["template"]["spec"]
    assert pod["containers"][0]["ports"] == [{"name": "http", "containerPort": port}]
    service_ports = _spec(fleet_render, server, "Service")["ports"]
    assert [(p["port"], p["targetPort"]) for p in service_ports] == [(port, port)]


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_scrape_is_wired_end_to_end(server: Path, fleet_render: Render) -> None:
    """`/metrics` is only observability if something is told to collect it.

    Service, ServiceMonitor and NetworkPolicy must line up. A ServiceMonitor's `endpoints[].port` is
    a port name resolved through the Service, and a name matching nothing yields no targets and no
    error, so the name is held between the two objects here.
    """
    name = f"chemclaw-mcp-{server.name}"
    service = _spec(fleet_render, server, "Service")
    monitor = _spec(fleet_render, server, "ServiceMonitor")
    assert service["selector"]["app.kubernetes.io/name"] == name
    port = _port(server)
    ports = service["ports"]
    assert ports == [{"name": "http", "protocol": "TCP", "port": port, "targetPort": port}]
    assert monitor["selector"]["matchLabels"]["app.kubernetes.io/name"] == name
    endpoints = monitor["endpoints"]
    assert len(endpoints) == 1, "one process, one port, one scrape"
    assert endpoints[0]["port"] == ports[0]["name"], (
        "the ServiceMonitor names a port the Service does not declare; Prometheus resolves that "
        "name through the Service and reports no targets and no error when it misses"
    )
    assert endpoints[0]["path"] == "/metrics"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_pod_is_hardened(server: Path, fleet_render: Render) -> None:
    """Every deny-by-default securityContext field is present and set — the fields a plain cluster
    otherwise leaves at root/all-capabilities/unconfined/unbounded.
    """
    spec = _spec(fleet_render, server, "Deployment")["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    pod_sc = spec["securityContext"]
    assert pod_sc["runAsNonRoot"] is True
    assert pod_sc["seccompProfile"]["type"] == "RuntimeDefault"
    # No pinned UID: OpenShift's `restricted-v2` assigns one and rejects any other, and
    # `runAsNonRoot` above is the property that matters (`tests/test_deploy_shape.py`).
    assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(pod_sc)

    container = spec["containers"][0]
    csc = container["securityContext"]
    assert csc["allowPrivilegeEscalation"] is False
    assert csc["runAsNonRoot"] is True
    assert csc["capabilities"]["drop"] == ["ALL"]
    assert csc["seccompProfile"]["type"] == "RuntimeDefault"
    # Present and a boolean either way: true for a server that writes nothing, false for one that
    # needs scratch (`calc`, `pyexec`).
    assert isinstance(csc["readOnlyRootFilesystem"], bool)

    resources = container["resources"]
    assert resources["requests"]["cpu"] and resources["requests"]["memory"]
    assert resources["limits"]["cpu"] and resources["limits"]["memory"]

    # Liveness and readiness are different routes: a liveness failure kills the container, so it
    # must not consult what readiness consults. `tests/test_deploy_shape.py` holds this fleet-wide.
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["port"] == "http"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_pod_label_matches_the_networkpolicy_selector(
    server: Path, fleet_render: Render
) -> None:
    """The highest-value check here: a one-character drift between the Deployment's pod label and
    the NetworkPolicy's `podSelector` silently exempts this workload from default-deny egress, and
    the no-egress promise is void for it with nothing red. So the two are checked against each
    other, not against a literal.
    """
    template = _spec(fleet_render, server, "Deployment")["template"]
    pod_label = template["metadata"]["labels"]["app.kubernetes.io/name"]
    policy = _spec(fleet_render, server, "NetworkPolicy")
    selector = policy["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
    assert pod_label == selector, (
        f"Deployment pod label {pod_label!r} != NetworkPolicy podSelector {selector!r}: "
        "the egress policy would not select this workload"
    )


# The servers whose image loads HuggingFace weights from a baked tree.
MODEL_SERVERS = ("rxnlabel", "rxnpredict")


@pytest.mark.parametrize("name", MODEL_SERVERS)
def test_a_model_image_runs_its_framework_offline(name: str) -> None:
    """The HuggingFace offline switches stop the first model load reaching the hub.

    A hub lookup would raise `EgressForbidden` (an `OSError`) that a loader may swallow, and the
    server would answer from fewer models with no error anywhere; the empty `egress:` is what makes
    "the weights are baked in" a fact.
    """
    containerfile = (SERVERS / name / "Containerfile").read_text(encoding="utf-8")
    assert "HF_HUB_OFFLINE=1" in containerfile
    assert "TRANSFORMERS_OFFLINE=1" in containerfile
