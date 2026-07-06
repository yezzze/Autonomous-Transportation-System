"""记忆文件管理器 - 管理 workspace 中的 MEMORY.md、每日日志等"""
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List


class MemoryStore:
    """基于文件的记忆存储系统，管理 agent 的长期记忆和每日日志"""

    def __init__(self, workspace_dir: Path):
        self.workspace = Path(workspace_dir)
        self.memory_dir = self.workspace / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # ── 基础读写 ──────────────────────────────────────

    def read_file(self, filename: str) -> str:
        filepath = self.workspace / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""

    def write_file(self, filename: str, content: str):
        (self.workspace / filename).write_text(content, encoding="utf-8")

    def append_to_file(self, filename: str, content: str):
        filepath = self.workspace / filename
        existing = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
        filepath.write_text(existing + "\n" + content, encoding="utf-8")

    # ── 记忆文件 ───────────────────────────────────────

    def get_memory(self) -> str:
        return self.read_file("MEMORY.md")

    def get_daily_log(self, date_str: str = None) -> str:
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        return self.read_file(f"memory/{date_str}.md")

    def get_recent_logs(self, days: int = 2) -> str:
        logs = []
        for i in range(days):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            content = self.get_daily_log(date_str)
            if content:
                logs.append(f"## {date_str}\n{content}")
        return "\n\n".join(logs) if logs else ""

    def write_daily_log(self, content: str, date_str: str = None):
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        filepath = self.memory_dir / f"{date_str}.md"
        timestamp = datetime.now().strftime("%H:%M")
        entry = f"\n### [{timestamp}]\n{content}\n"
        if filepath.exists():
            filepath.write_text(filepath.read_text(encoding="utf-8") + entry, encoding="utf-8")
        else:
            filepath.write_text(
                f"# {date_str} 日志\n\n> 智能体每日记录\n\n---\n{entry}", encoding="utf-8"
            )

    def add_memory_entry(self, category: str, entry: str, importance: str = "中"):
        """向 MEMORY.md 添加记忆条目"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        formatted = f"\n### [{timestamp}] ⭐{importance}\n{entry}\n"

        content = self.get_memory()
        section = f"## {category}"
        if section in content:
            parts = content.split(section, 1)
            rest = parts[1]
            next_sec = re.search(r"\n## ", rest)
            if next_sec:
                pos = next_sec.start()
                new_content = parts[0] + section + rest[:pos] + formatted + rest[pos:]
            else:
                new_content = parts[0] + section + rest + formatted
        else:
            new_content = content.rstrip("\n") + f"\n{section}\n{formatted}\n"
        self.write_file("MEMORY.md", new_content)

    def list_all_files(self) -> List[dict]:
        """列出所有记忆文件"""
        files = []
        for f in sorted(self.workspace.glob("*.md")):
            st = f.stat()
            files.append({"name": f.name, "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).isoformat()})
        for f in sorted(self.memory_dir.glob("*.md")):
            st = f.stat()
            files.append({"name": f"memory/{f.name}", "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).isoformat()})
        return files
