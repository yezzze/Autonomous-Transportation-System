#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/load_cluster_env.sh
source "${SCRIPT_DIR}/lib/load_cluster_env.sh"

cat <<EOF
# Common NATS runtime env
- name: NATS_SERVERS
  value: "nats://nats:4222"
- name: NATS_JETSTREAM_DOMAIN
  value: "${EDGE_CLUSTER_ID}"
- name: NATS_WORKFLOW_STREAM_PREFIX
  value: "WF"
- name: NATS_STREAM_MAX_BYTES
  value: "${NATS_STREAM_MAX_BYTES:-512MiB}"
- name: NATS_STREAM_DISCARD
  value: "${NATS_STREAM_DISCARD:-new}"
- name: CLUSTER_ID
  value: "${EDGE_CLUSTER_ID}"

# Agent 自身实例 ID 使用 Downward API
- name: AGENT_INSTANCE_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.uid

# agent-grpc -> agent-b（由编排器填入目标 Pod UID）
- name: TARGET_B_CLUSTER_ID
  value: "${EDGE_CLUSTER_ID}"
- name: TARGET_B_AGENT_ID
  value: "b"
- name: TARGET_B_INSTANCE_ID
  value: "${TARGET_B_INSTANCE_ID:-<agent-b-pod-uid>}"

# agent-b -> agent-c（由编排器填入目标 Pod UID）
- name: TARGET_C_CLUSTER_ID
  value: "${EDGE_CLUSTER_ID}"
- name: TARGET_C_AGENT_ID
  value: "c"
- name: TARGET_C_INSTANCE_ID
  value: "${TARGET_C_INSTANCE_ID:-<agent-c-pod-uid>}"
EOF
