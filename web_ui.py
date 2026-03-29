"""
LangManus Web UI - 可视化界面
提供任务监控、Agent 状态、实时日志等功能
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import asyncio
import json
from datetime import datetime
import uvicorn
import logging
import queue

app = FastAPI(title="LangManus Web UI")

# 日志队列
log_queue = queue.Queue()

# 自定义日志处理器
class UILogHandler(logging.Handler):
    def emit(self, record):
        try:
            # 直接使用 record.getMessage() 获取消息内容
            msg = record.getMessage()
            
            # 调试：打印所有接收到的日志
            # print(f"[DEBUG] UILogHandler received: {msg[:100]}")
            
            if "UI_LOG_EVENT:" in msg:
                content = msg.split("UI_LOG_EVENT:", 1)[1].strip()
                # 转换为 HTML 换行
                content = content.replace("\n", "<br>")
                
                # 调试：确认已放入队列
                print(f"[DEBUG] 捕获到任务分解日志，已放入队列")
                
                log_queue.put({
                    "level": "info",
                    "message": f"📋 {content}",  # 添加表情符号
                    "task_id": None
                })
        except Exception as e:
            # 出错时打印到控制台便于调试
            print(f"UILogHandler error: {e}")
            self.handleError(record)

# WebSocket 连接管理
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

# 全局状态存储
class SystemState:
    def __init__(self):
        self.tasks: List[Dict[str, Any]] = []
        self.agents: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, Any]] = []
        self.current_task: Optional[Dict[str, Any]] = None
        
    def add_log(self, level: str, message: str, task_id: str = None):
        log = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "task_id": task_id
        }
        self.logs.append(log)
        if len(self.logs) > 1000:  # 限制日志数量
            self.logs.pop(0)
        return log

system_state = SystemState()

# 初始化 Agent 状态（从注册表加载）
async def init_agents():
    from src.service.agent_registry import get_registry_client
    registry = get_registry_client()
    agents = registry.get_all_agents()
    system_state.agents = agents
    await manager.broadcast({
        "type": "agents_update",
        "data": agents
    })

async def consume_logs():
    """后台任务：消费日志队列并广播"""
    while True:
        try:
            while not log_queue.empty():
                log_data = log_queue.get_nowait()
                log = system_state.add_log(
                    log_data["level"], 
                    log_data["message"], 
                    log_data["task_id"]
                )
                await manager.broadcast({
                    "type": "log",
                    "data": log
                })
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"Log consumer error: {e}")
            await asyncio.sleep(1)

@app.on_event("startup")
async def startup_event():
    await init_agents()
    
    # 添加日志监听
    handler = UILogHandler()
    handler.setLevel(logging.INFO)
    
    # 监听 distributed_nodes（Sequential 模式）
    nodes_logger = logging.getLogger("src.graph.distributed_nodes")
    nodes_logger.addHandler(handler)
    nodes_logger.setLevel(logging.INFO)
    nodes_logger.propagate = True
    
    # 监听 magentic_nodes（Magentic-One 模式）
    magentic_logger = logging.getLogger("src.graph.magentic_nodes")
    magentic_logger.addHandler(handler)
    magentic_logger.setLevel(logging.INFO)
    magentic_logger.propagate = True
    
    # 启动消费者
    asyncio.create_task(consume_logs())

# API Models
class TaskRequest(BaseModel):
    task_description: str
    adaptive_mode: bool = True

class LogEntry(BaseModel):
    level: str
    message: str
    task_id: Optional[str] = None

# 主页面
@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    html_content = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>自主式交通系统智联中枢</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            padding: 20px;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        
        header {
            background: white;
            border-radius: 12px;
            padding: 20px 30px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        h1 {
            color: #667eea;
            font-size: 2em;
            margin-bottom: 10px;
        }
        
        .subtitle {
            color: #666;
            font-size: 1em;
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #f0f0f0;
        }
        
        .card-title {
            font-size: 1.2em;
            font-weight: 600;
            color: #333;
        }
        
        .badge {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.85em;
            font-weight: 500;
        }
        
        .badge-success {
            background: #10b981;
            color: white;
        }
        
        .badge-warning {
            background: #f59e0b;
            color: white;
        }
        
        .badge-error {
            background: #ef4444;
            color: white;
        }
        
        .badge-info {
            background: #3b82f6;
            color: white;
        }
        
        .task-input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e5e7eb;
            border-radius: 8px;
            font-size: 1em;
            margin-bottom: 10px;
        }
        
        .task-input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 1em;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .btn-primary {
            background: #667eea;
            color: white;
        }
        
        .btn-primary:hover {
            background: #5568d3;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        
        .agent-list {
            display: grid;
            gap: 10px;
        }
        
        .agent-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            background: #f9fafb;
            border-radius: 8px;
            border-left: 4px solid #667eea;
        }
        
        .agent-info {
            flex: 1;
        }
        
        .agent-name {
            font-weight: 600;
            color: #333;
            margin-bottom: 4px;
        }
        
        .agent-capability {
            font-size: 0.85em;
            color: #666;
        }
        
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        .status-online {
            background: #10b981;
        }
        
        .status-offline {
            background: #ef4444;
        }
        
        @keyframes pulse {
            0%, 100% {
                opacity: 1;
            }
            50% {
                opacity: 0.5;
            }
        }
        
        .log-container {
            max-height: 400px;
            overflow-y: auto;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.85em;
        }
        
        .log-entry {
            padding: 8px 12px;
            margin-bottom: 4px;
            border-radius: 4px;
            border-left: 3px solid;
        }
        
        .log-info {
            background: #eff6ff;
            border-color: #3b82f6;
        }
        
        .log-success {
            background: #f0fdf4;
            border-color: #10b981;
        }
        
        .log-warning {
            background: #fffbeb;
            border-color: #f59e0b;
        }
        
        .log-error {
            background: #fef2f2;
            border-color: #ef4444;
        }
        
        .log-timestamp {
            color: #666;
            font-size: 0.9em;
            margin-right: 8px;
        }
        
        .task-item {
            padding: 12px;
            margin-bottom: 8px;
            background: #f9fafb;
            border-radius: 8px;
            border-left: 4px solid;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .task-item:hover {
            background: #f3f4f6;
            transform: translateX(4px);
        }
        
        .task-running {
            border-color: #3b82f6;
            animation: pulse-border 2s infinite;
        }
        
        .task-completed {
            border-color: #10b981;
        }
        
        .task-failed {
            border-color: #ef4444;
        }
        
        .task-result {
            margin-top: 8px;
            padding: 10px;
            background: white;
            border-radius: 6px;
            font-size: 0.9em;
            max-height: 300px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        @keyframes pulse-border {
            0%, 100% {
                opacity: 1;
            }
            50% {
                opacity: 0.6;
            }
        }
        
        .stats {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 15px;
        }
        
        .stat-item {
            text-align: center;
            padding: 15px;
            background: #f9fafb;
            border-radius: 8px;
        }
        
        .stat-value {
            font-size: 2em;
            font-weight: 700;
            color: #667eea;
        }
        
        .stat-label {
            font-size: 0.9em;
            color: #666;
            margin-top: 5px;
        }
        
        .connection-status {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #999;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🤖 自主式交通系统智联中枢</h1>
            <p class="subtitle">分布式 Agent 编排系统 - 实时监控面板</p>
            <div class="connection-status" style="margin-top: 10px;">
                <div id="connectionIndicator" class="status-indicator status-offline"></div>
                <span id="connectionText">连接中...</span>
            </div>
        </header>
        
        <div class="grid">
            <!-- 系统统计 -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">📊 系统统计</span>
                </div>
                <div class="stats">
                    <div class="stat-item">
                        <div class="stat-value" id="agentCount">0</div>
                        <div class="stat-label">在线 Agent</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="taskCount">0</div>
                        <div class="stat-label">执行任务</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="successRate">0%</div>
                        <div class="stat-label">成功率</div>
                    </div>
                </div>
            </div>
            
            <!-- 任务提交 -->
            <div class="card" style="grid-column: span 2;">
                <div class="card-header">
                    <span class="card-title">🚀 提交新任务</span>
                </div>
                <input 
                    type="text" 
                    id="taskInput" 
                    class="task-input" 
                    placeholder="输入任务描述，例如：搜索特斯拉最新消息"
                />
                <button class="btn btn-primary" onclick="submitTask()">
                    执行任务
                </button>
            </div>
        </div>
        
        <div class="grid">
            <!-- Agent 列表 -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">🤖 Agent 列表</span>
                    <span class="badge badge-success" id="agentBadge">0 在线</span>
                </div>
                <div class="agent-list" id="agentList">
                    <div class="empty-state">加载中...</div>
                </div>
            </div>
            
            <!-- 任务列表 -->
            <div class="card" style="grid-column: span 2;">
                <div class="card-header">
                    <span class="card-title">📋 任务列表</span>
                </div>
                <div id="taskList" style="max-height: 500px; overflow-y: auto;">
                    <div class="empty-state">暂无任务</div>
                </div>
            </div>
            
            <!-- 实时日志 -->
            <div class="card" style="grid-column: span 1;">
                <div class="card-header">
                    <span class="card-title">📝 实时日志</span>
                    <button class="btn" onclick="clearLogs()" style="padding: 6px 12px; background: #f3f4f6;">
                        清空
                    </button>
                </div>
                <div class="log-container" id="logContainer">
                    <div class="empty-state">暂无日志</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let reconnectInterval = null;
        
        // 连接 WebSocket
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
            
            ws.onopen = () => {
                console.log('WebSocket 已连接');
                document.getElementById('connectionIndicator').className = 'status-indicator status-online';
                document.getElementById('connectionText').textContent = '已连接';
                addLog('success', '✅ WebSocket 连接成功');
                
                if (reconnectInterval) {
                    clearInterval(reconnectInterval);
                    reconnectInterval = null;
                }
            };
            
            ws.onclose = () => {
                console.log('WebSocket 已断开');
                document.getElementById('connectionIndicator').className = 'status-indicator status-offline';
                document.getElementById('connectionText').textContent = '已断开';
                addLog('error', '❌ WebSocket 连接断开，尝试重连...');
                
                // 自动重连
                if (!reconnectInterval) {
                    reconnectInterval = setInterval(() => {
                        console.log('尝试重新连接...');
                        connectWebSocket();
                    }, 3000);
                }
            };
            
            ws.onmessage = (event) => {
                const message = JSON.parse(event.data);
                handleMessage(message);
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket 错误:', error);
            };
        }
        
        // 处理消息
        function handleMessage(message) {
            switch (message.type) {
                case 'agents_update':
                    updateAgentList(message.data);
                    break;
                case 'task_update':
                    updateTaskList(message.data);
                    break;
                case 'log':
                    addLog(message.data.level, message.data.message, message.data.task_id);
                    break;
                case 'stats':
                    updateStats(message.data);
                    break;
            }
        }
        
        // 更新 Agent 列表
        function updateAgentList(agents) {
            const container = document.getElementById('agentList');
            const onlineCount = agents.filter(a => a.status === 'online').length;
            
            document.getElementById('agentCount').textContent = onlineCount;
            document.getElementById('agentBadge').textContent = `${onlineCount} 在线`;
            
            if (agents.length === 0) {
                container.innerHTML = '<div class="empty-state">暂无 Agent</div>';
                return;
            }
            
            container.innerHTML = agents.map(agent => `
                <div class="agent-item">
                    <div class="agent-info">
                        <div class="agent-name">${agent.id}</div>
                        <div class="agent-capability">${agent.capability} - ${agent.description}</div>
                    </div>
                    <div class="status-indicator status-${agent.status}"></div>
                </div>
            `).join('');
        }
        
        // 更新任务列表
        function updateTaskList(tasks) {
            const container = document.getElementById('taskList');
            document.getElementById('taskCount').textContent = tasks.length;
            
            if (tasks.length === 0) {
                container.innerHTML = '<div class="empty-state">暂无任务</div>';
                return;
            }
            
            container.innerHTML = tasks.slice().reverse().map(task => {
                const hasResult = task.result && task.status === 'completed';
                const hasError = task.error && task.status === 'failed';
                
                return `
                    <div class="task-item task-${task.status}" onclick="toggleTaskResult('${task.id}')">
                        <div style="font-weight: 600; margin-bottom: 5px;">
                            ${task.description}
                        </div>
                        <div style="font-size: 0.85em; color: #666; margin-bottom: 5px;">
                            状态: ${getStatusText(task.status)} | 
                            模式: ${task.mode || 'N/A'} |
                            时间: ${new Date(task.created_at).toLocaleTimeString('zh-CN')}
                        </div>
                        ${hasResult ? `
                            <div id="result-${task.id}" class="task-result" style="display: none;">
                                <div style="font-weight: 600; color: #10b981; margin-bottom: 8px;">✅ 执行结果:</div>
                                ${formatTaskResult(task.result)}
                            </div>
                        ` : ''}
                        ${hasError ? `
                            <div class="task-result" style="color: #ef4444;">
                                <div style="font-weight: 600; margin-bottom: 8px;">❌ 错误信息:</div>
                                ${task.error}
                            </div>
                        ` : ''}
                        ${task.status === 'running' ? '<div style="font-size: 0.85em; color: #3b82f6; margin-top: 5px;">⏳ 正在执行中，请稍候...</div>' : ''}
                        ${hasResult ? '<div style="font-size: 0.85em; color: #666; margin-top: 5px;">💡 点击查看详细结果</div>' : ''}
                    </div>
                `;
            }).join('');
        }
        
        // 切换任务结果显示
        function toggleTaskResult(taskId) {
            const resultDiv = document.getElementById(`result-${taskId}`);
            if (resultDiv) {
                resultDiv.style.display = resultDiv.style.display === 'none' ? 'block' : 'none';
            }
        }
        
        // 格式化任务结果
        function formatTaskResult(result) {
            if (typeof result === 'string') {
                return result;
            }
            
            let output = '';
            
            // 显示复杂度和模式
            if (result.complexity_level) {
                output += `<div style="margin-bottom: 8px;"><strong>任务复杂度:</strong> ${result.complexity_level}</div>`;
            }
            if (result.orchestration_mode) {
                output += `<div style="margin-bottom: 8px;"><strong>编排模式:</strong> ${result.orchestration_mode}</div>`;
            }
            
            // 显示最终结果
            if (result.final_result) {
                output += `<div style="margin-top: 10px; padding: 10px; background: #f0fdf4; border-radius: 4px;">`;
                output += `<strong>📊 最终结果:</strong><br><br>${String(result.final_result).replace(/\\n/g, '<br>')}`;
                output += `</div>`;
            }
            
            // 显示消息历史
            if (result.messages && result.messages.length > 0) {
                const lastMessage = result.messages[result.messages.length - 1];
                
                // 提取 content 字段
                let content = lastMessage.content || lastMessage;
                
                if (content && typeof content === 'string') {
                    output += `<div style="margin-top: 10px; padding: 10px; background: #eff6ff; border-radius: 4px;">`;
                    output += `<strong>💬 详细内容:</strong><br><br>${content.replace(/\\n/g, '<br>')}`;
                    output += `</div>`;
                }
            }
            
            return output || JSON.stringify(result, null, 2);
        }
        
        // 添加日志
        function addLog(level, message, taskId = null) {
            const container = document.getElementById('logContainer');
            const timestamp = new Date().toLocaleTimeString('zh-CN');
            
            if (container.querySelector('.empty-state')) {
                container.innerHTML = '';
            }
            
            const logEntry = document.createElement('div');
            logEntry.className = `log-entry log-${level}`;
            logEntry.innerHTML = `
                <span class="log-timestamp">[${timestamp}]</span>
                ${message}
            `;
            
            container.appendChild(logEntry);
            container.scrollTop = container.scrollHeight;
            
            // 限制日志数量
            while (container.children.length > 100) {
                container.removeChild(container.firstChild);
            }
        }
        
        // 清空日志
        function clearLogs() {
            document.getElementById('logContainer').innerHTML = '<div class="empty-state">暂无日志</div>';
        }
        
        // 更新统计
        function updateStats(stats) {
            document.getElementById('agentCount').textContent = stats.online_agents || 0;
            document.getElementById('taskCount').textContent = stats.total_tasks || 0;
            document.getElementById('successRate').textContent = 
                stats.success_rate ? `${stats.success_rate}%` : '0%';
        }
        
        // 提交任务
        async function submitTask() {
            const input = document.getElementById('taskInput');
            const taskDescription = input.value.trim();
            
            if (!taskDescription) {
                addLog('warning', '⚠️ 请输入任务描述');
                return;
            }
            
            addLog('info', `📤 提交任务: ${taskDescription}`);
            
            try {
                const response = await fetch('/api/submit_task', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        task_description: taskDescription,
                        adaptive_mode: true
                    })
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    addLog('success', `✅ 任务已提交: ${result.task_id}`);
                    input.value = '';
                } else {
                    addLog('error', `❌ 任务提交失败: ${result.detail}`);
                }
            } catch (error) {
                addLog('error', `❌ 请求失败: ${error.message}`);
            }
        }
        
        // 获取状态文本
        function getStatusText(status) {
            const statusMap = {
                'pending': '⏳ 等待中',
                'running': '🔄 执行中',
                'completed': '✅ 已完成',
                'failed': '❌ 失败'
            };
            return statusMap[status] || status;
        }
        
        // 回车提交
        document.addEventListener('DOMContentLoaded', () => {
            const input = document.getElementById('taskInput');
            input.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    submitTask();
                }
            });
            
            // 连接 WebSocket
            connectWebSocket();
        });
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)

# WebSocket 端点
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    
    try:
        # 发送初始状态
        await websocket.send_json({
            "type": "agents_update",
            "data": system_state.agents
        })
        
        # 发送任务状态（确保可序列化）
        serializable_tasks = [serialize_result(task) for task in system_state.tasks]
        await websocket.send_json({
            "type": "task_update",
            "data": serializable_tasks
        })
        
        while True:
            # 保持连接活跃
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# API 端点
@app.post("/api/submit_task")
async def submit_task(request: TaskRequest):
    """提交新任务"""
    task_id = f"task_{len(system_state.tasks) + 1:04d}"
    
    task = {
        "id": task_id,
        "description": request.task_description,
        "status": "running",
        "mode": "adaptive" if request.adaptive_mode else "manual",
        "created_at": datetime.now().isoformat()
    }
    
    system_state.tasks.append(task)
    system_state.current_task = task
    
    # 广播任务更新
    await manager.broadcast({
        "type": "task_update",
        "data": [serialize_result(t) for t in system_state.tasks]
    })
    
    # 添加日志
    log = system_state.add_log("info", f"📤 新任务提交: {request.task_description}", task_id)
    await manager.broadcast({
        "type": "log",
        "data": log
    })
    
    # 异步执行任务（在后台运行）
    asyncio.create_task(execute_task_async(task_id, request.task_description, request.adaptive_mode))
    
    return {"task_id": task_id, "status": "accepted"}

async def execute_task_async(task_id: str, task_description: str, adaptive_mode: bool):
    """异步执行任务"""
    try:
        from src.distributed_workflow import run_distributed_workflow
        
        # 更新状态
        log = system_state.add_log("info", f"🔄 开始执行任务: {task_description}", task_id)
        await manager.broadcast({"type": "log", "data": log})
        
        # 执行工作流
        result = await run_distributed_workflow(
            user_input=task_description,
            adaptive_mode=adaptive_mode
        )
        
        # 将结果转换为可序列化的格式
        serializable_result = serialize_result(result)
        
        # 更新任务状态
        for task in system_state.tasks:
            if task["id"] == task_id:
                task["status"] = "completed"
                task["result"] = serializable_result
                break
        
        await manager.broadcast({
            "type": "task_update",
            "data": [serialize_result(t) for t in system_state.tasks]
        })
        
        log = system_state.add_log("success", f"✅ 任务完成: {task_description}", task_id)
        await manager.broadcast({"type": "log", "data": log})
        
    except Exception as e:
        import traceback
        error_detail = f"{str(e)}\n{traceback.format_exc()}"
        
        # 更新任务状态为失败
        for task in system_state.tasks:
            if task["id"] == task_id:
                task["status"] = "failed"
                task["error"] = str(e)
                break
        
        await manager.broadcast({
            "type": "task_update",
            "data": [serialize_result(t) for t in system_state.tasks]
        })
        
        log = system_state.add_log("error", f"❌ 任务失败: {str(e)}", task_id)
        await manager.broadcast({"type": "log", "data": log})

def serialize_result(result):
    """将结果转换为可序列化的格式"""
    if isinstance(result, dict):
        serializable = {}
        for key, value in result.items():
            if key == "messages":
                # 转换 LangChain 消息对象或字典消息
                serializable[key] = []
                for msg in value:
                    if isinstance(msg, dict):
                        # 如果已经是字典
                        msg_type = msg.get("type", "unknown")
                        # 尝试获取内容，如果是个字典转字符串可能会很丑，但比报错好
                        content = msg.get("content")
                        if content is None:
                            content = str(msg)
                        serializable[key].append({
                            "type": msg_type,
                            "content": str(content)
                        })
                    else:
                        # 如果是 LangChain 对象
                        serializable[key].append({
                            "type": getattr(msg, "__class__", type(msg)).__name__,
                            "content": str(msg.content) if hasattr(msg, "content") else str(msg)
                        })
            elif isinstance(value, (str, int, float, bool, type(None))):
                serializable[key] = value
            elif isinstance(value, (list, tuple)):
                serializable[key] = [serialize_result(item) for item in value]
            elif isinstance(value, dict):
                serializable[key] = serialize_result(value)
            else:
                # 其他对象转换为字符串
                serializable[key] = str(value)
        return serializable
    elif isinstance(result, (list, tuple)):
        return [serialize_result(item) for item in result]
    elif isinstance(result, (str, int, float, bool, type(None))):
        return result
    else:
        return str(result)

@app.post("/api/log")
async def add_log_api(entry: LogEntry):
    """添加日志（供外部调用）"""
    log = system_state.add_log(entry.level, entry.message, entry.task_id)
    await manager.broadcast({
        "type": "log",
        "data": log
    })
    return {"status": "ok"}

@app.get("/api/agents")
async def get_agents():
    """获取 Agent 列表"""
    return system_state.agents

@app.get("/api/tasks")
async def get_tasks():
    """获取任务列表"""
    return system_state.tasks

@app.get("/api/stats")
async def get_stats():
    """获取统计信息"""
    online_agents = len([a for a in system_state.agents if a["status"] == "online"])
    total_tasks = len(system_state.tasks)
    completed_tasks = len([t for t in system_state.tasks if t["status"] == "completed"])
    success_rate = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
    
    stats = {
        "online_agents": online_agents,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "success_rate": round(success_rate, 1)
    }
    
    await manager.broadcast({
        "type": "stats",
        "data": stats
    })
    
    return stats

if __name__ == "__main__":
    print("🚀 启动 LangManus Web UI...")
    print("📊 访问地址: http://localhost:8001")
    print("💡 提示: 先启动 agent_server.py，然后在 Web UI 中提交任务")
    uvicorn.run(app, host="0.0.0.0", port=8001)
