import os
import threading
import time
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings

from core.chroma_client import get_persistent_client
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from openai import OpenAI
from sqlalchemy import desc

from core.database import (
    FactORM,
    RelationSummaryORM,
    ShortTermMessageORM,
    get_db,
)
from core.chat_cache import (
    append_message as redis_append_message,
    bridge_enabled,
    clear as redis_clear_messages,
    get_last_assistant_content as redis_get_last_assistant,
    get_recent as redis_get_recent,
    get_total_count as redis_get_total_count,
    warm_from_db as redis_warm_from_db,
)

logger = logging.getLogger(__name__)


def normalize_message_text_for_dedup(s: str) -> str:
    """判断两条消息是否应视为同一条展示内容（忽略首尾空白与连续空白差异）。"""
    return " ".join((s or "").strip().split())


# ===== ChromaDB ONNX Model Paths =====
_CHROMA_MODEL_DIR = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
_MODEL_URL = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
_FALLBACK_TOTAL_SIZE = 79_300_000  # ~79.3MB

# ===== Embedding Download Status =====
_embedding_status = {
    "state": "idle",      # idle, downloading, ready, error
    "progress": 0.0,      # 0~100
    "message": "",
}


def get_embedding_status() -> dict:
    """获取当前 Embedding 模型下载状态"""
    return _embedding_status.copy()


def _get_total_size() -> int:
    """通过 HEAD 请求获取模型文件总大小"""
    try:
        import httpx
        resp = httpx.head(_MODEL_URL, follow_redirects=True)
        return int(resp.headers.get("content-length", _FALLBACK_TOTAL_SIZE))
    except Exception:
        return _FALLBACK_TOTAL_SIZE


def _monitor_chroma_download():
    """后台线程：监控 ChromaDB ONNX 模型下载进度"""
    global _embedding_status

    # 如果已配置 OpenAI，标记为 ready
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        _embedding_status.update({
            "state": "ready",
            "progress": 100.0,
            "message": "使用 OpenAI Embedding，无需本地模型",
        })
        return

    total_size = _get_total_size()
    extracted_dir = _CHROMA_MODEL_DIR / "onnx"

    # 检查是否已就绪
    if extracted_dir.exists() and (extracted_dir / "model.onnx").exists():
        _embedding_status.update({
            "state": "ready",
            "progress": 100.0,
            "message": "本地 Embedding 模型已就绪",
        })
        return

    _embedding_status.update({
        "state": "downloading",
        "progress": 0.0,
        "message": "正在准备下载...",
    })

    while True:
        # 检查是否已就绪（可能由另一个线程/进程完成）
        if extracted_dir.exists() and (extracted_dir / "model.onnx").exists():
            _embedding_status.update({
                "state": "ready",
                "progress": 100.0,
                "message": "本地 Embedding 模型已就绪",
            })
            return

        tar_path = _CHROMA_MODEL_DIR / "onnx.tar.gz"
        if tar_path.exists():
            downloaded = tar_path.stat().st_size
            pct = min(100.0, downloaded / total_size * 100) if total_size > 0 else 0
            _embedding_status.update({
                "state": "downloading",
                "progress": round(pct, 1),
                "message": f"正在下载 ONNX 模型... {pct:.1f}%",
            })
        else:
            _embedding_status.update({
                "state": "downloading",
                "progress": 0.0,
                "message": "正在开始下载...",
            })

        time.sleep(1)


def start_embedding_download():
    """启动后台线程：触发模型下载并监控进度"""
    if _embedding_status["state"] == "idle":
        # 启动监控线程
        threading.Thread(target=_monitor_chroma_download, daemon=True).start()
        # 同时触发 DefaultEmbeddingFunction 初始化（实际执行下载）
        def _trigger():
            try:
                ef = DefaultEmbeddingFunction()
                ef(["hello"])  # 调用一次以触发懒加载下载
            except Exception as e:
                logger.warning("Embedding warmup trigger failed: %s", e)
        threading.Thread(target=_trigger, daemon=True).start()


# ===== Embedding =====
_OPENAI_CLIENT = None
_DEFAULT_EF = None


def get_openai_client() -> OpenAI:
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("请设置环境变量 OPENAI_API_KEY 以使用三方 Embedding")
        _OPENAI_CLIENT = OpenAI(api_key=api_key)
    return _OPENAI_CLIENT


def get_default_ef():
    global _DEFAULT_EF
    if _DEFAULT_EF is None:
        _DEFAULT_EF = DefaultEmbeddingFunction()
    return _DEFAULT_EF


def get_embedding(text: str) -> Optional[List[float]]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        try:
            client = get_openai_client()
            resp = client.embeddings.create(
                model="text-embedding-3-small",
                input=text,
            )
            return resp.data[0].embedding
        except Exception:
            return None
    # 未配置 OpenAI Key 时，尝试 ChromaDB 本地 Embedding
    try:
        ef = get_default_ef()
        return ef([text])[0]
    except Exception:
        return None


# ===== ShortTermMemory (Redis 热路径 + PostgreSQL 异步持久化) =====
# 每轮回复注入近聊语料上限（消息级）；实际不足则全量，超过则截最近 N 条
MEMORY_RECENT_MESSAGES = max(1, int(os.getenv("MEMORY_RECENT_MESSAGES", "30") or 30))
_RING_BUFFER_SIZE = max(60, MEMORY_RECENT_MESSAGES)
_ring_buffers: Dict[str, List[Dict]] = {}
_ring_buffer_lock = threading.Lock()


class ShortTermMemory:
    """短期记忆：Redis 桥接优先，PostgreSQL 异步落库；无 Redis 时同步写 PG。
    传入 user_id 时按用户隔离读写（REQ-A2）。"""

    def __init__(self, companion_id: str, user_id: Optional[int] = None):
        self.companion_id = companion_id
        self.user_id = user_id

    def _buf_key(self) -> str:
        if self.user_id is not None:
            return f"{self.companion_id}:{self.user_id}"
        return self.companion_id

    def _msg_filter(self, q):
        q = q.filter(ShortTermMessageORM.companion_id == self.companion_id)
        if self.user_id is not None:
            q = q.filter(ShortTermMessageORM.user_id == self.user_id)
        return q

    def _append_buffer(self, role: str, content: str, ts: Optional[datetime] = None, msg_id: Optional[int] = None):
        ts = ts or datetime.now(timezone.utc)
        entry = {
            "id": msg_id,
            "role": role,
            "content": content,
            "timestamp": ts.isoformat() if ts.tzinfo else ts.replace(tzinfo=timezone.utc).isoformat(),
            "user_id": self.user_id,
        }
        key = self._buf_key()
        with _ring_buffer_lock:
            buf = _ring_buffers.setdefault(key, [])
            buf.append(entry)
            if len(buf) > _RING_BUFFER_SIZE:
                del buf[: len(buf) - _RING_BUFFER_SIZE]

    def _pg_add(self, role: str, content: str):
        with get_db() as db:
            row = ShortTermMessageORM(
                companion_id=self.companion_id,
                user_id=self.user_id,
                role=role,
                content=content,
            )
            db.add(row)
            db.flush()
            return row.created_at, row.id

    def add(self, role: str, content: str):
        if bridge_enabled():
            try:
                entry = redis_append_message(
                    self.companion_id, role, content, user_id=self.user_id
                )
                ts = None
                try:
                    ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                except (TypeError, ValueError, KeyError):
                    ts = datetime.now(timezone.utc)
                # Redis 条目 id 常为 None（未 flush）；ring 用 temp_id 便于前端去重
                self._append_buffer(
                    role,
                    content,
                    ts,
                    entry.get("id") or entry.get("temp_id"),
                )
                return
            except Exception as e:
                logger.warning(
                    "Redis chat bridge failed for %s, fallback to PG: %s",
                    self.companion_id,
                    e,
                )

        created, msg_id = self._pg_add(role, content)
        self._append_buffer(role, content, created, msg_id)

    def get_last_assistant_content(self) -> Optional[str]:
        if bridge_enabled():
            cached = redis_get_last_assistant(self.companion_id, user_id=self.user_id)
            if cached:
                return cached

        key = self._buf_key()
        with _ring_buffer_lock:
            buf = _ring_buffers.get(key, [])
            for item in reversed(buf):
                if item.get("role") == "assistant" and (item.get("content") or "").strip():
                    return item["content"]

        with get_db() as db:
            row = (
                self._msg_filter(db.query(ShortTermMessageORM))
                .filter(ShortTermMessageORM.role == "assistant")
                .order_by(desc(ShortTermMessageORM.id))
                .limit(1)
                .first()
            )
            if not row or not row.content:
                return None
            return row.content

    def _pg_get_recent(self, n: int, offset: int) -> List[Dict]:
        with get_db() as db:
            msgs = (
                self._msg_filter(db.query(ShortTermMessageORM))
                .order_by(desc(ShortTermMessageORM.id))
                .offset(offset)
                .limit(n)
                .all()
            )

            def _fmt_ts(ts):
                if not ts:
                    return datetime.now(timezone.utc).isoformat()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts.isoformat()

            return [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "timestamp": _fmt_ts(m.created_at),
                    "user_id": m.user_id,
                }
                for m in reversed(msgs)
            ]

    @staticmethod
    def _msg_ts_key(m: Dict) -> str:
        return str((m or {}).get("timestamp") or "")

    def _ring_snapshot(self) -> List[Dict]:
        key = self._buf_key()
        with _ring_buffer_lock:
            buf = _ring_buffers.get(key, [])
            return list(buf) if buf else []

    def _prefer_fresher_rows(
        self, redis_rows: Optional[List[Dict]], ring_rows: List[Dict], n: int
    ) -> List[Dict]:
        """Redis 与进程内 ring 取更新的一侧，避免二次打开读到落后热缓存。"""
        candidates: List[List[Dict]] = []
        if redis_rows:
            candidates.append(redis_rows)
        if ring_rows:
            candidates.append(ring_rows)
        if not candidates:
            return []
        best = max(
            candidates,
            key=lambda rows: (self._msg_ts_key(rows[-1]), len(rows)),
        )
        return best[-n:] if len(best) > n else list(best)

    def _heal_short_from_pg(self, hot: List[Dict], n: int, offset: int) -> List[Dict]:
        """热缓存不足 n 条时与 PG 比对；PG 更新则回填 Redis。"""
        if offset != 0:
            return hot
        pg_rows = self._pg_get_recent(n, offset)
        if not pg_rows:
            return hot
        if not hot or len(pg_rows) > len(hot) or (
            len(pg_rows) >= len(hot)
            and self._msg_ts_key(pg_rows[-1]) > self._msg_ts_key(hot[-1])
        ):
            from core.chat_cache import clear as redis_clear_room

            try:
                redis_clear_room(self.companion_id, user_id=self.user_id)
            except Exception:
                pass
            try:
                redis_warm_from_db(self.companion_id, pg_rows, user_id=self.user_id)
            except Exception:
                logger.debug("redis warm after heal failed", exc_info=True)
            key = self._buf_key()
            with _ring_buffer_lock:
                _ring_buffers[key] = pg_rows[-_RING_BUFFER_SIZE:]
            return pg_rows
        return hot

    def get_recent(self, n: int = MEMORY_RECENT_MESSAGES, offset: int = 0) -> List[Dict]:
        if offset == 0:
            redis_rows: Optional[List[Dict]] = None
            if bridge_enabled():
                redis_rows = redis_get_recent(
                    self.companion_id, n, offset, user_id=self.user_id
                )
            ring_rows = self._ring_snapshot()

            # Redis 客户端不可用且 ring 空 → 直接 PG
            if redis_rows is None and not ring_rows:
                return self._pg_get_recent(n, offset)

            hot = self._prefer_fresher_rows(redis_rows, ring_rows, n)

            # 热数据不足窗口：与 PG 对齐（避免残缺 Redis 挡住最新）
            if len(hot) < n:
                hot = self._heal_short_from_pg(hot, n, offset)

            if hot:
                key = self._buf_key()
                with _ring_buffer_lock:
                    _ring_buffers[key] = hot[-_RING_BUFFER_SIZE:]
                return hot

            return self._pg_get_recent(n, offset)

        return self._pg_get_recent(n, offset)

    def get_after(self, after_ts: str, limit: int = 50) -> List[Dict]:
        """返回时间戳严格晚于 after_ts 的消息（升序），用于前端增量同步。"""
        after_ts = (after_ts or "").strip()
        if not after_ts:
            return self.get_recent(limit, 0)

        # 热路径：先看近窗；不足再放宽到 ring 上限
        window = max(limit * 4, min(_RING_BUFFER_SIZE, 120))
        recent = self.get_recent(window, 0)
        newer = [m for m in recent if self._msg_ts_key(m) > after_ts]
        if len(newer) >= limit or len(recent) < window:
            return newer[-limit:] if len(newer) > limit else newer

        # 近窗全是新消息：可能还有更早缺口，但增量场景只需「比本地 tip 更新」
        return newer[-limit:] if len(newer) > limit else newer

    def warm_buffer(self):
        key = self._buf_key()
        if bridge_enabled():
            cached = redis_get_recent(
                self.companion_id, _RING_BUFFER_SIZE, 0, user_id=self.user_id
            )
            if cached:
                with _ring_buffer_lock:
                    _ring_buffers[key] = cached[-_RING_BUFFER_SIZE:]
                return

        with _ring_buffer_lock:
            if _ring_buffers.get(key):
                return
        recent = self.get_recent(_RING_BUFFER_SIZE, offset=0)
        if not recent:
            return
        if bridge_enabled():
            redis_warm_from_db(self.companion_id, recent, user_id=self.user_id)
        with _ring_buffer_lock:
            _ring_buffers[key] = recent[-_RING_BUFFER_SIZE:]

    def get_recent_turns(self, max_turns: int = 20) -> List[Dict]:
        """按轮次获取最近对话，合并同一轮的拆分消息"""
        recent = self.get_recent(n=max_turns * 6)  # 每轮最多6条拆分消息
        if not recent:
            return []

        turns = []
        current_role = None
        current_parts = []

        for msg in recent:
            if msg["role"] != current_role:
                if current_role:
                    turns.append({
                        "role": current_role,
                        "content": "\n".join(current_parts),
                    })
                current_role = msg["role"]
                current_parts = [msg["content"]]
            else:
                current_parts.append(msg["content"])

        if current_role:
            turns.append({
                "role": current_role,
                "content": "\n".join(current_parts),
            })

        # 限制轮次数
        if len(turns) > max_turns:
            turns = turns[-max_turns:]

        return turns

    def get_turn_count(self) -> int:
        with get_db() as db:
            count = self._msg_filter(db.query(ShortTermMessageORM)).count()
            return count // 2

    def get_total_count(self) -> int:
        if bridge_enabled():
            cached = redis_get_total_count(self.companion_id, user_id=self.user_id)
            if cached is not None and cached > 0:
                return cached
        with get_db() as db:
            return self._msg_filter(db.query(ShortTermMessageORM)).count()

    def clear(self):
        if bridge_enabled():
            redis_clear_messages(self.companion_id, user_id=self.user_id)
        with _ring_buffer_lock:
            _ring_buffers.pop(self._buf_key(), None)
        with get_db() as db:
            q = db.query(ShortTermMessageORM).filter(
                ShortTermMessageORM.companion_id == self.companion_id
            )
            if self.user_id is not None:
                q = q.filter(ShortTermMessageORM.user_id == self.user_id)
            q.delete(synchronize_session=False)


# ===== EpisodicMemory (Chroma 向量) =====
class EpisodicMemory:
    """向量情节记忆：Chroma 存储，按智能体隔离 collection"""

    def __init__(self, companion_id: str, companion_dir: str):
        self.companion_id = companion_id
        self.persist_dir = os.path.join(companion_dir, "chroma")
        os.makedirs(self.persist_dir, exist_ok=True)
        self.client = get_persistent_client(self.persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=f"companion_{companion_id}",
            metadata={"hnsw:space": "cosine"},
        )

    def add_episode(self, text: str, metadata: Optional[Dict] = None):
        embedding = get_embedding(text)
        if embedding is None:
            return
        doc_id = str(uuid.uuid4())
        meta = metadata or {}
        meta["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.collection.add(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[meta],
        )

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        embedding = get_embedding(query)
        if embedding is None:
            return []
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        items = []
        for i in range(len(results["documents"][0])):
            items.append({
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i],
            })
        return items

    def add_turn(
        self,
        user_text: str,
        assistant_text: str,
        pronoun: str = "TA",
        partial: bool = False,
    ):
        combined = f"用户说：{user_text}\n{pronoun}回复：{assistant_text}"
        self.add_episode(
            combined,
            {"type": "dialogue_turn", "partial": bool(partial), "pronoun": pronoun},
        )

# ===== FactMemory (PostgreSQL) =====
class FactMemory:
    """结构化事实记忆：PostgreSQL 存储"""

    def __init__(self, companion_id: str):
        self.companion_id = companion_id

    def add_facts(self, new_facts: List[str]):
        with get_db() as db:
            existing = {
                f.fact for f in db.query(FactORM).filter(
                    FactORM.companion_id == self.companion_id
                ).all()
            }
            for fact in new_facts:
                fact = fact.strip()
                if fact and fact not in existing:
                    db.add(FactORM(companion_id=self.companion_id, fact=fact))
                    existing.add(fact)

    def get_facts(self) -> List[str]:
        with get_db() as db:
            rows = (
                db.query(FactORM)
                .filter(FactORM.companion_id == self.companion_id)
                .order_by(FactORM.id)
                .all()
            )
            return [r.fact for r in rows]

    def to_text(self, max_items: int = 20) -> str:
        facts = self.get_facts()
        items = facts[-max_items:] if len(facts) > max_items else facts
        return "\n".join(f"- {item}" for item in items) if items else "（暂无已知事实）"


# ===== RelationSummary (PostgreSQL) =====
class RelationSummary:
    """关系摘要：每 8 轮自动更新一句温馨摘要"""

    def __init__(self, companion_id: str):
        self.companion_id = companion_id

    def _get_row(self) -> dict:
        with get_db() as db:
            row = (
                db.query(RelationSummaryORM)
                .filter(RelationSummaryORM.companion_id == self.companion_id)
                .first()
            )
            if not row:
                row = RelationSummaryORM(companion_id=self.companion_id)
                db.add(row)
                db.commit()
                db.refresh(row)
            # 在 session 内提取值，避免 detached instance 错误
            return {
                "summary": row.summary,
                "turns_since_update": row.turns_since_update,
            }

    def should_update(self) -> bool:
        data = self._get_row()
        return (data.get("turns_since_update") or 0) >= 8

    def update(self, new_summary: str):
        with get_db() as db:
            row = (
                db.query(RelationSummaryORM)
                .filter(RelationSummaryORM.companion_id == self.companion_id)
                .first()
            )
            if row:
                row.summary = new_summary.strip()
                row.turns_since_update = 0
            else:
                db.add(RelationSummaryORM(
                    companion_id=self.companion_id,
                    summary=new_summary.strip(),
                    turns_since_update=0,
                ))

    def increment_turn(self):
        with get_db() as db:
            row = (
                db.query(RelationSummaryORM)
                .filter(RelationSummaryORM.companion_id == self.companion_id)
                .first()
            )
            if row:
                row.turns_since_update = (row.turns_since_update or 0) + 1
            else:
                db.add(RelationSummaryORM(
                    companion_id=self.companion_id,
                    turns_since_update=1,
                ))

    def get_summary(self) -> str:
        data = self._get_row()
        return data.get("summary") or "（你们的关系正在萌芽，每一句对话都让她更靠近你。）"


# ===== CompanionMemory =====
class CompanionMemory:
    """聚合所有记忆层，对外统一接口"""

    def __init__(self, companion_id: str, companion_dir: str):
        self.companion_id = companion_id
        self.companion_dir = companion_dir
        self.short_term = ShortTermMemory(companion_id)
        self.episodic = EpisodicMemory(companion_id, companion_dir)
        self.facts = FactMemory(companion_id)  # 本版仍 companion 级；私密隔离属后续 ADR
        self.summary = RelationSummary(companion_id)

    def bind_user(self, user_id: Optional[int]) -> None:
        """将短期记忆绑定到会话用户（WS 必调）。"""
        current = getattr(self.short_term, "user_id", None)
        if current == user_id:
            return
        self.short_term = ShortTermMemory(self.companion_id, user_id=user_id)

    def add_user_message(self, content: str):
        self.short_term.add("user", content)

    def add_assistant_message(self, content: str):
        if not (content or "").strip():
            return
        last = self.short_term.get_last_assistant_content()
        if last is not None and normalize_message_text_for_dedup(
            content
        ) == normalize_message_text_for_dedup(last):
            return
        self.short_term.add("assistant", content)

    def commit_turn(
        self,
        user_text: str,
        assistant_text: str,
        pronoun: str = "TA",
        partial: bool = False,
    ):
        self.episodic.add_turn(
            user_text, assistant_text, pronoun=pronoun, partial=partial
        )
        self.summary.increment_turn()

    def get_context(self, query: str = "", user_id: Optional[int] = None) -> Dict[str, Any]:
        short = self.short_term
        if user_id is not None and short.user_id != user_id:
            short = ShortTermMemory(self.companion_id, user_id=user_id)
        recent = short.get_recent(MEMORY_RECENT_MESSAGES)
        episodes = []
        if query:
            episodes = self.episodic.search(query, top_k=5)
        return {
            "recent_dialogue": recent,
            "episodes": episodes,
            "facts": self.facts.get_facts(),
            "summary": self.summary.get_summary(),
            "_short_term": short,
        }

    def build_prompt_context(
        self,
        query: str = "",
        max_chars: int = 3500,
        tier: str = "full",
        user_id: Optional[int] = None,
        relation_card_text: str = "",
        truncate_episodes: bool = False,
    ) -> str:
        """构建上下文提示。user_id 传入时按该用户读近聊（REQ-A2）。

        规则：每个对话发送下一条前，读取近聊作为【最近对话】语料：
        - 不足 MEMORY_RECENT_MESSAGES（默认 30）条 → 以实际条数为准
        - 超过则只取最近 MEMORY_RECENT_MESSAGES 条
        关系卡/事实不得挤掉该窗口。
        """
        tier = (tier or "full").lower()
        recent_n = MEMORY_RECENT_MESSAGES
        if tier == "compact":
            max_chars = min(max_chars, 800)
            max_facts = 3
            recent_n = min(16, MEMORY_RECENT_MESSAGES)
            episode_limit = 2
            dialogue_reserve = min(500, max_chars // 2)
        else:
            # 理解优先：近聊窗口留足预算，避免关系卡挤掉语料
            full_cap = int(os.getenv("MEMORY_FULL_MAX_CHARS", "6500") or 6500)
            max_chars = min(max_chars, full_cap) if max_chars > 2000 else max_chars
            if max_chars < 4000:
                max_chars = min(full_cap, max(max_chars, 5000))
            max_facts = 6
            episode_limit = 4
            dialogue_reserve = int(os.getenv("MEMORY_DIALOGUE_RESERVE", "4500") or 4500)

        if relation_card_text and truncate_episodes:
            episode_limit = min(episode_limit, 3)

        need_vector = bool(query) and (
            len(query) > 6
            or any(
                k in query
                for k in (
                    "记得",
                    "以前",
                    "那次",
                    "还记得",
                    "上次",
                    "那天",
                    "那个",
                    "他说",
                    "她说",
                    "刚才",
                    "remember",
                    "before",
                    "last time",
                    "that",
                )
            )
        )
        # 有关系卡时仍允许语义检索；勿因 truncate_episodes 直接关闭往事

        ctx = self.get_context(query if need_vector else "", user_id=user_id)
        short = ctx.pop("_short_term", self.short_term)
        other_parts = []

        if relation_card_text:
            # 关系卡可截断；近聊优先
            card = relation_card_text.strip()
            card_cap = int(os.getenv("RELATION_CARD_PROMPT_CHARS", "700"))
            if len(card) > card_cap:
                card = card[: card_cap - 1] + "…"
            other_parts.append(("【关系卡】\n" + card, 85))

        if ctx["summary"] and not relation_card_text:
            other_parts.append(("【关系摘要】\n" + ctx["summary"], 100))

        facts = ctx["facts"]
        if facts:
            seen = set()
            unique_facts = []
            fact_cap = max_facts if not relation_card_text else min(max_facts, 4)
            for f in reversed(facts):
                f_norm = f.strip()
                if f_norm and f_norm not in seen:
                    seen.add(f_norm)
                    unique_facts.append(f_norm)
                    if len(unique_facts) >= fact_cap:
                        break
            unique_facts.reverse()
            if unique_facts:
                facts_text = "【关于他的已知事实】\n" + "\n".join(f"- {f}" for f in unique_facts)
                other_parts.append((facts_text, 80))

        episodes = ctx["episodes"]
        if episodes:
            filtered = [ep for ep in episodes if ep.get("distance", 1.0) < 0.45]
            if filtered:
                ep_lines = []
                for ep in filtered[:episode_limit]:
                    text = ep["text"][:160]
                    ep_lines.append(f"- {text}")
                ep_text = "【相关往事】\n" + "\n".join(ep_lines)
                other_parts.append((ep_text, 60))

        dialogue_text = ""
        # 近聊语料：不足 recent_n 条用实际数量；超过则只取最近 recent_n 条
        recent_msgs = list(short.get_recent(recent_n) or [])[-recent_n:]
        if recent_msgs:
            dialogue_lines = ["【最近对话】"]
            for msg in recent_msgs:
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                who = "他" if msg.get("role") == "user" else "你"
                dialogue_lines.append(f"{who}：{content}")
            if len(dialogue_lines) > 1:
                dialogue_text = "\n".join(dialogue_lines)

        # 先装近聊（理解预算），再装关系卡/事实
        result_parts = []
        total_len = 0
        dlg_budget = min(dialogue_reserve, max_chars) if dialogue_text else 0
        if dialogue_text:
            if len(dialogue_text) <= dlg_budget:
                result_parts.append(dialogue_text)
                total_len += len(dialogue_text)
            else:
                lines = dialogue_text.split("\n")
                kept = [lines[0]]
                kept_len = len(lines[0])
                for line in reversed(lines[1:]):
                    if kept_len + len(line) + 1 <= dlg_budget:
                        kept.insert(1, line)
                        kept_len += len(line) + 1
                    else:
                        break
                if len(kept) > 1:
                    trimmed = "\n".join(kept)
                    result_parts.append(trimmed)
                    total_len += len(trimmed)

        other_budget = max_chars - total_len
        for text, priority in sorted(other_parts, key=lambda x: -x[1]):
            if total_len + len(text) <= max_chars:
                result_parts.append(text)
                total_len += len(text)
                continue
            remaining = max_chars - total_len - 20
            if remaining > 120 and priority >= 80:
                result_parts.append(text[: remaining - 1] + "…")
                total_len = max_chars
                break

        # 近聊放最后，便于模型注意（但已保证装入）
        if dialogue_text and result_parts and result_parts[0].startswith("【最近对话】"):
            dlg = result_parts.pop(0)
            result_parts.append(dlg)

        return "\n\n".join(result_parts)
