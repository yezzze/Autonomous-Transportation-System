"""
NATS 云边可视化后端：连接状态、K8s Agent subject 发现、本机 port-forward。

UI 控制台跑在宿主机时，通过 NATS_SERVERS 或自动 port-forward 连接本集群 NATS；
业务 Agent 在集群内仍使用 nats://nats:4222，不经 AOE HTTP 转发。
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PORT_FORWARD_PROC: Optional[subprocess.Popen] = None

_AGENT_DEPLOY_PREFIXES = ("agent-", "agent_")


def default_nats_servers() -> List[str]:
    raw = os.getenv("NATS_SERVERS", "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    port = os.getenv("NATS_LOCAL_PORT", "4222").strip() or "4222"
    return [f"nats://127.0.0.1:{port}"]


def resolve_nats_servers(servers: Optional[List[str]]) -> List[str]:
    if servers:
        cleaned = [item.strip() for item in servers if item and item.strip()]
        if cleaned:
            return cleaned
    return default_nats_servers()


def ui_config_defaults() -> Dict[str, Any]:
    return {
        "local_cluster": os.getenv("EDGE_CLUSTER_ID", "edge-a"),
        "peer_clusters": [
            c.strip()
            for c in os.getenv("NATS_PEER_CLUSTERS", "edge-a,edge-b").split(",")
            if c.strip()
        ],
        "servers": ",".join(default_nats_servers()),
        "jetstream_domain": os.getenv("NATS_JETSTREAM_DOMAIN", "hub"),
        "stream": os.getenv("NATS_STREAM", "WORKFLOW"),
        "stream_subjects": os.getenv("NATS_STREAM_SUBJECTS", "workflow.>"),
        "namespace": os.getenv("K8S_NAMESPACE", "default"),
        "service_name": os.getenv("NATS_SERVICE_NAME", "nats"),
    }


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _local_port_from_servers(servers: List[str]) -> Optional[int]:
    for item in servers:
        match = re.match(r"^nats://(127\.0\.0\.1|localhost):(\d+)$", item.strip())
        if match:
            return int(match.group(2))
    return None


def stop_nats_port_forward() -> None:
    global _PORT_FORWARD_PROC
    if _PORT_FORWARD_PROC is not None:
        _PORT_FORWARD_PROC.terminate()
        try:
            _PORT_FORWARD_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _PORT_FORWARD_PROC.kill()
        _PORT_FORWARD_PROC = None


def maybe_start_nats_port_forward() -> None:
    """若 NATS_SERVERS 指向本机端口且未监听，则 kubectl port-forward。"""
    global _PORT_FORWARD_PROC

    flag = os.getenv("AUTO_NATS_PORT_FORWARD", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return

    servers = default_nats_servers()
    local_port = _local_port_from_servers(servers)
    if local_port is None:
        logger.info("[NATS UI] NATS_SERVERS 非本机地址，跳过 port-forward: %s", servers)
        return

    if _port_open(local_port):
        logger.info("[NATS UI] 127.0.0.1:%s 已可达", local_port)
        return

    if _PORT_FORWARD_PROC is not None and _PORT_FORWARD_PROC.poll() is None:
        return

    namespace = os.getenv("K8S_NAMESPACE", "default")
    service = os.getenv("NATS_SERVICE_NAME", "nats")
    try:
        subprocess.run(
            ["kubectl", "get", f"svc/{service}", "-n", namespace],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.warning("[NATS UI] 无法 port-forward（kubectl/svc 不可用）: %s", exc)
        return

    cmd = [
        "kubectl",
        "port-forward",
        "--address",
        "127.0.0.1",
        "-n",
        namespace,
        f"svc/{service}",
        f"{local_port}:4222",
    ]
    logger.info("[NATS UI] 启动: %s", " ".join(cmd))
    _PORT_FORWARD_PROC = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    for _ in range(40):
        if _port_open(local_port):
            logger.info("[NATS UI] port-forward 就绪: %s", servers[0])
            return
        if _PORT_FORWARD_PROC.poll() is not None:
            err = (_PORT_FORWARD_PROC.stderr.read() if _PORT_FORWARD_PROC.stderr else b"").decode(
                errors="replace"
            )
            logger.error("[NATS UI] port-forward 退出: %s", err.strip())
            _PORT_FORWARD_PROC = None
            return
        import time

        time.sleep(0.25)

    logger.warning("[NATS UI] 等待 127.0.0.1:%s 超时", local_port)


def _safe_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower())
    return token.strip("-") or "agent"


def subject_for_agent(cluster_id: str, agent_id: str) -> str:
    aid = _safe_token(agent_id)
    if aid == "agent-grpc":
        aid = "agent.grpc"
    elif aid == "agent-b":
        aid = "agent.b"
    elif aid == "agent-c":
        aid = "agent.c"
    elif aid.startswith("agent-"):
        aid = f"agent.{aid[6:]}"
    return f"workflow.{_safe_token(cluster_id)}.{aid}.in"


def _container_env(container: Any) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in getattr(container, "env", None) or []:
        if item.value is not None:
            result[item.name] = item.value
    return result


def _fallback_agents(cluster_id: Optional[str]) -> List[Dict[str, Any]]:
    cid = cluster_id or os.getenv("EDGE_CLUSTER_ID", "edge-a")
    catalog = [
        ("agent-grpc", "agent.grpc"),
        ("agent-b", "agent.b"),
        ("agent-c", "agent.c"),
    ]
    rows = []
    for agent_id, token in catalog:
        rows.append(
            {
                "deployment": agent_id,
                "agent_id": agent_id,
                "cluster_id": cid,
                "in_subject": f"workflow.{cid}.{token}.in",
                "ready_replicas": None,
                "replicas": None,
                "status": "unknown",
            }
        )
    return rows


def list_edge_agents(cluster_id: Optional[str] = None) -> Dict[str, Any]:
    namespace = os.getenv("K8S_NAMESPACE", "default")
    try:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        apps = client.AppsV1Api()
        deployments = apps.list_namespaced_deployment(namespace=namespace).items
        agents: List[Dict[str, Any]] = []

        for dep in deployments:
            name = dep.metadata.name or ""
            if not any(name.startswith(prefix) for prefix in _AGENT_DEPLOY_PREFIXES):
                continue
            containers = dep.spec.template.spec.containers or []
            if not containers:
                continue
            env = _container_env(containers[0])
            cid = env.get("CLUSTER_ID") or os.getenv("EDGE_CLUSTER_ID", "edge-a")
            if cluster_id and cid != cluster_id:
                continue
            agent_id = env.get("AGENT_ID") or name
            in_subject = env.get("IN_SUBJECT") or subject_for_agent(cid, agent_id)
            ready = dep.status.ready_replicas or 0
            desired = dep.spec.replicas or 0
            agents.append(
                {
                    "deployment": name,
                    "agent_id": agent_id,
                    "cluster_id": cid,
                    "in_subject": in_subject,
                    "ready_replicas": ready,
                    "replicas": desired,
                    "status": "ready" if ready and ready >= desired else "pending",
                    "nats_servers": env.get("NATS_SERVERS", "nats://nats:4222"),
                    "jetstream_domain": env.get("NATS_JETSTREAM_DOMAIN", "hub"),
                }
            )

        agents.sort(key=lambda row: (row["cluster_id"], row["agent_id"]))
        return {
            "agents": agents,
            "source": "kubernetes",
            "namespace": namespace,
            "cluster_filter": cluster_id,
        }
    except Exception as exc:
        logger.warning("[NATS UI] K8s agent 列表失败，使用内置模板: %s", exc)
        rows = _fallback_agents(cluster_id)
        return {
            "agents": rows,
            "source": "fallback",
            "namespace": namespace,
            "cluster_filter": cluster_id,
            "error": str(exc),
        }


async def nats_status(servers: Optional[List[str]] = None) -> Dict[str, Any]:
    from nats.aio.client import Client as NATS

    resolved = resolve_nats_servers(servers)
    domain = os.getenv("NATS_JETSTREAM_DOMAIN", "hub")
    stream = os.getenv("NATS_STREAM", "WORKFLOW")
    result: Dict[str, Any] = {
        "connected": False,
        "servers": resolved,
        "jetstream_domain": domain,
        "stream": stream,
        "ui_defaults": ui_config_defaults(),
        "port_forward_hint": (
            f"kubectl port-forward -n {os.getenv('K8S_NAMESPACE', 'default')} "
            f"svc/{os.getenv('NATS_SERVICE_NAME', 'nats')} "
            f"{_local_port_from_servers(resolved) or 4222}:4222"
        ),
    }

    nc = NATS()
    try:
        await nc.connect(
            servers=resolved,
            connect_timeout=5,
            max_reconnect_attempts=1,
        )
        result["connected"] = True
        result["server_id"] = nc.connected_server_id
        result["server_name"] = nc.connected_server_name

        js = nc.jetstream(domain=domain or None)
        try:
            info = await js.account_info()
            result["jetstream"] = {
                "domain": getattr(info, "domain", None) or domain,
                "streams": getattr(info, "streams", None),
                "consumers": getattr(info, "consumers", None),
            }
        except Exception as exc:
            result["jetstream_error"] = str(exc)

        try:
            stream_info = await js.stream_info(stream)
            result["stream_info"] = {
                "name": stream_info.config.name,
                "subjects": list(stream_info.config.subjects or []),
                "messages": stream_info.state.messages,
                "bytes": stream_info.state.bytes,
            }
        except Exception as exc:
            result["stream_info_error"] = str(exc)

        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        if nc.is_connected:
            await nc.drain()
