import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from control_api.main import app


class ControlApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = SimpleNamespace(
            create_instance=AsyncMock(
                return_value={
                    "created": True,
                    "instance_id": "pod-uid-a",
                }
            ),
            delete_instance=AsyncMock(
                return_value={
                    "deleted": True,
                    "dropped_messages": 0,
                }
            ),
            cluster_health=AsyncMock(
                return_value={
                    "status": "healthy",
                    "cluster_id": "edge-a",
                }
            ),
        )
        app.state.controller = self.controller
        app.state.api_token = "test-token"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_management_endpoint_requires_bearer_token(self):
        response = await self.client.post(
            "/v1/instances",
            json={
                "name": "detector-1",
                "agent_id": "detector",
                "image": "detector:v1",
            },
        )

        self.assertEqual(response.status_code, 401)
        self.controller.create_instance.assert_not_awaited()

    async def test_create_instance_passes_validated_request(self):
        response = await self.client.post(
            "/v1/instances",
            headers={"Authorization": "Bearer test-token"},
            json={
                "name": "detector-1",
                "agent_id": "detector",
                "image": "detector:v1",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["instance_id"], "pod-uid-a")
        request = self.controller.create_instance.await_args.args[0]
        self.assertEqual(request.agent_id, "detector")

    async def test_delete_forwards_drain_and_force_controls(self):
        response = await self.client.delete(
            "/v1/instances/default/detector-1"
            "?drain_timeout_sec=0&force=true&pod_grace_period_seconds=5",
            headers={"Authorization": "Bearer test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.controller.delete_instance.assert_awaited_once_with(
            namespace="default",
            name="detector-1",
            instance_id=None,
            drain_timeout_sec=0.0,
            force=True,
            pod_grace_period_seconds=5,
        )

    async def test_ready_probe_is_public(self):
        response = await self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cluster_id"], "edge-a")


if __name__ == "__main__":
    unittest.main()
