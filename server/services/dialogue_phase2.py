"""第 2 期编排：钩子轮换、话题债、关系阶段、主动聊门槛（C2/C3/C6）。"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DENY_HOOKS_WINDOW = int(os.getenv("DENY_HOOKS_WINDOW", "15"))
OPEN_THREADS_MAX = int(os.getenv("OPEN_THREADS_MAX", "5"))

_HOOK_TYPES = ("question", "unfinished_emotion", "small_promise", "memory_callback")


def resolve_relation_stage(turns: int, affection: float) -> str:
    """C6：turns + affection → 关系阶段。"""
    aff = float(affection or 0)
    t = int(turns or 0)
    if t <= 1 or aff < 5:
        return "stranger"
    if aff < 25 or t < 15:
        return "familiar"
    if aff < 55 or t < 40:
        return "ambiguous"
    return "intimate"


def stage_instruction(stage: str, language: str = "zh") -> str:
    stage = stage or "stranger"
    zh = {
        "stranger": "【关系阶段：陌生人】少肉麻、先礼貌试探，不要默认情侣口吻。",
        "familiar": "【关系阶段：熟悉】可以玩笑与关心，但仍克制过度依赖与告白。",
        "ambiguous": "【关系阶段：暧昧】允许试探与小暧昧，保留未说破的张力。",
        "intimate": "【关系阶段：亲密】允许更强依赖与撒娇，但仍要留钩子、勿空洞。",
    }
    en = {
        "stranger": "[Stage: stranger] Stay polite and light; no couple tone yet.",
        "familiar": "[Stage: familiar] Friendly teasing ok; avoid heavy confession.",
        "ambiguous": "[Stage: ambiguous] Flirty tension ok; leave things unsaid.",
        "intimate": "[Stage: intimate] Warm dependence ok; still end with a hook.",
    }
    if (language or "zh").startswith("en"):
        return en.get(stage, en["stranger"])
    return zh.get(stage, zh["stranger"])


def extract_hook_candidates(creative_text: str) -> List[str]:
    """从 Inner 创意段抽出 1～2 个钩子候选。"""
    text = (creative_text or "").strip()
    if not text:
        return []
    lines = []
    for raw in re.split(r"[\n；;]+", text):
        s = raw.strip(" -•\t")
        if len(s) < 4:
            continue
        lines.append(s[:80])
        if len(lines) >= 2:
            break
    if not lines and text:
        lines = [text[:80]]
    return lines


def pick_hook(candidates: List[str], deny_hooks: List[str]) -> str:
    deny = {d.strip() for d in (deny_hooks or []) if d and str(d).strip()}
    for c in candidates or []:
        if c not in deny:
            return c
    return (candidates[0] if candidates else "") or ""


def push_deny_hook(card: Dict[str, Any], hook: str) -> Dict[str, Any]:
    out = dict(card or {})
    hooks = list(out.get("deny_hooks") or [])
    h = (hook or "").strip()
    if h:
        hooks.append(h)
    # 滑动窗口
    out["deny_hooks"] = hooks[-DENY_HOOKS_WINDOW:]
    return out


def merge_open_threads(
    card: Dict[str, Any],
    *,
    new_thread: str = "",
    resolve_contains: str = "",
) -> Dict[str, Any]:
    out = dict(card or {})
    threads = [str(t).strip() for t in (out.get("open_threads") or []) if str(t).strip()]
    if resolve_contains:
        key = resolve_contains.strip()
        threads = [t for t in threads if key not in t]
    nt = (new_thread or "").strip()
    if nt and nt not in threads:
        threads.append(nt)
    out["open_threads"] = threads[-OPEN_THREADS_MAX:]
    return out


def proactive_should_send(
    card: Dict[str, Any],
    *,
    affection: float,
    last_user_ts: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """C3：主动消息信息量门槛；至少一条理由才发。"""
    now = now or datetime.now(timezone.utc)
    c = card or {}
    threads = [t for t in (c.get("open_threads") or []) if str(t).strip()]
    if threads:
        return True, "open_thread"
    # 时段：早 7–9 / 午 12–13 / 晚 20–23 更适合关心
    hour = now.hour
    in_window = hour in range(7, 10) or hour in range(12, 14) or hour in range(20, 24)
    if last_user_ts is not None:
        try:
            if last_user_ts.tzinfo is None:
                last_user_ts = last_user_ts.replace(tzinfo=timezone.utc)
            idle_s = (now - last_user_ts).total_seconds()
        except Exception:
            idle_s = 9999
        # 刚聊完 < 3 分钟不发
        if idle_s < 180:
            return False, "too_soon"
        if idle_s >= 600 and in_window:
            return True, "idle_window"
    aff = float(affection or 0)
    if aff >= 20 and in_window:
        return True, "affection_window"
    # 禁止无信息增量纯「在干嘛」
    return False, "no_signal"


def anti_repeat_hint(recent_assistant: List[str], max_items: int = 3) -> str:
    """近 N 条 assistant 摘要，供 Respond 勿重复。"""
    items = [str(x).strip()[:60] for x in (recent_assistant or []) if str(x).strip()]
    items = items[-max_items:]
    if not items:
        return ""
    return "【勿重复】近期已说过：" + " / ".join(items)
