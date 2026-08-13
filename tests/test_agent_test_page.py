import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from src.api.app import (
    _AgentTestCallRequest,
    agent_test_page,
    app,
    test_call_agent as call_agent_endpoint,
)
from src.protocols.a2a_protocol import A2ATaskResponse


def _instance(status="running"):
    return SimpleNamespace(
        instance_id="inst_agent-a_123456",
        agent_id="agent-a",
        status=status,
    )


def _install_dependencies(
    monkeypatch,
    *,
    instance=None,
    agent_url="http://agent-a:9001",
    response=None,
):
    captured = {}
    lifecycle = SimpleNamespace(get_instance=lambda _instance_id: instance)
    router = SimpleNamespace(route_direct=lambda _agent_id: agent_url)
    registry = SimpleNamespace(
        get_agent_by_id=lambda _agent_id: {"capability": "vision"}
    )

    class FakeA2AClient:
        async def send_task_request(self, url, request):
            captured["url"] = url
            captured["request"] = request
            return response or A2ATaskResponse(
                task_id=request.task_id,
                state="success",
                result={"answer": "ok"},
                metadata={"transport": "a2a-python"},
            )

    monkeypatch.setattr(
        "src.runtime.lifecycle_manager.get_lifecycle_manager",
        lambda: lifecycle,
    )
    monkeypatch.setattr("src.service.message_router.get_message_router", lambda: router)
    monkeypatch.setattr(
        "src.service.agent_registry.get_registry_client",
        lambda: registry,
    )
    monkeypatch.setattr(
        "src.service.a2a_client.get_global_a2a_client",
        lambda: FakeA2AClient(),
    )
    return captured


def test_agent_test_page_renders():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "app": app,
        }
    )
    response = asyncio.run(agent_test_page(request))
    template = Path("src/api/templates/test.html").read_text(encoding="utf-8")

    assert response.template.name == "test.html"
    assert "Agent 实例测试" in template
    assert "instances-body" in template
    assert "/js/test.js" in template
    assert "/css/test.css" in template


def test_agent_test_call_routes_and_passes_payload(monkeypatch):
    captured = _install_dependencies(monkeypatch, instance=_instance())

    body = asyncio.run(
        call_agent_endpoint(
            _AgentTestCallRequest(
                instance_id="inst_agent-a_123456",
                task_description="检测车辆",
                parameters={"threshold": 0.7},
                metadata={"trace": True},
            )
        )
    )

    assert body["instance_id"] == "inst_agent-a_123456"
    assert body["agent_id"] == "agent-a"
    assert body["status"] == "success"
    assert body["result"] == {"answer": "ok"}
    assert body["task_id"].startswith("test_")
    assert captured["url"] == "http://agent-a:9001"
    request = captured["request"]
    assert request.task_type == "vision"
    assert request.task_description == "检测车辆"
    assert request.parameters == {"threshold": 0.7}
    assert request.metadata == {"trace": True}


def test_agent_test_call_returns_business_error(monkeypatch):
    response_model = A2ATaskResponse(
        task_id="remote-task",
        state="error",
        result=None,
        error_message="remote failed",
        metadata={"transport": "a2a-python"},
    )
    _install_dependencies(
        monkeypatch,
        instance=_instance(),
        response=response_model,
    )

    body = asyncio.run(
        call_agent_endpoint(
            _AgentTestCallRequest(
                instance_id="inst_agent-a_123456",
                task_description="失败任务",
            )
        )
    )

    assert body["status"] == "error"
    assert body["error_message"] == "remote failed"


def test_agent_test_call_rejects_missing_instance(monkeypatch):
    _install_dependencies(monkeypatch, instance=None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            call_agent_endpoint(
                _AgentTestCallRequest(
                    instance_id="missing",
                    task_description="测试",
                )
            )
        )

    assert exc_info.value.status_code == 404
    assert "不存在" in exc_info.value.detail


def test_agent_test_call_rejects_non_running_instance(monkeypatch):
    _install_dependencies(monkeypatch, instance=_instance(status="stopped"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            call_agent_endpoint(
                _AgentTestCallRequest(
                    instance_id="inst_agent-a_123456",
                    task_description="测试",
                )
            )
        )

    assert exc_info.value.status_code == 409
    assert "stopped" in exc_info.value.detail


def test_agent_test_call_rejects_missing_route(monkeypatch):
    _install_dependencies(monkeypatch, instance=_instance(), agent_url=None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            call_agent_endpoint(
                _AgentTestCallRequest(
                    instance_id="inst_agent-a_123456",
                    task_description="测试",
                )
            )
        )

    assert exc_info.value.status_code == 503
    assert "没有可用路由" in exc_info.value.detail


def test_agent_test_call_validates_required_description(monkeypatch):
    with pytest.raises(ValidationError):
        _AgentTestCallRequest(
            instance_id="inst_agent-a_123456",
            task_description="",
        )
