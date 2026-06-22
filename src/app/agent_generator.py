"""
智能体生成器 (AG - Agent Generator)

应用管理层组件，负责：
- 根据用户提供的 Markdown 配置生成可 Docker 部署的 Agent 包
- Agent 包包含：A2A 协议服务 + 龙虾记忆系统 + Dockerfile
- 生成的 Agent 自动注册到 AW（Agent Warehouse）和 ARDC

这是从 Auto-Agent 项目合并到 ATS 主项目的核心模块。

对应接口文档：
- 应用管理层接口流程 §1 安装应用（扩展：支持 Markdown 驱动的 Agent 生成）
"""
import logging
import shutil
import zipfile
import uuid
import os
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent.parent

# Agent 模板目录
_AGENT_TEMPLATE_DIR = _PROJECT_ROOT / "agent_package"

# 输出目录
_OUTPUT_DIR = _PROJECT_ROOT / "outputs" / "agents"

# 模板目录
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"


class AgentGenerator:
    """
    智能体生成器（AG）

    职责：
    1. generate_agent()  — 根据 agent.md + workflow.md 生成 Agent ZIP 包
    2. build_and_register() — 生成 + 注册到 AW + 注册到 ARDC
    3. get_templates()  — 获取示例模板
    4. list_generated() — 列出已生成的 Agent 包

    生成的 Agent 包特性：
    - A2A 协议：标准 POST /a2a/execute 端点
    - NATS 数据流：JetStream pull_subscribe / publish
    - LLM 对话：DeepSeek API（agent.md 角色注入 + workflow.md 工作流）
    - 龙虾记忆：MEMORY.md + 每日日志 + TF-IDF/BM25 混合搜索
    - 多会话：X-Session-Id 隔离 + token 追踪 + 自动记忆整理
    - Docker 部署：一键 docker-compose up -d
    """

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or _OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.template_dir = _AGENT_TEMPLATE_DIR
        logger.info(
            f"AgentGenerator (AG) 初始化完成 | 模板目录: {self.template_dir}"
        )

    # ------------------------------------------------------------------
    # 核心生成接口
    # ------------------------------------------------------------------

    def generate_agent(
        self,
        agent_md_content: str,
        workflow_md_content: Optional[str] = None,
        agent_name: str = "custom-agent",
    ) -> Tuple[str, Path]:
        """
        生成 Agent 部署包

        流程：
        1. 验证输入
        2. 复制 agent_package 模板到临时构建目录
        3. 注入 agent.md / workflow.md
        4. 打包为 ZIP
        5. 清理临时目录
        6. 返回 agent_id 和 ZIP 路径

        Args:
            agent_md_content:    agent.md 的内容（必填，定义角色）
            workflow_md_content: workflow.md 的内容（可选，定义工作流）
            agent_name:          Agent 名称

        Returns:
            (agent_id: str, zip_path: Path)
        """
        if not agent_md_content or not agent_md_content.strip():
            raise ValueError("agent.md 内容不能为空")

        logger.info(
            f"[AG] 开始生成 Agent: name={agent_name}, "
            f"agent_md={len(agent_md_content)}chars, "
            f"has_workflow={bool(workflow_md_content)}"
        )

        # 生成唯一 ID
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        build_dir = self.output_dir / agent_id
        build_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1. 复制 agent_package 模板
            self._copy_dir(self.template_dir, build_dir)

            # 2. 写入配置
            config_dir = build_dir / "config"
            config_dir.mkdir(exist_ok=True)
            (config_dir / "agent.md").write_text(agent_md_content, encoding="utf-8")

            if workflow_md_content and workflow_md_content.strip():
                (config_dir / "workflow.md").write_text(workflow_md_content, encoding="utf-8")
            else:
                # 删除占位 workflow.md
                placeholder = config_dir / "workflow.md"
                if placeholder.exists():
                    placeholder.unlink()

            # 3. 打包 ZIP
            zip_path = self.output_dir / f"{agent_id}.zip"
            self._create_zip(build_dir, zip_path)

            logger.info(f"[AG] ✅ Agent 生成成功: {zip_path} ({zip_path.stat().st_size} bytes)")
            return agent_id, zip_path

        except Exception as e:
            logger.exception(f"[AG] Agent 生成失败: {e}")
            raise
        finally:
            # 清理构建目录
            if build_dir.exists():
                shutil.rmtree(build_dir, ignore_errors=True)

    def build_and_register(
        self,
        agent_md_content: str,
        workflow_md_content: Optional[str] = None,
        agent_name: str = "custom-agent",
        capability: str = "chat",
        version: str = "1.0.0",
    ) -> dict:
        """
        生成 Agent 并注册到 AW + ARDC（一站式接口）

        流程：
        1. 调用 generate_agent() 生成 ZIP
        2. 创建 AgentImage 并注册到 AW
        3. 注册到 ARDC（使编排层可发现）

        Args:
            agent_md_content:    agent.md 内容
            workflow_md_content: workflow.md 内容（可选）
            agent_name:          Agent 名称
            capability:          能力类型（chat/nlp/search/compute/vision）
            version:             版本号

        Returns:
            {
                "agent_id": str,
                "image_id": str,
                "zip_path": str,
                "capability": str,
                "registered": bool,
            }
        """
        # 生成 ZIP
        agent_id, zip_path = self.generate_agent(
            agent_md_content=agent_md_content,
            workflow_md_content=workflow_md_content,
            agent_name=agent_name,
        )

        # 注册到 AW
        try:
            from src.app.agent_warehouse import get_agent_warehouse
            from src.app.models import AgentImage

            warehouse = get_agent_warehouse()
            image = AgentImage.create(
                name=agent_name,
                version=version,
                capability=capability,
                description=f"AI Agent: {agent_name} — {capability} capability, deployed via Auto-Agent",
                metadata={
                    "agent_id": agent_id,
                    "zip_path": str(zip_path),
                    "has_workflow": bool(workflow_md_content),
                    "generated_at": datetime.utcnow().isoformat(),
                    "generator": "Auto-Agent v3",
                },
            )
            warehouse.install_agent(image, expose_external=False)
            logger.info(f"[AG→AW] 已注册: image_id={image.image_id}, capability={capability}")
            registered = True
            image_id = image.image_id
        except Exception as e:
            logger.warning(f"[AG→AW] AW 注册失败（非关键）: {e}")
            registered = False
            image_id = f"unregistered_{agent_id}"

        return {
            "agent_id": agent_id,
            "image_id": image_id,
            "zip_path": str(zip_path),
            "capability": capability,
            "registered": registered,
            "has_workflow": bool(workflow_md_content),
        }

    # ------------------------------------------------------------------
    # 模板管理
    # ------------------------------------------------------------------

    def get_agent_template(self) -> str:
        """获取 agent.md 示例模板"""
        path = _TEMPLATES_DIR / "agent_template.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return self._default_agent_template()

    def get_workflow_template(self) -> str:
        """获取 workflow.md 示例模板"""
        path = _TEMPLATES_DIR / "workflow_template.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return self._default_workflow_template()

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def list_generated(self) -> list:
        """列出所有已生成的 Agent 包"""
        agents = []
        for zip_file in sorted(self.output_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
            agents.append({
                "agent_id": zip_file.stem,
                "filename": zip_file.name,
                "size": zip_file.stat().st_size,
                "created_at": datetime.fromtimestamp(zip_file.stat().st_mtime).isoformat(),
                "download_path": str(zip_file),
            })
        return agents

    def get_agent_package(self, agent_id: str) -> Optional[Path]:
        """获取已生成的 Agent 包路径"""
        zip_path = self.output_dir / f"{agent_id}.zip"
        if zip_path.exists():
            return zip_path
        return None

    # ------------------------------------------------------------------
    # 静态工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _copy_dir(src: Path, dst: Path):
        """递归复制目录"""
        if not src.exists():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = dst / item.name
            if item.is_dir():
                AgentGenerator._copy_dir(item, target)
            else:
                shutil.copy2(item, target)

    @staticmethod
    def _create_zip(source_dir: Path, output_path: Path):
        """将目录打包为 ZIP"""
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in source_dir.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(source_dir)
                    zf.write(file_path, arcname)

    @staticmethod
    def _default_agent_template() -> str:
        return """# 角色：专业的技术顾问

## 核心职责
你是一个专业的技术顾问，负责为用户提供高质量的技术咨询和解决方案。

## 专业领域
- 软件开发（Python、JavaScript、Go、Rust 等）
- 系统架构设计（微服务、云原生、分布式系统）
- DevOps 与 CI/CD（Docker、Kubernetes、GitHub Actions）

## 行为准则
1. 回答前先理解用户真实需求，必要时追问确认
2. 提供具体可执行的解决方案，包含代码示例
3. 解释技术方案背后的原理和权衡
4. 所有回答使用中文，技术术语可保留英文

## 输出格式
- 先给出结论或核心建议
- 再展开详细说明
- 最后给出可操作的下一步建议
"""

    @staticmethod
    def _default_workflow_template() -> str:
        return """# 工作流程

## 第一步：需求分析
1. 仔细阅读用户的问题描述
2. 识别关键需求和约束条件
3. 如果不明确，用 1-2 个问题澄清

## 第二步：方案设计
1. 给出 1-2 个可行的解决方案
2. 说明每个方案的优缺点
3. 推荐最优方案并说明理由

## 第三步：详细展开
1. 提供具体的实现步骤
2. 附上可运行的代码示例
3. 标注关键的配置项和注意事项

## 第四步：总结与验证
1. 回顾方案的关键点
2. 给出测试/验证的方法
3. 提供进一步优化的建议
"""


# ======================================================================
# 单例访问
# ======================================================================
_generator_instance: Optional[AgentGenerator] = None


def get_agent_generator() -> AgentGenerator:
    """获取全局 AgentGenerator 单例"""
    global _generator_instance
    if _generator_instance is None:
        _generator_instance = AgentGenerator()
    return _generator_instance
