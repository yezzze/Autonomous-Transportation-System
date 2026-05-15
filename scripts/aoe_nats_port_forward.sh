#!/usr/bin/env bash

_AOE_NATS_PORT_FORWARD_PID=""
_AOE_NATS_PORT_FORWARD_LOG=""

_aoe_nats_local_port() {
  local servers="${NATS_SERVERS:-}"
  if [[ "$servers" =~ ^nats://(127\.0\.0\.1|localhost):([0-9]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

_aoe_port_open() {
  local port="$1"
  timeout 1 bash -c "</dev/tcp/127.0.0.1/${port}" >/dev/null 2>&1
}

cleanup_aoe_nats_port_forward() {
  if [[ -n "${_AOE_NATS_PORT_FORWARD_PID:-}" ]]; then
    kill "$_AOE_NATS_PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$_AOE_NATS_PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi
}

start_aoe_nats_port_forward() {
  if [[ "${AUTO_NATS_PORT_FORWARD:-1}" =~ ^(0|false|no|off)$ ]]; then
    echo "[aoe-nats] auto port-forward disabled"
    return 0
  fi

  local local_port
  if ! local_port="$(_aoe_nats_local_port)"; then
    echo "[aoe-nats] NATS_SERVERS=${NATS_SERVERS:-<unset>} is not local; skip port-forward"
    return 0
  fi

  if _aoe_port_open "$local_port"; then
    echo "[aoe-nats] 127.0.0.1:${local_port} already open; reuse existing NATS tunnel"
    return 0
  fi

  if ! command -v kubectl >/dev/null 2>&1; then
    echo "[aoe-nats] kubectl not found; cannot create NATS port-forward" >&2
    return 1
  fi

  kubectl get "svc/${NATS_SERVICE_NAME}" -n "${K8S_NAMESPACE:-default}" >/dev/null

  _AOE_NATS_PORT_FORWARD_LOG="${TMPDIR:-/tmp}/aoe-${NATS_SERVICE_NAME}-${local_port}-port-forward.log"
  echo "[aoe-nats] forwarding: 127.0.0.1:${local_port} -> svc/${NATS_SERVICE_NAME}:4222"
  echo "[aoe-nats] starting: kubectl port-forward -n ${K8S_NAMESPACE:-default} svc/${NATS_SERVICE_NAME} ${local_port}:4222"
  kubectl port-forward \
    --address 127.0.0.1 \
    -n "${K8S_NAMESPACE:-default}" \
    "svc/${NATS_SERVICE_NAME}" \
    "${local_port}:4222" \
    >"$_AOE_NATS_PORT_FORWARD_LOG" 2>&1 &
  _AOE_NATS_PORT_FORWARD_PID="$!"

  for _ in {1..40}; do
    if _aoe_port_open "$local_port"; then
      echo "[aoe-nats] ready: AOE uses ${NATS_SERVERS}; in-cluster agents use ${AGENT_NATS_SERVERS:-nats://${NATS_SERVICE_NAME}:4222}"
      return 0
    fi
    if ! kill -0 "$_AOE_NATS_PORT_FORWARD_PID" >/dev/null 2>&1; then
      echo "[aoe-nats] port-forward exited early; log follows:" >&2
      cat "$_AOE_NATS_PORT_FORWARD_LOG" >&2 || true
      return 1
    fi
    sleep 0.25
  done

  echo "[aoe-nats] timeout waiting for 127.0.0.1:${local_port}; log follows:" >&2
  cat "$_AOE_NATS_PORT_FORWARD_LOG" >&2 || true
  return 1
}
