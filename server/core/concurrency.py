"""Global concurrency limits for Agent and LLM calls (REQ-A6)."""
import asyncio
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Dict, Iterator, Optional, Tuple

# SRS aliases + backward-compatible env names
HEAVY_PATH_SEMAPHORE = int(
    os.getenv("HEAVY_PATH_SEMAPHORE")
    or os.getenv("MAX_CONCURRENT_AGENTS", "8")
)
MAX_CONCURRENT_AGENTS = HEAVY_PATH_SEMAPHORE
agent_semaphore = asyncio.Semaphore(HEAVY_PATH_SEMAPHORE)

MAX_LLM_CONCURRENT = int(os.getenv("MAX_LLM_CONCURRENT", "50"))

# asyncio.Semaphore is not safe across threads; LLM invoke runs in thread pools.
_llm_thread_gate = threading.Semaphore(MAX_LLM_CONCURRENT)
_inflight_lock = threading.Lock()
_llm_inflight = 0

_user_locks: Dict[str, asyncio.Lock] = {}
_user_locks_guard = asyncio.Lock()


def _user_lock_key(user_id: int, companion_id: str) -> str:
    return f"{user_id}:{companion_id}"


async def get_user_lock(user_id: int, companion_id: str) -> asyncio.Lock:
    key = _user_lock_key(user_id, companion_id)
    async with _user_locks_guard:
        lock = _user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[key] = lock
        return lock


@asynccontextmanager
async def user_turn_lock(user_id: int, companion_id: str) -> AsyncIterator[None]:
    """同一 user×companion 串行一轮对话（REQ-A6）。"""
    lock = await get_user_lock(user_id, companion_id)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def heavy_slots_in_use() -> Tuple[int, int]:
    """返回 (in_use, capacity)。"""
    # asyncio.Semaphore._value is available but private; track via bound - value
    try:
        free = agent_semaphore._value  # type: ignore[attr-defined]
    except Exception:
        free = HEAVY_PATH_SEMAPHORE
    in_use = max(0, HEAVY_PATH_SEMAPHORE - int(free))
    return in_use, HEAVY_PATH_SEMAPHORE


def heavy_load_ratio() -> float:
    in_use, cap = heavy_slots_in_use()
    if cap <= 0:
        return 1.0
    return in_use / cap


@contextmanager
def llm_gate() -> Iterator[None]:
    """Thread-safe gate for all synchronous LLM invoke paths."""
    global _llm_inflight
    _llm_thread_gate.acquire()
    with _inflight_lock:
        _llm_inflight += 1
    try:
        yield
    finally:
        with _inflight_lock:
            _llm_inflight -= 1
        _llm_thread_gate.release()


def llm_gate_stats() -> dict:
    """In-flight LLM calls vs configured cap (S-19)."""
    with _inflight_lock:
        inflight = _llm_inflight
    return {"max_concurrent": MAX_LLM_CONCURRENT, "in_flight": inflight}
