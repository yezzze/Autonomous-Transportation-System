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
  value: "hub"
- name: NATS_STREAM_SUBJECTS
  value: "workflow.>"
- name: CLUSTER_ID
  value: "${EDGE_CLUSTER_ID}"

# agent-grpc
- name: REQ_SUBJECT
  value: "workflow.${EDGE_CLUSTER_ID}.agent.b.in"
- name: REPLY_SUBJECT_PREFIX
  value: "workflow.${EDGE_CLUSTER_ID}.agent.grpc.reply"

# agent-b
- name: IN_SUBJECT
  value: "workflow.${EDGE_CLUSTER_ID}.agent.b.in"
- name: C_IN_SUBJECT
  value: "workflow.${EDGE_CLUSTER_ID}.agent.c.in"
- name: OUT_SUBJECT
  value: "workflow.${EDGE_CLUSTER_ID}.agent.grpc.reply.default"
- name: B_REPLY_PREFIX
  value: "workflow.${EDGE_CLUSTER_ID}.agent.b.c.reply"

# agent-c
- name: IN_SUBJECT
  value: "workflow.${EDGE_CLUSTER_ID}.agent.c.in"
EOF
