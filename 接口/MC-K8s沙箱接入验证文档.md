# MC 外置 + K8s 沙箱 Agent 接入验证文档

> 对应设计：`接口/智能体编排层接口流程v2.md`
> 当前验证模板：`k8s/mc-emptydir-mock-agent-job.yaml`
> MC 实现：`src/sdk/mc_service.py` + `src/sdk/mc_router.py`

---

## 1. 当前结论

当前采用 **MC 不进 K8s，Agent 进入 K8s 沙箱** 的方案。

```text
设备本体 / K8s 外:
  FastAPI 主服务
  MC 记忆中心
  data/memory-store/
  data/memory-bundles/

K8s / 沙箱内:
  Job 或 Pod
    initContainer: 下载记忆包
    agent container: 执行业务 Agent
    sidecar: 上传 output
    emptyDir: /sandbox/memory
```

验证结果已经跑通：

```text
MC 创建 bundle
initContainer 下载 bundle.tar.gz
Agent 在 /sandbox/memory 读取 input/local 并写 output
sidecar 上传 output
MC commit 写回长期记忆库
```

---

## 2. 为什么 MC 不放进 K8s

MC 是编排层控制面组件，不是业务 Agent。它管理长期记忆、记忆权限、bundle 生命周期和写回策略。

Agent Pod 只应该看到自己的沙箱目录：

```text
/sandbox/memory/
  manifest.json
  input/
  local/
  output/
```

Agent 不应该直接访问：

```text
data/memory-store/
data/memory-bundles/
```

因此当前边界是：

```text
MC 跟 AOE/AWM/ASD 走控制面
Agent 跟 K8s 走运行面
```

---

## 3. 完整执行流程

### 3.1 编排层创建记忆包

AOE/AWM 在部署每个 Agent 实例前调用 MC：

```http
POST /memory/bundles/create
```

请求中必须带唯一的 `agent_instance_id`：

```json
{
  "task_id": "task-a",
  "agent_id": "agent-a",
  "agent_instance_id": "agent-a-inst-001",
  "workflow_id": "wf-001",
  "device_id": "device-local",
  "user_id": "user-demo"
}
```

MC 在本机生成专属目录：

```text
data/memory-bundles/{workflow_id}/{agent_instance_id}/
  manifest.json
  input/
  local/
  output/
```

并返回：

```json
{
  "bundle_id": "mb_xxx",
  "memory_mount_spec": {
    "bundle_id": "mb_xxx",
    "mount_path": "/sandbox/memory",
    "input_mode": "readonly",
    "output_mode": "collect",
    "collector": "sidecar",
    "source_path": "data/memory-bundles/wf-001/agent-a-inst-001"
  }
}
```

其中 `source_path` 只用于 MC 本机调试。K8s Pod 不直接使用这个路径。

### 3.2 ASD 创建 K8s Job/Pod

ASD 根据 `bundle_id` 创建 K8s Job 或 Pod，并注入：

```text
MC_API_URL=http://host.minikube.internal:8000
BUNDLE_ID=mb_xxx
```

Pod 内包含：

```text
emptyDir volume: memory

initContainer:
  fetch-memory-bundle

containers:
  agent
  outbox-sidecar
```

### 3.3 initContainer 下载 bundle

initContainer 调用：

```http
GET /memory/bundles/{bundle_id}/archive
```

MC 将 bundle 目录打成 `tar.gz` 返回，内容包括：

```text
manifest.json
input/
local/
```

不包含 `output/`，因为 `output/` 是 Agent 执行后才生成的。

initContainer 解压到：

```text
/sandbox/memory
```

这个路径是 Pod 内的 `emptyDir`。

### 3.4 Agent 执行

Agent 容器只读：

```text
/sandbox/memory/manifest.json
/sandbox/memory/input/
/sandbox/memory/local/
```

Agent 写：

```text
/sandbox/memory/output/result.json
/sandbox/memory/output/execution_notes.md
/sandbox/memory/output/writeback_to_local.jsonl
/sandbox/memory/output/writeback_to_caller.jsonl
/sandbox/memory/output/artifacts/
/sandbox/memory/output/.done
```

`.done` 是 sidecar 的完成信号。

### 3.5 sidecar 上传 output

sidecar 等待：

```text
/sandbox/memory/output/.done
```

然后读取 output，调用：

```http
POST /memory/bundles/{bundle_id}/upload_outbox
```

随后调用：

```http
POST /memory/bundles/{bundle_id}/commit
```

MC 将候选记忆写回：

```text
data/memory-store/agent_memory/{agent_id}/
data/memory-store/workflow_summary/
```

---

## 4. 并发隔离规则

并发启动多个 Agent Job 不会混，只要遵守三条规则：

```text
1. 每个 Agent 实例必须有唯一 agent_instance_id
2. 每个 Agent 实例必须有唯一 bundle_id
3. 每个 Job/Pod 只注入自己的 BUNDLE_ID
```

示例：

```text
Job A:
  agent_instance_id = agent-a-inst-001
  bundle_id = mb_a
  MC 目录 = data/memory-bundles/wf1/agent-a-inst-001
  Pod 内路径 = /sandbox/memory

Job B:
  agent_instance_id = agent-b-inst-001
  bundle_id = mb_b
  MC 目录 = data/memory-bundles/wf1/agent-b-inst-001
  Pod 内路径 = /sandbox/memory
```

两个 Pod 内都叫 `/sandbox/memory`，但它们是不同 Pod 的不同 `emptyDir`，互相不可见。

不能这样做：

```text
多个 Job 共用同一个 bundle_id
多个 Job 共用同一个 agent_instance_id
多个并发 Job 共用同一个固定 Job name
```

---

## 5. Job、Pod、Deployment 的关系

当前验证使用 Job，因为它适合一次性任务：

```text
Job -> 创建 Pod -> Pod 内运行 Agent
```

它仍然是 Agent，只是由 Job 控制器创建和管理 Pod。

选择建议：

| 形态 | 适用场景 | 说明 |
|------|----------|------|
| Pod | 手工调试 | 不自动重建 |
| Job | 一次任务一个 Agent 实例 | 最符合当前 MemoryBundle 隔离模型 |
| Deployment | 常驻 Agent 服务 | 需要额外设计每次任务的 bundle/session 隔离 |

当前 MC v2 流程推荐先走：

```text
一个 task / agent_instance -> 一个 MemoryBundle -> 一个 Job/Pod -> 一个 /sandbox/memory
```

---

## 6. 当前验证资源

### 6.1 MC 新增接口

```http
GET /memory/bundles/{bundle_id}/archive
```

用途：

```text
initContainer 下载 bundle tar.gz
```

实现位置：

```text
src/sdk/mc_router.py
```

### 6.2 验证镜像

当前已创建并加载到 minikube：

```text
mc-mock-agent:local
```

这个镜像基于本地已有的：

```text
agent-template:0.1.1
```

它包含 Python 标准库，足够执行下载、解压、模拟 Agent 和 sidecar 上传。

### 6.3 验证模板

```text
k8s/mc-emptydir-mock-agent-job.yaml
```

模板内包含：

```text
fetch-memory-bundle
mock-agent
outbox-sidecar
emptyDir: memory
```

---

## 7. 复测步骤

### 7.1 启动 MC

```bash
cd /home/czl/Project/Autonomous-Transportation-System-sandbox
python3.12 -m uvicorn src.api.app:app --host 0.0.0.0 --port 8000
```

### 7.2 确认 K8s 能访问 MC

当前环境有代理变量时，建议去掉代理执行 kubectl：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
kubectl run mc-net-test --rm -i \
  --image=mc-mock-agent:local \
  --image-pull-policy=IfNotPresent \
  --restart=Never -- \
  python -c 'import urllib.request; print(urllib.request.urlopen("http://host.minikube.internal:8000/docs", timeout=8).status)'
```

期望输出：

```text
200
```

### 7.3 创建 MemoryBundle

```bash
curl -s -X POST http://127.0.0.1:8000/memory/bundles/create \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "k8s-emptydir-smoke",
    "agent_id": "mock-k8s-agent",
    "agent_instance_id": "mock-k8s-agent-001",
    "workflow_id": "wf-k8s-emptydir",
    "device_id": "dev-local",
    "user_id": "user-demo"
  }'
```

记录返回的：

```text
data.bundle_id
```

### 7.4 部署验证 Job

不要直接提交带 `REPLACE_BUNDLE_ID` 的模板。可以用管道替换：

```bash
export BUNDLE_ID=mb_xxx

perl -pe "s/REPLACE_BUNDLE_ID/${BUNDLE_ID}/g" \
  k8s/mc-emptydir-mock-agent-job.yaml | \
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  kubectl apply -f -
```

如果重复运行，先删除旧 Job：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
kubectl delete job mc-emptydir-mock-agent --ignore-not-found
```

### 7.5 等待完成

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
kubectl wait --for=condition=complete job/mc-emptydir-mock-agent --timeout=180s
```

### 7.6 查看日志

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
kubectl logs job/mc-emptydir-mock-agent -c fetch-memory-bundle

env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
kubectl logs job/mc-emptydir-mock-agent -c mock-agent

env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
kubectl logs job/mc-emptydir-mock-agent -c outbox-sidecar
```

期望看到：

```text
memory bundle fetched
mock agent done
Outbox 上传成功
提交成功
```

### 7.7 查询写回结果

```bash
curl -s -X POST http://127.0.0.1:8000/memory/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "K8s emptyDir",
    "agent_id": "mock-k8s-agent"
  }'
```

期望：

```text
data.total >= 1
```

---

## 8. 真实接入 ASD 时需要动态生成的字段

当前验证模板是静态 YAML。正式接入时，ASD 需要动态生成：

```text
job_name
agent_instance_id
bundle_id
MC_API_URL
agent image
task/env/resource config
```

建议规则：

```text
job_name = safe("agent-" + workflow_id + "-" + task_id + "-" + agent_instance_id)
BUNDLE_ID = create_bundle 返回的 bundle_id
AGENT_INSTANCE_ID = create_bundle 请求中的 agent_instance_id
MC_API_URL = 当前设备本体 MC 的可访问地址
```

正式部署对象中仍然保留同样的结构：

```text
emptyDir
initContainer
agent container
outbox sidecar
```

---

## 9. 已验证结果

当前环境中已经成功验证过一次：

```text
bundle_id = mb_c749d953572d
job = mc-emptydir-mock-agent
pod status = Completed
```

日志结果：

```text
fetch-memory-bundle:
  memory bundle fetched: mb_c749d953572d

mock-agent:
  agent manifest: {...}
  agent task: # Task: k8s-emptydir-smoke
  mock agent done

outbox-sidecar:
  {"code":0,"message":"Outbox 上传成功"}
  {"code":0,"message":"提交成功"}
  outbox uploaded
```

MC 查询结果：

```text
content = K8s emptyDir + initContainer + sidecar 已验证 MC 闭环
total = 1
```
