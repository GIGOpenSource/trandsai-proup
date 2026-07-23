"""第 2 期编排：钩子轮换、话题债、关系阶段、主动聊门槛（C2/C3/C6）。"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

DENY_HOOKS_WINDOW = int(os.getenv("DENY_HOOKS_WINDOW", "20"))
OPEN_THREADS_MAX = int(os.getenv("OPEN_THREADS_MAX", "5"))

_HOOK_TYPES = ("question", "unfinished_emotion", "small_promise", "memory_callback")
_PAREN_STRIP = re.compile(r"\([^)]*\)|（[^）]*）")


def resolve_relation_stage(turns: int, affection: float) -> str:
    """C6：turns + affection → 关系阶段。

    兼容旧亲密度公式（约 0.01/轮）导致「聊很久仍 aff<5 → 永锁陌生人」的问题：
    轮次足够时用有效亲密度抬阶段，避免一直客服腔/梦钩复读。
    """
    aff = float(affection or 0)
    t = int(turns or 0)
    if t >= 10 and aff < 5:
        aff = max(aff, min(55.0, t * 1.1))
    if t <= 1 or (t < 5 and aff < 8):
        return "stranger"
    if aff < 22 and t < 12:
        return "familiar"
    if aff < 50 or t < 35:
        return "ambiguous"
    return "intimate"


def stage_instruction(stage: str, language: str = "zh") -> str:
    from services.dialogue_i18n import stage_text

    return stage_text(stage or "stranger", language)


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


# 主题级复读：软提示经常被模型无视，需硬拦截（中文为主，并补英/日/韩常见换皮）
_REPEAT_THEMES: Tuple[str, ...] = (
    "抱枕",
    "喜欢什么颜色",
    "什么颜色呀",
    "梦到我",
    "告诉我梦",
    "明天告诉我",
    "早点睡",
    "快睡吧",
    "快休息",
    "赶紧休息",
    "晚安呀",
    "你昨晚",
    "好心疼",
    "我会想你",
    "念给你",
    "诗集",
    "明天念",
    "在干嘛",
    "还忙吗",
    "累不累",
    "猜猜",
    "猜我",
    "你爱吃啥",
    # EN
    "pillow",
    "what color",
    "favourite colour",
    "favorite color",
    "dreamed of me",
    "tell me your dream",
    "go to sleep",
    "get some rest",
    "what are you doing",
    "are you busy",
    "tired?",
    "guess what",
    # JA / KO (常见逃题)
    "抱き枕",
    "何色",
    "早く寝",
    "何してる",
    "베개",
    "무슨 색",
    "자자",
    "뭐 해",
)

# 话术骨架：换词仍同构（约定→催确认→晚安后再约定）
_SCRIPT_SKELETONS: Tuple[Tuple[str, str], ...] = (
    ("骨架:明日约定", r"(明天|明早|今晚|tomorrow|tmr).{0,16}(告诉|念|说|报|听|tell|read|promise)"),
    ("骨架:催睡收尾", r"(快|赶紧|早点|go\s*to|get\s*some).{0,10}(睡|休息|sleep|rest)|睡好哦|乖乖睡"),
    ("骨架:报梦软着陆", r"梦到(我|什么)|告诉我梦|dream(ed)?\s*(of\s*)?me|tell me (your )?dream"),
    ("骨架:心疼催睡", r"(心疼|宝贝|babe|honey).{0,24}(休息|睡|加班|rest|sleep)"),
    ("骨架:安全逃题", r"抱枕|喜欢什么颜色|pillow|what co[lu]r|抱き枕|베개"),
    ("骨架:连环在干嘛", r"(在干嘛|今晚在干嘛|现在在干嘛|你在干嘛|what are you doing|whatcha doing|何してる|뭐 해)"),
    ("骨架:猜谜追问", r"(猜猜|猜我|玩猜|猜你|guess what|guess)"),
    ("骨架:嘻嘻开场", r"^(嘻嘻|hehe|ㅎㅎ)"),
)

_ANTI_QUESTION_USER = re.compile(
    r"(一直问|别的问题|怎么这么多问题|为什么这么多问题|能不能不好奇|不要问|别问|不猜|少问|太多问题|懒得理|"
    r"stop asking|too many questions|don'?t ask|quit asking|왜 자꾸 물어|質問しすぎ|jangan tanya|deja de preguntar|para de perguntar)",
    re.I,
)


def is_anti_question_user(text: str) -> bool:
    return bool(_ANTI_QUESTION_USER.search(text or ""))


def ends_with_question(text: str) -> bool:
    s = re.sub(r"[\s❤💕😊😘😣~～。.!！]+$", "", text or "").strip()
    return bool(s) and (s.endswith("？") or s.endswith("?") or s.endswith("吗") or s.endswith("嘛"))


def count_question_marks(text: str) -> int:
    s = text or ""
    return s.count("？") + s.count("?")


def recent_question_tail_streak(recent_assistant: List[str]) -> int:
    """从最近一条往回数，连续以问句收尾的助手轮数。"""
    streak = 0
    for x in reversed(list(recent_assistant or [])):
        if ends_with_question(str(x)):
            streak += 1
        else:
            break
    return streak


def question_spam_hits(reply: str, recent_assistant: List[str], lookback: int = 4) -> List[str]:
    """陈述/问句比例门控：连发问句收尾、近窗问句过多、单条多问 → 命中。"""
    if not ends_with_question(reply):
        # 单条内部堆多个问号也算问句刷屏
        if count_question_marks(reply) >= 2:
            return ["单条多问句"]
        return []
    hits: List[str] = []
    recent = [str(x).strip() for x in (recent_assistant or [])[-lookback:] if str(x).strip()]
    streak = recent_question_tail_streak(recent)
    if streak >= 2:
        hits.append("问句连发")
    q_in_window = sum(1 for x in recent if ends_with_question(x))
    if len(recent) >= 3 and q_in_window >= 3:
        hits.append("问句占比过高")
    if count_question_marks(reply) >= 2:
        hits.append("单条多问句")
    return hits


def opener_spam_hits(reply: str, recent_assistant: List[str], lookback: int = 4) -> List[str]:
    """近几轮同口头禅开场（如嘻嘻）再开同样头 → 命中。"""
    recent = [str(x).strip() for x in (recent_assistant or [])[-lookback:] if str(x).strip()]
    if not recent:
        return []
    hits: List[str] = []
    for opener in ("嘻嘻", "哈哈", "hehe", "haha", "ㅎㅎ"):
        recent_hits = sum(1 for x in recent if re.sub(r"\s+", "", x).lower().startswith(opener))
        if recent_hits >= 2 and re.sub(r"\s+", "", reply or "").lower().startswith(opener):
            hits.append(f"开场:{opener}")
    return hits


def list_repeat_themes(text: str) -> List[str]:
    compact = re.sub(r"\s+", "", text or "")
    return [t for t in _REPEAT_THEMES if t in compact]


def list_script_skeletons(text: str) -> List[str]:
    compact = re.sub(r"\s+", "", text or "")
    hits: List[str] = []
    for name, pat in _SCRIPT_SKELETONS:
        if re.search(pat, compact):
            hits.append(name)
    return hits


def theme_repeat_hits(reply: str, recent_assistant: List[str], lookback: int = 5) -> List[str]:
    """若本轮回复复用了近几轮已出现的忌用主题，返回命中主题列表。"""
    recent = [str(x) for x in (recent_assistant or [])[-lookback:] if str(x).strip()]
    if not recent:
        return []
    recent_blob = re.sub(r"\s+", "", "".join(recent))
    hits: List[str] = []
    for t in list_repeat_themes(reply):
        if t in recent_blob:
            hits.append(t)
    return hits


def skeleton_repeat_hits(reply: str, recent_assistant: List[str], lookback: int = 5) -> List[str]:
    """近几轮已用过的话术骨架，本轮再次命中则返回。"""
    recent = [str(x) for x in (recent_assistant or [])[-lookback:] if str(x).strip()]
    if not recent:
        return []
    used = set()
    for x in recent:
        used.update(list_script_skeletons(x))
    if not used:
        return []
    return [s for s in list_script_skeletons(reply) if s in used]


def is_user_tease(text: str) -> bool:
    return bool(
        re.search(
            r"(胸|奶|性|床|脱|内衣|尺度|黄|大的|"
            r"boob|breast|sex|sexy|nude|naked|horny|dick|cock|pussy|"
            r"おっぱい|エロ|下着|セックス|"
            r"야한|섹스|가슴|"
            r"peito|sexo|gostos|"
            r"tetas|sexo|caliente|"
            r"seksi|telanjang|nafsu)",
            text or "",
            re.I,
        )
    )


def is_escape_safe_reply(text: str) -> bool:
    return bool(
        re.search(
            r"(抱枕|梦到我|告诉我梦|早点睡|喜欢什么颜色|快休息|乖乖睡|"
            r"pillow|dreamed of me|go to sleep|what co[lu]r|get some rest|"
            r"抱き枕|何色|早く寝|"
            r"베개|무슨 색|자자)",
            text or "",
            re.I,
        )
    )


def leave_close_violations(reply: str) -> List[str]:
    """离开/晚安场景下禁止的新钩子。"""
    compact = re.sub(r"\s+", "", reply or "")
    bad: List[str] = []
    checks = (
        ("明日约定", r"(明天|明早|tomorrow).{0,16}(告诉|念|说|听|来|tell|read)"),
        ("催继续聊", r"(还醒着|在不在|好不好\？|好吗\？|想不想|still awake|you there)"),
        ("新话题钩", r"(诗集|念给你|听我|淘到|poem|read (you )?a)"),
        ("连环催睡", r"(快睡|赶紧休息|早点睡|go to sleep|get some rest).{0,12}(哦|呀|吧|吗|ok|please)?"),
    )
    for name, pat in checks:
        if re.search(pat, compact, re.I):
            bad.append(name)
    return bad


def force_single_bubble(text: str) -> str:
    """离开场景压成一条，去掉空行拆条。"""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(parts) <= 1:
        return (text or "").strip()
    return " ".join(parts)


def chat_rules_block(
    *,
    has_leave_intent: bool = False,
    user_input: str = "",
    affection: float = 0,
    recent_assistant: Optional[Sequence[str]] = None,
    language: str = "zh",
) -> str:
    """本轮硬规则块：先答后撩 / 单条 / 黄腔接住（随亲密度）/ 离开收束 / 陈述问句轮换。"""
    from services.dialogue_i18n import quality_ui

    q = quality_ui(language)
    aff = float(affection or 0)
    recent = [str(x) for x in (recent_assistant or []) if str(x).strip()]
    q_streak = recent_question_tail_streak(recent)
    lines = [q["rules_title"], q["r1"], q["r2"], q["r3"], q["r4"]]
    if q_streak >= 1:
        lines.append(q["r5_streak"])
    elif is_anti_question_user(user_input):
        lines.append(q["r5_anti"])
    if is_user_tease(user_input):
        if aff < 22:
            lines.append(q["tease_low"])
        elif aff < 50:
            lines.append(q["tease_mid"])
        elif aff < 80:
            lines.append(q["tease_high"])
        else:
            lines.append(q["tease_max"])
    if has_leave_intent:
        lines.append(q["leave_rule"])
    return "\n".join(lines)


def seed_deny_from_recent(
    deny_hooks: Optional[List[str]],
    recent_assistant: Optional[List[str]],
) -> List[str]:
    """每轮用近聊指纹+主题+骨架补齐忌用窗，不单靠关系卡。"""
    out: List[str] = []
    seen = set()
    for d in list(deny_hooks or []) + [
        fp for x in (recent_assistant or [])[-5:] for fp in extract_reply_fingerprints(x)
    ] + [
        t for x in (recent_assistant or [])[-5:] for t in list_repeat_themes(x)
    ] + [
        s for x in (recent_assistant or [])[-5:] for s in list_script_skeletons(x)
    ]:
        s = str(d).strip()
        if len(s) < 2 or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out[-DENY_HOOKS_WINDOW:]


def extract_reply_fingerprints(text: str) -> List[str]:
    """从回复抽出开头/结尾/问句指纹，供忌用窗防复读。"""
    raw = _PAREN_STRIP.sub("", text or "")
    raw = raw.strip()
    if not raw:
        return []
    compact = re.sub(r"\s+", "", raw)
    out: List[str] = []
    if len(compact) >= 6:
        out.append(compact[:28])
    clauses = [c for c in re.split(r"[。！？!?～~]+", compact) if len(c) >= 4]
    if clauses:
        out.append(clauses[-1][-36:])
    qs = re.findall(r"[^。！？!?\n]{4,36}[？?]", raw)
    if qs:
        out.append(re.sub(r"\s+", "", qs[-1])[:40])
    for pat in _REPEAT_THEMES + (
        "想到你",
        "在干嘛",
        "忙完回",
        "陪我聊",
        "好不好",
        "要不要",
        "记得想我",
        "宝贝",
    ):
        if pat in compact:
            out.append(pat)
    for sk in list_script_skeletons(compact):
        out.append(sk)
    seen = set()
    uniq: List[str] = []
    for x in out:
        x = (x or "").strip()
        if len(x) < 2 or x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq[:8]


def push_deny_hook(card: Dict[str, Any], hook: str) -> Dict[str, Any]:
    out = dict(card or {})
    hooks = list(out.get("deny_hooks") or [])
    h = (hook or "").strip()
    if h and h not in hooks:
        hooks.append(h)
    out["deny_hooks"] = hooks[-DENY_HOOKS_WINDOW:]
    return out


def push_deny_fingerprints(card: Dict[str, Any], reply_text: str) -> Dict[str, Any]:
    """把本轮回复指纹写入忌用窗（v2 无 picked_hook 时也必须更新）。"""
    out = dict(card or {})
    for fp in extract_reply_fingerprints(reply_text):
        out = push_deny_hook(out, fp)
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
    hour = now.hour
    in_window = hour in range(7, 10) or hour in range(12, 14) or hour in range(20, 24)
    if last_user_ts is not None:
        try:
            if last_user_ts.tzinfo is None:
                last_user_ts = last_user_ts.replace(tzinfo=timezone.utc)
            idle_s = (now - last_user_ts).total_seconds()
        except Exception:
            idle_s = 9999
        if idle_s < 180:
            return False, "too_soon"
        if idle_s >= 600 and in_window:
            return True, "idle_window"
    aff = float(affection or 0)
    if aff >= 20 and in_window:
        return True, "affection_window"
    return False, "no_signal"


def _summarize_recent_reply(text: str, limit: int = 110) -> str:
    s = _PAREN_STRIP.sub("", text or "").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) <= limit:
        return s
    return s[:40] + "…" + s[-(limit - 42) :]


def anti_repeat_hint(
    recent_assistant: List[str],
    max_items: int = 4,
    deny_hooks: Optional[List[str]] = None,
    language: str = "zh",
) -> str:
    """近 N 条 assistant 摘要 + 忌用指纹，供 Respond 勿重复。"""
    from services.dialogue_i18n import quality_ui

    q = quality_ui(language)
    parts: List[str] = []
    recent = [str(x).strip() for x in (recent_assistant or []) if str(x).strip()]
    recent = recent[-max_items:]
    if recent:
        items = [_summarize_recent_reply(x) for x in recent]
        parts.append(q["anti_title"] + "\n- " + "\n- ".join(items))
    deny: List[str] = []
    seen = set()
    for d in list(deny_hooks or []) + [
        fp for x in recent for fp in extract_reply_fingerprints(x)
    ]:
        d = str(d).strip()
        if len(d) < 2 or d in seen:
            continue
        seen.add(d)
        deny.append(d)
    deny = deny[-12:]
    if deny:
        joiner = "、" if (language or "zh").startswith("zh") else ", "
        parts.append(q["deny_title"] + joiner.join(deny))
    themes = []
    tseen = set()
    for x in recent:
        for t in list_repeat_themes(x):
            if t not in tseen:
                tseen.add(t)
                themes.append(t)
    if themes:
        joiner = "、" if (language or "zh").startswith("zh") else ", "
        parts.append(q["theme_title"] + joiner.join(themes))
    skeletons = []
    sseen = set()
    for x in recent:
        for s in list_script_skeletons(x):
            if s not in sseen:
                sseen.add(s)
                skeletons.append(s)
    if skeletons:
        joiner = "、" if (language or "zh").startswith("zh") else ", "
        parts.append(q["skel_title"] + joiner.join(skeletons))
    if parts:
        parts.append(q["anti_hard"])
    return "\n".join(parts)
