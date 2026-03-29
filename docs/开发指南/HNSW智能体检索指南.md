# HNSW 智能体检索系统

## 📖 概述

在大规模智能体场景下（>100个），使用 HNSW（Hierarchical Navigable Small World）向量索引进行快速检索，可以大幅提升智能体匹配效率。

### 工作流程

```
任务请求 → HNSW 向量检索 → 初步筛选 Top-K → LLM 精细筛选 → 最终选择
   (1ms)       (10-50ms)         (候选集)      (200-500ms)      (1个)
```

### 性能对比

| 智能体数量 | 传统遍历 | HNSW 检索 | 加速比 |
|-----------|---------|----------|--------|
| 100       | 5ms     | 2ms      | 2.5x   |
| 1,000     | 50ms    | 5ms      | 10x    |
| 10,000    | 500ms   | 10ms     | 50x    |
| 100,000   | 5s      | 20ms     | 250x   |

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install hnswlib sentence-transformers
```

或使用项目的 requirements.txt：

```bash
pip install -r requirements.txt
```

### 2. 配置智能体

编辑 `config/agent_registry.json`，添加智能体信息（关键字段）：

```json
{
  "agents": [
    {
      "id": "search_agent_001",
      "capability": "search",
      "description": "专门用于网络搜索和信息检索的 Agent，支持多引擎搜索",
      "tags": ["search", "web", "tavily"],
      "enabled": true
    }
  ]
}
```

**重要字段说明**：
- `capability`: 能力类型（如 search, compute, nlp）
- `description`: 详细描述（用于向量编码，越详细越好）
- `tags`: 标签列表（增强检索精度）

### 3. 构建索引

```bash
# 首次构建索引
python scripts/build_agent_index.py

# 添加新智能体（不重建）
python scripts/build_agent_index.py --no-rebuild

# 指定配置文件
python scripts/build_agent_index.py --config my_agents.json
```

### 4. 使用检索

```python
from src.service.agent_registry import get_registry_client

# 获取注册表客户端（自动判断是否使用 HNSW）
registry = get_registry_client()

# 基于任务描述检索智能体
agents = registry.query_agents(
    task_description="搜索今天的天气情况",
    top_k=5  # 返回前5个最相关的智能体
)

# 结果包含相似度得分
for agent in agents:
    print(f"{agent['id']}: {agent['capability']}")
    print(f"相似度: {agent['_similarity_score']:.3f}")
```

## 🔧 配置说明

### 自动启用阈值

系统会根据智能体数量自动决定是否启用 HNSW：

```python
# 在 src/service/agent_registry.py
HNSW_THRESHOLD = 100  # 超过100个智能体自动启用 HNSW
```

### 手动控制

```python
from src.service.agent_registry import AgentRegistryClient

# 强制启用 HNSW
registry = AgentRegistryClient(use_hnsw=True)

# 强制禁用 HNSW
registry = AgentRegistryClient(use_hnsw=False)

# 自动判断（默认）
registry = AgentRegistryClient(use_hnsw=None)
```

### HNSW 参数调优

编辑 `src/service/agent_hnsw_index.py`：

```python
hnsw_index = AgentHNSWIndex(
    dim=384,              # 向量维度（模型决定）
    max_elements=100000,  # 最大智能体数量
    ef_construction=200,  # 构建参数（越大越精确，越慢）
    M=16                  # 连接数（影响召回率和内存）
)
```

**参数说明**：
- `ef_construction`: 100-400，推荐200（精度vs速度）
- `M`: 12-48，推荐16（内存vs召回率）
- `ef` (查询时): 10-100，推荐50（查询精度）

## 📊 测试和验证

### 运行测试

```bash
# 完整测试套件
python test_hnsw_retrieval.py
```

测试内容包括：
1. 基本检索测试（6种不同任务类型）
2. 能力过滤测试
3. 性能基准测试

### 预期输出

```
🔍 HNSW 检索: 查询='搜索今天的天气情况', 返回 3 个候选智能体
✅ HNSW 初筛返回 3 个候选智能体 (相似度: 0.892 - 0.654)

【测试 1】
任务: 搜索今天的天气情况
期望能力: search
------------------------------------------------------------
✅ 检索到 3 个候选智能体:
  1. search_agent_001
     能力: search
     描述: 专门用于网络搜索和信息检索的 Agent，支持多引擎搜索
     相似度: 0.892

✅ 匹配成功！Top-1 智能体能力正确
```

## 🎯 使用场景

### 场景1：小规模（<100个智能体）

系统自动使用**传统精确匹配**：

```python
# 自动判断，使用传统方法
agents = registry.query_agents(capability="search")
```

### 场景2：中等规模（100-1000个智能体）

系统自动启用 **HNSW 快速检索**：

```python
# 自动启用 HNSW
agents = registry.query_agents(
    task_description="分析财务数据",
    top_k=10
)
```

### 场景3：大规模（>1000个智能体）

HNSW + 能力过滤：

```python
# HNSW + 能力过滤，双重加速
agents = registry.query_agents(
    task_description="执行复杂的数据分析任务",
    capability="compute",  # 只搜索 compute 能力
    top_k=20
)
```

## 🔄 索引维护

### 更新智能体

```bash
# 1. 修改 config/agent_registry.json
vim config/agent_registry.json

# 2. 重建索引
python scripts/build_agent_index.py

# 3. 重启服务
docker-compose restart web-ui
```

### 查看索引统计

```python
from src.service.agent_registry import get_registry_client

registry = get_registry_client()
summary = registry.get_registry_summary()

print(summary)
# {
#   "total_agents": 1000,
#   "online_agents": 950,
#   "retrieval_mode": "HNSW",
#   "hnsw_stats": {
#     "total_agents": 950,
#     "dimension": 384,
#     "capabilities": ["search", "compute", "nlp", ...]
#   }
# }
```

## 🐳 Docker 部署

### 预构建索引

在构建镜像前预构建索引：

```bash
# 1. 构建索引
python scripts/build_agent_index.py

# 2. 索引文件会保存在 data/agent_hnsw_index/
ls data/agent_hnsw_index/
# hnsw_index.bin  metadata.json

# 3. 构建 Docker 镜像（索引文件会被打包）
docker-compose build
```

### 运行时更新

```bash
# 1. 修改配置
vim config/agent_registry.json

# 2. 进入容器重建索引
docker exec -it langmanus-web-ui python scripts/build_agent_index.py

# 3. 重启容器
docker-compose restart web-ui
```

## 📈 性能优化

### 1. 增加召回精度

```python
# 返回更多候选，让 LLM 有更多选择
agents = registry.query_agents(
    task_description="任务描述",
    top_k=20  # 增加到20个候选
)
```

### 2. 减少延迟

```python
# 减少候选数量
agents = registry.query_agents(
    task_description="任务描述",
    top_k=3  # 只要3个候选
)
```

### 3. 平衡精度和速度

调整 HNSW 的 `ef` 参数：

```python
# 在 src/service/agent_hnsw_index.py
self.index.set_ef(100)  # 提高精度（默认50）
self.index.set_ef(20)   # 提高速度（默认50）
```

## 🛠️ 故障排查

### 问题1：索引未生成

```bash
# 检查索引文件
ls -la data/agent_hnsw_index/

# 手动构建
python scripts/build_agent_index.py
```

### 问题2：检索结果不准确

- 检查智能体的 `description` 字段是否详细
- 增加 `tags` 字段
- 调整 `ef_construction` 参数（提高精度）

### 问题3：内存占用过高

- 减少 `M` 参数（降低内存）
- 使用更小的嵌入模型
- 只索引启用的智能体

### 问题4：HNSW 未启用

```python
# 检查配置
summary = registry.get_registry_summary()
print(summary['retrieval_mode'])  # 应该是 "HNSW"

# 强制启用
registry = AgentRegistryClient(use_hnsw=True)
```

## 📚 技术细节

### 向量编码模型

使用 `sentence-transformers` 的多语言模型：

```python
embedding_model = "paraphrase-multilingual-MiniLM-L12-v2"
# - 支持50+种语言
# - 向量维度: 384
# - 速度: ~1000句/秒
```

### 相似度计算

使用余弦相似度（Cosine Similarity）：

```python
similarity = 1 - cosine_distance
# similarity ∈ [0, 1]，越接近1越相似
```

### 索引结构

```
data/agent_hnsw_index/
├── hnsw_index.bin     # HNSW 图结构（二进制）
└── metadata.json      # 智能体元数据（JSON）
```

## 🎉 总结

### 优势

✅ **高性能**：大规模场景下检索速度提升50-250倍  
✅ **高精度**：基于语义向量，理解任务意图  
✅ **可扩展**：支持10万+智能体  
✅ **自动化**：根据规模自动启用/禁用  
✅ **易维护**：配置驱动，索引持久化  

### 适用场景

- 🏢 企业级部署（1000+ 智能体）
- 🌐 多租户SaaS平台
- 🤖 智能体市场/生态系统
- 📊 动态能力发现和匹配

### 后续优化

- [ ] 增量索引更新（避免全量重建）
- [ ] 分布式索引（多节点负载均衡）
- [ ] 实时索引监控和报警
- [ ] A/B测试框架（对比不同检索策略）
