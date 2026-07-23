"""对话质量补充层（REQ-Q*）：从单智能体失败对话反推的通用规则。

标准优化思路（可复用到任意 companion，禁止写死 id）：
1. 证据优先：拉 short_term_messages + relation_card（deny/stage/facts/aff）
2. 失效分类：字面复读 / 主题复读 / 骨架同构 / 离开失控 / 黄腔逃题 / 语义滑移 / 记忆污染 / 阶段卡死
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
    is_anti_question_user,
    is_escape_safe_reply,
    is_user_tease,
    ends_with_question,
    leave_close_violations,
    list_script_skeletons,
    opener_spam_hits,
    question_spam_hits,
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
    affection: float = 0,
    language: str = "zh",
) -> Dict[str, str]:
    """组装 Respond 侧质量提示（规则块 + 勿复读）。"""
    from services.dialogue_i18n import quality_ui

    q = quality_ui(language)
    recent = [str(x) for x in (recent_assistant or []) if str(x).strip()]
    deny = seed_deny_from_recent(list(deny_hooks or []), recent)
    return {
        "rules_block": chat_rules_block(
            has_leave_intent=has_leave_intent,
            user_input=user_input or "",
            affection=affection,
            recent_assistant=recent,
            language=language,
        ),
        "repeat_hint": anti_repeat_hint(recent, deny_hooks=deny, language=language),
        "leave_hint": (q["leave_hint"] if has_leave_intent else ""),
        "reply_shape": (q["shape_leave"] if has_leave_intent else q["shape_normal"]),
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
    from services.eval_probes import probe_leitmotif_repeat, probe_near_duplicate

    recent = [str(x) for x in (recent_assistant or []) if str(x).strip()]
    theme_hits = theme_repeat_hits(reply, recent)
    skel_hits = skeleton_repeat_hits(reply, recent)
    opener_hits = opener_spam_hits(reply, recent)
    q_hits = question_spam_hits(reply, recent)
    leave_bad = leave_close_violations(reply) if has_leave_intent else []
    user_tease = is_user_tease(user_input or "")
    escape_safe = is_escape_safe_reply(reply)
    anti_q = is_anti_question_user(user_input or "")
    bad_question_tail = anti_q and ends_with_question(reply)
    # 用户要行动建议时仍回「在干嘛」→ 语义逃题
    action_ask = bool(
        re.search(
            r"(怎么做|怎么办|要怎样|你要怎么|what should i do|how (do|can) i|どうすれば|어떻게 해)",
            user_input or "",
            re.I,
        )
    )
    action_miss = action_ask and bool(
        re.search(r"(在干嘛|猜猜|爱吃啥|what are you doing|guess)", reply or "", re.I)
    )
    thr = float(
        os.getenv("REPEAT_REWRITE_THRESHOLD", str(near_dup_threshold)) or near_dup_threshold
    )
    near = bool(recent) and any(
        probe_near_duplicate(reply, prev, thr) for prev in recent[-4:]
    )
    # 换皮复读：Jaccard 常漏检「这些小事/说出来」类主旋律
    leitmotif = bool(recent) and probe_leitmotif_repeat(reply, recent)
    need_rewrite = (
        near
        or leitmotif
        or bool(theme_hits)
        or bool(skel_hits)
        or bool(opener_hits)
        or bool(q_hits)
        or bool(leave_bad)
        or (user_tease and escape_safe)
        or bad_question_tail
        or action_miss
    )
    ban_bits = (
        list(theme_hits)
        + list(skel_hits)
        + list(opener_hits)
        + list(q_hits)
        + list(leave_bad)
    )
    if user_tease and escape_safe and "黄腔逃题" not in ban_bits:
        ban_bits.append("黄腔逃题")
    if bad_question_tail:
        ban_bits.append("反感提问仍问句收尾")
    if action_miss:
        ban_bits.append("该给行动却问在干嘛")
    if leitmotif and "换皮复读" not in ban_bits:
        ban_bits.append("换皮复读")
    return {
        "need_rewrite": need_rewrite,
        "near_duplicate": near or leitmotif,
        "theme_hits": theme_hits,
        "skeleton_hits": skel_hits,
        "opener_hits": opener_hits,
        "question_hits": q_hits,
        "leave_violations": leave_bad,
        "user_tease": user_tease,
        "escape_safe": escape_safe,
        "anti_question": anti_q,
        "action_miss": action_miss,
        "ban_summary": "、".join(ban_bits) if ban_bits else "近几轮同款/安全逃题",
        "skeletons_in_reply": list_script_skeletons(reply),
    }


def rewrite_instruction(
    ban_summary: str,
    *,
    final_pass: bool = False,
    affection: float = 0,
    language: str = "zh",
) -> str:
    from services.dialogue_i18n import quality_ui

    q = quality_ui(language)
    aff = float(affection or 0)
    adult = q["rewrite_adult_high"] if aff >= 50 else q["rewrite_adult"]
    extra = q["rewrite_extra"]
    if final_pass:
        return q["rewrite_final"].format(adult=adult, extra=extra)
    return q["rewrite_hard"].format(ban=ban_summary, adult=adult, extra=extra)


def enrich_human_prompt(
    base: str,
    *,
    user_input: str,
    has_leave_intent: bool = False,
    affection: float = 0,
    language: str = "zh",
) -> str:
    from services.dialogue_i18n import quality_ui

    q = quality_ui(language)
    out = base
    if has_leave_intent:
        out += q["enrich_leave"]
    if is_user_tease(user_input or ""):
        aff = float(affection or 0)
        out += q["enrich_tease_high"] if aff >= 50 else q["enrich_tease"]
    return out


def finalize_reply_for_leave(reply: str, has_leave_intent: bool) -> str:
    if not has_leave_intent:
        return (reply or "").strip()
    return force_single_bubble(reply or "")


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
        if re.search(r"[\u4e00-\u9fff]", ui) and has_ko and not has_zh:
            continue
        out.append(s)
    return out
