from contextlib import asynccontextmanager
from collections import deque
from pathlib import Path
from typing import Any
import base64
import json
import os

import cv2
import numpy as np
import socketio
import torch
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.types.a2a_pb2 import TaskState
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from utils.logger_utils import get_logger
from utils.numpy_utils import decode_array_from_dict
from utils.open3d_utils import render_vis
from protocols import NatsComm

from .model_runtime import model_runtime


BASE_DIR = Path(__file__).resolve().parent
FIXED_MODEL_CHECKPOINT_PATH = os.getenv(
    "MODEL_CHECKPOINT_PATH",
    "checkpoints/point_pillar_where2comm/",
)

A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:9032")
NATS_SERVER_URL = os.getenv("NATS_SERVER_URL", "nats://nats:4222")
NATS_SUBJECT = os.getenv("NATS_SUBJECT", "workflow.demo.perception2feature.result")
NATS_DURABLE = os.getenv("NATS_DURABLE", "workflow-demo-perception2feature-result")

logger = get_logger(__name__)


class CooperativeFeatureFusionWebApp:
    def __init__(self):
        self.socketio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins="*",
        )
        self.templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
        self.data_queue = deque(maxlen=5)
        self.nats_comm = NatsComm(servers=[NATS_SERVER_URL])
        self.model_runtime = model_runtime
        self.fastapi_app: FastAPI | None = None

        logger.info("A2A Agent URL initialized as: %s", A2A_AGENT_URL)
        logger.info("NATS communication initialized with server: %s", NATS_SERVER_URL)

    def build(self) -> socketio.ASGIApp:
        fastapi_app = FastAPI(
            title="Cooperative Feature Fusion Detection Viz Agent API",
            lifespan=self._lifespan,
        )
        fastapi_app.mount(
            "/static",
            StaticFiles(directory=str(BASE_DIR / "static")),
            name="static",
        )
        self._register_routes(fastapi_app)
        self._register_a2a_routes(fastapi_app)
        self._register_socket_events()
        self.fastapi_app = fastapi_app
        return socketio.ASGIApp(self.socketio, other_asgi_app=fastapi_app)

    @asynccontextmanager
    async def _lifespan(self, _: FastAPI):
        self._load_model()
        try:
            yield
        finally:
            await self.nats_comm.close()

    @staticmethod
    def _decode_structured_numpy(payload):
        """将传输友好的结构化数组递归还原为 numpy.ndarray。"""
        if isinstance(payload, dict):
            if {"shape", "dtype", "data"}.issubset(payload.keys()):
                return decode_array_from_dict(payload)
            return {k: CooperativeFeatureFusionWebApp._decode_structured_numpy(v) for k, v in payload.items()}

        if isinstance(payload, list):
            return [CooperativeFeatureFusionWebApp._decode_structured_numpy(v) for v in payload]

        return payload

    @staticmethod
    def _convert_numpy_to_tensor(payload, device="cpu"):
        """递归遍历容器，将 numpy.ndarray 转换为 torch.Tensor。"""
        if isinstance(payload, dict):
            return {
                k: CooperativeFeatureFusionWebApp._convert_numpy_to_tensor(v, device=device)
                for k, v in payload.items()
            }

        if isinstance(payload, list):
            return [CooperativeFeatureFusionWebApp._convert_numpy_to_tensor(v, device=device) for v in payload]

        if isinstance(payload, tuple):
            return tuple(CooperativeFeatureFusionWebApp._convert_numpy_to_tensor(v, device=device) for v in payload)

        if isinstance(payload, np.ndarray):
            return torch.from_numpy(payload.copy()).to(device)

        return payload

    @staticmethod
    def _restore_original_feature_from_dict(feature_dict, target_hw=(96, 352), device="cpu") -> torch.Tensor:
        """从 dict 形式的输入中恢复原始特征图，要求包含 'feature' 和 'mask'。"""
        if not isinstance(feature_dict, dict):
            raise TypeError("feature_dict must be a dict")

        if "feature" not in feature_dict or "mask" not in feature_dict:
            raise KeyError("feature_dict must contain 'feature' and 'mask'")

        feature_dict = CooperativeFeatureFusionWebApp._convert_numpy_to_tensor(feature_dict, device=device)

        masked_feature_tensor = feature_dict["feature"]
        mask_tensor = feature_dict["mask"]

        if not isinstance(masked_feature_tensor, torch.Tensor) or not isinstance(mask_tensor, torch.Tensor):
            raise TypeError("'feature' and 'mask' must be torch.Tensor")

        # 1. 插值掩码到目标尺寸
        target_h, target_w = target_hw
        if mask_tensor.shape[-2:] != (target_h, target_w):
            mask_tensor = torch.nn.functional.interpolate(
                mask_tensor.float(),
                size=(target_h, target_w),
                mode="bilinear",
                align_corners=False,
            )     # [1, 1, H, W]

        # 2. 获取掩码的非零位置索引
        spatial_mask = mask_tensor.squeeze(0).squeeze(0)       # [H, W]
        non_zero_indices = torch.nonzero(spatial_mask != 0, as_tuple=False) # [N, 2]

        # 3. 创建全零特征图
        c = masked_feature_tensor.shape[0]
        feature = torch.zeros(
            (1, c, target_h, target_w),
            dtype=masked_feature_tensor.dtype,
            device=masked_feature_tensor.device,
        )             # [1, C, H, W]

        if non_zero_indices.numel() == 0:
            return feature

        if masked_feature_tensor.shape[1] != non_zero_indices.shape[0]:
            raise ValueError(
                f"Feature count mismatch: {masked_feature_tensor.shape[1]} vs {non_zero_indices.shape[0]}"
            )
        # 4. 将提取的特征填充到非零位置
        # 对于每个通道
        for ch in range(c):
            # 在非零位置填充值
            feature[0, ch, non_zero_indices[:, 0], non_zero_indices[:, 1]] = masked_feature_tensor[ch]

        return feature

    def _load_model(self):
        try:
            model_runtime.load_model(FIXED_MODEL_CHECKPOINT_PATH)
            logger.info("Model loaded from fixed path: %s", FIXED_MODEL_CHECKPOINT_PATH)
        except FileNotFoundError as exc:
            logger.error("Fixed checkpoint missing (%s), loading built-in demo weights", exc)
        except Exception as exc:
            logger.exception("Failed to load model during startup")
            raise RuntimeError(f"Startup model loading failed: {exc}") from exc

    async def _receive_data(self, nats_subject=NATS_SUBJECT, nats_durable=NATS_DURABLE) -> dict | None:
        try:
            messages = await self.nats_comm.receive(
                subject=nats_subject,
                durable=nats_durable,
                batch=1,
                timeout_sec=5,
            )
            for message in messages:
                logger.info("Received message on subject '%s'", nats_subject)
                await message.ack()
                return message.payload
            return None
        finally:
            await self.nats_comm.close()

    async def run_forward(self, nats_subject=NATS_SUBJECT, nats_durable=NATS_DURABLE) -> dict:
        data = await self._receive_data(nats_subject=nats_subject, nats_durable=nats_durable)

        if data is None:
            raise HTTPException(status_code=404, detail="No data available")

        try:
            decoded_data = self._decode_structured_numpy(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to decode numpy payload: {exc}") from exc

        masked_intermediate_feature = decoded_data.get("intermediate_feature")
        if masked_intermediate_feature is None:
            raise HTTPException(status_code=400, detail="intermediate_feature is missing in the payload")

        masked_intermediate_feature_tensor = self._convert_numpy_to_tensor(
            masked_intermediate_feature,
            device=self.model_runtime.device,
        )
        try:
            intermediate_feature_tensor = self._restore_original_feature_from_dict(
                masked_intermediate_feature_tensor,
                target_hw=(96, 352),
                device=self.model_runtime.device,
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Failed to restore feature map: {exc}") from exc

        intermediate_features = [intermediate_feature_tensor]

        ego_pred_box, fused_pred_box, ego_feature, fused_feature = self.model_runtime.process_intermediate_features(
            intermediate_features
        )

        pcd = decoded_data.get("pcd")
        pcd_img = render_vis(None, pcd, ego_pred_box, fused_pred_box)

        await self._update_images(pcd_img=pcd_img, ego_feature=ego_feature, fused_feature=fused_feature)

        return {
            "status": "success",
            "pred_box": ego_pred_box.tolist() if ego_pred_box is not None else None,
        }

    def _register_routes(self, fastapi_app: FastAPI):
        @fastapi_app.get("/", response_class=HTMLResponse)
        async def home(request: Request):
            return self.templates.TemplateResponse(request, "home.html")

        @fastapi_app.get("/temp/forward")
        async def forward(nats_subject: str = NATS_SUBJECT, nats_durable: str = NATS_DURABLE):
            return await self.run_forward(nats_subject=nats_subject, nats_durable=nats_durable)

        @fastapi_app.post("/temp/post_data")
        async def post_data(data: dict = Body(...)):
            if data.get("status") != "success":
                raise HTTPException(status_code=400, detail="Status is not success")

            self.data_queue.append(data)
            return {"status": "success", "message": "Data received"}

        @fastapi_app.get("/get_id")
        async def get_id():
            return {"id": "TEST"}

    def _register_socket_events(self):
        @self.socketio.on("connect")
        async def handle_connect(sid, environ, auth=None):
            logger.info("Client connected: %s", sid)

    def _register_a2a_routes(self, fastapi_app: FastAPI):
        agent_card = self._build_agent_card()
        request_handler = DefaultRequestHandler(
            agent_executor=CooperativeFeatureFusionExecutor(self),
            task_store=InMemoryTaskStore(),
            agent_card=agent_card,
        )
        add_a2a_routes_to_fastapi(
            fastapi_app,
            agent_card_routes=create_agent_card_routes(agent_card),
            jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
        )

    @staticmethod
    def _parse_a2a_text_payload(text: str) -> tuple[str, dict]:
        if not text:
            return "", {}

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text, {}

        if not isinstance(payload, dict):
            return text, {}

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        task_description = (
            payload.get("task_description")
            or payload.get("description")
            or payload.get("message")
            or text
        )
        return str(task_description), metadata

    @staticmethod
    def _resolve_nats_config(metadata: dict) -> tuple[str, str]:
        nats_subject = metadata.get("nats_subject") or NATS_SUBJECT
        nats_durable = metadata.get("nats_durable")
        if not nats_durable:
            nats_durable = nats_subject.replace(".", "-") if metadata.get("nats_subject") else NATS_DURABLE
        return nats_subject, nats_durable

    @staticmethod
    def _json_default(value: Any):
        if hasattr(value, "tolist"):
            return value.tolist()
        return str(value)

    @staticmethod
    def _build_agent_card() -> AgentCard:
        skill = AgentSkill(
            id="cooperative_feature_fusion_detection_viz",
            name="Cooperative Feature Fusion Detection Visualization",
            description=(
                "Consumes intermediate feature data from NATS, runs cooperative "
                "feature fusion detection, and pushes visualization frames to the web UI."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["cooperative-feature-fusion", "detection", "visualization", "nats"],
            examples=[
                "Run cooperative feature fusion detection visualization",
                (
                    '{"task_description": "Run visualization", '
                    '"metadata": {"nats_subject": "workflow.demo.perception2feature.result", '
                    '"nats_durable": "workflow-demo-perception2feature-result"}}'
                ),
            ],
        )

        return AgentCard(
            name="CooperativeFeatureFusionDetectionViz Agent",
            description=(
                "FastAPI visualization agent that consumes intermediate features "
                "from NATS and exposes standard A2A JSON-RPC."
            ),
            version="0.1.5",
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            capabilities=AgentCapabilities(streaming=True),
            supported_interfaces=[
                AgentInterface(
                    protocol_binding="JSONRPC",
                    url=A2A_AGENT_URL,
                )
            ],
            skills=[skill],
        )

    def _encode_image_to_base64(self, img):
        """辅助函数：将OpenCV图像转换为Base64字符串"""
        if img is None:
            return ""
        ret, buffer = cv2.imencode('.jpg', img)
        if not ret:
            return ""
        return base64.b64encode(buffer).decode("utf-8")

    async def _update_images(
        self,
        pcd_img=None,
        ego_comm_mask=None,
        others_comm_mask=None,
        ego_feature=None,
        fused_feature=None,
        pred_photos=None,
    ):
        """更新缓存中的图片数据"""
        data_payload = {}

        if pcd_img is not None and pcd_img.size != 0:
            pcd_img = pcd_img.copy()
            pcd_img *= 255
            pcd_img = cv2.cvtColor(pcd_img, cv2.COLOR_RGB2BGR)
            data_payload["pcd_img"] = self._encode_image_to_base64(pcd_img)

        if ego_comm_mask is not None and ego_comm_mask.size != 0:
            ego_comm_mask = ego_comm_mask.copy()
            request_map = 1 - ego_comm_mask.squeeze()

            request_map_img = np.zeros((*request_map.shape, 3), dtype=np.uint8)

            color_0 = [255, 0, 255]
            color_1 = [0, 255, 255]

            request_map_img[request_map == 0] = color_0
            request_map_img[request_map == 1] = color_1

            original_height, original_width = request_map_img.shape[:2]

            scale_factor = 5
            new_width = original_width * scale_factor
            new_height = original_height * scale_factor

            request_map_img = cv2.resize(request_map_img, (new_width, new_height), interpolation=cv2.INTER_NEAREST)
            data_payload["request_map_img"] = self._encode_image_to_base64(request_map_img)

        if others_comm_mask is not None and others_comm_mask.size != 0:
            others_comm_mask = others_comm_mask.copy()
            others_comm_mask = others_comm_mask.squeeze()

            others_comm_mask_img = np.zeros((*others_comm_mask.shape, 3), dtype=np.uint8)

            color_0 = [255, 0, 255]
            color_1 = [0, 255, 255]

            others_comm_mask_img[others_comm_mask == 0] = color_0
            others_comm_mask_img[others_comm_mask == 1] = color_1

            original_height, original_width = others_comm_mask_img.shape[:2]

            scale_factor = 5
            new_width = original_width * scale_factor
            new_height = original_height * scale_factor

            others_comm_mask_img = cv2.resize(
                others_comm_mask_img,
                (new_width, new_height),
                interpolation=cv2.INTER_NEAREST,
            )
            data_payload["others_comm_mask_img"] = self._encode_image_to_base64(others_comm_mask_img)

        if ego_feature is not None and ego_feature.size != 0:
            ego_feature = ego_feature.copy()
            ego_feature = ego_feature.squeeze()

            ego_feature = ego_feature.sum(0)
            min_val = np.min(ego_feature)
            max_val = np.max(ego_feature)

            if max_val == min_val:
                normalized_ego_feature = np.zeros_like(ego_feature, dtype=np.float32)
            else:
                normalized_ego_feature = (ego_feature - min_val) / (max_val - min_val)

            green = np.array([0, 255, 0], dtype=np.float32)
            blue = np.array([255, 0, 0], dtype=np.float32)

            alpha_channel = normalized_ego_feature[:, :, np.newaxis]
            interpolated_colors = (1 - alpha_channel) * green + alpha_channel * blue

            ego_feature_img = np.clip(interpolated_colors, 0, 255).astype(np.uint8)

            original_height, original_width = ego_feature_img.shape[:2]

            scale_factor = 5
            new_width = original_width * scale_factor
            new_height = original_height * scale_factor

            ego_feature_img = cv2.resize(
                ego_feature_img,
                (new_width, new_height),
                interpolation=cv2.INTER_NEAREST,
            )
            data_payload["ego_feature_img"] = self._encode_image_to_base64(ego_feature_img)

        if fused_feature is not None and fused_feature.size != 0:
            fused_feature = fused_feature.copy()
            fused_feature = fused_feature.squeeze()
            fused_feature = fused_feature.sum(0)

            min_val = np.min(fused_feature)
            max_val = np.max(fused_feature)

            if max_val == min_val:
                normalized_fused_feature = np.zeros_like(fused_feature, dtype=np.float32)
            else:
                normalized_fused_feature = (fused_feature - min_val) / (max_val - min_val)

            green = np.array([0, 255, 0], dtype=np.float32)
            blue = np.array([255, 0, 0], dtype=np.float32)

            alpha_channel = normalized_fused_feature[:, :, np.newaxis]
            interpolated_colors = (1 - alpha_channel) * green + alpha_channel * blue

            fused_feature_img = np.clip(interpolated_colors, 0, 255).astype(np.uint8)

            original_height, original_width = fused_feature_img.shape[:2]

            scale_factor = 5
            new_width = original_width * scale_factor
            new_height = original_height * scale_factor

            fused_feature_img = cv2.resize(
                fused_feature_img,
                (new_width, new_height),
                interpolation=cv2.INTER_NEAREST,
            )
            data_payload["fused_feature_img"] = self._encode_image_to_base64(fused_feature_img)

        if pred_photos is not None and len(pred_photos) == 4:
            pred_img_0 = pred_photos[0].copy()
            pred_img_0 = cv2.cvtColor(pred_img_0, cv2.COLOR_RGB2BGR)
            data_payload["pred_img_0"] = self._encode_image_to_base64(pred_img_0)

            pred_img_3 = pred_photos[3].copy()
            pred_img_3 = cv2.cvtColor(pred_img_3, cv2.COLOR_RGB2BGR)
            data_payload["pred_img_3"] = self._encode_image_to_base64(pred_img_3)

        if data_payload:
            await self.socketio.emit("update_frames", data_payload)


class CooperativeFeatureFusionExecutor(AgentExecutor):
    def __init__(self, web_app: CooperativeFeatureFusionWebApp):
        self.web_app = web_app

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Processing request..."),
        )

        text = get_message_text(context.message)
        task_description, metadata = self.web_app._parse_a2a_text_payload(text)
        nats_subject, nats_durable = self.web_app._resolve_nats_config(metadata)

        logger.info(
            "Processing A2A task: description=%s, nats_subject=%s, nats_durable=%s",
            task_description,
            nats_subject,
            nats_durable,
        )

        try:
            result = await self.web_app.run_forward(
                nats_subject=nats_subject,
                nats_durable=nats_durable,
            )
        except Exception as exc:
            logger.exception("Agent execution failed")
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(f"Request failed: {message}"),
            )
            return

        result_text = json.dumps(result, ensure_ascii=False, default=self.web_app._json_default)
        await updater.add_artifact(
            parts=[new_text_part(text=result_text, media_type="text/plain")],
            name="cooperative-feature-fusion-detection-viz-result",
        )

        if result.get("status") != "success":
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("Request failed."),
            )
            return

        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Request is completed!"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


_web_app = CooperativeFeatureFusionWebApp()
app = _web_app.build()
fastapi_app = _web_app.fastapi_app
