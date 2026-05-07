#!/usr/bin/env bash
# 启动可视化服务
#   ./scripts/run_visualization.sh           # demo 模式,端口 8888
#   ./scripts/run_visualization.sh live      # live 模式
#   ./scripts/run_visualization.sh demo 9999 # 自定义端口
set -e
cd "$(dirname "$0")/.."

MODE=${1:-demo}
PORT=${2:-8888}

if [ -d venv ]; then
  source venv/bin/activate
fi

echo "🚀 启动 LangManus 可视化  http://127.0.0.1:${PORT}  (mode=${MODE})"
exec python visualization_server.py --port "${PORT}" --mode "${MODE}"
