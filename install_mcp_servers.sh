#!/bin/bash

# MCP 服务器安装脚本（Python 版本）

set -e

echo "=========================================="
echo "MCP 服务器安装脚本（Python 版本）"
echo "=========================================="
echo ""

# 检查 Python
echo "📦 检查 Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ 未安装 Python3"
    echo "请先安装 Python: https://www.python.org/"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
echo "✅ Python 版本: $PYTHON_VERSION"
echo ""

# 检查 pip
if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
    echo "❌ 未安装 pip"
    exit 1
fi

# 使用 pip3 或 pip
if command -v pip3 &> /dev/null; then
    PIP_CMD="pip3"
else
    PIP_CMD="pip"
fi

PIP_VERSION=$($PIP_CMD --version)
echo "✅ pip 版本: $PIP_VERSION"
echo ""

# 安装 MCP 服务器
echo "📦 安装 Python MCP 服务器..."
echo ""

# 1. MCP SDK（核心依赖）
echo "1️⃣  安装 MCP SDK..."
$PIP_CMD install mcp httpx
echo "✅ MCP SDK 安装完成"
echo ""

# 2. SQLite 服务器（已验证可用）
echo "2️⃣  安装 SQLite 服务器..."
$PIP_CMD install mcp-server-sqlite
echo "✅ SQLite 服务器安装完成"
echo ""

# 3. Git 服务器（已验证可用）
echo "3️⃣  安装 Git 服务器..."
$PIP_CMD install mcp-server-git
echo "✅ Git 服务器安装完成"
echo ""

# 注意：以下包在 PyPI 上不可用，需要使用 Node.js 版本或自行实现
echo ""
echo "⚠️  注意：文件系统和网络请求功能需要额外配置"
echo "   选项 1: 安装 Node.js 版本的 MCP 服务器"
echo "   选项 2: 使用 Python 标准库直接实现（推荐）"
echo ""

# 验证安装
echo "=========================================="
echo "验证安装..."
echo "=========================================="
echo ""

# 列出已安装的 MCP 包
$PIP_CMD list | grep -i mcp || true

echo ""
echo "=========================================="
echo "✅ MCP SDK 安装完成！"
echo "=========================================="
echo ""
echo "📝 已安装的组件："
echo "  - mcp                    (MCP 核心 SDK)"
echo "  - mcp-server-sqlite      (SQLite 数据库)"
echo "  - mcp-server-git         (Git 版本控制)"
echo ""
echo "⚠️  注意：Python MCP 服务器启动方式复杂，不推荐使用"
echo ""
echo "✨ 推荐方案：使用内置工具（已集成）"
echo "  - 文件系统操作 ✅ (Python 标准库)"
echo "  - 网络搜索 ✅ (httpx + DuckDuckGo API)"
echo "  - 无需额外配置"
echo "  - 性能更快、更稳定"
echo ""
echo "📝 测试内置工具："
echo "   python tests/test_builtin_tools.py"
echo ""
echo "💡 如需高级功能（可选）："
echo "   可以安装 Node.js MCP 服务器（需要 Node.js 环境）"
echo "   npm install -g @modelcontextprotocol/server-brave-search"
echo "   npm install -g @modelcontextprotocol/server-filesystem"
echo ""
