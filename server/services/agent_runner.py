"""Agent 统一入口：v1/v2 路由 + fallback。"""
import asyncio
import logging
import os

from services.agent import run_agent as run_pipeline_v1
from services.agent_pipeline_v2 import run_memory_update, run_pipeline_v2

logger = logging.getLogger(__name__)


def run_agent(
    user_input: str,
    profile: dict,
    companion_state: dict,
    memory_text: str,
    knowledge_text: str = "",
    language: str = "zh",
    current_time: str = "",
    user_gender: str = "",
    summary_due: bool = False,
    extract_due: bool = True,
    has_leave_intent: bool = False,
    burst_count: int = 1,
    open_threads: list = None,
    deny_hooks: list = None,
    recent_assistant: list = None,
    idle_seconds: float = None,
    last_extreme_ts: str = "",
) -> dict:
    version = os.getenv("AGENT_PIPELINE", "v1").lower()
    kwargs = dict(
        user_input=user_input,
        profile=profile,
        companion_state=companion_state,
        memory_text=memory_text,
        knowledge_text=knowledge_text,
        language=language,
        current_time=current_time,
        user_gender=user_gender,
        summary_due=summary_due,
        extract_due=extract_due,
        has_leave_intent=has_leave_intent,
        burst_count=burst_count,
        open_threads=open_threads or [],
        deny_hooks=deny_hooks or [],
        recent_assistant=recent_assistant or [],
        idle_seconds=idle_seconds,
        last_extreme_ts=last_extreme_ts or "",
    )
    if version == "v2":
        try:
            # 贯通反重复 / 离开意图 / 话题债；其余 v1 专属字段由 **_extra 忽略
            return run_pipeline_v2(**kwargs)
        except Exception as e:
            logger.warning("Pipeline v2 failed, fallback to v1: %s", e, exc_info=True)
    return run_pipeline_v1(**kwargs)



async def run_memory_update_async(snapshot: dict) -> dict:
    from core.executor import AGENT_POOL

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(AGENT_POOL, run_memory_update, snapshot)
