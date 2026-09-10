"""宿主机运行 API 时自动管理 Kubernetes Prometheus 端口转发。"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import IO
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PORT_FORWARD_PROC: Optional[subprocess.Popen] = None
_PORT_FORWARD_LOG: Optional[IO[str]] = None
_FALSE_VALUES = {"0", "false", "no", "off"}
_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "prometheus-forward.log"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _forward_address() -> str:
    address = os.getenv("PROMETHEUS_FORWARD_ADDRESS", "127.0.0.1").strip()
    if address == "localhost":
        return "127.0.0.1"
    try:
        ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError("PROMETHEUS_FORWARD_ADDRESS 必须是单个 IP 地址") from exc
    return address


def _local_target() -> Optional[tuple[str, int]]:
    """返回 API 应连接的转发地址；显式配置远端 URL 时不启动转发。"""
    configured_url = os.getenv("PROMETHEUS_URL", "").strip()
    if configured_url:
        parsed = urlparse(configured_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            logger.warning("[Prometheus] PROMETHEUS_URL 无效，跳过自动 port-forward")
            return None
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            logger.info("[Prometheus] 使用外部地址，跳过 port-forward: %s", configured_url)
            return None
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

    address = _forward_address()
    port = int(os.getenv("PROMETHEUS_LOCAL_PORT", "9090").strip() or "9090")
    client_host = "127.0.0.1" if address == "0.0.0.0" else address
    os.environ["PROMETHEUS_URL"] = f"http://{client_host}:{port}"
    return client_host, port


def stop_prometheus_port_forward() -> None:
    global _PORT_FORWARD_LOG, _PORT_FORWARD_PROC
    if _PORT_FORWARD_PROC is not None:
        _PORT_FORWARD_PROC.terminate()
        try:
            _PORT_FORWARD_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _PORT_FORWARD_PROC.kill()
        _PORT_FORWARD_PROC = None
    if _PORT_FORWARD_LOG is not None:
        _PORT_FORWARD_LOG.close()
        _PORT_FORWARD_LOG = None


def maybe_start_prometheus_port_forward() -> None:
    """在需要时将 Kubernetes Prometheus Service 转发到宿主机。"""
    global _PORT_FORWARD_LOG, _PORT_FORWARD_PROC

    if os.getenv("AUTO_PROMETHEUS_PORT_FORWARD", "1").strip().lower() in _FALSE_VALUES:
        return

    try:
        target = _local_target()
        address = _forward_address()
    except (TypeError, ValueError) as exc:
        logger.warning("[Prometheus] 自动 port-forward 配置无效: %s", exc)
        return
    if target is None:
        return
    probe_host, local_port = target
    if _port_open(probe_host, local_port):
        logger.info("[Prometheus] %s:%s 已可达", probe_host, local_port)
        return
    if _PORT_FORWARD_PROC is not None and _PORT_FORWARD_PROC.poll() is None:
        return

    namespace = os.getenv("PROMETHEUS_NAMESPACE", "monitoring").strip() or "monitoring"
    service = (
        os.getenv(
            "PROMETHEUS_SERVICE_NAME",
            "monitoring-kube-prometheus-prometheus",
        ).strip()
        or "monitoring-kube-prometheus-prometheus"
    )
    remote_port = int(os.getenv("PROMETHEUS_SERVICE_PORT", "9090").strip() or "9090")
    try:
        subprocess.run(
            ["kubectl", "get", f"svc/{service}", "-n", namespace],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.warning("[Prometheus] 无法 port-forward（kubectl/svc 不可用）: %s", exc)
        return

    cmd = [
        "kubectl",
        "port-forward",
        "--address",
        address,
        "-n",
        namespace,
        f"svc/{service}",
        f"{local_port}:{remote_port}",
    ]
    logger.info("[Prometheus] 启动: %s", " ".join(cmd))
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PORT_FORWARD_LOG = _LOG_PATH.open("a", encoding="utf-8", buffering=1)
    try:
        _PORT_FORWARD_PROC = subprocess.Popen(
            cmd,
            stdout=_PORT_FORWARD_LOG,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.SubprocessError):
        _PORT_FORWARD_LOG.close()
        _PORT_FORWARD_LOG = None
        raise

    for _ in range(40):
        if _port_open(probe_host, local_port):
            logger.info("[Prometheus] port-forward 就绪: %s", os.environ["PROMETHEUS_URL"])
            return
        if _PORT_FORWARD_PROC.poll() is not None:
            logger.error("[Prometheus] port-forward 已退出")
            _PORT_FORWARD_PROC = None
            _PORT_FORWARD_LOG.close()
            _PORT_FORWARD_LOG = None
            return
        time.sleep(0.25)

    logger.warning("[Prometheus] 等待 %s:%s 超时", probe_host, local_port)
