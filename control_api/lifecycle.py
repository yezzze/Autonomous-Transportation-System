from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Set

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field, field_validator

from runtime_api import NatsComm

logger = logging.getLogger(__name__)

MANAGED_BY_LABEL = "k8s-demo.io/managed-by"
AGENT_ID_LABEL = "k8s-demo.io/agent-id"
CLUSTER_ID_LABEL = "k8s-demo.io/cluster-id"
STREAM_ENABLED_LABEL = "k8s-demo.io/workflow-stream"
FRAME_STREAM_ENABLED_LABEL = "k8s-demo.io/frame-stream"
MANAGED_BY_VALUE = "edge-lifecycle-controller"

DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_ENV = {
    "AGENT_ID",
    "AGENT_INSTANCE_ID",
    "CLUSTER_ID",
    "NATS_JETSTREAM_DOMAIN",
    "NATS_FRAME_ACK_WAIT_SEC",
    "NATS_FRAME_MAX_DELIVER",
    "NATS_FRAME_STREAM_MAX_AGE_SEC",
    "NATS_FRAME_STREAM_MAX_BYTES",
    "NATS_FRAME_STREAM_PREFIX",
    "NATS_SERVERS",
    "NATS_STREAM_DISCARD",
    "NATS_STREAM_MAX_BYTES",
    "NATS_STREAM_PROVISION_TIMEOUT_SEC",
    "NATS_STREAM_RETENTION",
    "NATS_STREAM_STORAGE",
    "NATS_WORKFLOW_STREAM_PREFIX",
    "POD_NAME",
}


class LifecycleError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.context = context or {}


class ResourceConfig(BaseModel):
    requests: Dict[str, str] = Field(default_factory=dict)
    limits: Dict[str, str] = Field(default_factory=dict)


class PortConfig(BaseModel):
    name: Optional[str] = Field(default=None, max_length=15)
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["TCP", "UDP", "SCTP"] = "TCP"


class CreateInstanceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    namespace: str = Field(default="default", min_length=1, max_length=63)
    agent_id: str = Field(min_length=1, max_length=63)
    image: str = Field(min_length=1, max_length=512)
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    command: Optional[List[str]] = None
    args: Optional[List[str]] = None
    env: Dict[str, str] = Field(default_factory=dict)
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    node_selector: Dict[str, str] = Field(default_factory=dict)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    ports: List[PortConfig] = Field(default_factory=list)
    workflow_stream: bool = True
    frame_stream: bool = True
    wait_ready_timeout_sec: float = Field(default=0, ge=0, le=300)
    termination_grace_period_seconds: int = Field(default=30, ge=0, le=300)

    @field_validator("name", "namespace")
    @classmethod
    def validate_dns_label(cls, value: str) -> str:
        if not DNS_LABEL_RE.fullmatch(value):
            raise ValueError("must be a Kubernetes DNS label")
        return value

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if not TOKEN_RE.fullmatch(value):
            raise ValueError(
                "must contain only ASCII letters, digits, '-' and '_'"
            )
        return value

    @field_validator("env")
    @classmethod
    def reject_reserved_env(cls, value: Dict[str, str]) -> Dict[str, str]:
        conflicts = sorted(RESERVED_ENV.intersection(value))
        if conflicts:
            raise ValueError(
                f"controller-managed env cannot be overridden: {conflicts}"
            )
        return value


@dataclass(frozen=True)
class ControllerSettings:
    cluster_id: str
    allowed_namespaces: Set[str]
    agent_nats_servers: str
    agent_service_account: Optional[str]
    image_pull_secrets: List[str]
    stream_prefix: str
    stream_max_bytes: str
    frame_stream_prefix: str
    frame_stream_max_bytes: str
    frame_stream_max_age_sec: float
    frame_ack_wait_sec: float
    frame_max_deliver: int
    stream_provision_timeout_sec: int
    reconcile_interval_sec: float
    orphan_grace_sec: float
    delete_empty_orphan_streams: bool

    @classmethod
    def from_env(cls) -> "ControllerSettings":
        cluster_id = os.environ.get("CLUSTER_ID", "").strip()
        if not cluster_id or not TOKEN_RE.fullmatch(cluster_id):
            raise RuntimeError(
                "CLUSTER_ID is required and must be one NATS subject token"
            )
        raw_namespaces = os.environ.get(
            "CONTROL_ALLOWED_NAMESPACES",
            "default",
        )
        namespaces = {
            item.strip() for item in raw_namespaces.split(",") if item.strip()
        }
        if not namespaces:
            raise RuntimeError("CONTROL_ALLOWED_NAMESPACES cannot be empty")
        invalid = sorted(
            namespace
            for namespace in namespaces
            if not DNS_LABEL_RE.fullmatch(namespace)
        )
        if invalid:
            raise RuntimeError(f"invalid allowed namespaces: {invalid}")
        image_pull_secrets = [
            item.strip()
            for item in os.environ.get(
                "AGENT_IMAGE_PULL_SECRETS",
                "",
            ).split(",")
            if item.strip()
        ]
        return cls(
            cluster_id=cluster_id,
            allowed_namespaces=namespaces,
            agent_nats_servers=os.environ.get(
                "AGENT_NATS_SERVERS",
                os.environ.get("NATS_SERVERS", "nats://nats:4222"),
            ),
            agent_service_account=(
                os.environ.get("AGENT_SERVICE_ACCOUNT", "").strip() or None
            ),
            image_pull_secrets=image_pull_secrets,
            stream_prefix=os.environ.get("NATS_WORKFLOW_STREAM_PREFIX", "WF"),
            stream_max_bytes=os.environ.get(
                "NATS_STREAM_MAX_BYTES",
                "512MiB",
            ),
            frame_stream_prefix=os.environ.get(
                "NATS_FRAME_STREAM_PREFIX",
                "FRAME",
            ),
            frame_stream_max_bytes=os.environ.get(
                "NATS_FRAME_STREAM_MAX_BYTES",
                "512MiB",
            ),
            frame_stream_max_age_sec=float(
                os.environ.get("NATS_FRAME_STREAM_MAX_AGE_SEC", "120")
            ),
            frame_ack_wait_sec=float(
                os.environ.get("NATS_FRAME_ACK_WAIT_SEC", "60")
            ),
            frame_max_deliver=int(
                os.environ.get("NATS_FRAME_MAX_DELIVER", "3")
            ),
            stream_provision_timeout_sec=int(
                os.environ.get("NATS_STREAM_PROVISION_TIMEOUT_SEC", "120")
            ),
            reconcile_interval_sec=float(
                os.environ.get("CONTROLLER_RECONCILE_INTERVAL_SEC", "30")
            ),
            orphan_grace_sec=float(
                os.environ.get("CONTROLLER_ORPHAN_GRACE_SEC", "300")
            ),
            delete_empty_orphan_streams=(
                os.environ.get(
                    "CONTROLLER_DELETE_EMPTY_ORPHAN_STREAMS",
                    "true",
                ).strip().lower()
                not in {"0", "false", "no"}
            ),
        )


async def _call_in_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


class EdgeLifecycleController:
    def __init__(
        self,
        core_api,
        nats: NatsComm,
        settings: ControllerSettings,
        call_sync: Callable[..., Awaitable[Any]] = _call_in_thread,
    ) -> None:
        self.core = core_api
        self.nats = nats
        self.settings = settings
        self.call_sync = call_sync
        self._reconcile_task: Optional[asyncio.Task] = None
        self._last_reconcile: Dict[str, Any] = {
            "status": "not_started",
            "timestamp": None,
        }
        self._orphan_first_seen: Dict[str, datetime] = {}

    def _check_namespace(self, namespace: str) -> None:
        if namespace not in self.settings.allowed_namespaces:
            raise LifecycleError(
                403,
                f"namespace is not allowed: {namespace}",
                {"allowed_namespaces": sorted(self.settings.allowed_namespaces)},
            )

    async def start(self) -> None:
        try:
            await self.nats.connect(ensure_stream=False)
        except Exception:
            logger.exception(
                "initial NATS connection failed; controller starts degraded"
            )
        if self.settings.reconcile_interval_sec > 0:
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def close(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            await asyncio.gather(
                self._reconcile_task,
                return_exceptions=True,
            )
        await self.nats.close()

    def _managed_labels(
        self,
        request: CreateInstanceRequest,
    ) -> Dict[str, str]:
        labels = dict(request.labels)
        labels.update(
            {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                AGENT_ID_LABEL: request.agent_id,
                CLUSTER_ID_LABEL: self.settings.cluster_id,
                STREAM_ENABLED_LABEL: str(request.workflow_stream).lower(),
                FRAME_STREAM_ENABLED_LABEL: str(
                    request.frame_stream
                ).lower(),
            }
        )
        return labels

    def _container_env(self, request: CreateInstanceRequest):
        values = {
            **request.env,
            "AGENT_ID": request.agent_id,
            "CLUSTER_ID": self.settings.cluster_id,
            "NATS_JETSTREAM_DOMAIN": self.settings.cluster_id,
            "NATS_FRAME_ACK_WAIT_SEC": str(
                self.settings.frame_ack_wait_sec
            ),
            "NATS_FRAME_MAX_DELIVER": str(
                self.settings.frame_max_deliver
            ),
            "NATS_FRAME_STREAM_MAX_AGE_SEC": str(
                self.settings.frame_stream_max_age_sec
            ),
            "NATS_FRAME_STREAM_MAX_BYTES": (
                self.settings.frame_stream_max_bytes
            ),
            "NATS_FRAME_STREAM_PREFIX": self.settings.frame_stream_prefix,
            "NATS_SERVERS": self.settings.agent_nats_servers,
            "NATS_STREAM_DISCARD": "new",
            "NATS_STREAM_MAX_BYTES": self.settings.stream_max_bytes,
            "NATS_STREAM_PROVISION_TIMEOUT_SEC": str(
                self.settings.stream_provision_timeout_sec
            ),
            "NATS_STREAM_RETENTION": "workqueue",
            "NATS_STREAM_STORAGE": "file",
            "NATS_WORKFLOW_STREAM_PREFIX": self.settings.stream_prefix,
        }
        env = [
            client.V1EnvVar(name=name, value=str(value))
            for name, value in sorted(values.items())
        ]
        env.extend(
            [
                client.V1EnvVar(
                    name="AGENT_INSTANCE_ID",
                    value_from=client.V1EnvVarSource(
                        field_ref=client.V1ObjectFieldSelector(
                            field_path="metadata.uid"
                        )
                    ),
                ),
                client.V1EnvVar(
                    name="POD_NAME",
                    value_from=client.V1EnvVarSource(
                        field_ref=client.V1ObjectFieldSelector(
                            field_path="metadata.name"
                        )
                    ),
                ),
            ]
        )
        return env

    def build_pod(self, request: CreateInstanceRequest):
        self._check_namespace(request.namespace)
        container = client.V1Container(
            name="agent",
            image=request.image,
            image_pull_policy=request.image_pull_policy,
            command=request.command,
            args=request.args,
            env=self._container_env(request),
            ports=[
                client.V1ContainerPort(
                    name=port.name,
                    container_port=port.container_port,
                    protocol=port.protocol,
                )
                for port in request.ports
            ]
            or None,
            resources=client.V1ResourceRequirements(
                requests=request.resources.requests or None,
                limits=request.resources.limits or None,
            ),
        )
        pod_spec = client.V1PodSpec(
            containers=[container],
            restart_policy="Always",
            node_selector=request.node_selector or None,
            service_account_name=self.settings.agent_service_account,
            image_pull_secrets=[
                client.V1LocalObjectReference(name=name)
                for name in self.settings.image_pull_secrets
            ]
            or None,
            termination_grace_period_seconds=(
                request.termination_grace_period_seconds
            ),
        )
        return client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=request.name,
                namespace=request.namespace,
                labels=self._managed_labels(request),
                annotations=request.annotations or None,
            ),
            spec=pod_spec,
        )

    async def _read_pod(self, namespace: str, name: str):
        try:
            return await self.call_sync(
                self.core.read_namespaced_pod,
                name=name,
                namespace=namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise LifecycleError(
                exc.status or 500,
                f"Kubernetes read Pod failed: {exc.reason}",
            ) from exc

    def _validate_existing(
        self,
        pod,
        request: CreateInstanceRequest,
    ) -> None:
        labels = pod.metadata.labels or {}
        if (
            labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE
            or labels.get(AGENT_ID_LABEL) != request.agent_id
            or labels.get(STREAM_ENABLED_LABEL, "true")
            != str(request.workflow_stream).lower()
            or labels.get(FRAME_STREAM_ENABLED_LABEL, "true")
            != str(request.frame_stream).lower()
            or not pod.spec.containers
            or pod.spec.containers[0].image != request.image
        ):
            raise LifecycleError(
                409,
                "Pod name already exists and is not the requested managed instance",
                {
                    "namespace": request.namespace,
                    "name": request.name,
                },
            )

    async def create_instance(
        self,
        request: CreateInstanceRequest,
    ) -> Dict[str, Any]:
        self._check_namespace(request.namespace)
        pod = await self._read_pod(request.namespace, request.name)
        created = False
        if pod is None:
            try:
                pod = await self.call_sync(
                    self.core.create_namespaced_pod,
                    namespace=request.namespace,
                    body=self.build_pod(request),
                )
                created = True
            except ApiException as exc:
                raise LifecycleError(
                    exc.status or 500,
                    f"Kubernetes create Pod failed: {exc.reason}",
                ) from exc
        else:
            self._validate_existing(pod, request)

        instance_id = str(pod.metadata.uid or "")
        if not instance_id:
            if created:
                try:
                    await self._delete_pod(request.namespace, request.name, 0)
                except Exception:
                    logger.exception("failed to roll back Pod without UID")
            raise LifecycleError(500, "Kubernetes did not return a Pod UID")

        workflow_stream = None
        frame_stream = None
        try:
            if request.workflow_stream:
                workflow_stream = await self.nats.provision_workflow_stream(
                    target_cluster=self.settings.cluster_id,
                    agent_id=request.agent_id,
                    instance_id=instance_id,
                )
            if request.frame_stream:
                frame_stream = await self.nats.provision_memory_frame_stream(
                    target_cluster=self.settings.cluster_id,
                    agent_id=request.agent_id,
                    instance_id=instance_id,
                )
        except Exception as exc:
            if created and workflow_stream is not None:
                try:
                    await self.nats.delete_workflow_stream(
                        target_cluster=self.settings.cluster_id,
                        instance_id=instance_id,
                    )
                except Exception:
                    logger.exception(
                        "failed to roll back workflow Stream"
                    )
            if created and frame_stream is not None:
                try:
                    await self.nats.delete_memory_frame_stream(
                        target_cluster=self.settings.cluster_id,
                        instance_id=instance_id,
                    )
                except Exception:
                    logger.exception("failed to roll back frame Stream")
            if created:
                try:
                    await self._delete_pod(
                        request.namespace,
                        request.name,
                        0,
                    )
                except Exception:
                    logger.exception(
                        "failed to roll back Pod after Stream error"
                    )
            raise LifecycleError(
                502,
                f"failed to provision instance Streams: {exc}",
                {"pod_rolled_back": created},
            ) from exc

        if request.wait_ready_timeout_sec > 0:
            pod = await self._wait_for_ready(
                request.namespace,
                request.name,
                request.wait_ready_timeout_sec,
            )
        else:
            latest = await self._read_pod(request.namespace, request.name)
            if latest is not None:
                pod = latest

        result = await self.describe_pod(pod, include_stream=False)
        result.update(
            {
                "created": created,
                "stream": workflow_stream,
                "workflow_stream": workflow_stream,
                "frame_stream": frame_stream,
            }
        )
        return result

    async def _wait_for_ready(
        self,
        namespace: str,
        name: str,
        timeout_sec: float,
    ):
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while True:
            pod = await self._read_pod(namespace, name)
            if pod is None:
                raise LifecycleError(404, "Pod disappeared while waiting")
            if self._pod_ready(pod):
                return pod
            if pod.status and pod.status.phase in {"Failed", "Succeeded"}:
                raise LifecycleError(
                    409,
                    f"Pod entered terminal phase: {pod.status.phase}",
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise LifecycleError(
                    504,
                    f"timed out waiting for Pod readiness: {namespace}/{name}",
                )
            await asyncio.sleep(0.5)

    @staticmethod
    def _pod_ready(pod) -> bool:
        return any(
            condition.type == "Ready" and condition.status == "True"
            for condition in (getattr(pod.status, "conditions", None) or [])
        )

    async def describe_pod(
        self,
        pod,
        include_stream: bool = True,
    ) -> Dict[str, Any]:
        labels = pod.metadata.labels or {}
        instance_id = str(pod.metadata.uid or "")
        stream_enabled = (
            labels.get(STREAM_ENABLED_LABEL, "true").lower() == "true"
        )
        frame_stream_enabled = (
            labels.get(FRAME_STREAM_ENABLED_LABEL, "true").lower() == "true"
        )
        stream = None
        stream_error = None
        if include_stream and stream_enabled and instance_id:
            try:
                stream = await self.nats.workflow_stream_status(
                    target_cluster=self.settings.cluster_id,
                    instance_id=instance_id,
                )
            except Exception as exc:
                stream_error = str(exc)
        frame_stream = None
        frame_stream_error = None
        if include_stream and frame_stream_enabled and instance_id:
            try:
                frame_stream = await self.nats.memory_frame_stream_status(
                    target_cluster=self.settings.cluster_id,
                    instance_id=instance_id,
                )
            except Exception as exc:
                frame_stream_error = str(exc)

        phase = getattr(pod.status, "phase", None) or "Unknown"
        ready = self._pod_ready(pod)
        deleting = pod.metadata.deletion_timestamp is not None
        if deleting:
            health = "terminating"
        elif phase in {"Failed", "Succeeded"}:
            health = "failed"
        elif phase == "Running" and ready:
            health = "healthy"
        else:
            health = "starting"
        if stream_enabled and (
            stream_error or (stream is not None and not stream["exists"])
        ):
            health = "degraded"
        if frame_stream_enabled and (
            frame_stream_error
            or (
                frame_stream is not None
                and not frame_stream["exists"]
            )
        ):
            health = "degraded"

        containers = []
        for status in getattr(pod.status, "container_statuses", None) or []:
            containers.append(
                {
                    "name": status.name,
                    "ready": status.ready,
                    "restart_count": status.restart_count,
                    "image": status.image,
                }
            )
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "agent_id": labels.get(AGENT_ID_LABEL),
            "cluster_id": labels.get(
                CLUSTER_ID_LABEL,
                self.settings.cluster_id,
            ),
            "instance_id": instance_id,
            "pod": {
                "phase": phase,
                "ready": ready,
                "health": health,
                "deleting": deleting,
                "pod_ip": getattr(pod.status, "pod_ip", None),
                "host_ip": getattr(pod.status, "host_ip", None),
                "node_name": getattr(pod.spec, "node_name", None),
                "reason": getattr(pod.status, "reason", None),
                "message": getattr(pod.status, "message", None),
                "containers": containers,
            },
            "stream_enabled": stream_enabled,
            "stream": stream,
            "stream_error": stream_error,
            "frame_stream_enabled": frame_stream_enabled,
            "frame_stream": frame_stream,
            "frame_stream_error": frame_stream_error,
        }

    async def get_instance(
        self,
        namespace: str,
        name: str,
    ) -> Dict[str, Any]:
        self._check_namespace(namespace)
        pod = await self._read_pod(namespace, name)
        if pod is None:
            raise LifecycleError(404, f"Pod not found: {namespace}/{name}")
        labels = pod.metadata.labels or {}
        if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
            raise LifecycleError(404, "managed instance not found")
        return await self.describe_pod(pod)

    async def list_instances(
        self,
        namespace: Optional[str] = None,
    ) -> Dict[str, Any]:
        namespaces = (
            [namespace]
            if namespace is not None
            else sorted(self.settings.allowed_namespaces)
        )
        instances = []
        for item in namespaces:
            self._check_namespace(item)
            try:
                response = await self.call_sync(
                    self.core.list_namespaced_pod,
                    namespace=item,
                    label_selector=f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
                )
            except ApiException as exc:
                raise LifecycleError(
                    exc.status or 500,
                    f"Kubernetes list Pods failed: {exc.reason}",
                ) from exc
            for pod in response.items:
                instances.append(await self.describe_pod(pod))
        instances.sort(key=lambda item: (item["namespace"], item["name"]))
        return {"instances": instances, "count": len(instances)}

    async def _delete_pod(
        self,
        namespace: str,
        name: str,
        grace_period_seconds: int,
    ) -> bool:
        try:
            await self.call_sync(
                self.core.delete_namespaced_pod,
                name=name,
                namespace=namespace,
                grace_period_seconds=grace_period_seconds,
                propagation_policy="Foreground",
            )
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise LifecycleError(
                exc.status or 500,
                f"Kubernetes delete Pod failed: {exc.reason}",
            ) from exc

    async def delete_instance(
        self,
        namespace: str,
        name: str,
        instance_id: Optional[str] = None,
        drain_timeout_sec: float = 30,
        force: bool = False,
        pod_grace_period_seconds: int = 30,
    ) -> Dict[str, Any]:
        self._check_namespace(namespace)
        if drain_timeout_sec < 0 or drain_timeout_sec > 300:
            raise LifecycleError(422, "drain_timeout_sec must be 0..300")
        if pod_grace_period_seconds < 0 or pod_grace_period_seconds > 300:
            raise LifecycleError(
                422,
                "pod_grace_period_seconds must be 0..300",
            )
        pod = await self._read_pod(namespace, name)
        stream_enabled = True
        frame_stream_enabled = True
        if pod is not None:
            labels = pod.metadata.labels or {}
            if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
                raise LifecycleError(409, "refusing to delete unmanaged Pod")
            pod_uid = str(pod.metadata.uid or "")
            if instance_id and instance_id != pod_uid:
                raise LifecycleError(409, "instance_id does not match Pod UID")
            instance_id = pod_uid
            stream_enabled = (
                labels.get(STREAM_ENABLED_LABEL, "true").lower() == "true"
            )
            frame_stream_enabled = (
                labels.get(
                    FRAME_STREAM_ENABLED_LABEL,
                    "true",
                ).lower()
                == "true"
            )
        if not instance_id:
            return {
                "deleted": False,
                "pod_deleted": False,
                "stream_deleted": False,
                "frame_stream_deleted": False,
                "reason": "instance_not_found",
                "namespace": namespace,
                "name": name,
            }

        stream_status = None
        frame_stream_status = None
        drain_tasks = []
        drain_kinds = []
        if stream_enabled:
            drain_kinds.append("workflow")
            drain_tasks.append(
                self._wait_for_stream_drain(
                    instance_id,
                    drain_timeout_sec,
                    self.nats.workflow_stream_status,
                )
            )
        if frame_stream_enabled:
            drain_kinds.append("frame")
            drain_tasks.append(
                self._wait_for_stream_drain(
                    instance_id,
                    drain_timeout_sec,
                    self.nats.memory_frame_stream_status,
                )
            )
        if drain_tasks:
            drain_results = await asyncio.gather(*drain_tasks)
            statuses = dict(zip(drain_kinds, drain_results))
            stream_status = statuses.get("workflow")
            frame_stream_status = statuses.get("frame")
        pending_statuses = [
            status
            for status in (stream_status, frame_stream_status)
            if status is not None and status["messages"] > 0
        ]
        if pending_statuses and not force:
            raise LifecycleError(
                409,
                "instance Streams still contain messages",
                {
                    "hint": "remove the instance from routing, retry later, "
                    "or explicitly use force=true",
                    "streams": pending_statuses,
                },
            )

        pod_deleted = await self._delete_pod(
            namespace,
            name,
            pod_grace_period_seconds,
        )
        stream_deleted = False
        frame_stream_deleted = False
        dropped_messages = sum(
            status["messages"]
            for status in (stream_status, frame_stream_status)
            if status is not None
        )
        if stream_enabled and stream_status is not None:
            stream_deleted = await self.nats.delete_workflow_stream(
                target_cluster=self.settings.cluster_id,
                instance_id=instance_id,
            )
        if frame_stream_enabled and frame_stream_status is not None:
            frame_stream_deleted = (
                await self.nats.delete_memory_frame_stream(
                    target_cluster=self.settings.cluster_id,
                    instance_id=instance_id,
                )
            )
        return {
            "deleted": (
                stream_deleted or frame_stream_deleted or pod_deleted
            ),
            "pod_deleted": pod_deleted,
            "stream_deleted": stream_deleted,
            "workflow_stream_deleted": stream_deleted,
            "frame_stream_deleted": frame_stream_deleted,
            "forced": force,
            "dropped_messages": dropped_messages,
            "namespace": namespace,
            "name": name,
            "instance_id": instance_id,
        }

    async def _wait_for_stream_drain(
        self,
        instance_id: str,
        timeout_sec: float,
        status_method,
    ) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while True:
            status = await status_method(
                target_cluster=self.settings.cluster_id,
                instance_id=instance_id,
            )
            if not status["exists"] or status["messages"] == 0:
                return status
            if asyncio.get_running_loop().time() >= deadline:
                return status
            await asyncio.sleep(0.5)

    async def cluster_health(self) -> Dict[str, Any]:
        kubernetes_status: Dict[str, Any]
        nats_status: Dict[str, Any]
        namespace = sorted(self.settings.allowed_namespaces)[0]
        try:
            await self.call_sync(
                self.core.list_namespaced_pod,
                namespace=namespace,
                limit=1,
            )
            kubernetes_status = {"healthy": True}
        except Exception as exc:
            kubernetes_status = {"healthy": False, "error": str(exc)}
        try:
            workflow_streams, frame_streams = await asyncio.gather(
                self.nats.list_workflow_streams(self.settings.cluster_id),
                self.nats.list_memory_frame_streams(
                    self.settings.cluster_id
                ),
            )
            nats_status = {
                "healthy": True,
                "domain": self.settings.cluster_id,
                "instance_streams": len(workflow_streams),
                "workflow_streams": len(workflow_streams),
                "frame_streams": len(frame_streams),
            }
        except Exception as exc:
            nats_status = {
                "healthy": False,
                "domain": self.settings.cluster_id,
                "error": str(exc),
            }
        healthy = kubernetes_status["healthy"] and nats_status["healthy"]
        return {
            "status": "healthy" if healthy else "degraded",
            "cluster_id": self.settings.cluster_id,
            "kubernetes": kubernetes_status,
            "nats": nats_status,
            "reconcile": self._last_reconcile,
        }

    async def reconcile_once(self) -> Dict[str, Any]:
        live: Dict[str, Dict[str, str]] = {}
        provision_errors = []
        for namespace in sorted(self.settings.allowed_namespaces):
            response = await self.call_sync(
                self.core.list_namespaced_pod,
                namespace=namespace,
                label_selector=f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
            )
            for pod in response.items:
                if pod.metadata.deletion_timestamp is not None:
                    continue
                labels = pod.metadata.labels or {}
                uid = str(pod.metadata.uid or "")
                if not uid:
                    continue
                live[uid] = {
                    "namespace": namespace,
                    "name": pod.metadata.name,
                    "agent_id": labels.get(AGENT_ID_LABEL, ""),
                    "stream_enabled": labels.get(
                        STREAM_ENABLED_LABEL,
                        "true",
                    ),
                    "frame_stream_enabled": labels.get(
                        FRAME_STREAM_ENABLED_LABEL,
                        "true",
                    ),
                }
                if (
                    live[uid]["stream_enabled"].lower() == "true"
                    and live[uid]["agent_id"]
                ):
                    try:
                        await self.nats.provision_workflow_stream(
                            target_cluster=self.settings.cluster_id,
                            agent_id=live[uid]["agent_id"],
                            instance_id=uid,
                        )
                    except Exception as exc:
                        provision_errors.append(
                            {
                                "instance_id": uid,
                                "kind": "workflow",
                                "error": str(exc),
                            }
                        )
                if (
                    live[uid]["frame_stream_enabled"].lower() == "true"
                    and live[uid]["agent_id"]
                ):
                    try:
                        await self.nats.provision_memory_frame_stream(
                            target_cluster=self.settings.cluster_id,
                            agent_id=live[uid]["agent_id"],
                            instance_id=uid,
                        )
                    except Exception as exc:
                        provision_errors.append(
                            {
                                "instance_id": uid,
                                "kind": "frame",
                                "error": str(exc),
                            }
                        )

        workflow_streams, frame_streams = await asyncio.gather(
            self.nats.list_workflow_streams(self.settings.cluster_id),
            self.nats.list_memory_frame_streams(self.settings.cluster_id),
        )
        now = datetime.now(timezone.utc)
        orphan_streams = []
        deleted_empty_orphans = []
        current_orphan_keys = set()
        stream_groups = (
            (
                "workflow",
                workflow_streams,
                self.nats.delete_workflow_stream,
            ),
            (
                "frame",
                frame_streams,
                self.nats.delete_memory_frame_stream,
            ),
        )
        for kind, streams, delete_method in stream_groups:
            for stream in streams:
                if stream["instance_id"] in live:
                    continue
                orphan_key = f"{kind}:{stream['instance_id']}"
                current_orphan_keys.add(orphan_key)
                if stream.get("created"):
                    created = datetime.fromisoformat(stream["created"])
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age_sec = max(
                        0.0,
                        (now - created).total_seconds(),
                    )
                else:
                    first_seen = self._orphan_first_seen.setdefault(
                        orphan_key,
                        now,
                    )
                    age_sec = max(
                        0.0,
                        (now - first_seen).total_seconds(),
                    )
                orphan = {**stream, "kind": kind, "age_sec": age_sec}
                orphan_streams.append(orphan)
                eligible = (
                    self.settings.delete_empty_orphan_streams
                    and stream["messages"] == 0
                    and age_sec >= self.settings.orphan_grace_sec
                )
                if eligible:
                    deleted = await delete_method(
                        target_cluster=self.settings.cluster_id,
                        instance_id=stream["instance_id"],
                    )
                    if deleted:
                        deleted_empty_orphans.append(stream["stream"])
                        self._orphan_first_seen.pop(orphan_key, None)

        for orphan_key in (
            set(self._orphan_first_seen) - current_orphan_keys
        ):
            self._orphan_first_seen.pop(orphan_key, None)

        self._last_reconcile = {
            "status": "ok" if not provision_errors else "degraded",
            "timestamp": now.isoformat(),
            "live_instances": len(live),
            "instance_streams": len(workflow_streams) + len(frame_streams),
            "workflow_streams": len(workflow_streams),
            "frame_streams": len(frame_streams),
            "provision_errors": provision_errors,
            "orphan_streams": orphan_streams,
            "deleted_empty_orphans": deleted_empty_orphans,
        }
        return self._last_reconcile

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("edge lifecycle reconciliation failed")
                self._last_reconcile = {
                    "status": "error",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            await asyncio.sleep(self.settings.reconcile_interval_sec)
