# Cluster A AOE Deployment

本机集群 B：
- AOE: `http://10.112.221.121:8001`
- NATS Service: `nats-b`

对端集群 A：
- AOE: `http://10.112.136.44:8001`
- NATS Service: `nats-a`

## 1. 同步代码

在集群 A 机器上同步两个目录：

```bash
cd /home/t/mydisk/czl/K8S-Autonomous
git pull

cd /home/t/mydisk/czl/K8S
git pull
```

如果不是 git 同步，确保至少包含这些文件：

```text
K8S-Autonomous/src/api/app.py
K8S-Autonomous/scripts/start_cluster_a_aoe.sh
K8S-Autonomous/.gitignore
K8S/k8s/nats-a.yaml
```

## 2. 确认 minikube 和 kubectl

```bash
hash -r
minikube status
kubectl version --client
```

如果 minikube 没启动：

```bash
minikube start \
  --driver=docker \
  --image-mirror-country=cn \
  --image-repository=registry.cn-hangzhou.aliyuncs.com/google_containers
```

如果当前 shell 有代理，先绕过 minikube 内网：

```bash
export NO_PROXY="localhost,127.0.0.1,10.112.136.44,10.112.221.121,192.168.49.0/24,$(minikube ip)"
export no_proxy="$NO_PROXY"
```

验证：

```bash
kubectl get nodes -o wide
kubectl get pods -A
```

## 3. 部署集群 A 本地 NATS

```bash
cd /home/t/mydisk/czl/K8S
kubectl apply -f k8s/nats-a.yaml
kubectl rollout status deployment/nats-a --timeout=180s
kubectl get deploy,svc,pods -o wide
```

## 4. 启动集群 A AOE

前台启动，便于第一次看日志：

```bash
cd /home/t/mydisk/czl/K8S-Autonomous
conda activate k8s
CLUSTER_A_HOST_IP=10.112.136.44 \
CLUSTER_B_AOE_URL=http://10.112.221.121:8001 \
PYTHON=/home/t/anaconda3/envs/k8s/bin/python \
./scripts/start_cluster_a_aoe.sh
```

确认没问题后，可以后台启动：

```bash
cd /home/t/mydisk/czl/K8S-Autonomous
mkdir -p logs
setsid env \
  CLUSTER_A_HOST_IP=10.112.136.44 \
  CLUSTER_B_AOE_URL=http://10.112.221.121:8001 \
  PYTHON=/home/t/anaconda3/envs/k8s/bin/python \
  ./scripts/start_cluster_a_aoe.sh > logs/cluster-a-aoe.log 2>&1 < /dev/null &
echo $! > logs/cluster-a-aoe.pid
```

## 5. 验证互通

在集群 A：

```bash
curl http://127.0.0.1:8001/docs
curl http://10.112.221.121:8001/docs
curl -X POST http://10.112.221.121:8001/registry/sync \
  -H 'Content-Type: application/json' \
  -d '{"source_url":"http://10.112.136.44:8001","agents":[]}'
```

在集群 B：

```bash
curl http://10.112.136.44:8001/docs
tail -f /home/t/mydisk/czl/K8S-Autonomous/logs/cluster-b-aoe.log
```

两边日志中应看到：

```text
[ARDC Gossip] 推送到 ... 成功
```

## 6. 停止 AOE

集群 A：

```bash
kill $(cat /home/t/mydisk/czl/K8S-Autonomous/logs/cluster-a-aoe.pid)
```

集群 B：

```bash
kill $(cat /home/t/mydisk/czl/K8S-Autonomous/logs/cluster-b-aoe.pid)
```
