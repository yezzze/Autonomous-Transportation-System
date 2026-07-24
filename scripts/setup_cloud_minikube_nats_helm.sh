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
STREAM="${NATS_STREAM:-WORKFLOW_LEGACY}"
STREAM_SUBJECTS="${NATS_STREAM_SUBJECTS:-legacy.workflow.>}"
STREAM_MAX_BYTES="${NATS_STREAM_MAX_BYTES:-5GiB}"
STREAM_MAX_AGE="${NATS_STREAM_MAX_AGE:-24h}"
STREAM_MAX_MSGS="${NATS_STREAM_MAX_MSGS:--1}"
STREAM_MAX_MSGS_PER_SUBJECT="${NATS_STREAM_MAX_MSGS_PER_SUBJECT:--1}"
STREAM_DISCARD="${NATS_STREAM_DISCARD:-new}"
STREAM_RETENTION="${NATS_STREAM_RETENTION:-workqueue}"
STREAM_STORAGE="${NATS_STREAM_STORAGE:-file}"
CONSUMER_MAX_INACTIVE="${NATS_CONSUMER_MAX_INACTIVE:-10m}"
CLEAN_CONSUMERS_ON_START="${NATS_CLEAN_CONSUMERS_ON_START:-false}"
CREATE_CLOUD_WORKFLOW_STREAM="${NATS_CREATE_CLOUD_WORKFLOW_STREAM:-false}"

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
  --image-repository="${MINIKUBE_IMAGE_REPOSITORY:-registry.cn-hangzhou.aliyuncs.com/google_containers}" \
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
if [[ "${CREATE_CLOUD_WORKFLOW_STREAM}" == "true" ]] \
  && kubectl get deploy -n "${NAMESPACE}" "${RELEASE}-box" >/dev/null 2>&1; then
  kubectl rollout status -n "${NAMESPACE}" deploy/"${RELEASE}-box" --timeout=300s

  echo "[cloud-nats] ensuring stream=${STREAM} subjects=${STREAM_SUBJECTS} max_age=${STREAM_MAX_AGE} max_bytes=${STREAM_MAX_BYTES}"
  kubectl exec -n "${NAMESPACE}" deploy/"${RELEASE}-box" -- sh -c '
    set -eu
    stream="$1"
    subjects="$2"
    max_bytes="$3"
    max_age="$4"
    max_msgs="$5"
    max_msgs_per_subject="$6"
    discard="$7"
    retention="$8"
    storage="$9"
    consumer_max_inactive="${10}"
    clean_consumers="${11}"
    server="${12}"

    if nats stream info "$stream" --server "$server" --js-domain hub >/dev/null 2>&1; then
      nats stream edit "$stream" \
        --subjects "$subjects" \
        --max-bytes "$max_bytes" \
        --max-age "$max_age" \
        --max-msgs "$max_msgs" \
        --max-msgs-per-subject "$max_msgs_per_subject" \
        --discard "$discard" \
        --retention "$retention" \
        --server "$server" \
        --js-domain hub \
        --force >/dev/null
    else
      nats stream add "$stream" \
        --subjects "$subjects" \
        --storage "$storage" \
        --retention "$retention" \
        --max-bytes "$max_bytes" \
        --max-age "$max_age" \
        --max-msgs "$max_msgs" \
        --max-msgs-per-subject "$max_msgs_per_subject" \
        --discard "$discard" \
        --limit-consumer-inactive "$consumer_max_inactive" \
        --server "$server" \
        --js-domain hub \
        --defaults >/dev/null
    fi

    if [ "$clean_consumers" != "false" ]; then
      echo "[cloud-nats] cleaning consumers on stream=$stream"
      nats consumer ls "$stream" --names --server "$server" --js-domain hub \
        | while IFS= read -r consumer; do
            [ -n "$consumer" ] || continue
            nats consumer rm "$stream" "$consumer" --server "$server" --js-domain hub --force >/dev/null
          done
    fi
  ' sh \
    "${STREAM}" \
    "${STREAM_SUBJECTS}" \
    "${STREAM_MAX_BYTES}" \
    "${STREAM_MAX_AGE}" \
    "${STREAM_MAX_MSGS}" \
    "${STREAM_MAX_MSGS_PER_SUBJECT}" \
    "${STREAM_DISCARD}" \
    "${STREAM_RETENTION}" \
    "${STREAM_STORAGE}" \
    "${CONSUMER_MAX_INACTIVE}" \
    "${CLEAN_CONSUMERS_ON_START}" \
    "nats://${RELEASE}:4222"
else
  echo "[cloud-nats] skip shared WORKFLOW stream; each Agent Pod owns a stream in its edge domain"
fi

kubectl get pods,svc,pvc -n "${NAMESPACE}"

echo
echo "Cloud NATS Hub:"
echo "  client:   nats://127.0.0.1:4222"
echo "  leafnode: nats://127.0.0.1:7422"
echo "  monitor:  http://127.0.0.1:8222"
