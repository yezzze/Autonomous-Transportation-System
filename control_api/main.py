import json
import os
import uuid
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from nats.errors import TimeoutError as NatsTimeoutError
from kubernetes import client, config
from nats.aio.client import Client as NATS
from pydantic import BaseModel, Field

app = FastAPI(title="K8S Demo Control API", version="1.0.0")

DEFAULT_NATS_SERVERS = [s.strip() for s in os.environ.get("NATS_SERVERS", "nats://nats:4222").split(",") if s.strip()]
DEFAULT_STREAM = os.environ.get("NATS_STREAM", "WORKFLOW")
DEFAULT_STREAM_SUBJECTS = [s.strip() for s in os.environ.get("NATS_STREAM_SUBJECTS", "workflow.demo.>").split(",") if s.strip()]


def _load_kube() -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


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
    try:
        await js.add_stream(name=stream, subjects=subjects)
    except Exception:
        # Stream creation is idempotent for this demo gateway. Existing streams are reused.
        pass


def _decode_message(data: bytes) -> Dict:
    try:
        return json.loads(data.decode())
    except Exception:
        return {"raw": data.decode(errors="replace")}


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


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
