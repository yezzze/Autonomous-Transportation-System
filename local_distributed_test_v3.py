#!/usr/bin/env python3
"""协作感知分布式端到端测试脚本（Collaborator + Ego + NATS）。

测试核心流程：
    Step 1. 启动 NATS JetStream 消息中间件（端口 4223，单包上限 4MB）
           —— 负责两个智能体之间的高斯特征数据传输。
    Step 2. 分别启动两个 Agent（均基于 FastAPI/Uvicorn）：
              Collaborator 端口 9001  →  GET /.well-known/agent-card.json
              Ego          端口 9002  →  GET /.well-known/agent-card.json
           轮询 agent-card 接口直到返回 200，确认服务就绪后再往下走。
    Step 3. 触发 Collaborator（发送方）执行基准推理
           ——  POST http://localhost:9001/  （A2A JSON-RPC / SendMessage）
           Collaborator 内部调用 model_runtime.run_benchmark_sync()，
           逐帧把高斯特征通过 NATS 发布到目标 Ego 的 Workflow Stream。
    Step 4. 触发 Ego（接收方）执行协作融合推理
           ——  POST http://localhost:9002/  （A2A JSON-RPC / SendMessage）
           Ego 内部调用 model_runtime.run_benchmark()，从同一 NATS Stream
           拉取 Collaborator 的数据，与自身特征融合后计算分割 mIoU。
    Step 5. 依次关闭 Ego → Collaborator → NATS，输出最终 PASS/FAIL 结论。
"""

import os
import sys
import time
import json
import socket
import shutil
import subprocess
import requests
import atexit
import signal


def wait_for_service(url, timeout=30):
    """轮询 HTTP 服务直到返回 200，用于确认 Agent 的 FastAPI 服务已启动完毕。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"Service at {url} is up!")
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    print(f"Timeout waiting for service at {url}")
    return False


def trigger_a2a_task(agent_url, parameters):
    """
    通过 A2A JSON-RPC 协议向指定 Agent 发送 "SendMessage" 指令。
    对应 Agent 的入口：fast_api/app.py 中 SendMessage 路由 → execute() → agent_function()。

    返回的 JSON 结构里：
        result.task.status.state   →  TASK_STATE_COMPLETED / TASK_STATE_FAILED
        result.task.artifacts[0].parts[0].text
            → 由 Agent 内部 model_runtime 构建的指标字典（status/mean_latency_ms/mIoU 等），
               经 json.dumps 序列化后作为 A2A artifact 文本返回。
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "test-1",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "data": {
                            "parameters": parameters
                        }
                    }
                ]
            }
        },
        "id": 1
    }
    
    print(f"\nSending A2A JSON-RPC to {agent_url}:")
    print(json.dumps(payload, indent=2))
    headers = {"A2A-Version": "1.0"}
    # —— 实际调用 Agent 的 POST / 接口（A2A SendMessage） ——
    resp = requests.post(agent_url, json=payload, headers=headers)
    print(f"Response ({resp.status_code}):")
    result = resp.json()
    # 这里会把包含 mIoU、mean_latency_ms 的 artifact 文本整段打印出来
    print(json.dumps(result, indent=2))
    return result


def start_nats():
    """Step 1：启动 NATS JetStream 服务（端口 4223，max_payload=4MB）。"""
    print("=" * 60)
    print("Starting NATS server on port 4223 with max_payload=4MB...")
    # 清除上次运行残留的 JetStream 存储目录，避免元数据损坏导致流查找失败
    store_dir = "/tmp/nats-store-fix"
    if os.path.exists(store_dir):
        shutil.rmtree(store_dir)
        print(f"Cleaned stale NATS store dir: {store_dir}")
    config_content = """max_payload: 4MB
listen: 127.0.0.1:4223
jetstream {
    store_dir: /tmp/nats-store-fix
}
"""
    config_path = "/tmp/nats-maxpay.conf"
    with open(config_path, "w") as f:
        f.write(config_content)
    nats_proc = subprocess.Popen(
        ["/tmp/nats-server", "-c", config_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    
    # TCP 探活 4223 端口，确认 NATS server 真正在监听
    start = time.time()
    while time.time() - start < 15:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", 4223))
            sock.close()
            if result == 0:
                print("NATS server on port 4223 is up!")
                return nats_proc
        except Exception:
            pass
        time.sleep(1)
    print("Timeout waiting for NATS server!")
    nats_proc.kill()
    sys.exit(1)


def stop_nats(nats_proc):
    """Step 5 收尾：停止 NATS server 进程。"""
    if nats_proc and nats_proc.poll() is None:
        print("Stopping NATS server...")
        nats_proc.terminate()
        try:
            nats_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            nats_proc.kill()
            nats_proc.wait(timeout=5)


def main():
    # ============================================================
    # Step 1：启动 NATS 消息中间件
    # ============================================================
    nats_proc = start_nats()
    
    collab_proc = None
    ego_proc = None
    
    # 异常退出 / atexit 时的兜底清理，避免残留进程占用端口/GPU
    def cleanup():
        nonlocal collab_proc, ego_proc
        print("\nCleaning up...")
        for name, proc in [("Ego", ego_proc), ("Collaborator", collab_proc)]:
            if proc and proc.poll() is None:
                print(f"Stopping {name}...")
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=5)
        stop_nats(nats_proc)
    
    atexit.register(cleanup)
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    nats_url = "nats://127.0.0.1:4223"
    max_payload = "4194304"   # 4MB，与 NATS server 配置保持一致
    uvicorn_bin = "/home/sxy/miniconda3/envs/vogs_dist/bin/uvicorn"
    
    # ============================================================
    # Step 2：启动 Collaborator 与 Ego 两个 Agent（共享同一张 GPU）
    #         —— 两者各自加载模型 checkpoint 并初始化 DataLoader
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 2: Starting BOTH agents...")
    
    # ---- 2.1 启动 Collaborator Agent（发送方，端口 9001）----
    # 启动命令：uvicorn fast_api.app:app --port 9001
    # Agent 初始化时会：连接 NATS → 创建自身的 Workflow Stream → 加载 VOGS 模型
    collab_env = os.environ.copy()
    collab_env.update({
        "PYTHONUNBUFFERED": "1",
        "A2A_AGENT_URL": "http://localhost:9001",
        "NATS_SERVER_URL": nats_url,
        "NATS_CONTROL_MAX_BYTES": max_payload,
        "CLUSTER_ID": "collab-cluster",
        "AGENT_ID": "vogs-collab",
        "AGENT_INSTANCE_ID": "collab-1",
        "NUM_FRAMES": "5",        # 只跑 5 帧数据
        "CUDA_VISIBLE_DEVICES": "0"
    })
    collab_dir = os.path.join(base_dir, "VOGS_Collaborator_Agent")
    collab_proc = subprocess.Popen(
        [uvicorn_bin, "fast_api.app:app", "--host", "0.0.0.0", "--port", "9001"],
        cwd=collab_dir,
        env=collab_env,
        start_new_session=True,
    )
    
    # 通过 Agent 自身提供的 /.well-known/agent-card.json 接口探活
    if not wait_for_service("http://localhost:9001/.well-known/agent-card.json", timeout=60):
        cleanup()
        sys.exit(1)
    print("Collaborator agent is ready!")
    
    # ---- 2.2 启动 Ego Agent（接收方，端口 9002）----
    # 同样由 uvicorn 启动 fast_api.app；两个 Agent 进程同时存活才能保证
    # NATS Stream 在 Collaborator 发完数据后不会被立刻销毁，Ego 能顺利消费。
    ego_env = os.environ.copy()
    ego_env.update({
        "PYTHONUNBUFFERED": "1",
        "A2A_AGENT_URL": "http://localhost:9002",
        "NATS_SERVER_URL": nats_url,
        "NATS_CONTROL_MAX_BYTES": max_payload,
        "CLUSTER_ID": "ego-cluster",
        "AGENT_ID": "vogs-ego",
        "AGENT_INSTANCE_ID": "ego-1",
        "NUM_FRAMES": "5",
        "CUDA_VISIBLE_DEVICES": "0"
    })
    ego_dir = os.path.join(base_dir, "VOGS_Ego_Agent")
    ego_proc = subprocess.Popen(
        [uvicorn_bin, "fast_api.app:app", "--host", "0.0.0.0", "--port", "9002"],
        cwd=ego_dir,
        env=ego_env,
        start_new_session=True,
    )
    
    if not wait_for_service("http://localhost:9002/.well-known/agent-card.json", timeout=60):
        cleanup()
        sys.exit(1)
    print("Ego agent is ready!")
    
    # ============================================================
    # Step 3：触发 Collaborator 执行 —— POST http://localhost:9001/ (A2A SendMessage)
    #         内部：model_runtime.run_benchmark_sync()
    #                → 逐帧推理 → 把高斯特征 publish 到 NATS subject:
    #                  workflow.global.ego-cluster.agent.vogs-ego.instance.ego-1.in
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 3: Triggering Collaborator (send data to NATS)...")
    collab_result = trigger_a2a_task("http://localhost:9001/", {
        "target_cluster": "ego-cluster",        # 指定数据发往哪个集群
        "target_agent_id": "vogs-ego",          # 目标 Agent
        "target_instance_id": "ego-1",          # 目标实例（决定 NATS Stream 名称 WF_ego-1）
        "operation": "in"
    })
    
    # 从 A2A 响应中提取任务状态，失败则立刻终止
    collab_state = collab_result.get("result", {}).get("task", {}).get("status", {}).get("state", "")
    print(f"\nCollaborator task state: {collab_state}")
    
    if "FAILED" in collab_state:
        print("\nCollaborator failed! Cleaning up...")
        cleanup()
        sys.exit(1)
    
    # ============================================================
    # Step 4：触发 Ego 执行 —— POST http://localhost:9002/ (A2A SendMessage)
    #         内部：model_runtime.run_benchmark(receive_func)
    #                → receive_func 从同一个 NATS Stream 拉取 Collaborator 数据
    #                → 与 Ego 自身特征融合推理 → 计算 mIoU / 各类 IoU / 平均延迟
    #                → 把结果写入 A2A artifact（就是您看到的含 mean_latency_ms 的那串 JSON）
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 4: Triggering Ego (read data from NATS)...")
    ego_result = trigger_a2a_task("http://localhost:9002/", {
        "source_cluster": "collab-cluster",     # 声明协作数据来自哪个上游集群
        "operation": "in"
    })
    
    ego_state = ego_result.get("result", {}).get("task", {}).get("status", {}).get("state", "")
    print(f"\nEgo task state: {ego_state}")
    
    # ============================================================
    # Step 5：按顺序回收进程（先 Ego → 再 Collaborator → 最后 NATS），判定 PASS/FAIL
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 5: Cleaning up agents...")
    
    # 先关 Ego（它已经消费完数据，结果也已返回）
    if ego_proc and ego_proc.poll() is None:
        print("Stopping Ego...")
        os.killpg(os.getpgid(ego_proc.pid), signal.SIGTERM)
        ego_proc.wait(timeout=10)
        ego_proc = None
    
    # 再关 Collaborator
    if collab_proc and collab_proc.poll() is None:
        print("Stopping Collaborator...")
        os.killpg(os.getpgid(collab_proc.pid), signal.SIGTERM)
        collab_proc.wait(timeout=10)
        collab_proc = None
    
    # 最后关 NATS
    stop_nats(nats_proc)
    
    print("\n" + "=" * 60)
    if "COMPLETED" in ego_state:
        print("DISTRIBUTED TEST PASSED!")
    else:
        print("DISTRIBUTED TEST - Check logs for details")
    print("=" * 60)

if __name__ == "__main__":
    main()