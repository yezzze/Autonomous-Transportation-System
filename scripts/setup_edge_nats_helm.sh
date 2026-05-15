#!/usr/bin/env bash
set -euo pipefail

HELM="${HELM:-helm}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NATS_EDGE_NAMESPACE:-default}"
RELEASE="${NATS_EDGE_RELEASE:-nats}"
CHART_VERSION="${NATS_CHART_VERSION:-2.14.0}"
VALUES_FILE="${NATS_EDGE_VALUES:-${REPO_ROOT}/k8s/helm/nats-edge-values.yaml}"
CLOUD_HOST="${NATS_CLOUD_HOST:?Set NATS_CLOUD_HOST to the cloud NATS Hub host or IP reachable from this edge cluster}"
CLOUD_PASSWORD="${NATS_CLOUD_PASSWORD:-change-me-leaf-password}"
EDGE_CLUSTER_ID="${EDGE_CLUSTER_ID:?Set EDGE_CLUSTER_ID, for example edge-a or edge-b}"

if ! command -v "${HELM}" >/dev/null 2>&1; then
  if [[ -x "${HOME}/.local/bin/helm" ]]; then
    HELM="${HOME}/.local/bin/helm"
  else
    echo "helm not found. Install it first or set HELM=/path/to/helm." >&2
    exit 1
  fi
fi

tmp_values="$(mktemp)"
trap 'rm -f "${tmp_values}"' EXIT

sed \
  -e "s/CHANGE_ME_CLOUD_HOST/${CLOUD_HOST}/g" \
  -e "s/change-me-leaf-password/${CLOUD_PASSWORD}/g" \
  "${VALUES_FILE}" > "${tmp_values}"

"${HELM}" repo add nats https://nats-io.github.io/k8s/helm/charts/ >/dev/null 2>&1 || true
"${HELM}" repo update

"${HELM}" upgrade --install "${RELEASE}" nats/nats \
  --namespace "${NAMESPACE}" \
  --version "${CHART_VERSION}" \
  -f "${tmp_values}"

kubectl create configmap edge-cluster-config \
  -n "${NAMESPACE}" \
  --from-literal=CLUSTER_ID="${EDGE_CLUSTER_ID}" \
  --from-literal=NATS_SERVERS="nats://${RELEASE}:4222" \
  --from-literal=NATS_JETSTREAM_DOMAIN="hub" \
  --from-literal=NATS_STREAM_SUBJECTS="workflow.>" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout status -n "${NAMESPACE}" statefulset/"${RELEASE}" --timeout=300s
kubectl patch service "${RELEASE}" \
  -n "${NAMESPACE}" \
  --type=json \
  -p='[{"op":"remove","path":"/spec/selector/app"}]' || true
kubectl get pods,svc -n "${NAMESPACE}" -l app.kubernetes.io/instance="${RELEASE}"
kubectl get configmap edge-cluster-config -n "${NAMESPACE}" -o yaml
