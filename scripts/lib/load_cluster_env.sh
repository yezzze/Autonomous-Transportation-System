#!/usr/bin/env bash
# 加载 scripts/local/cluster.env 并导出集群 A/B/C 与当前边缘集群变量。
# 用法: source "$(dirname "$0")/lib/load_cluster_env.sh"

_cluster_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_cluster_scripts_dir="$(cd "${_cluster_lib_dir}/.." && pwd)"
CLUSTER_ENV_FILE="${CLUSTER_ENV_FILE:-${_cluster_scripts_dir}/local/cluster.env}"

if [[ ! -f "${CLUSTER_ENV_FILE}" ]]; then
  echo "缺少本地配置: ${CLUSTER_ENV_FILE}" >&2
  echo "请执行: cp scripts/local/cluster.env.example scripts/local/cluster.env" >&2
  echo "并按本机所属集群编辑 LOCAL_CLUSTER、CLUSTER_*_HOST 等。" >&2
  return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1090
source "${CLUSTER_ENV_FILE}"

LOCAL_CLUSTER="${LOCAL_CLUSTER:?请在 cluster.env 中设置 LOCAL_CLUSTER=a、b 或 c}"
LOCAL_CLUSTER="${LOCAL_CLUSTER,,}"

: "${CLUSTER_A_HOST:?请在 cluster.env 中设置 CLUSTER_A_HOST}"
: "${CLUSTER_B_HOST:?请在 cluster.env 中设置 CLUSTER_B_HOST}"
: "${CLUSTER_C_HOST:?请在 cluster.env 中设置 CLUSTER_C_HOST}"
CLUSTER_A_EDGE_ID="${CLUSTER_A_EDGE_ID:-edge-a}"
CLUSTER_B_EDGE_ID="${CLUSTER_B_EDGE_ID:-edge-b}"
CLUSTER_C_EDGE_ID="${CLUSTER_C_EDGE_ID:-edge-c}"
NATS_CLOUD_PASSWORD="${NATS_CLOUD_PASSWORD:?请在 cluster.env 中设置 NATS_CLOUD_PASSWORD}"

case "${LOCAL_CLUSTER}" in
  a)
    EDGE_CLUSTER_ID="${EDGE_CLUSTER_ID:-${CLUSTER_A_EDGE_ID}}"
    LOCAL_HOST="${CLUSTER_A_HOST}"
    PEER_HOSTS="${CLUSTER_B_HOST},${CLUSTER_C_HOST}"
  ;;
  b)
    EDGE_CLUSTER_ID="${EDGE_CLUSTER_ID:-${CLUSTER_B_EDGE_ID}}"
    LOCAL_HOST="${CLUSTER_B_HOST}"
    PEER_HOSTS="${CLUSTER_A_HOST},${CLUSTER_C_HOST}"
  ;;
  c)
    EDGE_CLUSTER_ID="${EDGE_CLUSTER_ID:-${CLUSTER_C_EDGE_ID}}"
    LOCAL_HOST="${CLUSTER_C_HOST}"
    PEER_HOSTS="${CLUSTER_A_HOST},${CLUSTER_B_HOST}"
  ;;
  *)
    echo "LOCAL_CLUSTER 必须是 a、b 或 c，当前: ${LOCAL_CLUSTER}" >&2
    return 1 2>/dev/null || exit 1
  ;;
esac

PEER_HOST="${PEER_HOSTS%%,*}"
NATS_CLOUD_HOST="${NATS_CLOUD_HOST:-${CLUSTER_A_HOST}}"

export LOCAL_CLUSTER EDGE_CLUSTER_ID LOCAL_HOST PEER_HOST PEER_HOSTS
export CLUSTER_A_HOST CLUSTER_B_HOST CLUSTER_C_HOST
export CLUSTER_A_EDGE_ID CLUSTER_B_EDGE_ID CLUSTER_C_EDGE_ID
export NATS_CLOUD_HOST NATS_CLOUD_PASSWORD
