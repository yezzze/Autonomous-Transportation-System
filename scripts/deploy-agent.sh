#!/bin/bash
# 在远程节点上部署 Agent 的辅助脚本

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

show_usage() {
    echo "使用方法:"
    echo "  ./deploy-agent.sh <agent_id> <port> [capability]"
    echo ""
    echo "示例:"
    echo "  ./deploy-agent.sh search_agent_001 8080 search"
    echo "  ./deploy-agent.sh compute_agent_001 8081 compute"
    echo ""
    echo "支持的能力:"
    echo "  - search: 搜索服务"
    echo "  - compute: 计算服务"
    echo "  - vision: 视觉服务"
    echo "  - nlp: NLP 服务"
    echo "  - code_execution: 代码执行"
    echo "  - web_interaction: Web 自动化"
    echo ""
}

if [ "$#" -lt 2 ]; then
    show_usage
    exit 1
fi

AGENT_ID=$1
PORT=$2
CAPABILITY=${3:-search}

echo ""
echo "============================================================"
echo "  部署 LangManus Agent 到远程节点"
echo "============================================================"
echo "Agent ID: $AGENT_ID"
echo "端口: $PORT"
echo "能力: $CAPABILITY"
echo ""

# 检查 Docker
if ! command -v docker &> /dev/null; then
    print_warning "Docker 未安装，将使用 Python 方式部署"
    USE_DOCKER=false
else
    USE_DOCKER=true
fi

# 使用 Docker 部署
if [ "$USE_DOCKER" = true ]; then
    print_info "使用 Docker 部署 Agent..."
    
    # 拉取镜像
    print_info "拉取镜像..."
    docker pull langmanus/agent-orchestrator:latest
    
    # 停止旧容器（如果存在）
    if docker ps -a | grep -q $AGENT_ID; then
        print_info "停止旧容器..."
        docker stop $AGENT_ID || true
        docker rm $AGENT_ID || true
    fi
    
    # 启动新容器
    print_info "启动 Agent 容器..."
    docker run -d \
        --name $AGENT_ID \
        --restart unless-stopped \
        -p $PORT:$PORT \
        -e AGENT_ID=$AGENT_ID \
        -e AGENT_CAPABILITY=$CAPABILITY \
        -e TAVILY_API_KEY=${TAVILY_API_KEY:-} \
        langmanus/agent-orchestrator:latest \
        python agent_server.py $PORT
    
    print_success "Agent 容器已启动"
    
    # 等待启动
    sleep 3
    
    # 检查状态
    print_info "检查 Agent 状态..."
    if docker ps | grep -q $AGENT_ID; then
        print_success "Agent 正在运行"
        docker ps | grep $AGENT_ID
    else
        print_warning "Agent 未运行，查看日志:"
        docker logs $AGENT_ID
        exit 1
    fi
    
else
    # 使用 Python 直接运行
    print_info "使用 Python 方式部署 Agent..."
    
    # 检查 Python
    if ! command -v python &> /dev/null; then
        print_warning "Python 未安装，无法部署"
        exit 1
    fi
    
    # 检查项目目录
    if [ ! -f "agent_server.py" ]; then
        print_warning "未找到 agent_server.py，请先克隆项目"
        print_info "运行: git clone https://github.com/your-org/langmanus.git"
        exit 1
    fi
    
    # 安装依赖
    print_info "安装依赖..."
    pip install -r requirements.txt
    
    # 启动 Agent
    print_info "启动 Agent..."
    export AGENT_ID=$AGENT_ID
    export AGENT_CAPABILITY=$CAPABILITY
    
    nohup python agent_server.py $PORT > logs/agent_${PORT}.log 2>&1 &
    AGENT_PID=$!
    
    echo $AGENT_PID > /tmp/langmanus_agent_${PORT}.pid
    
    print_success "Agent 已启动，PID: $AGENT_PID"
    print_info "日志文件: logs/agent_${PORT}.log"
fi

# 健康检查
echo ""
print_info "执行健康检查..."
sleep 2

if curl -f http://localhost:$PORT/health &> /dev/null; then
    print_success "✅ Agent 健康检查通过"
else
    print_warning "⚠️  健康检查失败，请查看日志"
fi

echo ""
print_success "🎉 部署完成！"
echo ""
echo "下一步:"
echo "  1. 在 config/agent_registry.json 中添加此 Agent"
echo "  2. 将 IP 设置为此节点的地址"
echo "  3. 重启 Web UI: docker-compose restart web-ui"
echo ""
echo "管理命令:"
if [ "$USE_DOCKER" = true ]; then
    echo "  查看日志: docker logs -f $AGENT_ID"
    echo "  停止服务: docker stop $AGENT_ID"
    echo "  重启服务: docker restart $AGENT_ID"
else
    echo "  查看日志: tail -f logs/agent_${PORT}.log"
    echo "  停止服务: kill \$(cat /tmp/langmanus_agent_${PORT}.pid)"
fi
echo ""
