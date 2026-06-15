#!/usr/bin/env bash
# Deploy OpenSandbox with the local fixed Helm chart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

HELM="${HELM:-helm}"
KUBECTL="${KUBECTL:-kubectl}"

OPENSANDBOX_ROOT="${OPENSANDBOX_ROOT:-/home/czl/Project/OpenSandbox}"
CHARTS_DIR="${OPENSANDBOX_ROOT}/kubernetes/charts"
CHART_DIR="${CHARTS_DIR}/opensandbox"

RELEASE="${OPENSANDBOX_RELEASE:-opensandbox}"
SYSTEM_NAMESPACE="${OPENSANDBOX_SYSTEM_NAMESPACE:-opensandbox-system}"
WORKLOAD_NAMESPACE="${OPENSANDBOX_WORKLOAD_NAMESPACE:-opensandbox}"
API_KEY="${OPENSANDBOX_API_KEY:-gs666}"

SERVER_IMAGE_TAG="${OPENSANDBOX_SERVER_IMAGE_TAG:-v0.1.13}"
EXECD_IMAGE="${OPENSANDBOX_EXECD_IMAGE:-sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/execd:v1.0.16}"
EGRESS_IMAGE="${OPENSANDBOX_EGRESS_IMAGE:-sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/egress:v1.0.11}"
EGRESS_MODE="${OPENSANDBOX_EGRESS_MODE:-dns+nft}"
SANDBOX_CREATE_TIMEOUT_SECONDS="${OPENSANDBOX_CREATE_TIMEOUT_SECONDS:-180}"

VALUES_FILE="${OPENSANDBOX_VALUES_FILE:-}"
WAIT_TIMEOUT="${OPENSANDBOX_WAIT_TIMEOUT:-300s}"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "missing required command: ${cmd}" >&2
    exit 1
  fi
}

require_cmd "${HELM}"
require_cmd "${KUBECTL}"

if [[ ! -d "${CHART_DIR}" ]]; then
  echo "OpenSandbox chart not found: ${CHART_DIR}" >&2
  echo "Set OPENSANDBOX_ROOT to the OpenSandbox repository path." >&2
  exit 1
fi

echo "[opensandbox] project=${PROJECT_ROOT}"
echo "[opensandbox] chart=${CHART_DIR}"
echo "[opensandbox] release=${RELEASE} system_namespace=${SYSTEM_NAMESPACE} workload_namespace=${WORKLOAD_NAMESPACE}"

"${KUBECTL}" create namespace "${SYSTEM_NAMESPACE}" --dry-run=client -o yaml | "${KUBECTL}" apply -f -
"${KUBECTL}" create namespace "${WORKLOAD_NAMESPACE}" --dry-run=client -o yaml | "${KUBECTL}" apply -f -

tmp_values=""
if [[ -n "${VALUES_FILE}" ]]; then
  tmp_values="${VALUES_FILE}"
else
  tmp_values="$(mktemp)"
  trap 'rm -f "${tmp_values}"' EXIT
  cat >"${tmp_values}" <<EOF
opensandbox-server:
  server:
    image:
      tag: "${SERVER_IMAGE_TAG}"

  configToml: |
    [server]
    host = "0.0.0.0"
    port = 80
    api_key = "${API_KEY}"

    [log]
    level = "INFO"

    [runtime]
    type = "kubernetes"
    execd_image = "${EXECD_IMAGE}"

    [kubernetes]
    kubeconfig_path = ""
    namespace = "${WORKLOAD_NAMESPACE}"
    informer_enabled = true
    informer_resync_seconds = 300
    informer_watch_timeout_seconds = 60
    sandbox_create_timeout_seconds = ${SANDBOX_CREATE_TIMEOUT_SECONDS}
    snapshot_create_timeout_seconds = 900
    workload_provider = "batchsandbox"
    batchsandbox_template_file = "/etc/opensandbox/example.batchsandbox-template.yaml"

    [egress]
    image = "${EGRESS_IMAGE}"
    mode = "${EGRESS_MODE}"
EOF
fi

echo "[opensandbox] values=${tmp_values}"

(
  cd "${CHARTS_DIR}"
  # The fixed OpenSandbox tag has Chart.yaml dependencies newer than the
  # checked-in Chart.lock, so refresh local file:// dependencies before install.
  "${HELM}" dependency update opensandbox
  "${HELM}" upgrade --install "${RELEASE}" ./opensandbox \
    -n "${SYSTEM_NAMESPACE}" \
    -f "${tmp_values}" \
    --wait \
    --timeout "${WAIT_TIMEOUT}"
)

# config.toml is mounted with subPath, so ConfigMap changes such as api_key do
# not refresh inside existing pods. Restart the server after Helm applies values.
"${KUBECTL}" rollout restart deployment/opensandbox-server -n "${SYSTEM_NAMESPACE}"
"${KUBECTL}" rollout status deployment/opensandbox-server -n "${SYSTEM_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"

echo "[opensandbox] deployed"
"${KUBECTL}" get pods -n "${SYSTEM_NAMESPACE}"
"${KUBECTL}" get svc -n "${SYSTEM_NAMESPACE}"
"${KUBECTL}" get crd | grep -E 'batchsandboxes|pools|sandboxsnapshots' || true

cat <<EOF

OpenSandbox is ready for ATS.

For local ATS server:
  kubectl port-forward -n ${SYSTEM_NAMESPACE} svc/opensandbox-server 8088:80
  export OPENSANDBOX_SERVER_URL=http://127.0.0.1:8088
  export OPENSANDBOX_API_KEY=${API_KEY}

For ATS running inside Kubernetes:
  export OPENSANDBOX_SERVER_URL=http://opensandbox-server.${SYSTEM_NAMESPACE}.svc.cluster.local
  export OPENSANDBOX_API_KEY=${API_KEY}
EOF
