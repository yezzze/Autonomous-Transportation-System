"""Shared AOE cluster configuration loading."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict


AOE_CLUSTER_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "aoe_cluster_config.json"


def default_aoe_config() -> Dict[str, Any]:
    local_url = os.getenv("LOCAL_AOE_URL", "http://localhost:8001").strip()
    peer_urls = [
        value.strip().rstrip("/")
        for value in re.split(r"[\s,]+", os.getenv("PEER_AOE_URLS", "").strip())
        if value.strip()
    ]
    return {
        "local_name": os.getenv("AOE_CLUSTER_NAME", "").strip() or "cluster",
        "local_aoe_url": local_url,
        "default_peer_url": peer_urls[0] if peer_urls else "",
        "peers": [
            {"name": f"peer-{index + 1}", "url": url}
            for index, url in enumerate(peer_urls)
        ],
        "default_timeout_seconds": int(os.getenv("AOE_DEFAULT_TIMEOUT_SECONDS", "60")),
    }


def load_aoe_config(path: Path | str = AOE_CLUSTER_CONFIG_PATH) -> Dict[str, Any]:
    """Load config with file > environment > built-in defaults precedence."""
    config = default_aoe_config()
    config_path = Path(path)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as stream:
            file_config = json.load(stream) or {}
        config.update(file_config)

    # An empty file value is considered unset and must fall back to the env/default.
    config["local_name"] = str(config.get("local_name") or "").strip() or default_aoe_config()["local_name"]
    return config


def get_local_cluster_name(path: Path | str = AOE_CLUSTER_CONFIG_PATH) -> str:
    return load_aoe_config(path)["local_name"]
