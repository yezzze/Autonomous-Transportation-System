#!/usr/bin/env bash
# Start ATS locally and route app deployments through OpenSandbox.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export PYTHONPATH="${ROOT_DIR}"
export PORT="${PORT:-8001}"
export AGENT_DEPLOY_BACKEND="${AGENT_DEPLOY_BACKEND:-opensandbox}"

# ATS runs locally; OpenSandbox server is reached through kubectl port-forward.
export OPENSANDBOX_SERVER_URL="${OPENSANDBOX_SERVER_URL:-http://127.0.0.1:8088}"
export OPENSANDBOX_API_KEY="${OPENSANDBOX_API_KEY:-gs666}"
export OPENSANDBOX_REQUEST_TIMEOUT="${OPENSANDBOX_REQUEST_TIMEOUT:-240}"

# UI/control plane uses local NATS port-forward; sandbox workloads use cluster DNS.
export NATS_SERVICE_NAME="${NATS_SERVICE_NAME:-nats}"
export NATS_SERVERS="${NATS_SERVERS:-nats://127.0.0.1:4222}"
export AGENT_NATS_SERVERS="${AGENT_NATS_SERVERS:-nats://nats.default.svc.cluster.local:4222}"
export NATS_JETSTREAM_DOMAIN="${NATS_JETSTREAM_DOMAIN:-hub}"
export NATS_CLOUD_JETSTREAM_DOMAIN="${NATS_CLOUD_JETSTREAM_DOMAIN:-hub}"
export AGENT_NATS_JETSTREAM_DOMAIN="${AGENT_NATS_JETSTREAM_DOMAIN:-hub}"
export NATS_STREAM_SUBJECTS="${NATS_STREAM_SUBJECTS:-workflow.>}"
export NATS_STREAM="${NATS_STREAM:-WORKFLOW}"
export NATS_STREAM_MAX_BYTES="${NATS_STREAM_MAX_BYTES:-5GB}"
export NATS_STREAM_DISCARD="${NATS_STREAM_DISCARD:-old}"
export NATS_STREAM_RETENTION="${NATS_STREAM_RETENTION:-limits}"
export NATS_STREAM_STORAGE="${NATS_STREAM_STORAGE:-file}"

# Default sandbox egress policy: deny all except NATS and host callback endpoints.
export OPENSANDBOX_NETWORK_DEFAULT_ACTION="${OPENSANDBOX_NETWORK_DEFAULT_ACTION:-deny}"
export OPENSANDBOX_ALLOWED_EGRESS="${OPENSANDBOX_ALLOWED_EGRESS:-nats.default.svc.cluster.local,host.minikube.internal}"
export OPENSANDBOX_SANDBOX_TIMEOUT_SECONDS="${OPENSANDBOX_SANDBOX_TIMEOUT_SECONDS:-3600}"

echo "[sandbox-server] ui=http://127.0.0.1:${PORT}/ui opensandbox=${OPENSANDBOX_SERVER_URL}"
echo "[sandbox-server] backend=${AGENT_DEPLOY_BACKEND} agent_nats=${AGENT_NATS_SERVERS} default_agent_js_domain=${AGENT_NATS_JETSTREAM_DOMAIN:-<local>} cloud_js_domain=${NATS_CLOUD_JETSTREAM_DOMAIN}"
if [[ -n "${OPENSANDBOX_ENTRYPOINT:-}" ]]; then
  echo "[sandbox-server] opensandbox_entrypoint=${OPENSANDBOX_ENTRYPOINT}"
fi
exec "${PYTHON:-python}" server.py
