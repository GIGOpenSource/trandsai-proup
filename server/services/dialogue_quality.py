"""对话质量补充层（REQ-Q*）：从单智能体失败对话反推的通用规则。

标准优化思路（可复用到任意 companion，禁止写死 id）：
1. 证据优先：拉 short_term_messages + relation_card（deny/stage/facts/aff）
2. 失效分类：字面复读 / 主题复读 / 骨架同构 / 离开没收 / 黄腔逃题 / 语义滑移 / 记忆污染 / 阶段卡死
3. 软→硬阶梯：提示词 → 忌用窗 → 主题禁 → 骨架冷却 → 强制改写 → 离开硬收束
4. 状态卫生：指纹先落库；后台 merge 不得冲掉 deny/stage；事实语言门禁
5. 先答后撩：当前意图 > 人设表演；默认一条消息

本模块是 pipeline v2 / WS 的补充入口，实现细节多在 dialogue_phase2。
规格见：docs/DIALOGUE_QUALITY_SUPPLEMENT.md 与《对话系统分析与优化合一文档》B.15。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

from services.dialogue_phase2 import (
    anti_repeat_hint,
    chat_rules_block,
    force_single_bubble,
    is_escape_safe_reply,
    is_user_tease,
    leave_close_violations,
    list_script_skeletons,
    seed_deny_from_recent,
    skeleton_repeat_hits,
    theme_repeat_hits,
)


def build_respond_quality_hints(
    *,
    user_input: str,
    recent_assistant: Optional[Sequence[str]] = None,
    deny_hooks: Optional[Sequence[str]] = None,
    has_leave_intent: bool = False,
) -> Dict[str, str]:
    """组装 Respond 侧质量提示（规则块 + 勿复读）。"""
    recent = [str(x) for x in (recent_assistant or []) if str(x).strip()]
    deny = seed_deny_from_recent(list(deny_hooks or []), recent)
    return {
        "rules_block": chat_rules_block(
            has_leave_intent=has_leave_intent, user_input=user_input or ""
        ),
        "repeat_hint": anti_repeat_hint(recent, deny_hooks=deny),
        "leave_hint": (
            "【离开硬收束】用户要结束：只输出一句短晚安/短收束；"
            "禁止明天约定、诗集、还醒着吗、连环催睡；禁止拆多条。"
            if has_leave_intent
            else ""
        ),
        "reply_shape": (
            "【Reply shape】只许一条短消息，不要空行拆条。"
            if has_leave_intent
            else "【Reply shape】优先一条消息；仅当输出含空行时拆两条。长短随心情，不要凑字数。"
        ),
    }


def diagnose_reply_violations(
    reply: str,
    *,
    user_input: str = "",
    recent_assistant: Optional[Sequence[str]] = None,
    has_leave_intent: bool = False,
    near_dup_threshold: float = 0.55,
) -> Dict[str, Any]:
    """对草稿做质量诊断，供改写门控。不依赖具体 companion。"""
    from services.eval_probes import probe_near_duplicate

    recent = [str(x) for x in (recent_assistant or []) if str(x).strip()]
    theme_hits = theme_repeat_hits(reply, recent)
    skel_hits = skeleton_repeat_hits(reply, recent)
    leave_bad = leave_close_violations(reply) if has_leave_intent else []
    user_tease = is_user_tease(user_input or "")
    escape_safe = is_escape_safe_reply(reply)
    thr = float(
        os.getenv("REPEAT_REWRITE_THRESHOLD", str(near_dup_threshold)) or near_dup_threshold
    )
    near = bool(recent) and any(
        probe_near_duplicate(reply, prev, thr) for prev in recent[-4:]
    )
    need_rewrite = (
        near
        or bool(theme_hits)
        or bool(skel_hits)
        or bool(leave_bad)
        or (user_tease and escape_safe)
    )
    ban_bits = list(theme_hits) + list(skel_hits) + list(leave_bad)
    if user_tease and escape_safe and "黄腔逃题" not in ban_bits:
        ban_bits.append("黄腔逃题")
    return {
        "need_rewrite": need_rewrite,
        "near_duplicate": near,
        "theme_hits": theme_hits,
        "skeleton_hits": skel_hits,
        "leave_violations": leave_bad,
        "user_tease": user_tease,
        "escape_safe": escape_safe,
        "ban_summary": "、".join(ban_bits) if ban_bits else "近几轮同款/安全逃题",
        "skeletons_in_reply": list_script_skeletons(reply),
    }


def rewrite_instruction(ban_summary: str, *, final_pass: bool = False) -> str:
    if final_pass:
        return (
            "\n\n【最终改写】仍在复读/逃题/离开后开新钩。只用一两句："
            "接住用户原意；离开则短晚安；禁止抱枕、颜色、梦、诗集、明天约定、连环催睡。"
        )
    return (
        f"\n\n【改写·硬性】草稿违规（命中：{ban_summary}）。"
        "必须：1) 先直接回应用户这句意思；"
        "2) 禁止抱枕/颜色/梦到我/告诉我梦/催睡连环/明日约定同构；"
        "3) 黄腔：可羞但接住所指并轻回撩，勿逃题；"
        "4) 若用户要离开：一句短收束即可；"
        "5) 换全新信息点，可更短，默认一条。"
    )


def finalize_reply_for_leave(reply: str, has_leave_intent: bool) -> str:
    if not has_leave_intent:
        return (reply or "").strip()
    return force_single_bubble(reply or "")


def enrich_human_prompt(
    base: str,
    *,
    user_input: str,
    has_leave_intent: bool = False,
) -> str:
    out = base
    if has_leave_intent:
        out += "本轮是离开收束：一句即可，不要新话题。"
    if is_user_tease(user_input or ""):
        out += "对方在撩/开黄腔：接住所指再回撩，勿逃题。"
    return out


def filter_fact_language(facts: List[str], user_input: str, lang: str = "zh") -> List[str]:
    """事实语言门禁（与 agent._filter_facts_by_user_lang 对齐的轻量版）。"""
    out: List[str] = []
    ui = user_input or ""
    for f in facts or []:
        s = str(f).strip()
        if not s:
            continue
        has_ko = bool(re.search(r"[\uac00-\ud7af]", s))
        has_ja = bool(re.search(r"[\u3040-\u30ff]", s))
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", s))
        if lang == "zh" and (has_ko or (has_ja and not has_zh)):
            continue
        if re.search(r"[\u4e00-\u9fff]", ui) and has_ko:
            continue
        out.append(s)
    return out
