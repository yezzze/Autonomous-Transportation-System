# 共享 GPU 配置示例

这个示例适用于 NVIDIA device plugin 已经部署在集群中的情况。共享方式采用 time-slicing：让多个 Pod 共享同一张物理 GPU，但每个 Pod 在 Kubernetes 里仍然申请整数个 `nvidia.com/gpu`。

## 1. 集群侧：启用 GPU time-slicing

如果你的 NVIDIA device plugin 支持通过 ConfigMap 读取共享配置，可以使用下面的配置：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: nvidia-device-plugin-config
  namespace: kube-system
data:
  config.yaml: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
          - name: nvidia.com/gpu
            replicas: 4
```

含义：1 张 GPU 最多可被 4 个 Pod 共享。

注意：这个 ConfigMap 本身不会自动生效，device plugin 的 DaemonSet 还需要被配置为读取这个文件。常见做法是给 DaemonSet 挂载该 ConfigMap，并将启动参数或环境变量指向 `/etc/kubernetes/nvidia-device-plugin/config.yaml`。

## 2. 业务 Deployment：保持每个 Pod 申请 1 张 GPU

业务 Pod 侧不要写成 `0.5`，仍然写整数 `1`。如果你要让 4 个副本共享同一张 GPU，可以把 replicas 调高，并补上 CPU / 内存 requests 和 limits：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cooperativefeaturefusiondetectionviz-agent
spec:
  replicas: 4
  selector:
    matchLabels:
      app: cooperativefeaturefusiondetectionviz-agent
  template:
    metadata:
      labels:
        app: cooperativefeaturefusiondetectionviz-agent
    spec:
      containers:
        - name: cooperativefeaturefusiondetectionviz-agent
          image: cooperativefeaturefusiondetectionviz-agent:0.1.1
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 9002
              name: http
          env:
            - name: MODEL_CHECKPOINT_PATH
              value: /app/checkpoints/point_pillar_where2comm/
            - name: QT_GRAPHICSSYSTEM
              value: native
            - name: NATS_SERVER_URL
              value: nats://nats:4222
            - name: NATS_SUBJECT
              value: workflow.demo.perception2feature.result
            - name: NATS_DURABLE
              value: workflow-demo-perception2feature-result
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "1"
              memory: "2Gi"
              nvidia.com/gpu: "1"
```

如果你需要保留原来的 `Service`，可以不改 Service 定义。

## 3. 另一个示例：perception2intermediatefeature-agent

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: perception2intermediatefeature-agent
spec:
  replicas: 4
  selector:
    matchLabels:
      app: perception2intermediatefeature-agent
  template:
    metadata:
      labels:
        app: perception2intermediatefeature-agent
    spec:
      containers:
        - name: perception2intermediatefeature-agent
          image: perception2intermediatefeature-agent:0.1.1
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 9001
              name: http
          env:
            - name: MCP_SERVER_HOST
              value: host.minikube.internal
            - name: MCP_SERVER_PORT
              value: "8123"
            - name: MODEL_CHECKPOINT_PATH
              value: /app/checkpoints/point_pillar_where2comm/
            - name: FRONTEND_CALLBACK_URL
              value: http://host.minikube.internal:9002/temp/post_data
            - name: NATS_SERVER_URL
              value: nats://nats:4222
            - name: NATS_SUBJECT
              value: workflow.demo.perception2feature.result
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "1"
              memory: "2Gi"
              nvidia.com/gpu: "1"
```

## 4. 适合你的场景的结论

- 如果集群已经装了 NVIDIA device plugin，并开启 time-slicing，Pod 侧仍然申请 `nvidia.com/gpu: "1"`。
- 如果没有开启共享，多个 Pod 还是会因为 `Insufficient nvidia.com/gpu` 而 Pending。
- 共享 GPU 时，建议同时设置 CPU / 内存 requests 和 limits，否则 Pod 会落成 BestEffort，调度和稳定性都比较差。