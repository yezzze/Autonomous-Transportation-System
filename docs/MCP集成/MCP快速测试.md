# MCP 快速测试脚本

快速测试 MCP 各个组件是否正常工作。

## 测试 1: MCP SDK 安装

```bash
python -c "import mcp; print('MCP SDK 版本:', mcp.__version__)"
```

## 测试 2: Node.js MCP 服务器

```bash
# 测试 Brave Search 服务器
npx @modelcontextprotocol/server-brave-search --help

# 测试 Filesystem 服务器
npx @modelcontextprotocol/server-filesystem --help
```

## 测试 3: Brave API Key

```bash
echo $BRAVE_API_KEY
# 应该输出你的 API key
```

## 测试 4: MCP 客户端连接

```bash
python tests/test_mcp_client.py
```

预期输出：
```
🚀 开始 MCP 客户端测试
============================================================
测试 1: Brave Web Search
============================================================
🔌 连接 MCP 服务器 [search]: npx @modelcontextprotocol/server-brave-search
✅ 发现 1 个 MCP 工具: ['brave_web_search']
✅ 搜索成功
```

## 测试 5: 统一执行层

```bash
python tests/test_unified_executor.py
```

## 测试 6: 完整集成

```bash
python examples/mcp_integration_demo.py
```

## 常见问题排查

### 问题 1: 找不到 npx 命令

```bash
# 检查 Node.js 安装
node --version
npm --version

# 如果未安装，访问 https://nodejs.org/
```

### 问题 2: MCP 服务器启动失败

```bash
# 检查全局包
npm list -g | grep modelcontextprotocol

# 重新安装
npm install -g @modelcontextprotocol/server-brave-search
```

### 问题 3: Brave Search API 错误

```bash
# 检查 API key
echo $BRAVE_API_KEY

# 或在 .env 中添加
echo "BRAVE_API_KEY=your_key_here" >> .env
```

### 问题 4: 文件权限错误

```bash
# 确保 /tmp 目录可写
touch /tmp/test.txt
rm /tmp/test.txt

# 或修改 MCP 配置指向其他目录
```

## 性能测试

```bash
# 测试搜索速度
time python -c "
import asyncio
from src.service.mcp_client import get_global_mcp_registry

async def test():
    registry = get_global_mcp_registry()
    client = await registry.get_client('search')
    result = await client.call_tool('brave_web_search', {'query': 'test'})
    print(f'结果长度: {len(result)}')

asyncio.run(test())
"
```

## 日志调试

```bash
# 启用详细日志
export LOG_LEVEL=DEBUG
python tests/test_mcp_client.py
```

## 下一步

- ✅ 所有测试通过 → 开始使用 MCP
- ⚠️ 部分测试失败 → 查看上面的排查步骤
- ❌ 测试全部失败 → 检查基础环境 (Python, Node.js)
