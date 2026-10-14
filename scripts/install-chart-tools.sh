#!/usr/bin/env bash
# Install the pinned `helm` and `kubeconform` into a directory, for a machine that has neither.
#
#   scripts/install-chart-tools.sh <directory>      then put <directory> on PATH
#
# The deploy checks render `deploy/helm/chemclaw-fleet` and `make chart-validate` schema-checks the
# render, so a gate runner needs both binaries (Jenkins has no setup action to install them). The
# versions and checksums are the ones `.github/workflows/ci.yml` pins; a test holds them equal.
# Checksummed because a release asset, unlike a git tag, is a file its owner can replace. Each
# download goes into its own empty directory and is verified before anything is extracted.
set -euo pipefail

HELM_VERSION="v3.13.0"
HELM_SHA256="138676351483e61d12dfade70da6c03d471bbdcac84eaadeb5e1d06fa114a24f"
KUBECONFORM_VERSION="v0.6.7"
KUBECONFORM_SHA256="95f14e87aa28c09d5941f11bd024c1d02fdc0303ccaa23f61cef67bc92619d73"

[ $# -eq 1 ] || { echo "usage: install-chart-tools.sh <directory>" >&2; exit 2; }
bin="$1"
mkdir -p "$bin"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fetch() { # url sha256 file
  curl -sSfL --retry 3 -o "$work/$3" "$1"
  echo "$2  $work/$3" | sha256sum -c - >/dev/null || { echo "checksum mismatch for $1" >&2; exit 1; }
}

fetch "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" "$HELM_SHA256" helm.tgz
mkdir "$work/helm"
tar -xzf "$work/helm.tgz" -C "$work/helm"
install -m 0755 "$work/helm/linux-amd64/helm" "$bin/helm"

fetch "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" \
  "$KUBECONFORM_SHA256" kubeconform.tgz
mkdir "$work/kubeconform"
tar -xzf "$work/kubeconform.tgz" -C "$work/kubeconform" kubeconform
install -m 0755 "$work/kubeconform/kubeconform" "$bin/kubeconform"

"$bin/helm" version --short
"$bin/kubeconform" -v
