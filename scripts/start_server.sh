#!/usr/bin/env bash
# 云边模式：启动编排服务。NATS 由 Helm 部署，本脚本不再自建 NATS。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 复用 K8S 项目的本地集群配置（scripts/local/cluster.env，不提交 git）。
# 可通过 CLUSTER_ENV_FILE=/path/to/cluster.env 覆盖；也可直接传 EDGE_CLUSTER_ID 覆盖。
K8S_CONFIG_LOADER="${K8S_CONFIG_LOADER:-${ROOT_DIR}/../K8S/scripts/lib/load_cluster_env.sh}"
if [[ -f "$K8S_CONFIG_LOADER" ]]; then
  # shellcheck disable=SC1090
  source "$K8S_CONFIG_LOADER"
else
  echo "[server] warn: cluster config loader not found: $K8S_CONFIG_LOADER" >&2
fi

export PYTHONPATH="$ROOT_DIR"
export AGENT_DEPLOY_BACKEND="${AGENT_DEPLOY_BACKEND:-kubernetes}"
export K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
export PORT="${PORT:-8000}"

# UI/编排服务连本机 port-forward；集群内 Agent 连 svc/nats
export NATS_SERVICE_NAME="${NATS_SERVICE_NAME:-nats}"
export NATS_SERVERS="${NATS_SERVERS:-nats://127.0.0.1:4222}"
export AGENT_NATS_SERVERS="${AGENT_NATS_SERVERS:-nats://${NATS_SERVICE_NAME}:4222}"
export NATS_JETSTREAM_DOMAIN="${NATS_JETSTREAM_DOMAIN:-hub}"
export NATS_STREAM_SUBJECTS="${NATS_STREAM_SUBJECTS:-workflow.>}"
export AUTO_NATS_PORT_FORWARD="${AUTO_NATS_PORT_FORWARD:-1}"
if [[ -z "${PYTHON:-}" && -x "${HOME}/anaconda3/envs/k8s/bin/python" ]]; then
  PYTHON="${HOME}/anaconda3/envs/k8s/bin/python"
fi

if [[ "$AUTO_NATS_PORT_FORWARD" =~ ^(1|true|yes|on)$ ]]; then
  pkill -f "kubectl port-forward --address 127.0.0.1 -n ${K8S_NAMESPACE} svc/${NATS_SERVICE_NAME}" 2>/dev/null || true
fi

echo "[server] port=$PORT cluster=$EDGE_CLUSTER_ID local_cluster=${LOCAL_CLUSTER:-unknown} nats=$NATS_SERVERS agent_nats=$AGENT_NATS_SERVERS python=${PYTHON:-python}"
if [[ "${1:-}" == "--print-env" ]]; then
  exit 0
fi

exec "${PYTHON:-python}" server.py
