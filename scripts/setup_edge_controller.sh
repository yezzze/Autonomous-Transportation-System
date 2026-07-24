#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="default"
IMAGE="${CONTROL_API_IMAGE:-k8s-demo-control-api:v2}"
TOKEN="${CONTROL_API_TOKEN:-}"

if ! kubectl get configmap edge-cluster-config \
  -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "edge-cluster-config is missing in namespace ${NAMESPACE}" >&2
  echo "run scripts/setup_edge_nats_helm.sh first" >&2
  exit 1
fi

if [[ -n "${TOKEN}" ]]; then
  kubectl create secret generic edge-controller-auth \
    -n "${NAMESPACE}" \
    --from-literal=token="${TOKEN}" \
    --dry-run=client -o yaml | kubectl apply -f -
elif ! kubectl get secret edge-controller-auth \
  -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "set CONTROL_API_TOKEN to a strong random value" >&2
  exit 1
fi

kubectl apply -f "${REPO_ROOT}/k8s/control-api.yaml"
kubectl set image \
  -n "${NAMESPACE}" \
  deployment/control-api \
  controller="${IMAGE}"
kubectl rollout status \
  -n "${NAMESPACE}" \
  deployment/control-api \
  --timeout=300s
kubectl get pods,service \
  -n "${NAMESPACE}" \
  -l app=edge-lifecycle-controller \
  -o wide
