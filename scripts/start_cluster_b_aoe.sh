#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HOST_IP="${CLUSTER_B_HOST_IP:-$(ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')}"
PORT="${PORT:-8001}"
CLUSTER_A_AOE_URL="${CLUSTER_A_AOE_URL:-}"
CLUSTER_A_HOST="$(printf '%s' "$CLUSTER_A_AOE_URL" | sed -E 's#^[a-zA-Z]+://([^/:]+).*#\1#')"

MINIKUBE_IP="$(minikube ip 2>/dev/null || true)"
NO_PROXY_BASE="localhost,127.0.0.1,${HOST_IP},192.168.49.0/24"
if [[ -n "$CLUSTER_A_HOST" && "$CLUSTER_A_HOST" != "$CLUSTER_A_AOE_URL" ]]; then
  NO_PROXY_BASE="${NO_PROXY_BASE},${CLUSTER_A_HOST}"
fi
if [[ -n "$MINIKUBE_IP" ]]; then
  NO_PROXY_BASE="${NO_PROXY_BASE},${MINIKUBE_IP}"
fi

export PYTHONPATH="$ROOT_DIR"
export USE_LLM_SIMULATOR="${USE_LLM_SIMULATOR:-true}"
export AGENT_DEPLOY_BACKEND="${AGENT_DEPLOY_BACKEND:-kubernetes}"
export K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
export PORT
export REMOTE_AOE_PORT="${REMOTE_AOE_PORT:-$PORT}"

# Cluster B local NATS. Agent pods use this in-cluster Service name.
export NATS_DEPLOYMENT_NAME="${NATS_DEPLOYMENT_NAME:-nats-b}"
export NATS_SERVICE_NAME="${NATS_SERVICE_NAME:-nats-b}"
export NATS_APP_LABEL="${NATS_APP_LABEL:-nats-b}"
export NATS_SERVERS="${NATS_SERVERS:-nats://nats-b:4222}"
export NATS_IMAGE="${NATS_IMAGE:-docker.m.daocloud.io/library/nats:2.10}"

# Keep Kubernetes/minikube traffic away from the local HTTP proxy.
export NO_PROXY="${NO_PROXY:-$NO_PROXY_BASE}"
export no_proxy="${no_proxy:-$NO_PROXY_BASE}"

export LOCAL_AOE_URL="${LOCAL_AOE_URL:-http://${HOST_IP}:${PORT}}"
if [[ -n "$CLUSTER_A_AOE_URL" ]]; then
  export PEER_AOE_URLS="${PEER_AOE_URLS:-$CLUSTER_A_AOE_URL}"
else
  export PEER_AOE_URLS="${PEER_AOE_URLS:-}"
fi

echo "[cluster-b-aoe] root=$ROOT_DIR"
echo "[cluster-b-aoe] local=$LOCAL_AOE_URL"
echo "[cluster-b-aoe] peers=${PEER_AOE_URLS:-<none>}"
echo "[cluster-b-aoe] remote_aoe_port=$REMOTE_AOE_PORT"
echo "[cluster-b-aoe] nats=$NATS_SERVERS deployment=$NATS_DEPLOYMENT_NAME service=$NATS_SERVICE_NAME"
echo "[cluster-b-aoe] no_proxy=$no_proxy"

exec "${PYTHON:-python}" server.py
