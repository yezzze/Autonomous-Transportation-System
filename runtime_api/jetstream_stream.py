"""
JetStream Stream 配置管理
=========================

提供 JetStream 流的创建、更新和配置管理功能。
所有配置默认从环境变量读取，方便 K8S 容器化部署时通过 ConfigMap 或
环境变量灵活配置。

主要功能
--------
- parse_bytes         : 解析 NATS 风格的大小字符串 → int 字节数
- build_stream_config : 从环境变量构建 StreamConfig 对象
- ensure_jetstream_stream : 确保流存在（不存在则创建，存在则更新主题/限制）

使用示例
--------
    from runtime_api.jetstream_stream import ensure_jetstream_stream

    # 连接 NATS 后确保流存在
    nc = NATS()
    await nc.connect("nats://nats:4222")
    js = nc.jetstream(domain="hub")

    # 默认使用环境变量中的配置
    info = await ensure_jetstream_stream(js)
    print(info)  # {"created": True, "updated": False, "subjects": [...], ...}

    # 或手动指定名称和主题
    info = await ensure_jetstream_stream(
        js,
        name="MY_STREAM",
        subjects=["my.workflow.>", "my.events.>"],
        storage="file",
    )

环境变量
--------
    NATS_STREAM              兼容模式流名称（默认 WORKFLOW）
    NATS_STREAM_SUBJECTS     流主题，逗号分隔（默认 legacy.workflow.>）
    NATS_STREAM_MAX_BYTES    最大大小（默认 512MiB）
    NATS_STREAM_DISCARD      淘汰策略 old / new（默认 new）
    NATS_STREAM_RETENTION    保留策略 limits / interest / workqueue（默认 workqueue）
    NATS_STREAM_STORAGE      存储类型 file / memory（默认 file）
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError
from runtime_api.nats_subjects import merge_subject_patterns

logger = logging.getLogger(__name__)

# ============================================================
# 默认配置常量
# ============================================================
_DEFAULT_MAX_BYTES = "512MiB"      # 每实例流存储上限
_DEFAULT_DISCARD = "new"           # 满载时拒绝新消息，让发布方显式感知
_DEFAULT_RETENTION = "workqueue"   # ACK 后删除，未处理消息保留
_DEFAULT_STORAGE = "file"          # 文件存储（持久化）


# ============================================================
# 工具函数
# ============================================================

def parse_bytes(value: str) -> int:
    """
    解析 NATS 风格的大小字符串为字节整数。

    支持的格式:
        - 纯数字: "5000000000"
        - Si 单位: "5GB", "500MB"
        - 二进制单位: "5GiB", "512MiB"

    参数
    ----
    value : str
        大小字符串，例如 "5GB", "512MiB", "5000000"

    返回
    ----
    int
        对应的字节数

    示例
    ----
        parse_bytes("5GB")    → 5000000000 （十进制 GB）
        parse_bytes("5GiB")   → 5368709120 （二进制 GiB）
        parse_bytes("512MiB") → 536870912
        parse_bytes("1000")   → 1000
        parse_bytes("")       → 5368709120 （工具函数兼容默认值）
    """
    raw = (value or "").strip()
    if not raw:
        return 5 * 1024**3

    if raw.isdigit():
        return int(raw)

    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$", raw)
    if not match:
        raise ValueError(f"无效的字节大小格式: {value!r}")

    amount = float(match.group(1))
    unit = match.group(2).upper()
    if unit in {"B", ""}:
        return int(amount)
    if unit in {"KB", "KIB"}:
        base = 1024 if unit == "KIB" else 1000
        return int(amount * base)
    if unit in {"MB", "MIB"}:
        base = 1024**2 if unit == "MIB" else 1000**2
        return int(amount * base)
    if unit in {"GB", "GIB"}:
        base = 1024**3 if unit in {"GB", "GIB"} else 1000**3
        return int(amount * base)
    if unit in {"TB", "TIB"}:
        base = 1024**4 if unit == "TIB" else 1000**4
        return int(amount * base)
    raise ValueError(f"不支持的大小单位 in {value!r}")


def stream_name_from_env() -> str:
    """
    从环境变量 NATS_STREAM 读取流名称，不存在则返回默认值 "WORKFLOW"。

    返回
    ----
    str
        流名称
    """
    return os.getenv("NATS_STREAM", "WORKFLOW").strip() or "WORKFLOW"


def stream_subjects_from_env() -> List[str]:
    """
    从环境变量 NATS_STREAM_SUBJECTS 读取流主题列表。

    环境变量中多个主题用逗号分隔，例如:
        "workflow.>,task.>,event.>"

    返回
    ----
    List[str]
        主题列表，默认 ["legacy.workflow.>"]
    """
    raw = os.getenv("NATS_STREAM_SUBJECTS", "legacy.workflow.>")
    subjects = [item.strip() for item in raw.split(",") if item.strip()]
    return subjects or ["legacy.workflow.>"]


# ============================================================
# 内部策略解析函数
# ============================================================

def _retention_from_env() -> RetentionPolicy:
    """
    从环境变量 NATS_STREAM_RETENTION 解析保留策略。

    策略说明:
        - limits   : 基于 max_bytes / max_msgs 限制（默认）
        - interest : 当所有消费者确认消费后删除
        - workqueue: 工作队列模式，每个消息被一个消费者消费后即删除

    返回
    ----
    RetentionPolicy
    """
    raw = os.getenv("NATS_STREAM_RETENTION", _DEFAULT_RETENTION).strip().lower()
    if raw in {"interest", "interestpolicy"}:
        return RetentionPolicy.INTEREST
    if raw in {"work", "workqueue", "workqueuepolicy"}:
        return RetentionPolicy.WORK_QUEUE
    return RetentionPolicy.LIMITS


def _discard_from_env() -> DiscardPolicy:
    """
    从环境变量 NATS_STREAM_DISCARD 解析淘汰策略。

    策略说明:
        - old : 当达到限制时，淘汰最旧的消息（默认）
        - new : 当达到限制时，拒绝新消息

    返回
    ----
    DiscardPolicy
    """
    raw = os.getenv("NATS_STREAM_DISCARD", _DEFAULT_DISCARD).strip().lower()
    if raw in {"new", "discardnew"}:
        return DiscardPolicy.NEW
    return DiscardPolicy.OLD


def _storage_from_env(storage: Optional[str]) -> StorageType:
    """
    从环境变量 NATS_STREAM_STORAGE 或传入参数解析存储类型。

    参数
    ----
    storage : Optional[str]
        传入的存储类型，优先于环境变量

    返回
    ----
    StorageType
    """
    raw = (storage or os.getenv("NATS_STREAM_STORAGE", _DEFAULT_STORAGE)).strip().lower()
    if raw == "memory":
        return StorageType.MEMORY
    return StorageType.FILE


# ============================================================
# 核心构建/操作函数
# ============================================================

def build_stream_config(
    name: str,
    subjects: List[str],
    storage: Optional[str] = None,
) -> StreamConfig:
    """
    基于环境变量构建完整的 StreamConfig。

    配置来源（优先级: 环境变量 > 内部默认值）:
        - max_bytes : NATS_STREAM_MAX_BYTES → 默认 512MiB
        - discard   : NATS_STREAM_DISCARD   → 默认 new
        - retention : NATS_STREAM_RETENTION → 默认 workqueue
        - storage   : 传入参数 或 NATS_STREAM_STORAGE → 默认 file

    参数
    ----
    name : str
        流的名称
    subjects : List[str]
        流关联的主题列表
    storage : Optional[str]
        存储类型，可选 "file" / "memory"

    返回
    ----
    StreamConfig
        可直接用于 js.add_stream() 或 js.update_stream() 的配置对象
    """
    return StreamConfig(
        name=name,
        subjects=subjects,
        retention=_retention_from_env(),
        max_bytes=parse_bytes(os.getenv("NATS_STREAM_MAX_BYTES", _DEFAULT_MAX_BYTES)),
        discard=_discard_from_env(),
        storage=_storage_from_env(storage),
    )


def merge_stream_subjects(current: List[str], required: List[str]) -> List[str]:
    """
    合并已有的和必需的主题列表，去重。

    当需要向已存在的流添加新主题时使用此函数：
        - 保留已有主题
        - 添加不在已有的必需主题
        - 如果列表为空则返回必需主题列表

    参数
    ----
    current : List[str]
        流当前的主题列表
    required : List[str]
        需要确保存在的主题列表

    返回
    ----
    List[str]
        合并去重后的主题列表
    """
    return merge_subject_patterns(current or [], required)


async def apply_stream_config(js, config: StreamConfig) -> None:
    """
    将完整的流配置应用到已存在的流。

    用于更新流的限制（max_bytes, discard）和主题列表。
    注意: 对于 Stream 名称、存储类型等不可变属性不会生效。

    参数
    ----
    js : JetStream 上下文
        通过 nc.jetstream() 获取的 JetStream 实例
    config : StreamConfig
        要应用的新配置
    """
    await js.update_stream(config)


async def ensure_jetstream_stream(
    js,
    name: Optional[str] = None,
    subjects: Optional[List[str]] = None,
    storage: Optional[str] = None,
    replace_subjects: bool = False,
) -> Dict[str, Any]:
    """
    确保 JetStream 流存在，不存在则自动创建，存在则更新配置。

    该函数是"幂等"的——多次调用不会造成重复副作用：
        - 流不存在 → 新建并返回 {"created": True}
        - 流已存在但配置不符 → 更新并返回 {"updated": True}
        - 流已存在且配置一致 → 返回 {"updated": False}

    参数
    ----
    js : JetStream 上下文
        通过 nc.jetstream() 获取的 JetStream 实例
    name : Optional[str]
        流名称，不传则从环境变量 NATS_STREAM 读取
    subjects : Optional[List[str]]
        流主题列表，不传则从环境变量 NATS_STREAM_SUBJECTS 读取
    storage : Optional[str]
        存储类型 "file" / "memory"
    replace_subjects : bool
        True 时使用传入 subjects 精确替换现有主题；实例隔离 Stream 应启用

    返回
    ----
    Dict[str, Any]
        {
            "created": bool,    # 是否新建了流
            "updated": bool,    # 是否更新了配置
            "subjects": [...],  # 最终的主题列表
            "max_bytes": int,   # 最大字节数
            "discard": str,     # 淘汰策略
        }

    示例
    ----
        import asyncio
        from nats.aio.client import Client as NATS
        from runtime_api.jetstream_stream import ensure_jetstream_stream

        async def main():
            nc = NATS()
            await nc.connect("nats://nats:4222")
            js = nc.jetstream(domain="hub")

            # 使用默认配置创建/更新流
            result = await ensure_jetstream_stream(js)
            print(result)

            # 指定流名称和主题
            result = await ensure_jetstream_stream(
                js,
                name="MY_STREAM",
                subjects=["task.>", "event.>"],
            )

            await nc.drain()

        asyncio.run(main())
    """
    stream = name or stream_name_from_env()
    required = subjects or stream_subjects_from_env()
    config = build_stream_config(stream, required, storage=storage)

    try:
        info = await js.stream_info(stream)
    except NotFoundError:
        # 流不存在 → 创建
        await js.add_stream(config)
        logger.info(
            "created JetStream stream %s subjects=%s max_bytes=%s discard=%s",
            stream,
            required,
            config.max_bytes,
            config.discard,
        )
        return {
            "created": True,
            "updated": False,
            "subjects": required,
            "max_bytes": config.max_bytes,
            "discard": str(config.discard),
        }

    # 流已存在 → 检查并更新配置
    current = list(getattr(getattr(info, "config", None), "subjects", None) or [])
    final_subjects = required if replace_subjects else merge_stream_subjects(
        current,
        required,
    )
    config = build_stream_config(stream, final_subjects, storage=storage)

    await apply_stream_config(js, config)
    updated = final_subjects != current or (
        getattr(info.config, "max_bytes", None) != config.max_bytes
        or str(getattr(info.config, "discard", "")) != str(config.discard)
    )
    if updated:
        logger.info(
            "updated JetStream stream %s subjects=%s max_bytes=%s discard=%s",
            stream,
            final_subjects,
            config.max_bytes,
            config.discard,
        )
    return {
        "created": False,
        "updated": updated,
        "subjects": final_subjects,
        "max_bytes": config.max_bytes,
        "discard": str(config.discard),
    }
