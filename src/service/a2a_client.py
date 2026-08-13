"""
A2A (Agent-to-Agent) 客户端

用于 L2 Scheduler 和 L3 Agents 之间的通信。

主路径使用 a2a-python(a2a-sdk) 的标准 Agent Card + JSON-RPC 客户端；
旧的 /a2a/execute 协议仅作为兼容未升级 Agent 的后备路径保留。
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

import httpx

from ..protocols.a2a_protocol import (
    A2AMessage,
    A2AProgressNotification,
    A2ATaskRequest,
    A2ATaskResponse,
)

logger = logging.getLogger(__name__)


def _model_dump(model) -> dict:
    """
    兼容 pydantic v1/v2 的 model dump。

    旧协议仍需要把 DTO 序列化成 dict 后放进 A2AMessage.payload；
    pydantic v2 使用 model_dump()，v1 使用 dict()。
    """
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _legacy_task_payload(request: A2ATaskRequest) -> dict:
    """构造兼容旧版 A2ATaskRequest 的 payload。"""
    payload = _model_dump(request)
    parameters = payload.pop("parameters", {})
    metadata = dict(payload.get("metadata") or {})
    metadata["parameters"] = parameters
    payload["metadata"] = metadata
    return payload


def _copy_response(response: A2ATaskResponse, metadata: dict) -> A2ATaskResponse:
    """
    返回一个带新 metadata 的响应副本。

    A2ATaskResponse 可能运行在 pydantic v1 或 v2 环境；
    统一用副本更新，避免在多个调用层共享并修改同一个 metadata dict。
    """
    if hasattr(response, "model_copy"):
        return response.model_copy(update={"metadata": metadata})
    return response.copy(update={"metadata": metadata})


def _env_flag_enabled(name: str, default: bool = True) -> bool:
    """解析布尔环境开关，支持 0/false/no/off 关闭。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class _LegacyA2AClient:
    """
    旧版自研 A2A wire protocol 客户端。

    TODO: 所有 Agent 迁移到 a2a-python 后，删除本类和 A2AMessage wire 兼容结构。
    """

    def __init__(self, sender_id: str, timeout: httpx.Timeout):
        self.sender_id = sender_id
        self.timeout = timeout

    async def send_task_request(
        self,
        agent_url: str,
        request: A2ATaskRequest,
    ) -> A2ATaskResponse:
        """
        使用旧 /a2a/execute endpoint 发送任务请求。

        该路径只作为标准 A2A 调用失败后的兼容后备。它仍使用旧的
        A2AMessage 信封和 A2ATaskRequest/A2ATaskResponse payload。
        """
        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="request",
            payload=_legacy_task_payload(request),
        )

        logger.info(f"📤 发送旧版 A2A 请求 [{message.message_id[:8]}]: {request.task_description[:50]}...")
        logger.debug(f"目标旧版 Agent: {agent_url}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{agent_url}/a2a/execute",
                    json=_model_dump(message),
                )
                response.raise_for_status()

                response_message = A2AMessage(**response.json())
                task_response = A2ATaskResponse(**response_message.payload)

                logger.info(f"✅ 收到旧版 A2A 响应 [{message.message_id[:8]}]: {task_response.state}")
                return task_response

        except httpx.TimeoutException:
            logger.error(f"⏱️ 旧版 A2A 请求超时: {agent_url}")
            return A2ATaskResponse(
                task_id=request.task_id,
                state="timeout",
                result=None,
                error_message=f"Request timeout after {self.timeout.read}s",
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"❌ 旧版 A2A HTTP 错误: {e.response.status_code}")
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=None,
                error_message=f"HTTP {e.response.status_code}: {e.response.text}",
            )

        except Exception as e:
            logger.error(f"❌ 旧版 A2A 请求失败: {e}")
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=None,
                error_message=str(e),
            )

    def _extract_agent_id(self, url: str) -> str:
        try:
            parts = url.split("//")[1].split(":")
            ip = parts[0].replace(".", "_")
            port = parts[1] if len(parts) > 1 else "80"
            return f"agent_{ip}_{port}"
        except Exception:
            return "unknown_agent"


class A2AClient:
    """
    A2A 协议客户端。

    对外保持项目原有 send_task_request(agent_url, A2ATaskRequest) 接口；
    内部优先使用 a2a-python 标准客户端，必要时后备到旧协议。
    """

    def __init__(self, sender_id: str = "l2_scheduler"):
        self.sender_id = sender_id
        self.timeout = httpx.Timeout(60.0, connect=5.0)
        self._legacy_client = _LegacyA2AClient(sender_id=sender_id, timeout=self.timeout)

    async def send_task_request(
        self,
        agent_url: str,
        request: A2ATaskRequest,
    ) -> A2ATaskResponse:
        """
        发送 A2A 任务请求。

        新版 Agent 通过 a2a-python 的 Agent Card + JSON-RPC 调用；
        未升级 Agent 通过 A2A_LEGACY_FALLBACK 控制是否后备到旧 /a2a/execute。
        """
        start = time.monotonic()
        standard_error: Optional[Exception] = None

        try:
            # 标准路径只要返回了有效业务响应，就直接交给上层处理。
            # 远端返回 failed/input_required/canceled 属于标准 A2A 语义，不触发 legacy fallback。
            logger.info(f"📤 发送标准 A2A 请求: {request.task_description[:50]}...")
            response = await self._send_with_a2a_python(agent_url, request)
            return self._with_qos_metadata(
                response=response,
                request=request,
                agent_url=agent_url,
                start=start,
                transport="a2a-python",
            )
        except Exception as exc:
            # 只有 Agent Card 获取失败、SDK/网络异常、协议解析异常等“调用失败”
            # 才进入旧协议后备检查。
            standard_error = exc
            logger.warning(f"⚠️ 标准 A2A 调用失败，将检查旧协议后备: {exc}")

        if not _env_flag_enabled("A2A_LEGACY_FALLBACK", default=True):
            # 显式禁用 fallback 时，保留标准路径错误原因，便于迁移期排查未升级 Agent。
            return self._with_qos_metadata(
                response=A2ATaskResponse(
                    task_id=request.task_id,
                    state="error",
                    result=None,
                    error_message=f"标准 A2A 调用失败且旧协议后备已禁用: {standard_error}",
                    metadata={"standard_a2a_error": str(standard_error)},
                ),
                request=request,
                agent_url=agent_url,
                start=start,
                transport="a2a-python",
            )

        # legacy 响应仍会补充本次标准路径失败原因，方便后续统计哪些 Agent 仍需迁移。
        legacy_response = await self._legacy_client.send_task_request(agent_url, request)
        metadata = dict(legacy_response.metadata or {})
        if standard_error:
            metadata["standard_a2a_error"] = str(standard_error)
            legacy_response = _copy_response(legacy_response, metadata)
        return self._with_qos_metadata(
            response=legacy_response,
            request=request,
            agent_url=agent_url,
            start=start,
            transport="legacy",
        )

    async def _send_with_a2a_python(
        self,
        agent_url: str,
        request: A2ATaskRequest,
    ) -> A2ATaskResponse:
        try:
            from a2a.client import ClientConfig, create_client
            from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
            from google.protobuf.json_format import ParseDict
            from google.protobuf.struct_pb2 import Value
        except Exception as exc:
            raise RuntimeError("a2a-sdk 未安装或无法导入") from exc

        client = None
        async with httpx.AsyncClient(timeout=self.timeout) as httpx_client:
            # create_client(agent=<url>) 会在 SDK 内部获取 /.well-known/agent-card.json，
            # 再根据 Agent Card 中的 supported_interfaces 创建合适的 transport。
            # 这里传入同一个 httpx_client，是为了让 Agent Card 获取和 JSON-RPC 请求
            # 共享本类统一的 timeout 设置。
            client = await create_client(
                agent=agent_url,
                client_config=ClientConfig(
                    streaming=False,
                    httpx_client=httpx_client,
                ),
            )
            try:
                data = ParseDict(self._standard_message_data(request), Value())
                message = Message(
                    message_id=uuid4().hex,
                    role=Role.ROLE_USER,
                    parts=[Part(data=data, media_type="application/json")],
                )
                send_request = SendMessageRequest(message=message)
                final_event = None
                # streaming=False 时 SDK 仍以异步迭代器形式返回事件；
                # 最后一个事件代表最终 Message/Task/StreamResponse。
                async for chunk in client.send_message(send_request):
                    final_event = chunk
                return self._response_from_a2a_python_event(request, final_event)
            finally:
                if client and hasattr(client, "close"):
                    await client.close()

    def _standard_message_data(self, request: A2ATaskRequest) -> dict:
        """
        构造标准 A2A data Part 的结构化内容。

        Agent Template 从 application/json Part 中读取任务描述、元数据和业务参数。
        三个字段始终发送，以便接收端使用稳定的消息结构。
        """
        # 先经过 JSON 归一化，延续旧 text Part 对非原生 JSON 类型使用 str 的行为，
        # 再交给 ParseDict 转换为 google.protobuf.Value。
        return json.loads(
            json.dumps(
                {
                    "task_description": request.task_description,
                    "metadata": request.metadata,
                    "parameters": request.parameters,
                },
                ensure_ascii=False,
                default=str,
            )
        )

    def _response_from_a2a_python_event(
        self,
        request: A2ATaskRequest,
        event: Any,
    ) -> A2ATaskResponse:
        """
        将 a2a-sdk 返回的 Task/Message/StreamResponse 归一化为项目内部响应。

        统一转换的目标是让 UnifiedExecutor 不依赖 SDK 类型：
        - Message(parts) 映射为 success + parts 文本结果；
        - Task(status.state=completed) 映射为 success；
        - Task failed/input_required/canceled 映射为项目内部 error/cancelled；
        - SDK error 或未知结构映射为 error。
        """
        event = self._unwrap_a2a_event(event)
        if event is None:
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=None,
                error_message="标准 A2A 未返回结果",
            )

        error = getattr(event, "error", None)
        if error is not None:
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=None,
                error_message=str(error),
                metadata=self._a2a_event_metadata(event, state="error"),
            )

        if hasattr(event, "parts") and not hasattr(event, "status"):
            result = self._extract_parts_result(getattr(event, "parts", []))
            return A2ATaskResponse(
                task_id=request.task_id,
                state="success",
                result=result,
                metadata=self._a2a_event_metadata(event, state="message"),
            )

        status_obj = getattr(event, "status", None)
        if status_obj is None:
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=None,
                error_message=f"无法识别的标准 A2A 响应: {type(event).__name__}",
                metadata=self._a2a_event_metadata(event, state="unknown"),
            )

        state = self._normalize_task_state(getattr(status_obj, "state", None))
        artifact_parts: list[Any] = []
        # 标准 A2A 的最终业务结果通常放在 Task.artifacts[*].parts 中。
        # 如果没有 artifact，再退回读取 status.message.parts 作为状态说明。
        for artifact in getattr(event, "artifacts", []) or []:
            artifact_result = self._extract_parts_result(getattr(artifact, "parts", []))
            if artifact_result not in (None, ""):
                artifact_parts.append(artifact_result)

        status_parts: list[Any] = []
        status_message = getattr(status_obj, "message", None)
        if status_message is not None:
            status_result = self._extract_parts_result(getattr(status_message, "parts", []))
            if status_result not in (None, ""):
                status_parts.append(status_result)

        result = self._merge_result_parts(artifact_parts or status_parts)
        metadata = self._a2a_event_metadata(event, state=state)

        if state == "completed":
            return A2ATaskResponse(
                task_id=request.task_id,
                state="success",
                result=result,
                metadata=metadata,
            )
        if state in {"canceled", "cancelled"}:
            return A2ATaskResponse(
                task_id=request.task_id,
                state="cancelled",
                result=result,
                error_message="标准 A2A 任务已取消",
                metadata=metadata,
            )
        if state == "failed":
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=result,
                error_message=self._status_message_text(status_obj) or "标准 A2A 任务失败",
                metadata=metadata,
            )
        if state == "input_required":
            return A2ATaskResponse(
                task_id=request.task_id,
                state="error",
                result=result,
                error_message="标准 A2A 任务需要更多输入",
                metadata=metadata,
            )

        return A2ATaskResponse(
            task_id=request.task_id,
            state="error",
            result=result,
            error_message=f"标准 A2A 任务未完成: {state or 'unknown'}",
            metadata=metadata,
        )

    def _unwrap_a2a_event(self, event: Any) -> Any:
        """
        剥离 SDK/传输层包装，返回真正需要解析的 A2A 对象。

        a2a-sdk 不同路径可能返回：
        - (event, metadata) tuple；
        - pydantic RootModel / JSON-RPC result wrapper；
        - protobuf StreamResponse(task/message/status_update/artifact_update)。
        后续转换逻辑只关心最终的 Task、Message 或 update event，因此这里集中处理包装差异。
        """
        if isinstance(event, tuple) and event:
            return self._unwrap_a2a_event(event[0])

        root = getattr(event, "root", None)
        if root is not None:
            result = getattr(root, "result", None)
            if result is not None:
                return self._unwrap_a2a_event(result)
            return root

        result = getattr(event, "result", None)
        if result is not None and result is not event:
            return self._unwrap_a2a_event(result)

        for field_name in ("task", "message", "status_update", "artifact_update"):
            payload = self._get_set_a2a_payload(event, field_name)
            if payload is not None:
                return self._unwrap_a2a_event(payload)

        return event

    def _get_set_a2a_payload(self, event: Any, field_name: str) -> Any:
        """
        从 protobuf oneof wrapper 中安全读取已设置的 payload 字段。

        a2a_pb2.StreamResponse 同时暴露 task/message/status_update/artifact_update
        属性，但只有 oneof 中实际设置的字段才应被处理；HasField() 用于避免
        误读 protobuf 默认空对象。
        """
        if not hasattr(event, field_name):
            return None

        has_field = getattr(event, "HasField", None)
        if callable(has_field):
            try:
                if not has_field(field_name):
                    return None
            except (ValueError, TypeError):
                pass

        return getattr(event, field_name, None)

    def _normalize_task_state(self, state: Any) -> str:
        """
        归一化 A2A TaskState。

        SDK 可能返回字符串枚举名、Python enum、protobuf 数字值。
        优先读取 a2a_pb2.TaskState descriptor，避免手写数字映射和 SDK 版本漂移。
        fallback 表按 A2A specification 4.1.3 TaskState 的顺序保留。
        """
        if state is None:
            return ""
        if isinstance(state, int):
            enum_name = self._task_state_enum_name(state)
            if enum_name:
                return self._normalize_task_state_name(enum_name)
            return {
                0: "unspecified",
                1: "submitted",
                2: "working",
                3: "completed",
                4: "failed",
                5: "canceled",
                6: "input_required",
                7: "rejected",
                8: "auth_required",
            }.get(state, str(state))

        raw = getattr(state, "name", None) or getattr(state, "value", None) or str(state)
        if isinstance(raw, int):
            return self._normalize_task_state(raw)
        return self._normalize_task_state_name(raw)

    def _task_state_enum_name(self, state: int) -> Optional[str]:
        """
        从 a2a-sdk protobuf descriptor 获取 TaskState 名称。

        这样可以优先跟随安装环境中的 a2a_pb2 枚举定义；只有 SDK 不可用时，
        _normalize_task_state() 才使用下方符合规范顺序的 fallback 表。
        """
        try:
            from a2a.types import a2a_pb2

            enum_value = a2a_pb2.TaskState.DESCRIPTOR.values_by_number.get(state)
            if enum_value is not None:
                return enum_value.name
        except Exception:
            return None
        return None

    def _normalize_task_state_name(self, state: Any) -> str:
        """把 TASK_STATE_COMPLETED / TaskState.COMPLETED 等名称转为 completed。"""
        raw = str(state).split(".")[-1].strip().lower()
        if raw.isdigit():
            return self._normalize_task_state(int(raw))
        if raw.startswith("task_state_"):
            raw = raw[len("task_state_"):]
        return raw

    def _extract_parts_result(self, parts: Any) -> Any:
        """提取标准 A2A Part 列表中的 text/data/file 内容。"""
        results: list[Any] = []
        for part in parts or []:
            root = getattr(part, "root", part)
            text = getattr(root, "text", None)
            if text is None:
                text = getattr(part, "text", None)
            if text is not None:
                results.append(text)
                continue

            data = getattr(root, "data", None)
            if data is not None:
                results.append(data)
                continue

            file_part = getattr(root, "file", None)
            if file_part is not None:
                results.append(
                    {
                        "file": getattr(file_part, "name", None) or "unnamed",
                        "mime_type": getattr(file_part, "mime_type", None),
                    }
                )
        return self._merge_result_parts(results)

    def _merge_result_parts(self, parts: list[Any]) -> Any:
        """合并多个 Part 结果；纯文本多段用换行连接，混合类型保留 list。"""
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        if all(isinstance(part, str) for part in parts):
            return "\n".join(parts)
        return parts

    def _status_message_text(self, status_obj: Any) -> str:
        """提取 Task.status.message.parts 中的文本，主要用于 failed 错误信息。"""
        message = getattr(status_obj, "message", None)
        if message is None:
            return ""
        result = self._extract_parts_result(getattr(message, "parts", []))
        return result if isinstance(result, str) else str(result)

    def _a2a_event_metadata(self, event: Any, state: str) -> dict:
        """保留标准 A2A task/context/state 和远端 QoS metadata。"""
        metadata = {
            "a2a": {
                "task_id": getattr(event, "id", None) or getattr(event, "task_id", None),
                "context_id": getattr(event, "context_id", None),
                "state": state,
            }
        }
        for key, value in self._collect_a2a_metadata(event).items():
            if key != "a2a":
                metadata[key] = value
        return metadata

    def _collect_a2a_metadata(self, event: Any) -> dict:
        """从 Task、Artifact、Message 或 update event 中收集 metadata。"""
        collected: dict[str, Any] = {}

        def merge(source: Any) -> None:
            data = self._metadata_to_dict(source)
            for key, value in data.items():
                collected[key] = value

        merge(getattr(event, "metadata", None))

        message = getattr(event, "message", None)
        if message is not None:
            merge(getattr(message, "metadata", None))

        status = getattr(event, "status", None)
        status_message = getattr(status, "message", None) if status is not None else None
        if status_message is not None:
            merge(getattr(status_message, "metadata", None))

        artifact = getattr(event, "artifact", None)
        if artifact is not None:
            merge(getattr(artifact, "metadata", None))

        for item in getattr(event, "artifacts", []) or []:
            merge(getattr(item, "metadata", None))

        return collected

    def _metadata_to_dict(self, metadata: Any) -> dict:
        """把 protobuf Struct、pydantic model 或普通 dict metadata 转为 dict。"""
        if metadata is None:
            return {}
        if isinstance(metadata, dict):
            return dict(metadata)

        if hasattr(metadata, "DESCRIPTOR"):
            try:
                from google.protobuf.json_format import MessageToDict

                return MessageToDict(metadata, preserving_proto_field_name=True)
            except Exception:
                return {}

        if hasattr(metadata, "model_dump"):
            try:
                return metadata.model_dump()
            except Exception:
                return {}

        if hasattr(metadata, "dict"):
            try:
                return metadata.dict()
            except Exception:
                return {}

        return {}

    def _with_qos_metadata(
        self,
        response: A2ATaskResponse,
        request: A2ATaskRequest,
        agent_url: str,
        start: float,
        transport: str,
    ) -> A2ATaskResponse:
        """
        补充一次 A2A 调用的传输与 QoS metadata，并写入 Prometheus。

        total_latency_ms 是客户端观测到的端到端耗时；如果远端或旧协议已返回
        server_total_ms，则估算 network_ms = total - server_total。
        """
        total_latency_ms = max((time.monotonic() - start) * 1000, 0.0)
        metadata = dict(response.metadata or {})
        metadata["a2a_transport"] = transport

        qos = dict(metadata.get("qos") or {})
        server_total_ms = self._safe_float(qos.get("server_total_ms"))
        network_ms = None
        if server_total_ms is not None:
            network_ms = max(total_latency_ms - server_total_ms, 0.0)

        agent_id = qos.get("agent_id") or self._extract_agent_id(agent_url)
        instance_id = qos.get("instance_id") or "unknown"
        qos.update(
            {
                "task_id": request.task_id,
                "agent_id": agent_id,
                "instance_id": instance_id,
                "agent_url": agent_url,
                "status": response.state,
                "total_latency_ms": round(total_latency_ms, 3),
                "network_ms": round(network_ms, 3) if network_ms is not None else None,
                "transport": transport,
            }
        )
        metadata["qos"] = qos

        from src.runtime.prometheus_metrics import observe_a2a_call

        observe_a2a_call(
            agent_id=agent_id,
            instance_id=instance_id,
            status=response.state,
            total_latency_ms=total_latency_ms,
            network_ms=network_ms,
        )
        logger.info(
            "[A2A QoS] %s",
            json.dumps(qos, ensure_ascii=False, sort_keys=True),
        )
        return _copy_response(response, metadata)

    def _safe_float(self, value: Any) -> Optional[float]:
        """把可选数值转换为 float；转换失败时返回 None，避免 QoS 计算报错。"""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def stream_task_execution(
        self,
        agent_url: str,
        request: A2ATaskRequest,
    ) -> AsyncIterator[A2AProgressNotification]:
        """
        旧版流式接收 A2A 执行进度。

        当前主调用链不使用该方法；待所有 Agent 迁移到 a2a-python streaming 后替换。

        Args:
            agent_url: Agent 服务地址
            request: 任务请求对象

        Yields:
            任务进度通知
        """

        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="request",
            payload=_legacy_task_payload(request),
        )

        logger.info(f"📤 启动流式 A2A 请求 [{message.message_id[:8]}]")

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{agent_url}/a2a/execute/stream",
                    json=_model_dump(message),
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if line:
                            try:
                                progress_data = json.loads(line)
                                yield A2AProgressNotification(**progress_data)
                            except json.JSONDecodeError:
                                logger.warning(f"⚠️ 无法解析进度数据: {line}")

        except Exception as e:
            logger.error(f"❌ 流式 A2A 请求失败: {e}")

    async def send_notification(
        self,
        agent_url: str,
        notification_type: str,
        payload: dict,
    ):
        """
        旧版发送通知消息（不需要响应）。

        当前主调用链不使用该方法；待旧协议下线时删除。

        Args:
            agent_url: Agent 服务地址
            notification_type: 通知类型
            payload: 通知内容
        """

        message = A2AMessage(
            sender_id=self.sender_id,
            receiver_id=self._extract_agent_id(agent_url),
            message_type="notification",
            payload={"type": notification_type, "data": payload},
        )

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{agent_url}/a2a/notify",
                    json=_model_dump(message),
                )
                logger.info(f"📣 通知已发送: {notification_type}")
        except Exception as e:
            logger.warning(f"⚠️ 通知发送失败: {e}")

    def _extract_agent_id(self, url: str) -> str:
        """
        从 URL 提取 Agent ID

        Args:
            url: Agent URL（如 http://192.168.1.10:8080）

        Returns:
            Agent ID（如 agent_192_168_1_10）
        """
        try:
            parts = url.split("//")[1].split(":")
            ip = parts[0].replace(".", "_")
            port = parts[1] if len(parts) > 1 else "80"
            return f"agent_{ip}_{port}"
        except Exception:
            return "unknown_agent"


class A2AHealthChecker:
    """
    A2A Agent 健康检查器

    定期检查 Agent 是否在线
    """

    def __init__(self, check_interval: int = 30):
        self.check_interval = check_interval
        self.agent_status: dict[str, dict] = {}

    async def check_agent_health(self, agent_url: str) -> bool:
        """
        检查单个 Agent 健康状态

        Args:
            agent_url: Agent 服务地址

        Returns:
            是否健康
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{agent_url}/health")

                if response.status_code == 200:
                    data = response.json()
                    self.agent_status[agent_url] = {
                        "status": "online",
                        "load": data.get("load", 0.0),
                        "tasks": data.get("active_tasks", 0),
                    }
                    return True

        except Exception as e:
            logger.warning(f"⚠️ Agent 健康检查失败 [{agent_url}]: {e}")

        self.agent_status[agent_url] = {"status": "offline"}
        return False

    async def check_all_agents(self, agent_urls: list[str]):
        """批量检查所有 Agent"""
        tasks = [self.check_agent_health(url) for url in agent_urls]
        await asyncio.gather(*tasks, return_exceptions=True)

    def get_healthy_agents(self) -> list[str]:
        """获取所有健康的 Agent"""
        return [
            url for url, status in self.agent_status.items()
            if status.get("status") == "online"
        ]

    def get_best_agent(self, agent_urls: list[str]) -> Optional[str]:
        """根据负载选择最佳 Agent"""
        healthy = [
            url
            for url in agent_urls
            if self.agent_status.get(url, {}).get("status") == "online"
        ]

        if not healthy:
            return None

        return min(healthy, key=lambda url: self.agent_status[url].get("load", 1.0))


# 全局单例
_global_a2a_client: Optional[A2AClient] = None


def get_global_a2a_client() -> A2AClient:
    """获取全局 A2A 客户端"""
    global _global_a2a_client
    if _global_a2a_client is None:
        _global_a2a_client = A2AClient()
    return _global_a2a_client
