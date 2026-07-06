"""会话管理器 v2 - 多会话隔离 + 持久化 + 自动整理"""
import re
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class Session:
    """单个会话：独立的消息历史、token 追踪、整理状态"""

    def __init__(self, session_id: str):
        self.id = session_id
        self.messages: list[dict] = []
        self.created_at = datetime.now()
        self.last_active = datetime.now()
        self.consolidation_done = False

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other = len(text) - chinese
        return int(chinese / 1.5 + other / 4)

    def total_tokens(self) -> int:
        return sum(self.estimate_tokens(m.get("content", "")) for m in self.messages)

    def usage_ratio(self, limit: int = 8000) -> float:
        return self.total_tokens() / limit

    def should_consolidate(self, limit: int = 8000, threshold: float = 0.8) -> bool:
        if self.consolidation_done:
            return False
        return self.usage_ratio(limit) >= threshold

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.last_active = datetime.now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "messages": self.messages[-40:],  # 最多保留 40 条恢复
            "consolidation_done": self.consolidation_done,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        s = cls(data["id"])
        s.messages = data.get("messages", [])
        s.consolidation_done = data.get("consolidation_done", False)
        s.created_at = datetime.fromisoformat(data.get("created_at", datetime.now().isoformat()))
        s.last_active = datetime.fromisoformat(data.get("last_active", datetime.now().isoformat()))
        return s


class SessionManager:
    """会话池管理器"""

    CONTEXT_MAX_TOKENS = 8000
    WARN_RATIO = 0.8
    SESSION_TTL_HOURS = 24

    def __init__(self, memory_store, api_key: str, base_url: str, model: str,
                 persist_dir: Optional[Path] = None):
        self.memory = memory_store
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._sessions: Dict[str, Session] = {}
        self._persist_dir = persist_dir
        if self._persist_dir:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            self._restore()

    # ── 会话生命周期 ────────────────────────────────

    def get(self, session_id: str = "default") -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(session_id)
        session = self._sessions[session_id]
        session.last_active = datetime.now()
        # 定期清理过期会话
        self._gc()
        return session

    def delete(self, session_id: str):
        self._sessions.pop(session_id, None)

    def _gc(self):
        cutoff = datetime.now() - timedelta(hours=self.SESSION_TTL_HOURS)
        stale = [sid for sid, s in self._sessions.items() if s.last_active < cutoff and sid != "default"]
        for sid in stale:
            del self._sessions[sid]
        if stale:
            logger.info(f"清理 {len(stale)} 个过期会话")

    # ── 持久化 ──────────────────────────────────────

    def _restore(self):
        if not self._persist_dir:
            return
        path = self._persist_dir / "sessions.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("sessions", []):
                s = Session.from_dict(item)
                if s.last_active > datetime.now() - timedelta(hours=self.SESSION_TTL_HOURS):
                    self._sessions[s.id] = s
            logger.info(f"恢复 {len(self._sessions)} 个会话")
        except Exception:
            pass

    def persist(self):
        if not self._persist_dir:
            return
        data = {"sessions": [s.to_dict() for s in self._sessions.values()]}
        (self._persist_dir / "sessions.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 记忆整理 ────────────────────────────────────

    async def consolidate(self, session_id: str) -> str:
        session = self.get(session_id)
        session.consolidation_done = True

        prompt = (
            "你正在执行静默记忆整理。从对话历史中提取需要持久化的信息。\n\n"
            "输出格式（严格，不要输出其他内容）：\n"
            "```memory\n[分类]: [具体内容]\n```\n"
            "```log\n[今日对话摘要，一句话]\n```\n"
            "如无值得记录的信息，输出\"无需记录\"。"
        )

        msgs = [{"role": "system", "content": prompt}] + session.messages[-20:]

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": self.model, "messages": msgs, "temperature": 0.3, "max_tokens": 600},
                )
                if resp.status_code != 200:
                    return "整理失败"

                text = resp.json()["choices"][0]["message"]["content"]
                mem_match = re.search(r"```memory\n(.*?)```", text, re.DOTALL)
                log_match = re.search(r"```log\n(.*?)```", text, re.DOTALL)

                results = []
                if mem_match and mem_match.group(1).strip() != "无需记录":
                    self.memory.add_memory_entry("记忆索引", mem_match.group(1).strip())
                    results.append("MEMORY.md")
                if log_match and log_match.group(1).strip() != "无需记录":
                    self.memory.write_daily_log(log_match.group(1).strip())
                    results.append("日志")

                return f"已写入 {', '.join(results)}" if results else "无需整理"
        except Exception as e:
            return f"整理异常: {e}"

    # ── 统计 ────────────────────────────────────────

    def stats(self, session_id: str = "default") -> dict:
        s = self.get(session_id)
        tokens = s.total_tokens()
        return {
            "session_id": session_id,
            "message_count": len(s.messages),
            "estimated_tokens": tokens,
            "context_usage_pct": round(tokens / self.CONTEXT_MAX_TOKENS * 100, 1),
            "consolidation_triggered": s.consolidation_done,
            "total_sessions": len(self._sessions),
        }
