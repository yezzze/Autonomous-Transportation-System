#!/usr/bin/env bash

set -euo pipefail

namespace="default"

read -r -p "请输入要删除的 Pod 名称: " pod_name

if [[ -z "${pod_name}" ]]; then
  echo "Pod 名称不能为空。" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "未找到 kubectl，请先安装并配置 kubectl。" >&2
  exit 1
fi

owner_kind=""
owner_name=""
service_name="${pod_name}"

if kubectl get pod "${pod_name}" -n "${namespace}" >/dev/null 2>&1; then
  owner_kind=$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || true)
  owner_name=$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.metadata.ownerReferences[0].name}' 2>/dev/null || true)
fi

if [[ -n "${owner_kind}" && -n "${owner_name}" ]]; then
  echo "检测到控制器: ${owner_kind}/${owner_name}"
  case "${owner_kind}" in
    ReplicaSet)
      rs_owner_kind=$(kubectl get rs "${owner_name}" -n "${namespace}" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || true)
      rs_owner_name=$(kubectl get rs "${owner_name}" -n "${namespace}" -o jsonpath='{.metadata.ownerReferences[0].name}' 2>/dev/null || true)

      if [[ "${rs_owner_kind}" == "Deployment" && -n "${rs_owner_name}" ]]; then
        service_name="${rs_owner_name}"
        echo "删除 Deployment: ${rs_owner_name}"
        kubectl delete deployment "${rs_owner_name}" -n "${namespace}" --grace-period=0 --force
      else
        service_name="${owner_name}"
        echo "删除 ReplicaSet: ${owner_name}"
        kubectl delete rs "${owner_name}" -n "${namespace}" --grace-period=0 --force
      fi
      ;;
    Deployment)
      service_name="${owner_name}"
      echo "删除 Deployment: ${owner_name}"
      kubectl delete deployment "${owner_name}" -n "${namespace}" --grace-period=0 --force
      ;;
    DaemonSet)
      service_name="${owner_name}"
      echo "删除 DaemonSet: ${owner_name}"
      kubectl delete daemonset "${owner_name}" -n "${namespace}" --grace-period=0 --force
      ;;
    StatefulSet)
      service_name="${owner_name}"
      echo "删除 StatefulSet: ${owner_name}"
      kubectl delete statefulset "${owner_name}" -n "${namespace}" --grace-period=0 --force
      ;;
    *)
      echo "未识别的控制器类型: ${owner_kind}/${owner_name}，仅删除 Pod。"
      ;;
  esac
fi

echo "正在删除对应的 Service: ${service_name} (namespace=${namespace})"
kubectl delete service "${service_name}" -n "${namespace}" --ignore-not-found

echo "正在删除 Pod: ${pod_name} (namespace=${namespace})"
kubectl delete pod "${pod_name}" -n "${namespace}" --grace-period=0 --force
