import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime_api.jetstream_stream import ensure_jetstream_stream as _ensure_stream_config

import httpx
from fastapi import FastAPI, HTTPException
from nats.errors import TimeoutError as NatsTimeoutError
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from nats.aio.client import Client as NATS
from pydantic import BaseModel, Field

app = FastAPI(title="K8S Demo Control API", version="1.0.0")

DEFAULT_NATS_SERVERS = [s.strip() for s in os.environ.get("NATS_SERVERS", "nats://nats:4222").split(",") if s.strip()]
DEFAULT_STREAM = os.environ.get("NATS_STREAM", "WORKFLOW")
DEFAULT_STREAM_SUBJECTS = [s.strip() for s in os.environ.get("NATS_STREAM_SUBJECTS", "workflow.demo.>").split(",") if s.strip()]
QUANTITY_SUFFIXES = {
    "Ki": Decimal(1024),
    "Mi": Decimal(1024) ** 2,
    "Gi": Decimal(1024) ** 3,
    "Ti": Decimal(1024) ** 4,
    "Pi": Decimal(1024) ** 5,
    "Ei": Decimal(1024) ** 6,
    "n": Decimal("0.000000001"),
    "u": Decimal("0.000001"),
    "m": Decimal("0.001"),
    "k": Decimal(1000),
    "M": Decimal(1000) ** 2,
    "G": Decimal(1000) ** 3,
    "T": Decimal(1000) ** 4,
    "P": Decimal(1000) ** 5,
    "E": Decimal(1000) ** 6,
}


def _load_kube() -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


def _parse_quantity(value: Optional[str]) -> Decimal:
    if not value:
        return Decimal(0)

    text = str(value).strip()
    for suffix in sorted(QUANTITY_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            return Decimal(text[: -len(suffix)]) * QUANTITY_SUFFIXES[suffix]

    return Decimal(text)


def _add_resources(target: Dict[str, Decimal], resources: Optional[Dict[str, str]]) -> None:
    if not resources:
        return

    for name, value in resources.items():
        target[name] = target.get(name, Decimal(0)) + _parse_quantity(value)


def _container_requests(containers: Optional[List]) -> Dict[str, Decimal]:
    requests: Dict[str, Decimal] = {}
    for container in containers or []:
        resources = container.resources.requests if container.resources else None
        _add_resources(requests, resources)
    return requests


def _pod_requested_resources(pod) -> Dict[str, Decimal]:
    app_requests = _container_requests(pod.spec.containers)
    init_requests: Dict[str, Decimal] = {}

    for init_container in pod.spec.init_containers or []:
        requests = init_container.resources.requests if init_container.resources else None
        for name, value in (requests or {}).items():
            init_requests[name] = max(init_requests.get(name, Decimal(0)), _parse_quantity(value))

    resource_names = set(app_requests) | set(init_requests)
    effective = {
        name: max(app_requests.get(name, Decimal(0)), init_requests.get(name, Decimal(0)))
        for name in resource_names
    }
    _add_resources(effective, pod.spec.overhead)
    return effective


def _decimal_to_number(value: Decimal):
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _resource_summary(resources: Dict[str, Decimal]) -> Dict:
    cpu = resources.get("cpu", Decimal(0))
    memory = resources.get("memory", Decimal(0))
    summary = {
        "cpu_millicores": _decimal_to_number(cpu * 1000),
        "cpu_cores": _decimal_to_number(cpu),
        "memory_bytes": int(memory),
        "memory_mib": round(float(memory / (Decimal(1024) ** 2)), 2),
    }

    for name in sorted(resources):
        if name not in {"cpu", "memory"}:
            summary[name] = _decimal_to_number(resources[name])

    return summary


def _to_resource_dict(cpu: Optional[str], memory: Optional[str], gpu: Optional[int]) -> Dict[str, str]:
    res: Dict[str, str] = {}
    if cpu:
        res["cpu"] = cpu
    if memory:
        res["memory"] = memory
    if gpu is not None:
        res["nvidia.com/gpu"] = str(gpu)
    return res


def _patch_deployment(
    namespace: str,
    deployment: str,
    image: Optional[str],
    replicas: Optional[int],
    requests: Dict[str, str],
    limits: Dict[str, str],
    env: Dict[str, str],
    node_selector: Dict[str, str],
) -> Dict:
    _load_kube()
    apps = client.AppsV1Api()
    current = apps.read_namespaced_deployment(name=deployment, namespace=namespace)

    if not current.spec.template.spec.containers:
        raise HTTPException(status_code=400, detail="deployment has no containers")

    container_name = current.spec.template.spec.containers[0].name

    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container_name,
                        }
                    ]
                }
            }
        }
    }

    c0 = patch["spec"]["template"]["spec"]["containers"][0]

    if image:
        c0["image"] = image

    if requests or limits:
        c0["resources"] = {}
        if requests:
            c0["resources"]["requests"] = requests
        if limits:
            c0["resources"]["limits"] = limits

    if env:
        c0["env"] = [{"name": k, "value": v} for k, v in env.items()]

    if node_selector:
        patch["spec"]["template"]["spec"]["nodeSelector"] = node_selector

    if replicas is not None:
        patch["spec"]["replicas"] = replicas

    resp = apps.patch_namespaced_deployment(name=deployment, namespace=namespace, body=patch)
    return {
        "name": resp.metadata.name,
        "namespace": resp.metadata.namespace,
        "replicas": resp.spec.replicas,
        "message": "deployment patched",
    }


class StartupConfigRequest(BaseModel):
    namespace: str = "default"
    deployment: str
    image: Optional[str] = None
    replicas: Optional[int] = Field(default=1, ge=1)
    request_cpu: Optional[str] = None
    request_memory: Optional[str] = None
    request_gpu: Optional[int] = Field(default=None, ge=0)
    limit_cpu: Optional[str] = None
    limit_memory: Optional[str] = None
    limit_gpu: Optional[int] = Field(default=None, ge=0)
    env: Dict[str, str] = Field(default_factory=dict)
    node_selector: Dict[str, str] = Field(default_factory=dict)


class ScaleRequest(BaseModel):
    namespace: str = "default"
    deployment: str
    replicas: Optional[int] = Field(default=None, ge=1)
    new_request_cpu: Optional[str] = None
    new_request_memory: Optional[str] = None
    new_request_gpu: Optional[int] = Field(default=None, ge=0)
    new_limit_cpu: Optional[str] = None
    new_limit_memory: Optional[str] = None
    new_limit_gpu: Optional[int] = Field(default=None, ge=0)


class HttpRouteRequest(BaseModel):
    target_url: str
    payload: Dict
    timeout_sec: float = 8.0


class NatsRouteRequest(BaseModel):
    nats_servers: List[str]
    subject: str
    payload: Dict


class MessageSendRequest(BaseModel):
    subject: str
    payload: Dict
    nats_servers: Optional[List[str]] = None
    use_jetstream: bool = True
    stream: str = DEFAULT_STREAM
    stream_subjects: List[str] = Field(default_factory=lambda: DEFAULT_STREAM_SUBJECTS.copy())


class MessageReceiveRequest(BaseModel):
    subject: str
    durable: str
    nats_servers: Optional[List[str]] = None
    batch: int = Field(default=1, ge=1, le=50)
    timeout_sec: float = Field(default=5.0, gt=0)
    stream: str = DEFAULT_STREAM
    stream_subjects: List[str] = Field(default_factory=lambda: DEFAULT_STREAM_SUBJECTS.copy())
    ack: bool = True


class MessageRequestReplyRequest(BaseModel):
    subject: str
    payload: Dict
    reply_subject: str
    nats_servers: Optional[List[str]] = None
    timeout_sec: float = Field(default=10.0, gt=0)
    correlation_field: str = "workflow_id"
    stream: str = DEFAULT_STREAM
    stream_subjects: List[str] = Field(default_factory=lambda: DEFAULT_STREAM_SUBJECTS.copy())


async def _connect_nats(nats_servers: Optional[List[str]]) -> NATS:
    servers = nats_servers or DEFAULT_NATS_SERVERS
    if not servers:
        raise HTTPException(status_code=400, detail="no nats servers configured")

    nc = NATS()
    await nc.connect(servers=servers)
    return nc


async def _ensure_stream(js, stream: str, subjects: List[str]) -> None:
    await _ensure_stream_config(js, name=stream, subjects=subjects)


def _decode_message(data: bytes) -> Dict:
    try:
        return json.loads(data.decode())
    except Exception:
        return {"raw": data.decode(errors="replace")}


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/nodes/resources")
def node_resources(node_name: Optional[str] = None) -> Dict:
    _load_kube()
    core = client.CoreV1Api()

    try:
        nodes = core.list_node().items
        pods = core.list_pod_for_all_namespaces().items
    except ApiException as exc:
        raise HTTPException(status_code=exc.status or 500, detail=exc.reason)

    requested_by_node: Dict[str, Dict[str, Decimal]] = {}
    pod_count_by_node: Dict[str, int] = {}
    for pod in pods:
        if pod.status.phase in {"Succeeded", "Failed"} or not pod.spec.node_name:
            continue

        node_requests = requested_by_node.setdefault(pod.spec.node_name, {})
        _add_resources(node_requests, _pod_requested_resources(pod))
        pod_count_by_node[pod.spec.node_name] = pod_count_by_node.get(pod.spec.node_name, 0) + 1

    results = []
    for node in nodes:
        if node_name and node.metadata.name != node_name:
            continue

        allocatable = {
            name: _parse_quantity(value)
            for name, value in (node.status.allocatable or {}).items()
        }
        requested = requested_by_node.get(node.metadata.name, {})
        resource_names = set(allocatable) | set(requested)
        remaining = {
            name: allocatable.get(name, Decimal(0)) - requested.get(name, Decimal(0))
            for name in resource_names
        }

        results.append(
            {
                "name": node.metadata.name,
                "pod_count": pod_count_by_node.get(node.metadata.name, 0),
                "allocatable": _resource_summary(allocatable),
                "requested": _resource_summary(requested),
                "remaining": _resource_summary(remaining),
            }
        )

    if node_name and not results:
        raise HTTPException(status_code=404, detail=f"node not found: {node_name}")

    return {"nodes": results, "count": len(results)}


@app.get("/v1/nodes/current/resources")
def current_node_resources() -> Dict:
    node_name = os.environ.get("NODE_NAME")
    if not node_name:
        raise HTTPException(status_code=400, detail="NODE_NAME env is not configured")

    return node_resources(node_name=node_name)


@app.post("/v1/apps/startup-config")
def startup_config(req: StartupConfigRequest) -> Dict:
    requests = _to_resource_dict(req.request_cpu, req.request_memory, req.request_gpu)
    limits = _to_resource_dict(req.limit_cpu, req.limit_memory, req.limit_gpu)
    return _patch_deployment(
        namespace=req.namespace,
        deployment=req.deployment,
        image=req.image,
        replicas=req.replicas,
        requests=requests,
        limits=limits,
        env=req.env,
        node_selector=req.node_selector,
    )


@app.post("/v1/apps/scale")
def scale(req: ScaleRequest) -> Dict:
    requests = _to_resource_dict(req.new_request_cpu, req.new_request_memory, req.new_request_gpu)
    limits = _to_resource_dict(req.new_limit_cpu, req.new_limit_memory, req.new_limit_gpu)
    return _patch_deployment(
        namespace=req.namespace,
        deployment=req.deployment,
        image=None,
        replicas=req.replicas,
        requests=requests,
        limits=limits,
        env={},
        node_selector={},
    )


@app.post("/v1/network/http-route")
async def http_route(req: HttpRouteRequest) -> Dict:
    async with httpx.AsyncClient(timeout=req.timeout_sec) as cli:
        resp = await cli.post(req.target_url, json=req.payload)
    return {
        "status_code": resp.status_code,
        "response_text": resp.text,
    }


@app.post("/v1/network/nats-route")
async def nats_route(req: NatsRouteRequest) -> Dict:
    nc = NATS()
    try:
        await nc.connect(servers=req.nats_servers)
        await nc.publish(req.subject, json.dumps(req.payload).encode())
        await nc.flush(timeout=3)
        return {"message": "nats payload sent", "subject": req.subject}
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


@app.post("/v1/messages/send")
async def send_message(req: MessageSendRequest) -> Dict:
    nc = await _connect_nats(req.nats_servers)
    try:
        data = json.dumps(req.payload).encode()
        if req.use_jetstream:
            js = nc.jetstream()
            await _ensure_stream(js, req.stream, req.stream_subjects)
            ack = await js.publish(req.subject, data)
            return {
                "message": "sent",
                "subject": req.subject,
                "stream": ack.stream,
                "seq": ack.seq,
            }

        await nc.publish(req.subject, data)
        await nc.flush(timeout=3)
        return {
            "message": "sent",
            "subject": req.subject,
        }
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


@app.post("/v1/messages/receive")
async def receive_message(req: MessageReceiveRequest) -> Dict:
    nc = await _connect_nats(req.nats_servers)
    try:
        js = nc.jetstream()
        await _ensure_stream(js, req.stream, req.stream_subjects)

        sub = await js.pull_subscribe(req.subject, durable=req.durable)
        try:
            msgs = await sub.fetch(req.batch, timeout=req.timeout_sec)
        except NatsTimeoutError:
            return {
                "messages": [],
                "count": 0,
                "subject": req.subject,
                "durable": req.durable,
            }

        messages = []
        for msg in msgs:
            payload = _decode_message(msg.data)
            metadata = msg.metadata
            messages.append(
                {
                    "subject": msg.subject,
                    "payload": payload,
                    "stream": metadata.stream,
                    "consumer": metadata.consumer,
                    "stream_seq": metadata.sequence.stream,
                    "consumer_seq": metadata.sequence.consumer,
                }
            )
            if req.ack:
                await msg.ack()

        return {
            "messages": messages,
            "count": len(messages),
            "subject": req.subject,
            "durable": req.durable,
        }
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


@app.post("/v1/messages/request")
async def request_message(req: MessageRequestReplyRequest) -> Dict:
    nc = await _connect_nats(req.nats_servers)
    try:
        js = nc.jetstream()
        await _ensure_stream(js, req.stream, req.stream_subjects)

        payload = dict(req.payload)
        correlation_id = payload.get(req.correlation_field) or str(uuid.uuid4())
        payload[req.correlation_field] = correlation_id

        sub = await js.subscribe(req.reply_subject, manual_ack=True)
        await js.publish(req.subject, json.dumps(payload).encode())

        while True:
            try:
                msg = await sub.next_msg(timeout=req.timeout_sec)
            except NatsTimeoutError:
                raise HTTPException(status_code=504, detail="timeout waiting for reply")

            data = _decode_message(msg.data)
            await msg.ack()
            if data.get(req.correlation_field) == correlation_id:
                return {
                    "message": "reply received",
                    "correlation_id": correlation_id,
                    "request_subject": req.subject,
                    "reply_subject": req.reply_subject,
                    "payload": data,
                }
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
