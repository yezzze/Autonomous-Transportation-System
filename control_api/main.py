import hmac
import os
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from control_api.lifecycle import (
    ControllerSettings,
    CreateInstanceRequest,
    EdgeLifecycleController,
    LifecycleError,
)
from runtime_api import NatsComm

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
        if os.environ.get(
            "CONTROL_ALLOW_KUBECONFIG",
            "false",
        ).strip().lower() not in {"1", "true", "yes"}:
            raise
        config.load_kube_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_kube()
    settings = ControllerSettings.from_env()
    servers = [
        value.strip()
        for value in os.environ.get(
            "NATS_SERVERS",
            "nats://nats:4222",
        ).split(",")
        if value.strip()
    ]
    controller = EdgeLifecycleController(
        core_api=client.CoreV1Api(),
        nats=NatsComm(
            servers=servers,
            jetstream_domain=settings.cluster_id,
        ),
        settings=settings,
    )
    app.state.controller = controller
    app.state.api_token = os.environ.get("CONTROL_API_TOKEN", "")
    await controller.start()
    try:
        yield
    finally:
        await controller.close()


app = FastAPI(
    title="K8S Edge Lifecycle Controller",
    version="2.0.0",
    lifespan=lifespan,
)
bearer = HTTPBearer(auto_error=False)


@app.exception_handler(LifecycleError)
async def lifecycle_error_handler(_request: Request, exc: LifecycleError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "context": exc.context},
    )


async def require_api_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> None:
    expected = request.app.state.api_token
    if not expected:
        raise LifecycleError(503, "CONTROL_API_TOKEN is not configured")
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected)
    ):
        raise LifecycleError(401, "invalid control API bearer token")


def get_controller(request: Request) -> EdgeLifecycleController:
    return request.app.state.controller


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request):
    health = await get_controller(request).cluster_health()
    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(status_code=status_code, content=health)


@app.post(
    "/v1/instances",
    status_code=201,
    dependencies=[Depends(require_api_token)],
)
async def create_instance(request: Request, body: CreateInstanceRequest):
    return await get_controller(request).create_instance(body)


@app.get(
    "/v1/instances",
    dependencies=[Depends(require_api_token)],
)
async def list_instances(
    request: Request,
    namespace: Optional[str] = None,
):
    return await get_controller(request).list_instances(namespace)


@app.get(
    "/v1/instances/{namespace}/{name}",
    dependencies=[Depends(require_api_token)],
)
async def get_instance(request: Request, namespace: str, name: str):
    return await get_controller(request).get_instance(namespace, name)


@app.delete(
    "/v1/instances/{namespace}/{name}",
    dependencies=[Depends(require_api_token)],
)
async def delete_instance(
    request: Request,
    namespace: str,
    name: str,
    instance_id: Optional[str] = None,
    drain_timeout_sec: float = Query(default=30, ge=0, le=300),
    force: bool = False,
    pod_grace_period_seconds: int = Query(default=30, ge=0, le=300),
):
    return await get_controller(request).delete_instance(
        namespace=namespace,
        name=name,
        instance_id=instance_id,
        drain_timeout_sec=drain_timeout_sec,
        force=force,
        pod_grace_period_seconds=pod_grace_period_seconds,
    )


@app.get(
    "/v1/cluster/health",
    dependencies=[Depends(require_api_token)],
)
async def cluster_health(request: Request):
    return await get_controller(request).cluster_health()


@app.post(
    "/v1/reconcile",
    dependencies=[Depends(require_api_token)],
)
async def reconcile(request: Request):
    return await get_controller(request).reconcile_once()


def _parse_quantity(value: Optional[str]) -> Decimal:
    if not value:
        return Decimal(0)
    text = str(value).strip()
    for suffix in sorted(QUANTITY_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            return Decimal(text[: -len(suffix)]) * QUANTITY_SUFFIXES[suffix]
    return Decimal(text)


def _add_resources(target, resources) -> None:
    for name, value in (resources or {}).items():
        target[name] = target.get(name, Decimal(0)) + _parse_quantity(value)


def _pod_requested_resources(pod):
    app_requests = {}
    for container in pod.spec.containers or []:
        resources = container.resources.requests if container.resources else None
        _add_resources(app_requests, resources)
    init_requests = {}
    for container in pod.spec.init_containers or []:
        resources = container.resources.requests if container.resources else None
        for name, value in (resources or {}).items():
            init_requests[name] = max(
                init_requests.get(name, Decimal(0)),
                _parse_quantity(value),
            )
    names = set(app_requests) | set(init_requests)
    effective = {
        name: max(
            app_requests.get(name, Decimal(0)),
            init_requests.get(name, Decimal(0)),
        )
        for name in names
    }
    _add_resources(effective, pod.spec.overhead)
    return effective


def _decimal_to_number(value: Decimal):
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _resource_summary(resources):
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


@app.get(
    "/v1/nodes/resources",
    dependencies=[Depends(require_api_token)],
)
async def node_resources(
    request: Request,
    node_name: Optional[str] = None,
):
    controller = get_controller(request)
    try:
        nodes = await controller.call_sync(controller.core.list_node)
        pods = await controller.call_sync(
            controller.core.list_pod_for_all_namespaces
        )
    except ApiException as exc:
        raise LifecycleError(
            exc.status or 500,
            f"Kubernetes resource query failed: {exc.reason}",
        ) from exc

    requested_by_node = {}
    pod_count_by_node = {}
    for pod in pods.items:
        if pod.status.phase in {"Succeeded", "Failed"} or not pod.spec.node_name:
            continue
        requested = requested_by_node.setdefault(pod.spec.node_name, {})
        _add_resources(requested, _pod_requested_resources(pod))
        pod_count_by_node[pod.spec.node_name] = (
            pod_count_by_node.get(pod.spec.node_name, 0) + 1
        )

    results = []
    for node in nodes.items:
        if node_name and node.metadata.name != node_name:
            continue
        allocatable = {
            name: _parse_quantity(value)
            for name, value in (node.status.allocatable or {}).items()
        }
        requested = requested_by_node.get(node.metadata.name, {})
        names = set(allocatable) | set(requested)
        remaining = {
            name: allocatable.get(name, Decimal(0))
            - requested.get(name, Decimal(0))
            for name in names
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
        raise LifecycleError(404, f"node not found: {node_name}")
    return {"nodes": results, "count": len(results)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
