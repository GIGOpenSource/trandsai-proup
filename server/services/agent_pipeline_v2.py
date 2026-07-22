"""Pipeline v2: 2 前台 LLM + 条件后台，Token/延迟优化。"""
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from services.agent import (
    _calculate_affection_delta,
    _parse_affection_signal,
    _should_extract,
    extract_facts_node,
    persona_evolve_node,
    summary_node,
)
from services.agent_utils import (
    build_system_prompt,
    get_content_restriction,
    humanize,
    llm_content_to_str,
    strip_outer_markdown_fence,
)
from services.agent import get_llm
from services.llm.client import llm_invoke, resolve_max_tokens
from services.knowledge_base import knowledge_base

logger = logging.getLogger(__name__)

_PREPARE_MAX_TOKENS = int(os.getenv("PREPARE_MAX_TOKENS", "512"))
_PREPARE_MEMORY_CHARS = int(os.getenv("PREPARE_MEMORY_CHARS", "1400"))

_PREPARE_JSON_HINT = """先理解用户本轮话，再严格输出 JSON（无 markdown 围栏）：
{"user_intent":"用户想表达/想得到什么（一句话）","must_answer":"回复必须直接回应的点（多项用分号；无则空）","refs":"本轮指代/承接的上文对象（无则空）","think":"结合近聊的理解要点，≤280字","flirt_move":"本轮可用的轻撩/绿茶手法标签（如：具体夸/推拉/轻吃醋/装无辜/鉴定；多数轮次选1个或空）","mood":"情绪词","affection_signal":"up|flat|down","creative_hint":"可选；多数轮次用空字符串；勿强制钩子"}
规则：优先弄清意图与必须回应点；不要编造用户没说的事；flirt_move 服务于续聊张力，不能盖过必须回应。"""


def _json_field(raw: str, key: str) -> str:
    m = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', raw)
    if not m:
        return ""
    try:
        return json.loads(f'"{m.group(1)}"')
    except Exception:
        return m.group(1)


def _parse_prepare_json(text: str) -> dict:
    raw = strip_outer_markdown_fence(text).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "user_intent": str(data.get("user_intent") or "").strip(),
                "must_answer": str(data.get("must_answer") or "").strip(),
                "refs": str(data.get("refs") or "").strip(),
                "think": str(data.get("think") or "").strip(),
                "flirt_move": str(data.get("flirt_move") or "").strip(),
                "mood": str(data.get("mood") or "开心").strip() or "开心",
                "affection_signal": str(data.get("affection_signal") or "up").strip(),
                "creative_hint": str(data.get("creative_hint") or "").strip(),
            }
    except json.JSONDecodeError:
        pass
    return {
        "user_intent": _json_field(raw, "user_intent"),
        "must_answer": _json_field(raw, "must_answer"),
        "refs": _json_field(raw, "refs"),
        "think": _json_field(raw, "think") or raw[:280],
        "flirt_move": _json_field(raw, "flirt_move"),
        "mood": _json_field(raw, "mood") or "开心",
        "affection_signal": _json_field(raw, "affection_signal") or "up",
        "creative_hint": _json_field(raw, "creative_hint"),
    }


def _slice_memory_for_prepare(memory_text: str, max_chars: int = _PREPARE_MEMORY_CHARS) -> str:
    """Prefer recent dialogue over relation-card head for understanding."""
    text = (memory_text or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    marker = "【最近对话】"
    idx = text.find(marker)
    if idx >= 0:
        dialogue = text[idx:]
        head = text[:idx].strip()
        dlg_budget = min(max(900, max_chars * 2 // 3), max_chars)
        if len(dialogue) > dlg_budget:
            lines = dialogue.split("\n")
            kept = [lines[0]]
            size = len(lines[0])
            for line in reversed(lines[1:]):
                if size + len(line) + 1 > dlg_budget:
                    break
                kept.insert(1, line)
                size += len(line) + 1
            dialogue = "\n".join(kept)
        head_budget = max_chars - len(dialogue) - 2
        if head_budget > 80 and head:
            # keep facts / card tail (often denser near dialogue)
            if len(head) > head_budget:
                head = "…" + head[-head_budget + 1 :]
            return f"{head}\n\n{dialogue}".strip()
        return dialogue

    # No dialogue marker: keep the end (recent content usually last)
    return text[-max_chars:]


def _search_knowledge(user_input: str) -> str:
    try:
        kb_results = knowledge_base.search_entries(user_input, top_k=3)
        # 会撩模式：额外捞一条关系策略类条目，供内化成语气（禁止术语复读）
        if os.getenv("FLIRT_TEA", "1").lower() not in ("0", "false", "no", "off"):
            try:
                extra = knowledge_base.search_entries(
                    "暧昧 推拉 吃醋 欲擒故纵 关系节奏", top_k=1
                )
                if extra:
                    titles = {r.get("title") for r in (kb_results or [])}
                    for r in extra:
                        if r.get("title") not in titles:
                            kb_results = (kb_results or []) + [r]
                            break
            except Exception:
                pass
        if kb_results:
            return (
                "【知识库参考】（内化成口语张力，禁止上课/甩PUA术语）\n"
                + "\n".join(f"- {r['title']}: {r['content'][:300]}" for r in kb_results)
                + "\n"
            )
    except Exception as e:
        logger.warning("KB search failed in v2 prepare: %s", e)
    return ""


def run_pipeline_v2(
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
    deny_hooks: list = None,
    recent_assistant: list = None,
    open_threads: list = None,
    **_extra,
) -> dict:
    """前台 2 次 LLM：prepare_reflect + respond。"""
    from services.dialogue_phase2 import resolve_relation_stage, stage_instruction
    from services.dialogue_quality import (
        build_respond_quality_hints,
        diagnose_reply_violations,
        enrich_human_prompt,
        finalize_reply_for_leave,
        rewrite_instruction,
    )

    lang = language
    evolved = {
        "personality": companion_state.get("evolved_personality", ""),
        "background": companion_state.get("evolved_background", ""),
        "speech_style": companion_state.get("evolved_speech_style", ""),
    }
    turns = companion_state.get("turns", 0)
    if extract_due is True:
        extract_due = _should_extract(user_input, turns)

    kb_text = knowledge_text or _search_knowledge(user_input)
    core_prompt = build_system_prompt(
        profile, lang, evolved=evolved, user_gender=user_gender, turns=turns, tier="core"
    )
    time_info = f"\n【当前时间】{current_time}" if current_time else ""
    prepare_memory = _slice_memory_for_prepare(memory_text)

    # Call-1: intent-first prepare
    llm1 = get_llm(max_tokens=_PREPARE_MAX_TOKENS, role="inner")
    prep_system = f"""{core_prompt}{time_info}

【当前状态】情绪：{companion_state.get('mood', '开心')} | 亲密度：{companion_state.get('affection', 0)}

【近聊与记忆（理解优先）】
{prepare_memory}

{kb_text}

任务：准确理解用户本轮语义（含指代、省略、承接）；先意图后情绪。"""
    prep_human = f"用户本轮说：{user_input}\n\n{_PREPARE_JSON_HINT}"
    prep_resp = llm_invoke(
        llm1,
        [SystemMessage(content=prep_system), HumanMessage(content=prep_human)],
        node="prepare_reflect",
        max_tokens=_PREPARE_MAX_TOKENS,
    )
    prep_json = _parse_prepare_json(llm_content_to_str(getattr(prep_resp, "content", "")))

    old_affection = companion_state.get("affection", 0)
    signal = _parse_affection_signal(
        str(prep_json.get("affection_signal") or prep_json.get("affection_note") or ""),
        default="up",
    )
    raw_sig = str(prep_json.get("affection_signal") or "").strip().lower()
    if raw_sig in ("up", "flat", "down"):
        signal = raw_sig
    new_affection = max(0, min(100, old_affection + _calculate_affection_delta(old_affection, signal)))
    mood = prep_json.get("mood") or companion_state.get("mood", "开心")

    # Call-2: respond（Full Prompt 仅 1 次）
    full_prompt = build_system_prompt(
        profile, lang, evolved=evolved, user_gender=user_gender, turns=turns, tier="full"
    )
    restriction_text = get_content_restriction(lang, new_affection)
    name = profile.get("name", "Companion")
    stage = resolve_relation_stage(turns, new_affection)
    stage_hint = stage_instruction(stage, lang)
    qhints = build_respond_quality_hints(
        user_input=user_input or "",
        recent_assistant=recent_assistant or [],
        deny_hooks=deny_hooks or [],
        has_leave_intent=has_leave_intent,
    )
    rules_block = qhints["rules_block"]
    repeat_hint = qhints["repeat_hint"]
    leave_hint = qhints["leave_hint"]
    reply_shape = qhints["reply_shape"]
    threads = [str(x).strip() for x in (open_threads or []) if str(x).strip()][:3]
    thread_hint = ("【未完话题】" + " / ".join(threads)) if threads else ""

    intent = (prep_json.get("user_intent") or "").strip()
    must_answer = (prep_json.get("must_answer") or "").strip()
    refs = (prep_json.get("refs") or "").strip()
    intent_block = "\n".join(
        x
        for x in (
            f"【用户意图】{intent}" if intent else "",
            f"【必须回应】{must_answer}" if must_answer else "",
            f"【指代/承接】{refs}" if refs else "",
        )
        if x
    )

    respond_system = f"""{full_prompt}{time_info}

{intent_block}
【理解要点】{prep_json.get('think', '')}
【本轮撩法】{prep_json.get('flirt_move') or '按阶段自然带一点；勿盖过必答点'}
【情绪】{mood} | 亲密度：{new_affection}
【创意提示】{prep_json.get('creative_hint', '') or '（无；勿硬加钩子）'}
{stage_hint}
{rules_block}
{repeat_hint}
{leave_hint}
{thread_hint}

【记忆上下文】
{memory_text}

{kb_text}

【当前状态】情绪：{mood} | 亲密度：{new_affection}

{restriction_text}

回复原则：
1. 先准确回应用户意图与「必须回应」点，再做人设润色；禁止答非所问、禁止忽略指代。
2. 表达要通顺：一句一事、主谓清楚、因果顺序正确；口语可以短，但不要残句乱跳、同义反复凑字。
3. 会撩/绿茶：在接住话题后加一点张力（推拉、轻吃醋、假装无辜），随关系阶段调节；禁止术语课本腔。
4. 钩子可选；严格遵守上方【勿复读】【忌用】【聊天规则】；禁止同义换皮/同构骨架复读。
{reply_shape}"""

    respond_human = enrich_human_prompt(
        f"用户刚刚说：{user_input}\n\n"
        f"请以 {name} 的身份直接输出对用户可见的口语回复正文。"
        "要求语句通顺、指代清楚、先答其意；禁止复述近几轮自己说过的话。",
        user_input=user_input or "",
        has_leave_intent=has_leave_intent,
    )

    max_tok = resolve_max_tokens(new_affection)
    llm2 = get_llm(max_tokens=max_tok, role="respond")
    resp2 = llm_invoke(
        llm2,
        [SystemMessage(content=respond_system), HumanMessage(content=respond_human)],
        node="respond_v2",
        affection=new_affection,
        max_tokens=max_tok,
    )
    raw = strip_outer_markdown_fence(llm_content_to_str(getattr(resp2, "content", ""))).strip()
    final_response = humanize(raw, lang)

    # 质量补充层：主题/骨架/离开/黄腔逃题 → 强制改写（REQ-Q1～Q3）
    rewrite_on = os.getenv("REPEAT_REWRITE", "1").lower() not in ("0", "false", "no", "off")
    if rewrite_on:
        diag = diagnose_reply_violations(
            final_response,
            user_input=user_input or "",
            recent_assistant=recent_assistant or [],
            has_leave_intent=has_leave_intent,
        )
        if diag["need_rewrite"]:
            rewrite_human = respond_human + rewrite_instruction(diag["ban_summary"])
            resp3 = llm_invoke(
                llm2,
                [SystemMessage(content=respond_system), HumanMessage(content=rewrite_human)],
                node="respond_v2_rewrite",
                affection=new_affection,
                max_tokens=max_tok,
            )
            raw2 = strip_outer_markdown_fence(
                llm_content_to_str(getattr(resp3, "content", ""))
            ).strip()
            if raw2:
                final_response = humanize(raw2, lang)
                diag2 = diagnose_reply_violations(
                    final_response,
                    user_input=user_input or "",
                    recent_assistant=recent_assistant or [],
                    has_leave_intent=has_leave_intent,
                )
                if diag2["need_rewrite"]:
                    rewrite_human2 = respond_human + rewrite_instruction(
                        diag2["ban_summary"], final_pass=True
                    )
                    resp4 = llm_invoke(
                        llm2,
                        [
                            SystemMessage(content=respond_system),
                            HumanMessage(content=rewrite_human2),
                        ],
                        node="respond_v2_rewrite2",
                        affection=new_affection,
                        max_tokens=max_tok,
                    )
                    raw3 = strip_outer_markdown_fence(
                        llm_content_to_str(getattr(resp4, "content", ""))
                    ).strip()
                    if raw3:
                        final_response = humanize(raw3, lang)

    final_response = finalize_reply_for_leave(final_response, has_leave_intent)

    # 供关系卡忌用窗落库
    fps = []
    try:
        from services.dialogue_phase2 import extract_reply_fingerprints

        fps = extract_reply_fingerprints(final_response)
    except Exception:
        fps = []

    return {
        "response": final_response,
        "think_result": prep_json.get("think", ""),
        "mood": mood,
        "affection": new_affection,
        "picked_hook": (fps[0] if fps else ""),
        "new_facts": [],
        "new_summary": companion_state.get("summary", ""),
        "evolved_personality": "",
        "evolved_background": "",
        "evolved_speech_style": "",
        "memory_snapshot": {
            "user_input": user_input,
            "final_response": final_response,
            "profile": profile,
            "companion_state": {**companion_state, "mood": mood, "affection": new_affection},
            "language": lang,
            "summary_due": summary_due,
            "extract_due": extract_due,
        },
    }


def run_memory_update(snapshot: dict) -> dict:
    """后台 Call-3：条件 extract + summary + persona evolve。"""
    if not snapshot:
        return {}

    pseudo_state = {
        "user_input": snapshot["user_input"],
        "final_response": snapshot["final_response"],
        "profile": snapshot["profile"],
        "state": snapshot["companion_state"],
        "language": snapshot.get("language", "zh"),
        "memory_text": "",
        "summary_due": snapshot.get("summary_due", False),
        "extract_due": snapshot.get("extract_due", True),
    }

    new_facts: list = []
    new_summary = snapshot["companion_state"].get("summary", "")
    evolved = {"evolved_personality": "", "evolved_background": "", "evolved_speech_style": ""}

    if snapshot.get("extract_due", True):
        facts_result = extract_facts_node(pseudo_state)  # type: ignore[arg-type]
        new_facts = facts_result.get("new_facts", [])

    if snapshot.get("summary_due", False):
        summary_result = summary_node(pseudo_state)  # type: ignore[arg-type]
        new_summary = summary_result.get("new_summary", new_summary)

    interval = int(os.getenv("PERSONA_EVOLVE_INTERVAL", "10"))
    turns = snapshot["companion_state"].get("turns", 0)
    if turns > 0 and turns % interval == 0:
        evolve_result = persona_evolve_node(pseudo_state)  # type: ignore[arg-type]
        evolved = {
            "evolved_personality": evolve_result.get("evolved_personality", ""),
            "evolved_background": evolve_result.get("evolved_background", ""),
            "evolved_speech_style": evolve_result.get("evolved_speech_style", ""),
        }

    return {
        "new_facts": new_facts,
        "new_summary": new_summary,
        **evolved,
    }
