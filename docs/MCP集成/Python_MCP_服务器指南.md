# Python MCP 服务器指南

## 📋 概述

本指南介绍如何使用 **Python 实现的 MCP 服务器**，无需 Node.js 依赖。

## 🎯 为什么选择 Python MCP 服务器？

### 优势
- ✅ **无需 Node.js**：纯 Python 环境，简化部署
- ✅ **环境一致**：与主项目使用相同的 Python 环境
- ✅ **易于调试**：Python 代码更易于追踪和调试
- ✅ **生态集成**：可以直接使用 Python 库
- ✅ **Docker 友好**：容器镜像更小

### 限制
- ⚠️ **功能较少**：部分 MCP 服务器只有 Node.js 版本（如 Brave Search, Puppeteer）
- ⚠️ **社区支持**：Python 实现的服务器相对较少

## 🚀 快速开始

### 1. 安装 Python MCP 服务器

```bash
# 方式 1: 使用自动安装脚本（推荐）
./install_mcp_servers.sh

# 方式 2: 手动安装
pip install mcp httpx
pip install mcp-server-sqlite
pip install mcp-server-git
pip install mcp-server-filesystem
pip install mcp-server-fetch
```

### 2. 验证安装

```bash
# 检查已安装的 MCP 包
pip list | grep mcp

# 应该看到：
# mcp                   0.9.0
# mcp-server-sqlite     0.1.0
# mcp-server-git        0.1.0
# mcp-server-filesystem 0.1.0
# mcp-server-fetch      0.1.0
```

### 3. 测试连接

```bash
python tests/test_mcp_client.py
```

## 📦 可用的 Python MCP 服务器

### 1. Filesystem 服务器

**功能**: 文件系统操作（读/写/列表/创建目录等）

**安装**:
```bash
pip install mcp-server-filesystem
```

**启动**:
```bash
python -m mcp_server_filesystem /tmp
```

**配置**（已在 `mcp_client.py` 中）:
```python
"filesystem": {
    "command": ["python", "-m", "mcp_server_filesystem", "/tmp"],
    "description": "文件系统操作工具"
}
```

**可用工具**:
- `read_file` - 读取文件
- `write_file` - 写入文件
- `list_directory` - 列出目录
- `create_directory` - 创建目录
- `move_file` - 移动文件
- `delete_file` - 删除文件

**示例**:
```python
from src.service.mcp_client import get_global_mcp_registry

registry = get_global_mcp_registry()
client = await registry.get_client("filesystem")

# 读取文件
content = await client.call_tool("read_file", {"path": "/tmp/test.txt"})

# 写入文件
await client.call_tool("write_file", {
    "path": "/tmp/test.txt",
    "content": "Hello MCP!"
})
```

---

### 2. SQLite 服务器

**功能**: SQLite 数据库操作（查询/插入/更新等）

**安装**:
```bash
pip install mcp-server-sqlite
```

**启动**:
```bash
python -m mcp_server_sqlite --db-path /tmp/test.db
```

**配置**:
```python
"sqlite": {
    "command": ["python", "-m", "mcp_server_sqlite", "--db-path", "/tmp/test.db"],
    "description": "SQLite 数据库工具"
}
```

**可用工具**:
- `query` - 执行 SQL 查询
- `execute` - 执行 SQL 语句（INSERT/UPDATE/DELETE）
- `list_tables` - 列出所有表
- `describe_table` - 查看表结构

**示例**:
```python
client = await registry.get_client("sqlite")

# 创建表
await client.call_tool("execute", {
    "sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"
})

# 插入数据
await client.call_tool("execute", {
    "sql": "INSERT INTO users (name) VALUES (?)",
    "params": ["Alice"]
})

# 查询数据
result = await client.call_tool("query", {
    "sql": "SELECT * FROM users"
})
```

---

### 3. Git 服务器

**功能**: Git 版本控制操作（status/commit/branch 等）

**安装**:
```bash
pip install mcp-server-git
```

**启动**:
```bash
python -m mcp_server_git --repository .
```

**配置**:
```python
"git": {
    "command": ["python", "-m", "mcp_server_git", "--repository", "."],
    "description": "Git 版本控制工具"
}
```

**可用工具**:
- `status` - 查看状态
- `diff` - 查看差异
- `commit` - 提交更改
- `log` - 查看日志
- `branch` - 分支管理

**示例**:
```python
client = await registry.get_client("git")

# 查看状态
status = await client.call_tool("status", {})

# 提交更改
await client.call_tool("commit", {
    "message": "Update feature",
    "files": ["src/main.py"]
})
```

---

### 4. Fetch 服务器

**功能**: HTTP 请求（可用于调用搜索 API）

**安装**:
```bash
pip install mcp-server-fetch
```

**启动**:
```bash
python -m mcp_server_fetch
```

**配置**:
```python
"fetch": {
    "command": ["python", "-m", "mcp_server_fetch"],
    "description": "HTTP 请求工具"
}
```

**可用工具**:
- `fetch` - 发送 HTTP 请求

**示例**:
```python
client = await registry.get_client("fetch")

# GET 请求
result = await client.call_tool("fetch", {
    "url": "https://api.github.com/users/octocat",
    "method": "GET"
})

# POST 请求
result = await client.call_tool("fetch", {
    "url": "https://api.example.com/data",
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "body": '{"key": "value"}'
})
```

**网络搜索替代方案**:
由于没有 Brave Search 的 Python 版本，可以使用 fetch 服务器调用搜索 API：

```python
# 使用 DuckDuckGo API
result = await client.call_tool("fetch", {
    "url": f"https://api.duckduckgo.com/?q=python&format=json"
})

# 使用 Google Custom Search API（需要 API key）
result = await client.call_tool("fetch", {
    "url": f"https://www.googleapis.com/customsearch/v1?q=python&key={api_key}"
})
```

## 🔧 配置说明

### 当前配置（`src/service/mcp_client.py`）

```python
MCP_SERVERS = {
    "filesystem": {
        "command": ["python", "-m", "mcp_server_filesystem", "/tmp"],
        "description": "文件系统操作工具"
    },
    "sqlite": {
        "command": ["python", "-m", "mcp_server_sqlite", "--db-path", "/tmp/test.db"],
        "description": "SQLite 数据库工具"
    },
    "git": {
        "command": ["python", "-m", "mcp_server_git", "--repository", "."],
        "description": "Git 版本控制工具"
    },
    "fetch": {
        "command": ["python", "-m", "mcp_server_fetch"],
        "description": "HTTP 请求工具"
    },
}
```

### 自定义配置

如果需要修改服务器配置：

```python
# 1. 修改文件系统允许的目录
"filesystem": {
    "command": ["python", "-m", "mcp_server_filesystem", "/your/custom/path"],
    "description": "文件系统操作工具"
}

# 2. 修改数据库路径
"sqlite": {
    "command": ["python", "-m", "mcp_server_sqlite", "--db-path", "/path/to/db.sqlite"],
    "description": "SQLite 数据库工具"
}

# 3. 修改 Git 仓库路径
"git": {
    "command": ["python", "-m", "mcp_server_git", "--repository", "/path/to/repo"],
    "description": "Git 版本控制工具"
}
```

## 🔄 混合使用 Python 和 Node.js MCP 服务器

如果需要同时使用 Python 和 Node.js 服务器：

```python
MCP_SERVERS = {
    # Python 服务器
    "filesystem": {
        "command": ["python", "-m", "mcp_server_filesystem", "/tmp"],
        "description": "文件系统（Python）"
    },
    "sqlite": {
        "command": ["python", "-m", "mcp_server_sqlite", "--db-path", "/tmp/test.db"],
        "description": "SQLite（Python）"
    },
    
    # Node.js 服务器（如果已安装）
    "search": {
        "command": ["npx", "@modelcontextprotocol/server-brave-search"],
        "description": "Brave 搜索（Node.js）"
    },
    "puppeteer": {
        "command": ["npx", "@modelcontextprotocol/server-puppeteer"],
        "description": "浏览器自动化（Node.js）"
    },
}
```

## 📊 性能对比

| 服务器 | Python 版本 | Node.js 版本 | 推荐 |
|--------|------------|--------------|------|
| Filesystem | ✅ 可用 | ✅ 可用 | Python（部署简单） |
| SQLite | ✅ 可用 | ❌ 不可用 | Python |
| Git | ✅ 可用 | ❌ 不可用 | Python |
| Fetch | ✅ 可用 | ❌ 不可用 | Python |
| Brave Search | ❌ 不可用 | ✅ 可用 | Node.js |
| Puppeteer | ❌ 不可用 | ✅ 可用 | Node.js |

## 🐛 故障排查

### 问题 1: 找不到模块

**症状**:
```
ModuleNotFoundError: No module named 'mcp_server_filesystem'
```

**解决**:
```bash
pip install mcp-server-filesystem
```

### 问题 2: 文件权限错误

**症状**:
```
PermissionError: [Errno 13] Permission denied: '/restricted/path'
```

**解决**:
修改 filesystem 服务器的允许目录：
```python
"filesystem": {
    "command": ["python", "-m", "mcp_server_filesystem", "/tmp"],  # 使用有权限的目录
}
```

### 问题 3: 数据库被锁定

**症状**:
```
sqlite3.OperationalError: database is locked
```

**解决**:
确保没有其他进程在使用数据库，或使用不同的数据库文件。

## 🎯 最佳实践

1. **使用 Python 服务器**：如果不需要 Brave Search 和 Puppeteer，纯 Python 部署最简单
2. **混合部署**：需要网络搜索时使用 Node.js Brave Search，其他使用 Python
3. **Fetch 替代**：使用 fetch 服务器 + 公开搜索 API 替代 Brave Search
4. **容器化**：Python MCP 服务器可以直接打包进 Docker 镜像，无需额外的 Node.js 层

## 📚 参考资料

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP 服务器列表](https://github.com/modelcontextprotocol/servers)
- [创建自定义 Python MCP 服务器](https://modelcontextprotocol.io/docs/tools/python)

---

**总结**: Python MCP 服务器提供了文件系统、数据库、Git、HTTP 请求等核心功能，对于大多数场景已经足够。如果需要 Brave Search 或浏览器自动化，可以选择性安装 Node.js 版本。
