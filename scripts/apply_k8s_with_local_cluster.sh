#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/load_cluster_env.sh
source "${SCRIPT_DIR}/lib/load_cluster_env.sh"

REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
shopt -s nullglob

render() {
  sed \
    -e "s/__EDGE_CLUSTER_ID__/${EDGE_CLUSTER_ID}/g" \
    -e "s/__CLUSTER_A_HOST__/${CLUSTER_A_HOST}/g" \
    -e "s/__CLUSTER_B_HOST__/${CLUSTER_B_HOST}/g" \
    "$1"
}

targets=(
  "${REPO_ROOT}/k8s/agent-b-deploy.yaml"
  "${REPO_ROOT}/k8s/agent-c-deploy.yaml"
  "${REPO_ROOT}/k8s/agent-grpc-deploy.yaml"
  "${REPO_ROOT}/k8s/nats.yaml"
  "${REPO_ROOT}/k8s/nats-b.yaml"
  "${REPO_ROOT}/k8s/multicluster/nats-cluster-a.yaml"
  "${REPO_ROOT}/k8s/multicluster/nats-cluster-b.yaml"
)

echo "[apply-k8s] local_cluster=${LOCAL_CLUSTER} edge_cluster_id=${EDGE_CLUSTER_ID}"
echo "[apply-k8s] cluster_a_host=${CLUSTER_A_HOST} cluster_b_host=${CLUSTER_B_HOST}"

for f in "${targets[@]}"; do
  if [[ -f "${f}" ]]; then
    echo "[apply-k8s] applying ${f}"
    render "${f}" | kubectl apply -f -
  fi
done
