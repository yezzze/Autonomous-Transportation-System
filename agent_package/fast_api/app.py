"""
FastAPI 应用主体 - Auto-Agent 核心服务

架构概览:
  外部调用方(L2 Scheduler) ──POST──> /a2a/execute ──> 解析 A2AMessage/A2ATaskRequest
                                          │
                                          ├─ 从 metadata 或环境变量获取 NATS 主题
                                          │
                                          ├─ agent_function() ── 从 NATS 拉取上游数据(可选)
                                          │                      │
                                          │                      ├─ task_type="chat" → LLM 对话
                                          │                      ├─ task_type="nlp"  → LLM 处理
                                          │                      └─ 其他类型 → 自定义处理
                                          │
                                          ├─ 将结果发布到 NATS 输出主题(可选)
                                          │
                                          └─ 构造 A2ATaskResponse / A2AMessage 返回给调用方

NATS 数据流(可选):
  上游 Agent ──publish──> NATS_IN_SUBJECT
                                         │
                              本 Agent 通过 pull_subscribe 消费
                                         │
  下游 Agent <──publish── NATS_OUT_SUBJECT
                              本 Agent 处理完毕后 publish 到此主题

业务核心:
  - LLM 对话: DeepSeek API，支持 agent.md 角色注入 + workflow.md 工作流
  - 记忆系统: MEMORY.md 长期记忆 + 每日日志 + 混合搜索(TF-IDF + BM25)
  - 会话管理: 多会话隔离 + token 追踪 + 自动记忆整理

环境变量:
  DEEPSEEK_API_KEY  : DeepSeek API 密钥
  DEEPSEEK_BASE_URL : DeepSeek API 地址
  DEEPSEEK_MODEL    : 模型名称
  NATS_SERVER_URL   : NATS 服务器地址，默认 nats://nats:4222
  NATS_IN_SUBJECT   : 输入主题(接收上游数据)
  NATS_OUT_SUBJECT  : 输出主题(向下游发送数据)
"""
import os
import json
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

from protocols import A2AMessage, A2ATaskRequest, A2ATaskResponse, NatsComm
from utils.logger_utils import get_logger
from utils.numpy_utils import encode_structured_numpy, decode_structured_numpy

from memory_store import MemoryStore
from search_engine import HybridSearchEngine
from session_manager import SessionManager

logger = get_logger(__name__)

# ── 路径 ────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
WORKSPACE_DIR = BASE_DIR / "workspace"
SESSIONS_DIR = WORKSPACE_DIR / "sessions"

# ── LLM 配置 ────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# ── NATS 连接配置 ───────────────────────────────────
NATS_SERVER_URL = os.getenv("NATS_SERVER_URL", "nats://nats:4222")
NATS_IN_SUBJECT = os.getenv("NATS_IN_SUBJECT", "workflow.previousagent.result")
NATS_IN_DURABLE = os.getenv("NATS_IN_DURABLE", "workflow-previousagent-result")
NATS_OUT_SUBJECT = os.getenv("NATS_OUT_SUBJECT", "workflow.autoagent.result")

logger.info("NATS server: %s | Model: %s", NATS_SERVER_URL, DEEPSEEK_MODEL)

# 全局 NATS 通信实例
_nats_comm = NatsComm(servers=[NATS_SERVER_URL])

# ── 加载角色定义 ────────────────────────────────────
def load_config():
    agent_md = ""
    workflow_md = ""
    for name, var in [("agent.md", "agent_md"), ("workflow.md", "workflow_md")]:
        path = CONFIG_DIR / name
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if var == "agent_md":
                agent_md = content
            else:
                workflow_md = content
            logger.info("已加载 %s (%d 字符)", name, len(content))
    return agent_md, workflow_md

agent_md, workflow_md = load_config()

# ── 全局业务组件(在 lifespan 中初始化) ──────────────
memory: Optional[MemoryStore] = None
search_engine: Optional[HybridSearchEngine] = None
sessions: Optional[SessionManager] = None

# ── 记忆规则 ────────────────────────────────────────
MEMORY_RULES = """
## 记忆规则（必须遵守）

1. **即时记录**：当用户表达姓名、身份、偏好、决策时，在回复末尾注明"已记录"并写入 MEMORY.md
2. **对话摘要**：每日对话结束后将摘要写入 memory/YYYY-MM-DD.md
3. **写入格式**：`### [时间] ⭐优先级` + 内容，分类归属到 ## 用户偏好 / ## 决策记录 / ## 经验教训
4. **核心原则**：如果没有写入文件，就等于不存在。重要信息必须在当前回复中完成持久化。
"""

# ── 系统提示词构建 ──────────────────────────────────
def build_system_prompt(user_message: str = "") -> str:
    parts = []

    if agent_md:
        parts.append(f"# 角色定义\n\n{agent_md}")
    if workflow_md:
        parts.append(f"# 工作流程\n\n{workflow_md}")
    if not agent_md and not workflow_md:
        parts.append("你是一个乐于助人的AI助手。")

    if memory:
        mem = memory.get_memory()
        if mem and len(mem) > 100:
            parts.append(f"# 长期记忆\n\n{mem}")

    if user_message and search_engine:
        related = search_engine.search_context(user_message, top_k=3)
        if related:
            parts.append(related)

    if memory:
        logs = memory.get_recent_logs(days=2)
        if logs:
            parts.append(f"# 近期日志\n\n{logs}")

    parts.append(MEMORY_RULES)
    return "\n\n---\n\n".join(parts)


# ── 数据模型 ────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    remember: bool = False
    memory_category: Optional[str] = "对话记忆"

class MemoryEntry(BaseModel):
    category: str = "记忆索引"
    content: str
    importance: str = "中"

class ClearRequest(BaseModel):
    session_id: Optional[str] = "default"


# ── NATS 数据收发辅助函数 ───────────────────────────
async def _receive_data_from_nats(
    nats_in_subject: str = NATS_IN_SUBJECT,
    nats_in_durable: str = NATS_IN_DURABLE,
) -> Optional[dict]:
    """从 NATS JetStream 拉取消息，无数据时返回 None"""
    try:
        await _nats_comm.connect()
    except Exception as e:
        logger.warning("NATS 连接失败（非致命，跳过数据拉取）: %s", e)
        return None

    try:
        messages = await _nats_comm.receive(
            subject=nats_in_subject,
            durable=nats_in_durable,
            batch=1,
            timeout_sec=3.0,
            ack=True,
        )
    except Exception as e:
        logger.warning("NATS 接收超时或失败（非致命）: %s", e)
        return None

    if messages:
        logger.info("从 NATS [%s] 收到 %d 条消息", nats_in_subject, len(messages))
        return messages[0].payload
    return None


async def _send_data_to_nats(
    data: dict,
    nats_out_subject: str = NATS_OUT_SUBJECT,
) -> bool:
    """将结果发布到 NATS JetStream，失败返回 False"""
    try:
        await _nats_comm.connect()
        encoded = encode_structured_numpy(data)
        await _nats_comm.send(nats_out_subject, encoded)
        logger.info("已发布到 NATS [%s]", nats_out_subject)
        return True
    except Exception as e:
        logger.warning("NATS 发送失败（非致命）: %s", e)
        return False


# ── 核心业务逻辑 ────────────────────────────────────
async def agent_function(
    task_request: A2ATaskRequest,
    nats_in_subject: str = NATS_IN_SUBJECT,
    nats_in_durable: str = NATS_IN_DURABLE,
    nats_out_subject: str = NATS_OUT_SUBJECT,
    session_id: str = "default",
    metadata: dict = None,
) -> dict:
    """
    核心 Agent 业务逻辑。

    支持的任务类型(task_type):
      - "chat" / "nlp": LLM 对话处理（使用 agent.md + 记忆系统 + 会话管理）
      - 其他类型: 从 NATS 拉取数据 → 自定义处理 → 发布结果

    参数:
        task_request: A2A 任务请求
        nats_in_subject: 输入 NATS 主题
        nats_in_durable: 输入持久化消费者名称
        nats_out_subject: 输出 NATS 主题
        session_id: 会话 ID
        metadata: 额外元数据
    """
    task_type = task_request.task_type
    task_description = task_request.task_description
    context = task_request.context or {}

    # ── chat / nlp 类型: LLM 对话 ──
    if task_type in ("chat", "nlp"):
        return await _handle_chat_task(task_request, session_id)

    # ── 其他类型: 尝试从 NATS 拉取数据 ──
    nats_data = await _receive_data_from_nats(nats_in_subject, nats_in_durable)

    if nats_data:
        # 还原 numpy 数组
        decoded = decode_structured_numpy(nats_data)

        # TODO: 在此处编写特定 task_type 的业务逻辑
        # 示例: result = model.inference(decoded)
        logger.info("task_type=%s | 收到上游数据，keys=%s", task_type, list(decoded.keys()))

        result = {
            "status": "success",
            "task_type": task_type,
            "processed_data": encode_structured_numpy(decoded),
        }

        # 发布到下游 NATS
        await _send_data_to_nats(result, nats_out_subject)

        return {"status": "success", "output": "data processed and published to NATS"}
    else:
        # 无 NATS 数据，当作 chat 兜底
        logger.info("task_type=%s | 无 NATS 数据，回退到 LLM 对话", task_type)
        return await _handle_chat_task(task_request, session_id)


# ── LLM 对话处理 ────────────────────────────────────
async def _handle_chat_task(task_request: A2ATaskRequest, session_id: str) -> dict:
    """
    处理 chat/nlp 类型的任务: 构建 system prompt → 调用 DeepSeek → 返回回复
    """
    user_msg = task_request.task_description
    context = task_request.context or {}
    metadata = task_request.metadata or {}

    # 如果 context 中有历史消息，使用它们
    messages = context.get("messages", [])
    temperature = metadata.get("temperature", 0.7)
    max_tokens = metadata.get("max_tokens", 2048)
    remember = metadata.get("remember", False)
    memory_category = metadata.get("memory_category", "对话记忆")

    # 构建 system prompt
    system_prompt = build_system_prompt(user_msg)
    api_messages = [{"role": "system", "content": system_prompt}]

    # 添加历史消息
    for m in messages:
        if isinstance(m, dict) and "role" in m and "content" in m:
            api_messages.append({"role": m["role"], "content": m["content"]})
        elif isinstance(m, str):
            api_messages.append({"role": "user", "content": m})

    # 如果 context 里没有消息，把 task_description 作为当前用户消息
    if not messages:
        api_messages.append({"role": "user", "content": user_msg})

    # 会话追踪
    if sessions:
        session = sessions.get(session_id)
        session.add("user", user_msg)
        logger.info("[%s] chat请求 | token使用率: %.0f%%", session_id, session.usage_ratio() * 100)

    # 调用 DeepSeek
    if not DEEPSEEK_API_KEY:
        return {"status": "error", "output": "DEEPSEEK_API_KEY 未配置"}
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": api_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            if resp.status_code != 200:
                logger.error("DeepSeek API 错误: %s", resp.text)
                return {"status": "error", "output": f"API 错误: {resp.status_code}"}

            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
    except Exception as e:
        logger.exception("DeepSeek 调用异常")
        return {"status": "error", "output": f"LLM 调用失败: {str(e)}"}

    # 会话追踪回复
    if sessions:
        session = sessions.get(session_id)
        session.add("assistant", reply)

    # 记忆写入
    memory_action = None
    if memory and sessions:
        session = sessions.get(session_id)
        should_write = (
            remember
            or session.should_consolidate()
            or any(kw in reply for kw in ["已记录", "已记住", "已保存", "记下了", "写入记忆"])
        )

        if should_write:
            if remember:
                memory.add_memory_entry(memory_category, user_msg[:500])
                memory_action = f"强制写入 → {memory_category}"
            elif any(kw in reply for kw in ["已记录", "已记住", "已保存", "记下了", "写入记忆"]):
                memory_action = "Agent 自主存记"
            else:
                memory_action = await sessions.consolidate(session_id)

            if search_engine:
                search_engine.build_index()

    return {
        "status": "success",
        "output": reply,
        "usage": usage,
        "memory_action": memory_action,
    }


# ── FastAPI 生命周期 ────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时初始化业务组件，关闭时持久化会话"""
    global memory, search_engine, sessions

    logger.info("=" * 50)
    logger.info("Auto-Agent v3 启动中...")
    logger.info("=" * 50)

    # 初始化记忆系统
    memory = MemoryStore(WORKSPACE_DIR)
    search_engine = HybridSearchEngine(WORKSPACE_DIR)
    search_engine.build_index()
    sessions = SessionManager(
        memory, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, SESSIONS_DIR
    )

    logger.info(
        "记忆索引: %d 文档 | 会话恢复: %d 个 | 模型: %s",
        len(search_engine._documents),
        len(sessions._sessions),
        DEEPSEEK_MODEL,
    )

    yield

    # 关闭时持久化
    if sessions:
        sessions.persist()
    logger.info("Auto-Agent 已关闭")


# ── FastAPI 应用 ────────────────────────────────────
app = FastAPI(
    title="Auto-Agent v3",
    description="生产级智能对话代理 · A2A协议 · 龙虾记忆系统",
    version="3.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════
# A2A 执行端点 — Agent 间通信的唯一入口
# ══════════════════════════════════════════════════════

@app.post("/a2a/execute")
async def a2a_execute(message: dict, request: Request) -> dict:
    """
    A2A 执行端点 - Agent 间通信的唯一入口。

    请求体示例:
        {
            "sender_id": "L2_Scheduler",
            "receiver_id": "AutoAgent",
            "message_type": "request",
            "payload": {
                "task_id": "task-001",
                "task_type": "chat",
                "task_description": "你好，请介绍一下你自己",
                "context": {
                    "messages": [
                        {"role": "user", "content": "你好"}
                    ]
                },
                "metadata": {
                    "nats_in_subject": "workflow.previous.result",
                    "nats_out_subject": "workflow.autoagent.result",
                    "temperature": 0.7,
                    "max_tokens": 2048,
                    "remember": false
                }
            }
        }
    """
    logger.info("收到 A2A 消息: sender=%s", message.get("sender_id", "unknown"))

    # 解析 A2A 消息
    request_message = A2AMessage(**message)
    task_request = A2ATaskRequest(**request_message.payload)

    # 从 metadata 获取 NATS 配置（优先级高于环境变量）
    metadata = getattr(task_request, "metadata", {}) or {}
    nats_in_subject = metadata.get("nats_in_subject") or NATS_IN_SUBJECT
    nats_in_durable = metadata.get("nats_in_durable") or nats_in_subject.replace(".", "-")
    nats_out_subject = metadata.get("nats_out_subject") or NATS_OUT_SUBJECT

    # 获取会话 ID
    session_id = request.headers.get("X-Session-Id", "default")

    # 执行核心业务逻辑
    try:
        result = await agent_function(
            task_request=task_request,
            nats_in_subject=nats_in_subject,
            nats_in_durable=nats_in_durable,
            nats_out_subject=nats_out_subject,
            session_id=session_id,
            metadata=metadata,
        )
    except Exception as e:
        logger.exception("agent_function 异常")
        task_response = A2ATaskResponse(
            task_id=task_request.task_id,
            status="error",
            result=None,
            error_message=str(e),
        )
        return A2AMessage(
            sender_id="AutoAgent",
            receiver_id=request_message.sender_id,
            message_type="error",
            payload=task_response.dict(),
        ).dict()

    # 构造 A2A 响应
    task_response = A2ATaskResponse(
        task_id=task_request.task_id,
        status=result.get("status", "unknown"),
        result=json.dumps(result, ensure_ascii=False),
        error_message=result.get("error_message"),
    )

    response_message = A2AMessage(
        sender_id="AutoAgent",
        receiver_id=request_message.sender_id,
        message_type="response",
        payload=task_response.dict(),
    )

    return response_message.dict()


# ══════════════════════════════════════════════════════
# 兼容旧接口（直接 HTTP 对话，无需 A2A 包装）
# ══════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request_data: ChatRequest, request: Request):
    """直接对话接口（兼容旧版客户端）"""
    session_id = request.headers.get("X-Session-Id", "default")

    # 构建伪 A2A 任务请求
    task_request = A2ATaskRequest(
        task_id="direct-chat",
        task_type="chat",
        task_description=request_data.messages[-1].content if request_data.messages else "",
        context={"messages": [m.dict() for m in request_data.messages[:-1]] if len(request_data.messages) > 1 else []},
        metadata={
            "temperature": request_data.temperature,
            "max_tokens": request_data.max_tokens,
            "remember": request_data.remember,
            "memory_category": request_data.memory_category,
        },
    )

    result = await agent_function(task_request=task_request, session_id=session_id)
    return {
        "response": result.get("output", ""),
        "usage": result.get("usage", {}),
        "memory_action": result.get("memory_action"),
        "status": result.get("status"),
    }


# ══════════════════════════════════════════════════════
# 服务信息 & 健康检查
# ══════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": "Auto-Agent v3",
        "version": "3.0.0",
        "model": DEEPSEEK_MODEL,
        "config": {"has_agent_md": bool(agent_md), "has_workflow_md": bool(workflow_md)},
        "memory": {
            "memory_size": len(memory.get_memory()) if memory else 0,
            "indexed_docs": len(search_engine._documents) if search_engine else 0,
        },
        "sessions": {"active": len(sessions._sessions) if sessions else 0} if sessions else {},
        "protocols": ["A2A", "NATS"],
        "endpoints": {
            "a2a": "/a2a/execute",
            "chat": "/chat",
            "health": "/health",
            "memory": "/memory/*",
            "session": "/session",
        },
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "indexed_docs": len(search_engine._documents) if search_engine else 0,
    }


# ══════════════════════════════════════════════════════
# 记忆管理 API
# ══════════════════════════════════════════════════════

@app.get("/memory/files")
async def list_files():
    if not memory:
        raise HTTPException(status_code=503, detail="记忆系统未初始化")
    return {"files": memory.list_all_files()}


@app.get("/memory/files/{filename:path}")
async def read_file(filename: str):
    if not memory:
        raise HTTPException(status_code=503, detail="记忆系统未初始化")
    content = memory.read_file(filename)
    if not content:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"filename": filename, "content": content}


@app.post("/memory/entry")
async def add_entry(entry: MemoryEntry):
    if not memory:
        raise HTTPException(status_code=503, detail="记忆系统未初始化")
    memory.add_memory_entry(entry.category, entry.content, entry.importance)
    if search_engine:
        search_engine.build_index()
    return {"status": "ok", "category": entry.category}


@app.get("/memory/search")
async def search_memory(q: str, top_k: int = 5):
    if not search_engine:
        raise HTTPException(status_code=503, detail="搜索引擎未初始化")
    return {"results": search_engine.search(q, top_k)}


# ══════════════════════════════════════════════════════
# 会话管理 API
# ══════════════════════════════════════════════════════

@app.get("/session")
async def session_stats(request: Request):
    if not sessions:
        raise HTTPException(status_code=503, detail="会话系统未初始化")
    session_id = request.headers.get("X-Session-Id", "default")
    return sessions.stats(session_id)


@app.delete("/session")
async def reset_session(request: Request):
    if not sessions:
        raise HTTPException(status_code=503, detail="会话系统未初始化")
    session_id = request.headers.get("X-Session-Id", "default")
    sessions.delete(session_id)
    return {"status": "ok", "session_id": session_id}
