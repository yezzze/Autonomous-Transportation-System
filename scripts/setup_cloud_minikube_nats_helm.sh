#!/usr/bin/env bash
set -euo pipefail

HELM="${HELM:-helm}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/load_cluster_env.sh
source "${SCRIPT_DIR}/lib/load_cluster_env.sh"

PROFILE="${MINIKUBE_PROFILE:-cloud}"
NAMESPACE="${NATS_CLOUD_NAMESPACE:-nats-cloud}"
RELEASE="${NATS_CLOUD_RELEASE:-nats-hub}"
CHART_VERSION="${NATS_CHART_VERSION:-2.14.0}"
VALUES_FILE="${NATS_CLOUD_VALUES:-${REPO_ROOT}/k8s/helm/nats-cloud-values.yaml}"

if ! command -v "${HELM}" >/dev/null 2>&1; then
  if [[ -x "${HOME}/.local/bin/helm" ]]; then
    HELM="${HOME}/.local/bin/helm"
  else
    echo "helm not found. Install it first or set HELM=/path/to/helm." >&2
    exit 1
  fi
fi

minikube start \
  -p "${PROFILE}" \
  --driver=docker \
  --ports=4222:30422 \
  --ports=7422:30472 \
  --ports=8222:30482

kubectl config use-context "${PROFILE}"
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

"${HELM}" repo add nats https://nats-io.github.io/k8s/helm/charts/ >/dev/null 2>&1 || true
"${HELM}" repo update

tmp_values="$(mktemp)"
trap 'rm -f "${tmp_values}"' EXIT
sed -e "s/change-me-leaf-password/${NATS_CLOUD_PASSWORD}/g" "${VALUES_FILE}" > "${tmp_values}"

echo "[cloud-nats] cluster_a_host=${CLUSTER_A_HOST} leaf_password=(from cluster.env)"

"${HELM}" upgrade --install "${RELEASE}" nats/nats \
  --namespace "${NAMESPACE}" \
  --version "${CHART_VERSION}" \
  -f "${tmp_values}"

kubectl rollout status -n "${NAMESPACE}" statefulset/"${RELEASE}" --timeout=300s
kubectl get pods,svc,pvc -n "${NAMESPACE}"

echo
echo "Cloud NATS Hub:"
echo "  client:   nats://127.0.0.1:4222"
echo "  leafnode: nats://127.0.0.1:7422"
echo "  monitor:  http://127.0.0.1:8222"
