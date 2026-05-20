#!/usr/bin/env bash
# 云边模式：启动编排服务。NATS 由 Helm 部署，本脚本不再自建 NATS。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

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
export EDGE_CLUSTER_ID="${EDGE_CLUSTER_ID:-edge-a}"
export AUTO_NATS_PORT_FORWARD="${AUTO_NATS_PORT_FORWARD:-1}"

echo "[server] port=$PORT cluster=$EDGE_CLUSTER_ID nats=$NATS_SERVERS agent_nats=$AGENT_NATS_SERVERS"
exec "${PYTHON:-python}" server.py
