# LangManus 项目规范

## 构建与运行命令

```bash
source venv/bin/activate          # 激活虚拟环境（必须）
python server.py                  # 启动主服务（端口 8000）
python agent_server.py 9000       # 启动 Agent 节点
make serve                        # uv run server.py

# 测试（必须加 PYTHONPATH=.）
PYTHONPATH=. pytest tests/integration/ --ignore=tests/integration/test_bash_tool.py -v --no-header --tb=short --no-cov
PYTHONPATH=. python tests/test_magentic.py
```

## 架构三层，禁止跨层混写

- **L1 本机工具**：`src/tools/`（bash、python_repl、search、crawl）
- **L2 编排层**（本代码库）：`src/graph/`、`src/service/`、`agent_server.py`
- **L3 远端 Agent**：HTTP 调用，`POST /orchestration/dispatch`、`DELETE /orchestration/session/{id}`

## 代码约定

- 所有状态通过 `DistributedState`（`src/graph/distributed_types.py`）流转，**禁止直接 mutate state**
- 节点返回 `dict`；Magentic 图节点返回 `Command(goto=...)`
- 多文件同步修改：**必须用 `multi_replace_string_in_file`**，不要分多次 edit
- 替换代码时上下文至少 3 行，避免匹配歧义
- 测试必须加 `PYTHONPATH=.`，否则 import 报错

## LLM 三模型体系

```python
REASONING_MODEL  # 规划/复杂分析，支持流式（qwq-plus 只能 streaming=True）
BASIC_MODEL      # 快速决策，reporter
VL_MODEL         # 视觉任务
# 通过 get_llm_by_type() 获取，不要直接构造 LLM 对象
```

## ⚠️ 骨架区域 — 不要"修复"或替换

以下是有意保留的 mock/骨架，生产环境才替换：

| 区域 | 文件 | 说明 |
|------|------|------|
| RRDC 资源分配 | `src/service/resource_registry.py` | 内存实现，无跨节点 |
| AW 镜像拉取 | `src/app/agent_warehouse.py` | 不实际拉 Docker 镜像 |
| ASD 容器管理 | `src/service/agent_scheduler.py` | subprocess 替代 Docker，不换 docker SDK |
| ALCM 引用计数 | `src/runtime/lifecycle_manager.py` | 内存计数，无实际容器关闭 |

## 编排模式选择（adaptive_orchestrator 自动路由）

```
SIMPLE  → Sequential（线性，2 次 LLM 调用）
MEDIUM  → Concurrent（并行，中等 LLM 消耗）
COMPLEX → Magentic-One（Progress Ledger 反馈循环）
```

## 🔄 对话结束必须执行（Closing Checklist）

每次对话结束前，在最后一条回复中执行：

1. 更新 `PROJECT_CONTEXT.md`：`当前正在解决的问题` + `当前进度` 字段
2. 更新 `TASKS.md`：完成的任务移到"已完成"，补充新的"进行中"
3. 如有新架构决策，追加到 `PROJECT_CONTEXT.md` 的"关键约束与决策"
4. 同步更新 `接口/当前进度.md` 对应章节（实现状态档案）

> 不要等用户提示——每次对话结束时主动执行。
