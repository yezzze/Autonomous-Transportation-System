import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.protocols.a2a_protocol import A2ATaskRequest
from src.service.a2a_client import A2AClient, _legacy_task_payload


def _request(**kwargs) -> A2ATaskRequest:
    values = {
        "task_id": "task-parameters",
        "task_type": "test",
        "task_description": "处理任务",
    }
    values.update(kwargs)
    return A2ATaskRequest(**values)


def test_a2a_task_request_parameters_default_and_value():
    assert _request().parameters == {}
    assert _request(parameters={"threshold": 0.5}).parameters == {"threshold": 0.5}


def test_standard_message_always_uses_complete_structure():
    data = A2AClient()._standard_message_data(_request())

    assert data == {
        "task_description": "处理任务",
        "metadata": {},
        "parameters": {},
    }


def test_standard_message_serializes_metadata_and_parameters():
    data = A2AClient()._standard_message_data(
        _request(
            metadata={"subject": "workflow.input"},
            parameters={"labels": ["车辆", "行人"]},
        )
    )

    assert data == {
        "task_description": "处理任务",
        "metadata": {"subject": "workflow.input"},
        "parameters": {"labels": ["车辆", "行人"]},
    }


def test_standard_sdk_request_uses_data_part(monkeypatch):
    sdk_client_module = pytest.importorskip("a2a.client")
    pytest.importorskip("a2a.types.a2a_pb2")
    from google.protobuf.json_format import MessageToDict

    sent = {}

    class FakeClient:
        async def send_message(self, request):
            sent["request"] = request
            yield SimpleNamespace(parts=[SimpleNamespace(text="ok")])

        async def close(self):
            return None

    async def fake_create_client(*args, **kwargs):
        return FakeClient()

    monkeypatch.setattr(sdk_client_module, "create_client", fake_create_client)

    result = asyncio.run(
        A2AClient()._send_with_a2a_python(
            "http://standard-agent:9001",
            _request(metadata={"subject": "input"}, parameters={"value": 7}),
        )
    )

    assert result.state == "success"
    part = sent["request"].message.parts[0]
    assert part.WhichOneof("content") == "data"
    assert part.media_type == "application/json"
    assert MessageToDict(part.data) == {
        "task_description": "处理任务",
        "metadata": {"subject": "input"},
        "parameters": {"value": 7.0},
    }


def test_legacy_payload_wraps_parameters_in_metadata():
    payload = _legacy_task_payload(
        _request(
            metadata={"subject": "workflow.input", "parameters": {"old": True}},
            parameters={"threshold": 0.75},
        )
    )

    assert "parameters" not in payload
    assert payload["metadata"] == {
        "subject": "workflow.input",
        "parameters": {"threshold": 0.75},
    }


def test_legacy_client_posts_compatible_payload(monkeypatch):
    posted = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "sender_id": "legacy-agent",
                "receiver_id": "l2_scheduler",
                "message_type": "response",
                "payload": {
                    "task_id": "task-parameters",
                    "status": "success",
                    "result": "ok",
                },
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            posted["url"] = url
            posted["json"] = json
            return FakeResponse()

    monkeypatch.setattr("src.service.a2a_client.httpx.AsyncClient", FakeAsyncClient)
    client = A2AClient()

    result = asyncio.run(
        client._legacy_client.send_task_request(
            "http://legacy-agent:9001",
            _request(metadata={"keep": True}, parameters={"value": 3}),
        )
    )

    assert result.state == "success"
    assert posted["url"] == "http://legacy-agent:9001/a2a/execute"
    payload = posted["json"]["payload"]
    assert "parameters" not in payload
    assert payload["metadata"] == {"keep": True, "parameters": {"value": 3}}


def test_legacy_stream_uses_compatible_payload(monkeypatch):
    posted = {}

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield json.dumps(
                {
                    "task_id": "task-parameters",
                    "progress": 0.5,
                    "current_step": "run",
                }
            )

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, json):
            posted["payload"] = json["payload"]
            return FakeStreamContext()

    monkeypatch.setattr("src.service.a2a_client.httpx.AsyncClient", FakeAsyncClient)
    client = A2AClient()

    async def collect():
        return [
            item
            async for item in client.stream_task_execution(
                "http://legacy-agent:9001",
                _request(parameters={"value": 4}),
            )
        ]

    progress = asyncio.run(collect())

    assert len(progress) == 1
    assert "parameters" not in posted["payload"]
    assert posted["payload"]["metadata"]["parameters"] == {"value": 4}


def test_unified_executor_passes_task_parameters(monkeypatch):
    pytest.importorskip("mcp")
    from src.graph.unified_executor import UnifiedExecutor

    executor = UnifiedExecutor()
    executor.a2a_client.send_task_request = AsyncMock(
        return_value=SimpleNamespace(
            state="success",
            result="ok",
            error_message=None,
            metadata={},
        )
    )
    monkeypatch.setattr(executor, "_resolve_agent_url", lambda _task: "http://agent:9001")

    asyncio.run(
        executor._execute_with_a2a(
            {
                "task_id": "task-parameters",
                "task_description": "处理任务",
                "assigned_agent_id": "agent-a",
                "parameters": {"threshold": 0.9},
            }
        )
    )

    request = executor.a2a_client.send_task_request.await_args.args[1]
    assert request.parameters == {"threshold": 0.9}
