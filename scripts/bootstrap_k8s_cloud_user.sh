#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash K8S_demo/scripts/bootstrap_k8s_cloud_user.sh" >&2
  exit 1
fi

if ! id k8s_cloud >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G docker k8s_cloud
fi

install -d -o k8s_cloud -g k8s_cloud /home/k8s_cloud/Project
install -d -o k8s_cloud -g k8s_cloud /home/k8s_cloud/.local/bin

if [[ ! -e /home/k8s_cloud/Project/K8S_demo ]]; then
  ln -s /home/czl/Project/K8S_demo /home/k8s_cloud/Project/K8S_demo
fi

chown -h k8s_cloud:k8s_cloud /home/k8s_cloud/Project/K8S_demo

if [[ -x /home/czl/.local/bin/helm && ! -x /home/k8s_cloud/.local/bin/helm ]]; then
  install -m 0755 -o k8s_cloud -g k8s_cloud /home/czl/.local/bin/helm /home/k8s_cloud/.local/bin/helm
fi

echo "Created user k8s_cloud."
echo "Next:"
echo "  sudo -iu k8s_cloud"
echo "  bash ~/Project/K8S_demo/scripts/setup_cloud_minikube_nats_helm.sh"
