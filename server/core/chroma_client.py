"""Reuse Chroma clients：本地 Persistent 或远程 Http（D3）。"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Tuple

import chromadb
from chromadb.config import Settings

_lock = threading.Lock()
_clients: Dict[str, Any] = {}


def _remote_endpoint() -> Tuple[str, int, bool]:
    host = (os.getenv("CHROMA_HOST") or "").strip()
    if not host:
        return "", 0, False
    port = int(os.getenv("CHROMA_PORT", "8000") or 8000)
    ssl = os.getenv("CHROMA_SSL", "false").lower() in ("1", "true", "yes")
    return host, port, ssl


def get_persistent_client(persist_dir: str) -> Any:
    """兼容旧调用名：若配置了 CHROMA_HOST 则返回 HttpClient，否则 PersistentClient。"""
    host, port, ssl = _remote_endpoint()
    key = f"http://{host}:{port}" if host else f"file://{persist_dir}"
    with _lock:
        client = _clients.get(key)
        if client is None:
            if host:
                client = chromadb.HttpClient(
                    host=host,
                    port=port,
                    ssl=ssl,
                    settings=Settings(anonymized_telemetry=False),
                )
            else:
                client = chromadb.PersistentClient(
                    path=persist_dir,
                    settings=Settings(anonymized_telemetry=False),
                )
            _clients[key] = client
        return client


def evict_client(persist_dir: str) -> None:
    """Drop the cached client (e.g. before deleting/recreating the persist dir)."""
    host, port, _ssl = _remote_endpoint()
    key = f"http://{host}:{port}" if host else f"file://{persist_dir}"
    with _lock:
        _clients.pop(key, None)
