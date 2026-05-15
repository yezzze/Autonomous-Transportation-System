#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
source "$ROOT_DIR/scripts/aoe_nats_port_forward.sh"

HOST_IP="${CLUSTER_A_HOST_IP:-$(ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')}"
PORT="${PORT:-8001}"
CLUSTER_B_AOE_URL="${CLUSTER_B_AOE_URL:-http://10.112.221.121:8001}"

MINIKUBE_IP="$(minikube ip 2>/dev/null || true)"
NO_PROXY_BASE="localhost,127.0.0.1,${HOST_IP},10.112.221.121,192.168.49.0/24"
if [[ -n "$MINIKUBE_IP" ]]; then
  NO_PROXY_BASE="${NO_PROXY_BASE},${MINIKUBE_IP}"
fi

export PYTHONPATH="$ROOT_DIR"
export USE_LLM_SIMULATOR="${USE_LLM_SIMULATOR:-true}"
export AGENT_DEPLOY_BACKEND="${AGENT_DEPLOY_BACKEND:-kubernetes}"
export K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
export PORT
export REMOTE_AOE_PORT="${REMOTE_AOE_PORT:-$PORT}"

# Cluster A local NATS. In separate Kubernetes clusters, both sides can keep the
# in-cluster service name as "nats"; the host AOE uses a distinct local tunnel.
export NATS_DEPLOYMENT_NAME="${NATS_DEPLOYMENT_NAME:-nats}"
export NATS_SERVICE_NAME="${NATS_SERVICE_NAME:-nats}"
export NATS_APP_LABEL="${NATS_APP_LABEL:-nats}"
export NATS_SERVERS="${NATS_SERVERS:-nats://127.0.0.1:14222}"
export AGENT_NATS_SERVERS="${AGENT_NATS_SERVERS:-nats://${NATS_SERVICE_NAME}:4222}"
export NATS_IMAGE="${NATS_IMAGE:-docker.m.daocloud.io/library/nats:2.10}"

# Keep Kubernetes/minikube and peer AOE traffic away from the local HTTP proxy.
export NO_PROXY="${NO_PROXY:-$NO_PROXY_BASE}"
export no_proxy="${no_proxy:-$NO_PROXY_BASE}"

export LOCAL_AOE_URL="${LOCAL_AOE_URL:-http://${HOST_IP}:${PORT}}"
export PEER_AOE_URLS="${PEER_AOE_URLS:-$CLUSTER_B_AOE_URL}"

echo "[cluster-a-aoe] root=$ROOT_DIR"
echo "[cluster-a-aoe] local=$LOCAL_AOE_URL"
echo "[cluster-a-aoe] peers=${PEER_AOE_URLS:-<none>}"
echo "[cluster-a-aoe] remote_aoe_port=$REMOTE_AOE_PORT"
echo "[cluster-a-aoe] aoe_nats=$NATS_SERVERS"
echo "[cluster-a-aoe] agent_nats=$AGENT_NATS_SERVERS deployment=$NATS_DEPLOYMENT_NAME service=$NATS_SERVICE_NAME"
echo "[cluster-a-aoe] no_proxy=$no_proxy"

trap cleanup_aoe_nats_port_forward EXIT INT TERM
start_aoe_nats_port_forward

"${PYTHON:-python}" server.py
