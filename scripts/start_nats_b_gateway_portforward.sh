#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MINIKUBE_IP="$(minikube ip 2>/dev/null || true)"
NO_PROXY_BASE="localhost,127.0.0.1,192.168.49.0/24"
if [[ -n "$MINIKUBE_IP" ]]; then
  NO_PROXY_BASE="${NO_PROXY_BASE},${MINIKUBE_IP}"
fi

export NO_PROXY="${NO_PROXY:-$NO_PROXY_BASE}"
export no_proxy="${no_proxy:-$NO_PROXY_BASE}"

exec kubectl port-forward --address 0.0.0.0 svc/nats-b 7222:7222
