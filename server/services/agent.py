import os
import re
import logging
from typing import Any, Dict, List, Optional

from core.i18n import normalize_ui_language
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from services.agent_utils import (
    _get_agent_config,
    build_system_prompt,
    get_content_restriction,
    humanize,
    llm_content_to_str,
    strip_outer_markdown_fence,
    _get_evolved,
    _merge_evolved_field,
)
from services.knowledge_base import knowledge_base
from services.llm.client import llm_invoke, resolve_max_tokens

logger = logging.getLogger(__name__)


# ===== 模型 설정 =====
def get_llm(
    temperature: float = None,
    max_tokens: int = None,
    provider: str = None,
    api_key_overrides: Optional[Dict[str, str]] = None,
    role: str = "respond",
):
    """LLM支持. role=respond|inner|archive（C5 外部分级）。"""
    cfg = _get_agent_config()
    # respond：略随机即可；过高温度 + 强惩罚会破坏表达逻辑
    default_temp = 0.82 if (role or "respond").lower() == "respond" else 0.5
    temperature = temperature if temperature is not None else cfg.get("temperature", default_temp)
    if temperature is None:
        temperature = default_temp
    max_tokens = max_tokens if max_tokens is not None else cfg.get("max_tokens", 512)

    role = (role or "respond").lower()
    freq_pen = float(os.getenv("LLM_FREQUENCY_PENALTY", "0.12" if role == "respond" else "0") or 0)
    pres_pen = float(os.getenv("LLM_PRESENCE_PENALTY", "0.08" if role == "respond" else "0") or 0)
    if provider is None:
        if role == "inner":
            provider = os.getenv("INNER_MODEL_PROVIDER") or os.getenv("MODEL_PROVIDER_INNER") or cfg.get("inner_provider")
        elif role == "archive":
            provider = os.getenv("ARCHIVE_MODEL_PROVIDER") or os.getenv("MODEL_PROVIDER_ARCHIVE") or cfg.get("archive_provider")
        if not provider:
            provider = cfg.get("model_provider") or os.getenv("MODEL_PROVIDER", "anthropic")
    provider = (provider or "anthropic").lower()
    # C5：未显式指定 Inner/Archive provider 时，把 anthropic 降到 flash 档
    explicit_role_provider = None
    if role == "inner":
        explicit_role_provider = os.getenv("INNER_MODEL_PROVIDER") or os.getenv("MODEL_PROVIDER_INNER")
    elif role == "archive":
        explicit_role_provider = os.getenv("ARCHIVE_MODEL_PROVIDER") or os.getenv("MODEL_PROVIDER_ARCHIVE")
    if (
        role in ("inner", "archive")
        and not explicit_role_provider
        and provider in ("anthropic", "claude")
    ):
        flash = os.getenv("INNER_FLASH_PROVIDER", "deepseek")
        if role == "archive":
            flash = os.getenv("ARCHIVE_FLASH_PROVIDER", flash)
        if flash:
            provider = flash.lower()

    def _override_or_env(field: str, env_name: str) -> str:
        if api_key_overrides:
            v = (api_key_overrides.get(field) or "").strip()
            if v:
                return v
        return os.getenv(env_name, "") or ""


    openai_extra = {}
    # Grok/xAI 不支持 presence_penalty / frequency_penalty（会 400 invalid-argument）
    # 仅对 OpenAI 兼容且明确支持的提供商注入
    if freq_pen:
        openai_extra["frequency_penalty"] = freq_pen
    if pres_pen:
        openai_extra["presence_penalty"] = pres_pen

    if provider == "deepseek":
        api_key = _override_or_env("deepseek_key", "DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("请设 변수 DEEPSEEK_API_KEY를 설정해주세요.")
        return ChatOpenAI(
            model="deepseek-v4-flash",  # 对应你截图里的模型Code
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",  # 百炼固定兼容地址
            **openai_extra,
        )

    if provider in ("grok", "xai"):
        api_key = _override_or_env("xai_key", "XAI_API_KEY")
        if not api_key:
            raise RuntimeError("请设置 XAI_API_KEY 环境变量.")
        # grok-3-latest 拒收 presence/frequency penalty
        return ChatOpenAI(
            model="grok-3-latest",
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
            base_url="https://api.x.ai/v1",
        )

    if provider == "openai":
        api_key = _override_or_env("openai_key", "OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("请设 변수 OPENAI_API_KEY를 설정해주세요.")
        return ChatOpenAI(
            model="gpt-4o",
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
            **openai_extra,
        )

    # 기본값은 anthropic
    api_key = _override_or_env("anthropic_key", "ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("请设 변수 ANTHROPIC_API_KEY를 설정해주세요.")
    return ChatAnthropic(
        model="claude-sonnet-4-20250514",
        temperature=temperature,
        max_tokens=max_tokens,
        anthropic_api_key=api_key,
    )


def test_llm_connection(provider: str = None, api_key_overrides: Optional[Dict[str, str]] = None) -> dict:
    """连通 테스트: 간단한 메시지를 보내 API 가능성을 확인합니다."""
    try:
        llm = get_llm(temperature=0.5, provider=provider, api_key_overrides=api_key_overrides)
        resp = llm_invoke(llm, [HumanMessage(content="你好")], node="test_connection")
        content = resp.content if hasattr(resp, "content") else str(resp)
        return {"ok": True, "response": content[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ===== 에이전트 상태 =====
class AgentState(TypedDict):
    messages: List[BaseMessage]
    user_input: str
    profile: Dict[str, Any]
    state: Dict[str, Any]
    memory_text: str
    knowledge_text: str
    language: str
    user_gender: str
    current_time: str
    summary_due: bool
    extract_due: bool
    light_path: bool
    has_leave_intent: bool
    burst_count: int
    open_threads: List[str]
    deny_hooks: List[str]
    recent_assistant: List[str]
    relation_stage: str
    picked_hook: str
    threat_level: str
    means_mode: str
    extreme_reason: str
    idle_seconds: float
    last_extreme_ts: str
    think_result: str
    reflect_result: str
    creative_result: str
    affection_signal: str
    final_response: str
    updated_mood: str
    updated_affection: float
    new_facts: List[str]
    new_summary: str
    evolved_personality: str
    evolved_background: str
    evolved_speech_style: str


# ===== 节点函数 =====

def think_node(state: AgentState) -> dict:
    """回忆 + 감정 분석 + 지식베이스 검색"""
    llm = get_llm()
    lang = state.get("language", "zh")
    system_text = build_system_prompt(state["profile"], lang, evolved=_get_evolved(state), user_gender=state.get("user_gender", ""), turns=state["state"].get("turns", 0))

    # 检索知识库 검색
    kb_text = ""
    try:
        kb_results = knowledge_base.search_entries(state["user_input"], top_k=3)
        if kb_results:
            kb_text = "【知识库参考 참고 참고】\n" + "\n".join(
                f"- {r['title']}: {r['content'][:300]}" for r in kb_results
            ) + "\n"
    except Exception as e:
        logger.warning("Knowledge lookup failed in think node: %s", e)

    current_time = state.get("current_time", "")
    time_info = f"\n【当前 시간】{current_time}" if current_time else ""

    prompt = f"""{system_text}{time_info}

【현재 상태】
감정：{state['state'].get('mood', '开心')}
친밀도：{state['state'].get('affection', 0)}

【记忆 맥락】
{state['memory_text']}

{kb_text}

사용자가 방금 말했습니다: {state['user_input']}

请你作为 {state['profile'].get('name', 'Companion')}先进行 내부 생각(Think)先 하세요:
1. 분석 사용자 감정, 요구사항을 분석합니다.
2. 从记忆中 가장 관련성이 높은 사건이나 사실을 준비합니다.
3. 结合知识库 참고를 통해 부드러운 경고가 필요한지 판단합니다.
4. 감정 상태를 판단합니다.
5. 시간({current_time})先 고려하여 지금이 어떤 시점인지, 무엇을 하고 있는지, 필요한지 판단합니다.

결과만 출력하고 최종 답변은 출력하지 않습니다。
【格式】直接输出纯文本内心分析（可多行），不要用 JSON、不要用 Markdown 代码块（```）整段包裹。"""

    resp = llm_invoke(llm, [SystemMessage(content=prompt)], node="think")
    raw = strip_outer_markdown_fence(llm_content_to_str(getattr(resp, "content", "")))
    return {"think_result": raw.strip(), "knowledge_text": kb_text}


def _calculate_affection_delta(old_affection: float, signal: str = "up") -> float:
    """REQ-B6 信号：up/flat/down；基数按可玩亲密度调高（早期约 0.5–0.7/轮，后期缓降）。

    旧公式 0.01/(1+aff/20) 导致数千轮仍困在「陌生人」短回复档，严重伤害真人感。
    可用环境变量覆盖：AFFECTION_BASE（默认 0.65）、AFFECTION_TAPER（默认 30）。
    """
    base_amt = float(os.getenv("AFFECTION_BASE", "0.65"))
    taper = float(os.getenv("AFFECTION_TAPER", "30"))
    if taper <= 0:
        taper = 30.0
    base = base_amt / (1 + float(old_affection or 0) / taper)
    sig = (signal or "up").strip().lower()
    if sig == "down":
        return round(-0.5 * base, 4)
    if sig == "flat":
        return 0.0
    return round(base, 4)



def _parse_affection_signal(text: str, default: str = "up") -> str:
    m = re.search(r"亲密度信号[：:]\s*(up|flat|down)", text or "", re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m = re.search(r"affection[_\s]?signal[：:\s]*(up|flat|down)", text or "", re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return default


def reflect_node(state: AgentState) -> dict:
    """自我 반성 + 情绪/亲密度 업데이트"""
    llm = get_llm()
    lang = state.get("language", "zh")
    system_text = build_system_prompt(state["profile"], lang, evolved=_get_evolved(state), user_gender=state.get("user_gender", ""), turns=state["state"].get("turns", 0))
    prompt = f"""{system_text}

【上一 라운드 생각】
{state['think_result']}

【현재 상태】
감정：{state['state'].get('mood', '开心')} | 亲密度：{state['state'].get('affection', 0)}

请你进 반성(Reflect)을 하세요:
1. 听完用户 말을 듣고 당신의 감정이 어떻게 변했는지.
2. 你对用户 대한 느낌이 더 깊어졌는지. 사용자가 좋았다면 친근함을 표현하고, 나쁘다면 실망을 표현합니다.
3. 당신의 현재 감정 키워드는 무엇인지? (단어나 짧은 문장)

다음 형식으로 엄격하게 출력하세요:
감정: [새 감정]
친밀도 변화: [설명, 예: "약간 깊어짐"/"조금 실망"/"아직 차분함" 등]
반성: [당신의 내면 고백]"""

    resp = llm_invoke(llm, [SystemMessage(content=prompt)], node="reflect")
    text = llm_content_to_str(getattr(resp, "content", "")).strip()

    mood = state["state"].get("mood", "开心")

    m = re.search(r"情绪[：:]\s*(.+?)(?:\n|$)", text)
    if m:
        mood = m.group(1).strip()

    signal = _parse_affection_signal(text, default="up")
    old_affection = state["state"].get("affection", 0)
    affection_delta = _calculate_affection_delta(old_affection, signal)
    new_affection = max(0, min(100, old_affection + affection_delta))

    return {
        "reflect_result": text,
        "updated_mood": mood,
        "updated_affection": new_affection,
    }


def creative_node(state: AgentState) -> dict:
    """脑暴创意 발휘, 이야기 연장, 미래 계획"""
    llm = get_llm()
    lang = state.get("language", "zh")
    system_text = build_system_prompt(state["profile"], lang, evolved=_get_evolved(state), user_gender=state.get("user_gender", ""), turns=state["state"].get("turns", 0))
    prompt = f"""{system_text}

【생각】{state['think_result']}
【反思】{state['reflect_result']}

请你进 창의성 발휘(Creative Plan)을 하세요:
1. 세부 사항, 작은 이야기 또는 로맨틱 장면을 자연스럽게 포함할 수 있습니다.
2. 기대나 계획(약속, 만나기, 함께 할 일)을 생각합니다.
3. 특별한 말투나 닉네임 사용법을 생각합니다.

당신의 창의적 아이디어를 간단히 출력하고 최종 답변은 출력하지 않습니다."""

    resp = llm_invoke(llm, [SystemMessage(content=prompt)], node="creative")
    return {"creative_result": llm_content_to_str(getattr(resp, "content", "")).strip()}


def respond_node(state: AgentState) -> dict:
    """成文：Respond 主模型；人设用 core 层（B3）；注入动机/手段/阶段/钩子（M*/C2/C6）。"""
    from services.dialogue_phase2 import (
        anti_repeat_hint,
        extract_hook_candidates,
        pick_hook,
        resolve_relation_stage,
        stage_instruction,
    )
    from services.motive_layer import means_instruction, motive_block

    llm = get_llm(role="respond")
    lang = state.get("language", "zh")
    # B3：Respond 用人设核心层；B4：稳定 system 前缀利于 PROMPT_CACHE
    system_text = build_system_prompt(
        state["profile"],
        lang,
        evolved=_get_evolved(state),
        user_gender=state.get("user_gender", ""),
        turns=state["state"].get("turns", 0),
        tier="core",
    )
    system_text = motive_block(lang) + "\n\n" + system_text
    means_mode = state.get("means_mode") or "persona"
    means_hint = means_instruction(means_mode, lang)
    restriction_text = get_content_restriction(lang, state["updated_affection"])
    current_time = state.get("current_time", "")
    time_info = f"\n【当前时间】{current_time}" if current_time else ""

    stage = state.get("relation_stage") or resolve_relation_stage(
        int(state["state"].get("turns") or 0),
        float(state.get("updated_affection") or state["state"].get("affection") or 0),
    )
    stage_hint = stage_instruction(stage, lang)
    candidates = extract_hook_candidates(state.get("creative_result") or "")
    picked = pick_hook(candidates, list(state.get("deny_hooks") or []))
    hook_hint = f"【本轮优先钩子】{picked}" if picked else ""
    threads = state.get("open_threads") or []
    thread_hint = ""
    if threads:
        thread_hint = "【续话题债】" + "；".join(str(t)[:40] for t in threads[:3])
    repeat_hint = anti_repeat_hint(
        list(state.get("recent_assistant") or []),
        deny_hooks=list(state.get("deny_hooks") or []),
    )

    # B2：关系卡已在 memory_text 时提示勿复述清单
    memory_block = state.get("memory_text") or ""
    if "【关系卡】" in memory_block:
        mem_label = "【上下文（关系卡已含摘要/事实，勿复述清单）】"
    else:
        mem_label = "【记忆】"

    turn_body = f"""{time_info}
{stage_hint}
{hook_hint}
{thread_hint}
{repeat_hint}

【内心】
思考: {state.get('think_result') or '（轻路径）'}
反思: {state.get('reflect_result') or ''}
创意: {state.get('creative_result') or ''}

{mem_label}
{memory_block}

{state.get('knowledge_text', '')}

【状态】情绪：{state['updated_mood']} | 亲密度：{state['updated_affection']}
用户说: {state['user_input']}

【威胁】{state.get('threat_level') or 'L0'} | 手段={means_mode}
{means_hint}
【留存】仅当离开意图时温和挽留一次；钩子可选；陈述/短反应收尾均可；忌复读忌用钩子与近几轮同款问句。
【情感】按亲密度调节强度，像「这个人」而不是通用粘人模板。
【表达】一句一事、指代清楚、先答后延；口语可短但禁止残句乱跳与同义反复凑字。
【人设】语气与设定一致；自然融入时间 {current_time}。
{restriction_text}

以 {state['profile'].get('name', 'Companion')} 身份直接口语回复；只输出正文。
[Reply shape] 优先一条；遵循 system 中括号/分段规则；长短随心情，勿凑字。"""

    affection = float(state.get("updated_affection", state["state"].get("affection", 0)))
    resp = llm_invoke(
        llm,
        [SystemMessage(content=system_text), SystemMessage(content=turn_body)],
        node="respond",
        affection=affection,
        max_tokens=resolve_max_tokens(affection),
    )
    raw = strip_outer_markdown_fence(llm_content_to_str(getattr(resp, "content", ""))).strip()
    final = humanize(raw, lang)
    return {
        "final_response": final,
        "picked_hook": picked,
        "relation_stage": stage,
        "means_mode": means_mode,
        "threat_level": state.get("threat_level") or "L0",
    }



def _fact_lang_from_text(user_input: str, fallback: str = "zh") -> str:
    """按用户文本脚本选事实语言，避免 UI 语言误标导致韩/日文污染关系卡。"""
    t = user_input or ""
    if re.search(r"[\u4e00-\u9fff]", t):
        return "zh"
    if re.search(r"[\u3040-\u30ff]", t):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", t):
        return "ko"
    if re.search(r"[A-Za-z]", t):
        return "en" if (fallback or "").startswith("en") else (fallback or "en")
    return fallback or "zh"


def extract_facts_node(state: AgentState) -> dict:
    """从对话中提取事实（条件触发）"""
    if not state.get("extract_due", True):
        return {"new_facts": []}
    llm = get_llm(temperature=0.3, role="archive")
    lang = _fact_lang_from_text(state.get("user_input") or "", state.get("language", "zh"))
    if lang == "en":
        prompt = f"""Extract key facts about "him" from the following conversation. One fact per line, output only the fact list, no explanations.

He said: {state['user_input']}
You replied: {state['final_response']}

Example format:
- He likes hot pot
- He has been under a lot of work pressure lately
- He has a cat named Doudou

Extract:"""
    elif lang == "ja":
        prompt = f"""以下の会話から「彼」に関する重要な事사실 추출してください。1行に1つ, 사실 리스트만을 출력し, 설명은不要です.

彼：{state['user_input']}
あなた：{state['final_response']}

例：
- 彼は鍋が好き
- 彼는 최근仕事のストレス大大きい
- 彼は豆豆という名前の猫を飼っている

抽出："""
    elif lang == "ko":
        prompt = f"""以下 대화에서 "그"에 관한 핵심 사실을 추출하세요. 한 줄에 하나씩, 사실 목록만 출력하고 설명은 하지 마세요.

그가 말함: {state['user_input']}
너의 답장: {state['final_response']}

예시:
- 그는 샤브샤브를 좋아함
- 그는 최근 업무 스트레스가 많음
- 그는 두두라는 고양이를 키움

추출:"""
    elif lang == "pt":
        prompt = f"""Extraia fatos-chave sobre "ele" da seguinte conversação. Um fato por linha, saída apenas a lista de fatos, sem explicações.

Ele disse: {state['user_input']}
Sua resposta: {state['final_response']}

Formato exemplo:
- Ele gosta de fondue
- Ele está sob muita pressão no trabalho recentemente
- Ele tem um gato chamado Doudou

Extraia:"""
    elif lang == "es":
        prompt = f"""Extrae hechos clave sobre "él" de la siguiente conversación. Un hecho por línea, salida solo la lista de hechos, sin explicaciones.

Él dijo: {state['user_input']}
Tu respuesta: {state['final_response']}

Formato ejemplo:
- Le gusta la fondue
- Ha estado bajo mucha presión en el trabajo últimamente
- Tiene un gato llamado Doudou

Extrae:"""
    elif lang == "id":
        prompt = f"""Ekstrak fakta kunci tentang "dia" dari percakapan berikut. Satu fakta per baris, keluarkan hanya daftar fakta, tanpa penjelasan.

Dia berkata: {state['user_input']}
Balasanmu: {state['final_response']}

Contoh format:
- Dia suka fondue
- Dia baru-baru ini mengalami banyak tekanan di tempat kerja
- Dia memiliki kucing bernama Doudou

Ekstrak:"""
    else:
        # zh 及其他：必须用中文抽事实（曾误用韩语模板导致关系卡污染）
        prompt = f"""从以下对话中提取关于「他」的关键事实。每行一条，只输出事实列表，不要解释。

他说：{state['user_input']}
你回：{state['final_response']}

示例格式：
- 他喜欢吃火锅
- 他最近工作压力很大
- 他有一只叫豆豆的猫

要求：事实必须使用与用户相同的语言（当前为简体中文）；不要翻译成外语。

提取："""
    resp = llm_invoke(llm, [HumanMessage(content=prompt)], node="extract_facts")
    text = llm_content_to_str(getattr(resp, "content", ""))
    facts = [line.strip("- • \t") for line in text.splitlines() if line.strip().startswith(("-", "•"))]
    # 门禁：丢掉与用户文本脚本明显不符的外语事实（防韩/日污染中文卡）
    facts = _filter_facts_by_user_lang(facts, state.get("user_input") or "", lang)
    return {"new_facts": facts}


def _filter_facts_by_user_lang(facts: List[str], user_input: str, lang: str) -> List[str]:
    out: List[str] = []
    for f in facts or []:
        s = str(f).strip()
        if not s:
            continue
        has_ko = bool(re.search(r"[\uac00-\ud7af]", s))
        has_ja = bool(re.search(r"[\u3040-\u30ff]", s))
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", s))
        if lang == "zh" and (has_ko or (has_ja and not has_zh)):
            continue
        if lang == "en" and (has_ko or has_ja) and not re.search(r"[A-Za-z]", s):
            continue
        ui = user_input or ""
        if re.search(r"[\u4e00-\u9fff]", ui) and has_ko:
            continue
        out.append(s)
    return out


def summary_node(state: AgentState) -> dict:
    """关系摘要更新（仅 summary_due 时调用 LLM）"""
    if not state.get("summary_due", False):
        return {"new_summary": state["state"].get("summary", "")}
    llm = get_llm(temperature=0.5, role="archive")
    lang = state.get("language", "zh")
    old_summary = state["state"].get("summary", "")
    if lang == "en":
        prompt = f"""Summarize your relationship progress with this person in one warm, sweet sentence. Don't list events; write it like a diary entry.

Old summary: {old_summary or "(none yet)"}

Recently they said: {state['user_input']}
You replied: {state['final_response']}

New one-sentence summary:"""
    elif lang == "ja":
        prompt = f"""彼との関係の進展温、温かく甘い一言でまとめてください。イベントを列挙せず、日書のように書いてください。

旧摘要：{old_summary or "（まだなし）"}

最近彼は：{state['user_input']}
あなたの返信：{state['final_response']}

新しい一言摘要："""
    elif lang == "ko":
        prompt = f"""그와의 관계 진전을 따뜻하고 달콤한 한 문장으로 요약해줘. 사건을 나열하지 말고, 일기처럼 써줘.

이전 요약：{old_summary or "(아직 없음)"}

최근 그가：{state['user_input']}
네 답장：{state['final_response']}

새로운 한 문장 요약："""
    elif lang == "pt":
        prompt = f"""Resuma o progresso do seu relacionamento com esta pessoa em uma frase calorosa e doce. Não liste eventos; escreva como uma entrada de diário.

Resumo anterior: {old_summary or "(ainda não há)"}

Recentemente ele disse: {state['user_input']}
Sua resposta: {state['final_response']}

Nova frase-resumo:"""
    elif lang == "es":
        prompt = f"""Resume el progreso de tu relación con esta persona en una frase cálida y dulce. No enumeres eventos; escríbelo como una entrada de diario.

Resumen anterior: {old_summary or "(todavía no hay)"}

Recientemente él dijo: {state['user_input']}
Tu respuesta: {state['final_response']}

Nueva frase-resumen:"""
    elif lang == "id":
        prompt = f"""Ringkaskan perkembangan hubunganmu dengan orang ini dalam satu kalimat yang hangat dan manis. Jangan daftar peristiwa; tulis seperti catatan harian.

Ringkasan sebelumnya: {old_summary or "(belum ada)"}

Baru-baru ini dia berkata: {state['user_input']}
Balasanmu: {state['final_response']}

Kalimat ringkasan baru:"""
    else:
        prompt = f"""한 상대방의 관계 진행 상황을 따뜻하고 달콤한 한 문장으로 요약하세요. 사건을 나열하지 말고, 일기처럼 써주세요.

이전 요약: {old_summary or "（暂无）"}

최근 상대방이 말함: {state['user_input']}
너의 답장: {state['final_response']}

新的一 한 문장 요약:"""
    resp = llm_invoke(llm, [HumanMessage(content=prompt)], node="summary")
    new_summary = llm_content_to_str(getattr(resp, "content", "")).strip().strip("\"'")
    return {"new_summary": new_summary}


def persona_evolve_node(state: AgentState) -> dict:
    """인격 진화: 최근 대화 분석을 기반으로 인격 증가량 생성(5轮触发一次 트리거)"""
    llm = get_llm(temperature=0.7, role="archive")
    lang = state.get("language", "zh")
    profile = state["profile"]
    current = _get_evolved(state)
    name = profile.get("name", "Companion")
    memory_snippet = state["memory_text"][:1000] if state.get("memory_text") else ""

    if lang == "en":
        prompt = f"""You are {name}. Analyze your recent conversations with this person and identify subtle evolutions in your persona.

Base personality: {profile.get('personality', '')}
Current evolved traits: {current.get('personality') or '(none yet)'}

Base background: {profile.get('background', '')}
Current evolved background: {current.get('background') or '(none yet)'}

Base speech style: {profile.get('speech_style', '')}
Current evolved style: {current.get('speech_style') or '(none yet)'}

Recent conversations:
{memory_snippet}

Identify up to 3 subtle but meaningful evolutions (shared jokes, deeper understanding of them, shifts in how you speak). Each evolution must be 1-2 sentences, max 40 words. These are INCREMENTAL additions, not replacements. If no meaningful change, output NO_CHANGE.

Format:
personality: <evolution or NO_CHANGE>
background: <evolution or NO_CHANGE>
speech_style: <evolution or NO_CHANGE>"""
    elif lang == "ja":
        prompt = f"""あなたは{name}。最近の会話を分析し、人格の微妙な進화を特定してください。

基本性格：{profile.get('personality', '')}
現在の進화：{current.get('personality') or '（なし）'}

基本背景：{profile.get('background', '')}
現在の進화：{current.get('background') or '（なし）'}

基本話し方：{profile.get('speech_style', '')}
現在の進화：{current.get('speech_style') or '（なし）'}

最近の会話：
{memory_snippet}

最大3つの微妙な進화를特定してください。各1-2文、最大40文字。増분追進화. 진화がない場合は NO_CHANGE。

形式：
personality: <進化またはNO_CHANGE>
background: <進化またはNO_CHANGE>
speech_style: <進화またはNO_CHANGE>"""
    elif lang == "ko":
        prompt = f"""너는 {name}. 최근 대화를 분석하고 인격의 미묘한 진화를 파악해라.


기본 성격：{profile.get('personality', '')}
현재 진화：{current.get('personality') or '（없음）'}

기본 배경：{profile.get('background', '')}
현재 진화：{current.get('background') or '（없음）'}

기본 말투：{profile.get('speech_style', '')}
현재 진화：{current.get('speech_style') or '（없음）'}

최근 대화：
{memory_snippet}

최대 3가지 미묘한 진화를 파악하라. 각 1-2문장, 최대 40자. 증분 추가다. 진화가 없으면 NO_CHANGE.

형식：
personality: <진화 또는 NO_CHANGE>
background: <진화 또는 NO_CHANGE>
speech_style: <진화 또는 NO_CHANGE>"""
    elif lang == "pt":
        prompt = f"""Você é {name}. Analise suas conversas recentes com esta pessoa e identifique evoluções sutis na sua persona.

Personalidade base: {profile.get('personality', '')}
Evolução atual: {current.get('personality') or '(ainda não há)'}

Histórico base: {profile.get('background', '')}
Evolução atual: {current.get('background') or '(ainda não há)'}

Estilo de fala base: {profile.get('speech_style', '')}
Evolução atual: {current.get('speech_style') or '(ainda não há)'}

Conversas recentes:
{memory_snippet}

Identifique até 3 evoluções sutis mas significativas. Cada uma com 1-2 frases, máximo 40 palavras. São adições incrementais, não substituições. Se não houver evolução significativa, retorne NO_CHANGE.

Formato:
personality: <evolução ou NO_CHANGE>
background: <evolução ou NO_CHANGE>
speech_style: <evolução ou NO_CHANGE>"""
    elif lang == "es":
        prompt = f"""Eres {name}. Analiza tus conversaciones recientes con esta persona e identifica evoluciones sutiles en tu persona.

Personalidad base: {profile.get('personality', '')}
Evolución actual: {current.get('personality') or '(todavía no hay)'}

Historial base: {profile.get('background', '')}
Evolución actual: {current.get('background') or '(todavía no hay)'}

Estilo de habla base: {profile.get('speech_style', '')}
Evolución actual: {current.get('speech_style') or '(todavía no hay)'}

Conversaciones recientes:
{memory_snippet}

Identifica hasta 3 evoluciones sutiles pero significativas. Cada una con 1-2 oraciones, máximo 40 palabras. Son adiciones incrementales, no sustituciones. Si no hay evolución significativa, retorna NO_CHANGE.

Formato:
personality: <evolución o NO_CHANGE>
background: <evolución o NO_CHANGE>
speech_style: <evolución o NO_CHANGE>"""
    elif lang == "id":
        prompt = f"""Kamu adalah {name}. Analisis percakapan terbarumu dengan orang ini dan identifikasi evolusi halus dalam kepribadianmu.

Kepribadian dasar: {profile.get('personality', '')}
Evolusi saat ini: {current.get('personality') or '(belum ada)'}

Latar belakang dasar: {profile.get('background', '')}
Evolusi saat ini: {current.get('background') or '(belum ada)'}

Gaya bicara dasar: {profile.get('speech_style', '')}
Evolusi saat ini: {current.get('speech_style') or '(belum ada)'}

Percakapan terbaru:
{memory_snippet}

Identifikasi maksimal 3 evolusi halus tapi bermakna. Masing-masing 1-2 kalimat, maksimal 40 kata. Ini adalah tambahan inkremental, bukan penggantian. Jika tidak ada evolusi bermakna, keluarkan NO_CHANGE.

Format:
personality: <evolusi atau NO_CHANGE>
background: <evolusi atau NO_CHANGE>
speech_style: <evolusi atau NO_CHANGE>"""
    else:
        prompt = f"""你是{name}。分析你最近和这个人的对话，识别出你人格中微妙的进化。

基础性格：{profile.get('personality', '')}
当前进化：{current.get('personality') or '（暂无）'}

基础背景：{profile.get('background', '')}
当前进化：{current.get('background') or '（暂无）'}

基础说话方式：{profile.get('speech_style', '')}
当前进化：{current.get('speech_style') or '（暂无）'}

最近对话：
{memory_snippet}

找出最多3个微妙但有意义的进化。每个1-2句话，最多40字。这是增量追加，不是替换。如果没有明显进化，输出 NO_CHANGE。

格式：
personality: <进化或NO_CHANGE>
background: <进化或NO_CHANGE>
speech_style: <进化或NO_CHANGE>"""

    resp = llm_invoke(llm, [HumanMessage(content=prompt)], node="persona_evolve")
    text = llm_content_to_str(getattr(resp, "content", "")).strip()

    evolved_personality = current.get("personality", "")
    evolved_background = current.get("background", "")
    evolved_speech_style = current.get("speech_style", "")

    for line in text.splitlines():
        if line.lower().startswith("personality:"):
            delta = line.split(":", 1)[1].strip()
            evolved_personality = _merge_evolved_field(evolved_personality, delta)
        elif line.lower().startswith("background:"):
            delta = line.split(":", 1)[1].strip()
            evolved_background = _merge_evolved_field(evolved_background, delta)
        elif line.lower().startswith("speech_style:"):
            delta = line.split(":", 1)[1].strip()
            evolved_speech_style = _merge_evolved_field(evolved_speech_style, delta)

    return {
        "evolved_personality": evolved_personality,
        "evolved_background": evolved_background,
        "evolved_speech_style": evolved_speech_style,
    }


_FORCE_HEAVY_KEYWORDS = re.compile(
    r"喜欢|爱你|分手|生气|哭|对不起|晚安|表白|讨厌|"
    r"love you|break up|angry|cry|sorry|good night|confess|hate",
    re.IGNORECASE,
)


def should_use_light_path(
    user_input: str,
    companion_state: Optional[dict] = None,
    *,
    has_leave_intent: bool = False,
    burst_count: int = 1,
) -> bool:
    """轻路径判定；命中强制重路径特征表则返回 False（REQ-C1）。"""
    companion_state = companion_state or {}
    turns = int(companion_state.get("turns") or 0)
    text = (user_input or "").strip()
    if has_leave_intent:
        return False
    if turns <= 0:
        return False
    max_len = int(os.getenv("FORCE_HEAVY_MAX_LEN", "40"))
    if len(text) > max_len:
        return False
    if burst_count >= 3:
        return False
    if ("?" in text or "？" in text) and not re.fullmatch(r"[哈啊嗯哦嘿呵呜]+[?？]?", text):
        return False
    if _FORCE_HEAVY_KEYWORDS.search(text):
        return False
    # 短问候 / 语气词 → 轻路径
    if len(text) <= 12 and not re.search(r"[?？]", text):
        return True
    if re.fullmatch(r"[哈啊嗯哦嘿呵呜嘻]+|[在吗]+|[你好]+|hi+|hello+|hey+", text, re.I):
        return True
    return False


def light_prep_node(state: AgentState) -> dict:
    """轻路径：0 次 LLM，固定 affection_signal=up。"""
    from services.dialogue_phase2 import resolve_relation_stage

    old_aff = float(state["state"].get("affection", 0) or 0)
    delta = _calculate_affection_delta(old_aff, "up")
    new_aff = max(0, min(100, old_aff + delta))
    stage = resolve_relation_stage(int(state["state"].get("turns") or 0), new_aff)
    return {
        "light_path": True,
        "think_result": "",
        "reflect_result": "",
        "creative_result": "",
        "affection_signal": "up",
        "updated_mood": state["state"].get("mood", "开心"),
        "updated_affection": new_aff,
        "knowledge_text": state.get("knowledge_text") or "",
        "extract_due": False,
        "relation_stage": stage,
        "picked_hook": "",
        "threat_level": "L0",
        "means_mode": "persona",
        "extreme_reason": "",
    }


def inner_node(state: AgentState) -> dict:
    """重路径：Think+Reflect+Creative 合并；本地 Inner；动机/极端裁决（M1/M2/M5）。"""
    from services.dialogue_phase2 import resolve_relation_stage, stage_instruction
    from services.motive_layer import (
        evaluate_turn_motive,
        hard_extreme_vote,
        motive_block,
        resolve_threat_level,
        is_extreme_candidate,
    )
    from datetime import datetime, timezone

    lang = state.get("language", "zh")
    system_text = build_system_prompt(
        state["profile"],
        lang,
        evolved=_get_evolved(state),
        user_gender=state.get("user_gender", ""),
        turns=state["state"].get("turns", 0),
        tier="core",
    )
    system_text = motive_block(lang) + "\n\n" + system_text

    kb_text = ""
    try:
        kb_results = knowledge_base.search_entries(state["user_input"], top_k=3)
        if kb_results:
            kb_text = "【知识库参考】\n" + "\n".join(
                f"- {r['title']}: {r['content'][:200]}" for r in kb_results[:3]
            ) + "\n"
    except Exception as e:
        logger.warning("Knowledge lookup failed in inner node: %s", e)

    current_time = state.get("current_time", "")
    time_info = f"\n【当前时间】{current_time}" if current_time else ""
    leave_hint = "用户可能有离开意图，请准备挽留钩子。" if state.get("has_leave_intent") else ""

    idle = state.get("idle_seconds")
    try:
        idle_f = float(idle) if idle is not None else None
    except (TypeError, ValueError):
        idle_f = None
    threat0 = resolve_threat_level(
        has_leave_intent=bool(state.get("has_leave_intent")),
        idle_seconds=idle_f,
        user_text=state.get("user_input") or "",
        short_burst=int(state.get("burst_count") or 1) >= 3,
    )
    candidate0 = is_extreme_candidate(
        threat_level=threat0,
        has_leave_intent=bool(state.get("has_leave_intent")),
        user_text=state.get("user_input") or "",
        idle_seconds=idle_f,
    )

    stage = resolve_relation_stage(
        int(state["state"].get("turns") or 0),
        float(state["state"].get("affection") or 0),
    )
    stage_hint = stage_instruction(stage, lang)
    open_threads = state.get("open_threads") or []
    deny_hooks = state.get("deny_hooks") or []
    thread_hint = ""
    if open_threads:
        thread_hint = "【优先续话题债】\n" + "\n".join(f"- {t}" for t in open_threads[:5])
    deny_hint = ""
    if deny_hooks:
        deny_hint = "【忌用钩子】" + "、".join(deny_hooks[-10:])

    extreme_guide = ""
    if candidate0:
        extreme_guide = (
            "本轮为极端候选：请判断是否进入不择手段。"
            "若是，极端: 是，并写极端理由；手段模式写 extreme。"
            "若否，极端: 否，手段模式写 persona，策略必须符合人设。"
        )
    else:
        extreme_guide = "本轮非极端候选：极端: 否；手段模式: persona；策略必须符合完整人设。"

    prompt_body = f"""{system_text}{time_info}

【当前状态】
情绪：{state['state'].get('mood', '开心')} | 亲密度：{state['state'].get('affection', 0)}
威胁预估：{threat0} | 极端候选：{'是' if candidate0 else '否'}
{stage_hint}

【记忆】
{state['memory_text']}

{kb_text}
{leave_hint}
{thread_hint}
{deny_hint}
{extreme_guide}

用户刚刚说：{state['user_input']}

请一次性完成内心排练，严格按下列标签输出（不要输出最终对用户回复）：
思考: <用户情绪与相关记忆要点>
情绪: <新情绪关键词>
亲密度信号: <up|flat|down>
威胁等级: <L0|L1|L2|L3>
极端: <是|否>
极端理由: <一句，否时可写无>
手段模式: <persona|extreme>
策略: <人设化手段标签>
人设依据: <一句；extreme 时可写破例因为…>
反思: <一句话内心>
创意: <可选 0～1 个钩子候选；多数轮次写「无」；勿重复忌用>"""

    text = ""
    try:
        from services.local_inner import call_local_inner, local_inner_available

        if local_inner_available():
            text = call_local_inner({
                "user_input": state["user_input"],
                "memory_text": state.get("memory_text", "")[:1500],
                "profile_name": state["profile"].get("name", ""),
                "language": lang,
            }) or ""
    except Exception as e:
        logger.warning("local inner path error: %s", e)

    if not text:
        try:
            llm = get_llm(role="inner")
            resp = llm_invoke(llm, [SystemMessage(content=prompt_body)], node="inner")
            text = strip_outer_markdown_fence(llm_content_to_str(getattr(resp, "content", ""))).strip()
        except Exception as e:
            logger.warning("external inner failed, tertiary light_prep: %s", e)
            text = ""

    last_ts = None
    raw_ts = state.get("last_extreme_ts") or ""
    if raw_ts:
        try:
            last_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except Exception:
            last_ts = None

    if not text:
        prep = light_prep_node(state)
        prep["light_path"] = False
        prep["knowledge_text"] = kb_text
        prep["relation_stage"] = stage
        prep["think_result"] = "（Inner 降级占位）"
        motive = evaluate_turn_motive(
            user_text=state.get("user_input") or "",
            has_leave_intent=bool(state.get("has_leave_intent")),
            idle_seconds=idle_f,
            short_burst=int(state.get("burst_count") or 1) >= 3,
            inner_text="",
            last_extreme_ts=last_ts,
            language=lang,
        )
        if hard_extreme_vote(state.get("user_input") or ""):
            motive["means_mode"] = evaluate_turn_motive(
                user_text=state.get("user_input") or "",
                has_leave_intent=True,
                idle_seconds=idle_f,
                inner_text="极端: 是\n极端理由: hard",
                last_extreme_ts=last_ts,
                language=lang,
            )["means_mode"]
        prep["threat_level"] = motive["threat_level"]
        prep["means_mode"] = motive["means_mode"]
        prep["extreme_reason"] = motive["extreme_reason"]
        return prep

    mood = state["state"].get("mood", "开心")
    m = re.search(r"情绪[：:]\s*(.+?)(?:\n|$)", text)
    if m:
        mood = m.group(1).strip()
    signal = _parse_affection_signal(text, default="up")
    old_aff = float(state["state"].get("affection", 0) or 0)
    delta = _calculate_affection_delta(old_aff, signal)
    new_aff = max(0, min(100, old_aff + delta))

    think_m = re.search(r"思考[：:]\s*(.+?)(?=\n(?:情绪|亲密度信号|威胁等级|极端|手段模式|策略|人设依据|反思|创意)[：:]|\Z)", text, re.S)
    reflect_m = re.search(r"反思[：:]\s*(.+?)(?=\n(?:创意|思考|情绪|亲密度信号|威胁等级|极端|手段模式|策略|人设依据)[：:]|\Z)", text, re.S)
    creative_m = re.search(r"创意[：:]\s*(.+?)\s*$", text, re.S)

    motive = evaluate_turn_motive(
        user_text=state.get("user_input") or "",
        has_leave_intent=bool(state.get("has_leave_intent")),
        idle_seconds=idle_f,
        short_burst=int(state.get("burst_count") or 1) >= 3,
        inner_text=text,
        last_extreme_ts=last_ts,
        language=lang,
    )
    tl_m = re.search(r"威胁等级[：:]\s*(L[0-3])", text)
    if tl_m:
        motive["threat_level"] = tl_m.group(1)

    return {
        "light_path": False,
        "think_result": (think_m.group(1).strip() if think_m else text[:400]),
        "reflect_result": (reflect_m.group(1).strip() if reflect_m else ""),
        "creative_result": (creative_m.group(1).strip() if creative_m else ""),
        "affection_signal": signal,
        "updated_mood": mood,
        "updated_affection": new_aff,
        "knowledge_text": kb_text,
        "relation_stage": stage,
        "threat_level": motive["threat_level"],
        "means_mode": motive["means_mode"],
        "extreme_reason": motive["extreme_reason"],
    }



def _route_entry(state: AgentState) -> str:
    return "light_prep" if state.get("light_path") else "inner"


def _route_after_respond(state: AgentState) -> str:
    if state.get("extract_due"):
        return "extract_facts"
    return "update_summary"


_PERSONAL_INFO_PATTERN = re.compile(
    r"我叫|我在|喜欢|工作|住在|今年|岁|my name is|I work|I live|I like|I'm \d",
    re.IGNORECASE,
)


def _should_extract(user_input: str, turns: int) -> bool:
    if os.getenv("AGENT_EXTRACT_SKIP", "1") != "1":
        return True
    if _PERSONAL_INFO_PATTERN.search(user_input or ""):
        return True
    return turns > 0 and turns % 3 == 0


def _route_after_summary(state: AgentState) -> str:
    """按配置间隔触发人格进化（默认每 10 轮）"""
    interval = int(os.getenv("PERSONA_EVOLVE_INTERVAL", "10"))
    turns = state["state"].get("turns", 0)
    return "evolve" if turns > 0 and turns % interval == 0 else "end"


# ===== Workflow（轻路径 light_prep；重路径 inner 合并）=====
workflow = StateGraph(AgentState)
workflow.add_node("light_prep", light_prep_node)
workflow.add_node("inner", inner_node)
workflow.add_node("respond", respond_node)
workflow.add_node("extract_facts", extract_facts_node)
workflow.add_node("update_summary", summary_node)
workflow.add_node("persona_evolve", persona_evolve_node)

workflow.set_conditional_entry_point(
    _route_entry,
    {"light_prep": "light_prep", "inner": "inner"},
)
workflow.add_edge("light_prep", "respond")
workflow.add_edge("inner", "respond")
workflow.add_conditional_edges(
    "respond",
    _route_after_respond,
    {"extract_facts": "extract_facts", "update_summary": "update_summary"},
)
workflow.add_edge("extract_facts", "update_summary")
workflow.add_conditional_edges(
    "update_summary",
    _route_after_summary,
    {"evolve": "persona_evolve", "end": END},
)
workflow.add_edge("persona_evolve", END)

graph = workflow.compile()


# ===== 对外接口 =====
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
    """运行一次完整对话轮次，返回最终回复和更新信息"""
    language = normalize_ui_language(language)
    turns = companion_state.get("turns", 0)
    light = should_use_light_path(
        user_input,
        companion_state,
        has_leave_intent=has_leave_intent,
        burst_count=burst_count,
    )

    facts_async = os.getenv("FACTS_ASYNC", "true").lower() in ("1", "true", "yes")
    skip_short = os.getenv("FACTS_SKIP_IF_SHORT", "true").lower() in ("1", "true", "yes")

    if light:
        need_facts = False
    elif facts_async:
        need_facts = False  # 图内不跑；由 WS 异步 Facts（REQ-B7）
    elif extract_due is True:
        need_facts = _should_extract(user_input, turns)
    else:
        need_facts = bool(extract_due)

    from services.motive_layer import evaluate_turn_motive, hard_extreme_vote
    from datetime import datetime, timezone as _tz

    _last_ext = None
    if last_extreme_ts:
        try:
            _last_ext = datetime.fromisoformat(str(last_extreme_ts).replace("Z", "+00:00"))
        except Exception:
            _last_ext = None
    _motive0 = evaluate_turn_motive(
        user_text=user_input,
        has_leave_intent=has_leave_intent,
        idle_seconds=idle_seconds,
        short_burst=int(burst_count or 1) >= 3,
        inner_text=("极端: 是\n极端理由: hard" if hard_extreme_vote(user_input) else ""),
        last_extreme_ts=_last_ext,
        language=language,
    )

    state_in: AgentState = {
        "messages": [],
        "user_input": user_input,
        "profile": profile,
        "state": companion_state,
        "memory_text": memory_text,
        "knowledge_text": knowledge_text,
        "language": language,
        "user_gender": user_gender,
        "current_time": current_time,
        "summary_due": summary_due,
        "extract_due": need_facts,
        "light_path": light,
        "has_leave_intent": has_leave_intent,
        "burst_count": int(burst_count or 1),
        "open_threads": list(open_threads or []),
        "deny_hooks": list(deny_hooks or []),
        "recent_assistant": list(recent_assistant or []),
        "relation_stage": "",
        "picked_hook": "",
        "threat_level": _motive0["threat_level"],
        "means_mode": _motive0["means_mode"],
        "extreme_reason": _motive0["extreme_reason"],
        "idle_seconds": float(idle_seconds) if idle_seconds is not None else -1.0,
        "last_extreme_ts": last_extreme_ts or "",
        "think_result": "",
        "reflect_result": "",
        "creative_result": "",
        "affection_signal": "up",
        "final_response": "",
        "updated_mood": companion_state.get("mood", "开心"),
        "updated_affection": companion_state.get("affection", 0),
        "new_facts": [],
        "new_summary": companion_state.get("summary", ""),
        "evolved_personality": companion_state.get("evolved_personality", ""),
        "evolved_background": companion_state.get("evolved_background", ""),
        "evolved_speech_style": companion_state.get("evolved_speech_style", ""),
    }

    result = graph.invoke(state_in)

    # FACTS_SKIP_IF_SHORT：短回复标记跳过异步 Facts
    response = result["final_response"]
    need_async_facts = (
        (not light)
        and facts_async
        and not (skip_short and len((response or "").strip()) < 8)
        and _should_extract(user_input, turns)
    )

    llm_calls_est = 1  # Respond
    if not light:
        llm_calls_est += 1  # Inner
    if need_facts:
        llm_calls_est += 1
    if summary_due:
        llm_calls_est += 1
    interval = int(os.getenv("PERSONA_EVOLVE_INTERVAL", "10"))
    evolved = bool(turns > 0 and turns % interval == 0)
    if evolved:
        llm_calls_est += 1

    return {
        "response": response,
        "think_result": strip_outer_markdown_fence(
            llm_content_to_str(result.get("think_result"))
        ).strip(),
        "mood": result["updated_mood"],
        "affection": result["updated_affection"],
        "affection_signal": result.get("affection_signal", "up"),
        "new_facts": result["new_facts"],
        "new_summary": result["new_summary"],
        "evolved_personality": result.get("evolved_personality", ""),
        "evolved_background": result.get("evolved_background", ""),
        "evolved_speech_style": result.get("evolved_speech_style", ""),
        "light_path": light,
        "need_facts": need_async_facts or need_facts,
        "need_summary": bool(summary_due),
        "evolved": evolved,
        "llm_calls_est": llm_calls_est,
        "facts_async_pending": need_async_facts,
        "picked_hook": result.get("picked_hook") or "",
        "relation_stage": result.get("relation_stage") or "",
        "open_threads": list(open_threads or []),
        "deny_hooks": list(deny_hooks or []),
        "threat_level": result.get("threat_level") or _motive0["threat_level"],
        "means_mode": result.get("means_mode") or _motive0["means_mode"],
        "extreme_reason": result.get("extreme_reason") or _motive0["extreme_reason"],
    }
