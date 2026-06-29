# Prometheus + Alertmanager 接入编排层

## 目标链路

```text
QoSMonitor
  -> /metrics/
Prometheus (ServiceMonitor)
  -> PrometheusRule
Alertmanager
  -> POST /api/orchestration/alerts
编排层策略控制器
  -> 扩缩容、迁移或重编排
```

分阶段时延采用“指标产生位置直接暴露”的方式：

```text
Agent Pod /metrics/          -> queue_wait、execution、server_total
编排服务 /metrics/           -> A2A total RTT、network residual
Prometheus                   -> 分位数聚合、趋势查询和告警
```

不要先将所有 Agent Pod 指标汇总到编排进程再由 Prometheus 抓取，否则会丢失
`instance_id` 维度，难以定位单个异常 Pod。

Prometheus 和 Alertmanager 由 `kube-prometheus-stack` 中的 Prometheus
Operator 管理。编排 API 只接收告警状态，不在 webhook 请求路径中直接扩容，
避免 Alertmanager 重试或重复通知造成扩容风暴。

Alertmanager 仅将带 `action=evaluate_scaling` 或 `action=investigate` 标签的
告警发送到编排层；监控栈自带的 Kubernetes 告警不会进入该接口。

## 前置条件

- Kubernetes 1.19+
- Helm 3+
- 编排 API 已部署到 Kubernetes，并存在名为 `orchestration-api` 的 Service
- 该 Service 位于 `default` namespace，带有标签
  `app.kubernetes.io/name: orchestration-api`
- Service 的 8000 端口名为 `http`

Service 示例，`selector` 必须改成编排 API Pod 的实际标签：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: orchestration-api
  namespace: default
  labels:
    app.kubernetes.io/name: orchestration-api
spec:
  selector:
    app.kubernetes.io/name: orchestration-api
  ports:
    - name: http
      port: 8000
      targetPort: 8000
```

如果 Service 名称、namespace 或端口不同，需要同步修改：

- `k8s/monitoring/kube-prometheus-stack-values.yaml` 中的 webhook URL
- `k8s/monitoring/orchestration-service-monitor.yaml`

## 安装监控栈

生产环境应在验证后固定 Chart 版本，避免升级时意外引入 CRD 变更。

```bash
helm upgrade --install monitoring \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --values k8s/monitoring/kube-prometheus-stack-values.yaml
```

确认 Operator、Prometheus 和 Alertmanager 已启动：

```bash
kubectl get pods -n monitoring
kubectl get crd servicemonitors.monitoring.coreos.com
kubectl get crd prometheusrules.monitoring.coreos.com
```

## 创建采集与告警规则

```bash
kubectl apply -f k8s/monitoring/orchestration-service-monitor.yaml
kubectl apply -f k8s/monitoring/qos-prometheus-rules.yaml
```

验证发现状态：

```bash
kubectl get servicemonitor,prometheusrule -n monitoring
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093
```

在 Prometheus Targets 页面确认 `orchestration-api` 为 `UP`，并查询：

```promql
histogram_quantile(
  0.95,
  sum by (le, agent_id, instance_id) (
    rate(a2a_total_latency_seconds_bucket[5m])
  )
)
```

## 验证 webhook

先直接模拟 Alertmanager 请求：

```bash
curl -X POST http://localhost:8000/api/orchestration/alerts \
  -H 'Content-Type: application/json' \
  -d '{
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "AgentP95LatencyHigh",
        "agent_id": "vision-agent",
        "action": "evaluate_scaling"
      },
      "annotations": {"summary": "test alert"},
      "fingerprint": "test-vision-agent"
    }]
  }'
```

查询编排层已接收的活动告警：

```bash
curl 'http://localhost:8000/api/orchestration/alerts?active_only=true'
```

## 下一步接入自动扩缩容

新增编排层 `AutoscalingController`，周期性消费活动告警并结合 Prometheus
查询结果、冷却期、资源预算和 RRDC 可用资源做决策。不要仅依据
`AgentP95LatencyHigh` 直接增加副本：

- 排队时延高且执行时延正常：横向扩容
- 单次执行慢且 GPU/CPU 饱和：纵向扩容或迁移
- 资源利用率低但时延高：优先检查网络、锁和外部依赖

当前指标只有总调用时延和成功率。实现自动扩容前，还应增加
`queue_wait_seconds`、`execution_seconds`、`instance_id` 与资源利用率指标。

当前告警接收器使用进程内状态，适合单副本验证。编排 API 多副本部署时，
应将告警状态持久化到 Redis 或数据库，并为 webhook 增加集群内鉴权或
NetworkPolicy。
