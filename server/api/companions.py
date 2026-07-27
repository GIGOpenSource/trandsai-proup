import asyncio
import functools
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from langchain_core.messages import SystemMessage
from starlette.websockets import WebSocketState

from api.auth import verify_user_token
from core.concurrency import agent_semaphore, heavy_load_ratio, user_turn_lock
from core.agent_queue import AgentJob, enqueue_agent_job, new_job_id, queue_enabled, wait_agent_result
from core.database import UserORM, get_db
from core.executor import AGENT_POOL
from core.rate_limit import check_chat_rate_limit
from core.rest_async import run_rest
from core.config import (
    _AGENT_TIMEOUT_MESSAGE,
    _DUPLICATE_USER_MESSAGE,
    _MULTI_MESSAGE_AGENT_PREFIX,
    _QUEUE_COALESCED_MESSAGE,
    detect_leave_intent,
    split_response,
)
from core.state import get_companion_manager, get_session, set_session
from services.agent_utils import build_dialogue_time_context, build_system_prompt, get_content_restriction
from services.agent_runner import run_agent, run_memory_update_async
from services.agent import get_llm
from services.llm.client import llm_invoke
from services.culture_data import get_cultural_context_for_city, default_cultural_values, infer_language_from_city
from services.persona_axes import axes_prompt_block, resolve_axes_from_payload, sample_persona_axes
from services.region_catalog import find_region_for_city
from services.memory import normalize_message_text_for_dedup
from services.image_generation import generate_avatar_prompt, generate_image_with_cache
from core.i18n import (
    _LLM_WAIT_FILLER,
    normalize_ui_language,
    _WS_ACCESS_DENIED,
    _WS_AUTH_FAILED,
    _WS_CHAT_UNEXPECTED_ERROR,
    _WS_COMPANION_NOT_FOUND,
)
from services.async_tasks import start_avatar_generation
from services.companion_manager import hydrate_user_affection_turns
from services.relation_card import (
    RELATION_CARD_ENABLED,
    RELATION_CARD_FALLBACK_FULL_MEMORY,
    is_card_empty,
    load_card,
    merge_facts,
    render_card,
    save_card,
    touch_state,
)
from services.dialogue_phase2 import (
    merge_open_threads,
    proactive_should_send,
    push_deny_fingerprints,
    push_deny_hook,
    resolve_relation_stage,
    seed_deny_from_recent,
)
from services.archive_queue import enqueue_archive_job, archive_queue_enabled
from services.archive_process import process_archive_job
import time as _time_mod

router = APIRouter()
logger = logging.getLogger(__name__)
_chat_turn_logger = logging.getLogger("chat.turn")


def _companion_pronoun(companion) -> str:
    g = (getattr(companion.profile, "gender", None) or "").strip().lower()
    if g in ("male", "男", "m"):
        return "他"
    if g in ("female", "女", "f"):
        return "她"
    return "TA"


def _log_chat_turn(payload: dict) -> None:
    try:
        _chat_turn_logger.info("%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


# 单次从 WS 队列合并处理的用户消息条数上限：避免一次合并过多导致模型/记忆语义「串」、且 empty() 在竞态下不可靠
_WS_BURST_MAX_MESSAGES = 10

# 空闲 WS 应用层心跳：须明显小于 nginx proxy_read_timeout（现为 300s）
_WS_IDLE_HEARTBEAT_MIN_S = float(os.getenv("WS_IDLE_HEARTBEAT_MIN_S", "25") or 25)
_WS_IDLE_HEARTBEAT_MAX_S = float(os.getenv("WS_IDLE_HEARTBEAT_MAX_S", "45") or 45)
_WS_PROACTIVE_SLEEP_CHUNK_S = float(os.getenv("WS_PROACTIVE_SLEEP_CHUNK_S", "30") or 30)
# 发出 ping 后等待 pong 的上限；超时视为僵死连接并主动关闭
_WS_PONG_TIMEOUT_S = float(os.getenv("WS_PONG_TIMEOUT_S", "90") or 90)
# 后台清扫间隔：扫描无 pong 应答的注册连接
_WS_ZOMBIE_SWEEP_S = float(os.getenv("WS_ZOMBIE_SWEEP_S", "30") or 30)

_DELIVERY_DELAY_FACTOR = float(os.getenv("DELIVERY_DELAY_FACTOR", "0.6"))

# connection_id -> WebSocket；供僵尸清扫主动 close，释放协程与连接槽位
_ws_live_registry: Dict[str, WebSocket] = {}
_ws_janitor_lock = asyncio.Lock()
_ws_janitor_task: Optional[asyncio.Task] = None


def _ws_heartbeat_interval() -> float:
    lo = max(5.0, min(_WS_IDLE_HEARTBEAT_MIN_S, _WS_IDLE_HEARTBEAT_MAX_S))
    hi = max(lo, _WS_IDLE_HEARTBEAT_MAX_S)
    return random.uniform(lo, hi)


def _ws_mark_alive(websocket: WebSocket) -> None:
    """任意入站帧（含 pong / 业务消息）刷新存活时间。"""
    now = time.monotonic()
    setattr(websocket, "_trandsai_last_pong_at", now)
    setattr(websocket, "_trandsai_awaiting_pong", False)


def _ws_note_ping_sent(websocket: WebSocket) -> None:
    now = time.monotonic()
    setattr(websocket, "_trandsai_ping_sent_at", now)
    setattr(websocket, "_trandsai_awaiting_pong", True)


def _ws_is_zombie(websocket: WebSocket) -> bool:
    """已发 ping 且超过 WS_PONG_TIMEOUT_S 仍无 pong/入站 → 僵死。"""
    if not getattr(websocket, "_trandsai_awaiting_pong", False):
        return False
    ping_at = float(getattr(websocket, "_trandsai_ping_sent_at", 0) or 0)
    if ping_at <= 0:
        return False
    return (time.monotonic() - ping_at) >= max(30.0, _WS_PONG_TIMEOUT_S)


async def _ws_force_close(websocket: WebSocket, *, reason: str = "zombie") -> None:
    """主动关闭失效 WS，释放 Nginx/Uvicorn 连接槽。"""
    try:
        logger.info("WS force-close (%s) state=%s", reason, getattr(websocket, "client_state", None))
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1001)
    except Exception as e:
        logger.debug("WS force-close ignored: %s", e)


def _ws_register(connection_id: str, websocket: WebSocket) -> None:
    _ws_live_registry[connection_id] = websocket
    now = time.monotonic()
    setattr(websocket, "_trandsai_connection_id", connection_id)
    setattr(websocket, "_trandsai_last_pong_at", now)
    setattr(websocket, "_trandsai_awaiting_pong", False)
    setattr(websocket, "_trandsai_ping_sent_at", 0.0)


def _ws_unregister(connection_id: str) -> None:
    _ws_live_registry.pop(connection_id, None)


async def _ws_janitor_loop() -> None:
    """定时扫描无 pong 的僵死连接并 close。"""
    while True:
        try:
            await asyncio.sleep(max(10.0, _WS_ZOMBIE_SWEEP_S))
            dead: List[str] = []
            for cid, ws in list(_ws_live_registry.items()):
                try:
                    if ws.client_state != WebSocketState.CONNECTED:
                        dead.append(cid)
                        continue
                    if _ws_is_zombie(ws):
                        await _ws_force_close(ws, reason="janitor-no-pong")
                        dead.append(cid)
                except Exception:
                    dead.append(cid)
            for cid in dead:
                _ws_unregister(cid)
            if dead:
                logger.warning(
                    "WS janitor closed %s zombie connection(s); live=%s",
                    len(dead),
                    len(_ws_live_registry),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WS janitor loop error")


async def _ensure_ws_janitor() -> None:
    global _ws_janitor_task
    async with _ws_janitor_lock:
        if _ws_janitor_task is not None and not _ws_janitor_task.done():
            return
        _ws_janitor_task = asyncio.create_task(_ws_janitor_loop(), name="ws-zombie-janitor")


async def _ws_send_json(websocket: WebSocket, payload: dict) -> bool:
    """串行发送 JSON 帧（连接级 Lock），避免多协程并发 send_text 打坏连接。"""
    lock = getattr(websocket, "_trandsai_send_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(websocket, "_trandsai_send_lock", lock)
    try:
        async with lock:
            if websocket.client_state != WebSocketState.CONNECTED:
                return False
            await websocket.send_text(json.dumps(payload))
            return True
    except Exception:
        return False


async def _ws_send_ping_or_close(websocket: WebSocket) -> bool:
    """
    空闲心跳：若上一轮 ping 已超时无 pong，则关闭僵死连接并返回 False；
    否则下发 ping 并标记等待 pong。返回 False 表示应结束主循环。
    """
    if _ws_is_zombie(websocket):
        await _ws_force_close(websocket, reason="missed-pong")
        return False
    if not await _ws_send_json(websocket, {"type": "ping"}):
        return False
    _ws_note_ping_sent(websocket)
    return True


async def _sleep_with_ws_keepalive(
    websocket: WebSocket, total_seconds: float, *, chunk_seconds: Optional[float] = None
) -> bool:
    """
    长等待切成短片断。片间不再额外 ping（主循环空闲心跳已覆盖），仅检测连接仍可用。
    返回 False 表示连接已不可用。
    """
    remaining = max(0.0, float(total_seconds))
    chunk = float(chunk_seconds if chunk_seconds is not None else _WS_PROACTIVE_SLEEP_CHUNK_S)
    chunk = max(5.0, chunk)
    while remaining > 0:
        step = min(chunk, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if remaining <= 0:
            break
        if websocket.client_state != WebSocketState.CONNECTED:
            return False
        if _ws_is_zombie(websocket):
            await _ws_force_close(websocket, reason="proactive-missed-pong")
            return False
    return websocket.client_state == WebSocketState.CONNECTED


_AGENT_BUSY_MESSAGE = {
    "zh": "现在有点忙，稍后再聊～",
    "en": "We're busy right now — please try again in a moment.",
    "ja": "現在混雑しています。少し待ってからもう一度お試しください。",
    "ko": "지금 상담이 많습니다. 잠시 후 다시 시도해 주세요.",
}

# 带括号「思考」时，仅约本概率的轮次向客户端发送 💭 toast（约每 10 次用户-AI 交互 1～2 次展示）
_THINK_TOAST_UI_PROBABILITY = 0.15

# 极短用户文不参与「重复发送」去重，避免「在吗」连发被拦
_DUPLICATE_CHECK_MIN_LEN = 4

# 半角 () 与全角（）：整段匹配，用于在全文上按顺序切出「思考块」与「正文块」
_BRACKET_BLOCK_PATTERN = re.compile(r"\([^)]*\)|（[^）]*）")
# 去掉括号块（供正文条内二次清理）
_BRACKET_STRIP_PATTERN = re.compile(r"\([^)]*\)|（[^）]*）")
_THINK_INNER_MAX_RAW = 6000
_THINK_TOAST_WS_MAX = 2400

# 混合正文里「句末标点 + 空白 + 下一句（中文或左引号/左括号起头）」视作换条
_SOFT_BREAK_AFTER_CJK_SENTENCE = re.compile(
    r"(?<=[。？！])(?:[ \t\u00a0\u3000]+)(?=[\u4e00-\u9fff（「『《])"
)
# 句末标点后无空白、直接接下一句中文时，也换条（避免「…是吧？先进来」整段一条气泡）
_TIGHT_BREAK_AFTER_CJK_SENTENCE = re.compile(
    r"(?<=[。？！])(?=[\u4e00-\u9fff（「『《])"
)


def _burst_mood_from_user_text(s: str) -> str:
    """分条节奏：激动连发 / 平静长段 / 普通（影响 max_chars 与条间暂停）。"""
    t = (s or "").strip()
    if not t:
        return "neutral"
    ex = t.count("!") + t.count("！")
    if ex >= 2 or "哈哈" in t or "哈哈哈" in t or "ww" in t.lower():
        return "excited"
    if t.count("？") + t.count("?") >= 4:
        return "excited"
    if len(t) > 120 and t.count("。") >= 3 and ex == 0:
        return "calm"
    return "neutral"


def _inject_soft_linebreaks_for_mixed_messages(s: str) -> str:
    """少切气泡：仅在「句末 + 空白 + 下一句」时换行；不再对紧挨着的句号后强制切分。

    REQ-A7：无条间 sleep；减少机枪式短气泡。
    """
    if not (s or "").strip():
        return s or ""
    # 仅保留有空白的软换行；去掉 tight break（句号后立刻切条）
    s = _SOFT_BREAK_AFTER_CJK_SENTENCE.sub("\n", s)
    return s


def _split_thinks_and_text(s: str) -> list[tuple[str, str]]:
    """从左到右扫描整段回复，按出现顺序产出 ('think', …) / ('text', …)；与叙事时间线一致，勿重排。"""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _BRACKET_BLOCK_PATTERN.finditer(s):
        if m.start() > pos:
            chunk = s[pos : m.start()]
            if chunk.strip():
                out.append(("text", chunk))
        raw = m.group(0)
        if raw.startswith("(") and raw.endswith(")"):
            inner = raw[1:-1].strip()
        elif raw.startswith("（") and raw.endswith("）"):
            inner = raw[1:-1].strip()
        else:
            inner = raw.strip()
        if inner:
            out.append(("think", inner[:_THINK_INNER_MAX_RAW]))
        pos = m.end()
    if pos < len(s):
        tail = s[pos:]
        if tail.strip():
            out.append(("text", tail))
    return out


async def _send_plain_assistant_bubble(
    websocket: WebSocket, companion, display_text: str
) -> str:
    """仅发送正文气泡（已不含括号）。与上条去重后相同则一般不发；极短句或约 12% 长句仍发出（拟人「嘴笨重复」）。"""
    display_text = (display_text or "").strip()
    if not display_text:
        return ""
    skip = False
    if companion is not None:
        last_a = companion.memory.short_term.get_last_assistant_content()
        if last_a is not None:
            n_new = normalize_message_text_for_dedup(display_text)
            n_last = normalize_message_text_for_dedup(last_a)
            if n_new == n_last:
                if len(n_new) < 10:
                    skip = False
                else:
                    skip = random.random() < 0.88
    if not skip:
        ok = await _ws_send_json(
            websocket,
            {"type": "message", "role": "assistant", "text": display_text},
        )
        if not ok:
            return ""
    return "" if skip else display_text


async def _keepalive_during_agent(websocket: WebSocket, agent_task, language: str) -> None:
    """长推理时仅续传 typing（REQ-A7：无 filler）。"""
    n = 0
    while not agent_task.done():
        n += 1
        if not await _ws_send_json(websocket, {"type": "typing"}):
            return
        try:
            wait = 1.85 if n < 4 else 2.35
            await asyncio.wait_for(asyncio.shield(agent_task), timeout=wait)
            return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if agent_task.done():
                return
        except Exception:
            return


async def _deliver_assistant_content(
    websocket: WebSocket,
    companion,
    response_text: str,
    *,
    message_queue: Optional[asyncio.Queue] = None,
    show_think_toasts: bool = False,
    delivery_mood: str = "neutral",
) -> Tuple[List[str], bool]:
    """严格按 `_split_thinks_and_text` 的顺序下发：每条 think 可发 toast（由 show_think_toasts 控制频次），再处理紧随的正文块；
    正文块内先软换行再 split_response；分条节奏由 delivery_mood（激动/平静/普通）影响 max_chars 与条间是否打 typing、暂停长短。
    不再推送 LangGraph think 节点原文。返回 (已持久化的正文条列表, 是否被队列中断)。"""
    sent_segments: list[str] = []
    parts = _split_thinks_and_text(response_text or "")
    if not parts:
        return sent_segments, False

    bubble_idx = 0
    think_toast_sent = False  # 本轮若展示思考，仅发第一条 💭，避免同条回复内多条刷屏

    async def _interrupted() -> bool:
        return bool(message_queue is not None and not message_queue.empty())

    for kind, payload in parts:
        if await _interrupted():
            return sent_segments, True
        if kind == "think":
            if not show_think_toasts or think_toast_sent:
                continue
            inner = (payload or "").strip()
            if not inner:
                continue
            if len(inner) > _THINK_TOAST_WS_MAX:
                inner = inner[: _THINK_TOAST_WS_MAX - 1] + "…"
            try:
                if not await _ws_send_json(
                    websocket, {"type": "toast", "text": f"💭 {inner}"}
                ):
                    return sent_segments, True
            except Exception:
                return sent_segments, True
            think_toast_sent = True
            continue

        # 正文块：去残留括号 → 句间软换行 → 再按 split_response 分条（混合时拆多条消息）
        body = _BRACKET_STRIP_PATTERN.sub("", payload).strip()
        if not body:
            continue
        body = _inject_soft_linebreaks_for_mixed_messages(body)
        if delivery_mood == "excited":
            _mc = random.randint(160, 220)
        elif delivery_mood == "calm":
            _mc = random.randint(220, 320)
        else:
            _mc = random.randint(180, 260)
        subsegs = split_response(body, max_chars=_mc)
        for j, sub in enumerate(subsegs):
            if await _interrupted():
                return sent_segments, True
            sub_clean = _BRACKET_STRIP_PATTERN.sub("", (sub or "").strip()).strip()
            if not sub_clean:
                continue
            if bubble_idx > 0:
                # REQ-A7：无条间 sleep；仅可选 typing 提示
                skip_typing = False
                if delivery_mood == "excited":
                    skip_typing = random.random() < 0.48
                elif delivery_mood == "calm":
                    skip_typing = random.random() < 0.22
                else:
                    skip_typing = random.random() < 0.32
                if not skip_typing:
                    if not await _ws_send_json(websocket, {"type": "typing"}):
                        return sent_segments, True
                if await _interrupted():
                    return sent_segments, True
            stored = await _send_plain_assistant_bubble(websocket, companion, sub_clean)
            if stored:
                companion.memory.add_assistant_message(stored)
                sent_segments.append(stored)
            bubble_idx += 1

    return sent_segments, False


async def require_login_user(
    x_token: Optional[str] = Header(None, alias="x-token"),
) -> int:
    """REST 接口：必须携带有效用户 Token（与 WebSocket IM 一致）。"""
    uid = verify_user_token(x_token) if x_token else None
    if uid is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return uid


def _assert_companion_readable(companion, user_id: int) -> None:
    """登录用户可查看/聊天任意已存在智能体（业务：可见并互动别人创建的智能体）。"""
    if user_id is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")


def _assert_companion_owner(companion, user_id: int) -> None:
    """仅创建者可执行删除/重生头像等写操作。
    归属匹配 user_id 或唯一 username；不用 nickname（可撞车导致误授权）。
    """
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")
    cb = (companion.profile.created_by or "").strip()
    if not cb:
        raise HTTPException(status_code=403, detail="无权操作该智能体")
    if cb == str(user_id):
        return
    with get_db() as db:
        user = db.query(UserORM).filter(UserORM.id == user_id).first()
        if user:
            uname = (user.username or "").strip()
            if uname and cb == uname:
                return
    raise HTTPException(status_code=403, detail="无权操作该智能体")


# 兼容旧名：读路径语义改为「可读」
def _assert_companion_user_access(companion, user_id: int) -> None:
    _assert_companion_readable(companion, user_id)


# ===== 主动消息相关 =====

def _companion_city_str(companion) -> Optional[str]:
    try:
        c = (companion.profile.city or "").strip()
        return c or None
    except Exception:
        return None


def _build_proactive_prompt(companion, lang: str, time_context: str) -> str:
    """构建 AI 主动发消息的 prompt"""
    from services.motive_layer import motive_block

    lang = normalize_ui_language(lang)
    system_text = motive_block(lang) + "\n\n" + build_system_prompt(
        companion.profile.model_dump(), lang, turns=companion.state.turns
    )
    name = companion.profile.name
    mood = companion.state.mood
    affection = companion.state.affection
    recent = companion.memory.short_term.get_recent(6)
    recent_text = "\n".join(
        f"{'他' if m['role'] == 'user' else name}：{m['content']}"
        for m in recent
    )

    # 根据亲密度获取内容安全限制文本
    restriction_text = get_content_restriction(lang, affection)

    if lang == "en":
        return f"""{system_text}

【Current State】
Mood: {mood} | Affection: {affection}

【User local time & season】
{time_context}

【Recent Chat】
{recent_text}

They haven't replied for a while. As {name}, send a proactive message — check on them, be playful, or share a life fragment.
Requirements:
- Completely colloquial, like texting a lover
- 2-3 short sentences, each under 25 words
- Prefer a statement or share; ending with a question is optional — do not always ask
- Output the reply directly, no prefix

{restriction_text}"""

    if lang == "ja":
        return f"""{system_text}

【現在の状態】
感情：{mood} | 親密度：{affection}

【相手の現地・季節・時刻】
{time_context}

【最近のやり取り】
{recent_text}

相手がしばらく返信してこない。{name}として、率先してメッセージを送って——気遣ったり、甘えたり、生活の一片をシェアしたり。
要求：
- 完全に口語体、伙伴とのLINEのような感じ
- 2〜3文、それぞれ25字以内
- 最後は質問で締めなくてよい；陳述・共有で終わってOK（フックは任意）
- 直接返信内容を出力、前置き不要

{restriction_text}"""

    if lang == "ko":
        return f"""{system_text}

【현재 상태】
기분：{mood} | 친밀도：{affection}

【상대 현지·계절·시각】
{time_context}

【최근 대화】
{recent_text}

상대가 잠깐 동안 답장이 없어. {name}으로서, 능동적으로 메시지를 본내——챙겨주거나 애교 부리거나 일상의 조각을 공유하거나.
요구사항：
- 완전히 구어체, 연인이랑 카톡하는 느낌
- 2~3문장, 각각 25자 이내
- 마지막은 꼭 질문일 필요 없음; 서술/공유로 끝내도 됨（훅은 선택）
- 바로 답장 내용을 출력, 전치사 불필요

{restriction_text}"""

    if lang == "pt":
        return f"""{system_text}

【Estado Atual】
Humor: {mood} | Proximidade: {affection}

【Hora local e estação do usuário】
{time_context}

【Conversas Recentes】
{recent_text}

A pessoa não responde há um tempo. Como {name}, envie uma mensagem proativa — se preocupe, seja carinhosa, ou compartilhe um pedacinho da sua vida.
Requisitos:
- Completamente coloquial, como conversar com um namorado/namorada no WhatsApp
- 2-3 frases curtas, cada uma com menos de 25 palavras
- Prefira terminar com afirmação ou compartilhamento; pergunta é opcional — não pergunte sempre
- Saída direta do conteúdo da resposta, sem prefixo

{restriction_text}"""

    if lang == "es":
        return f"""{system_text}

【Estado Actual】
Ánimo: {mood} | Cercanía: {affection}

【Hora local y estación del usuario】
{time_context}

【Conversaciones Recientes】
{recent_text}

La persona no responde desde hace un rato. Como {name}, envía un mensaje proactivo — preocúpate, sé cariñoso/a, o comparte un pedacito de tu vida.
Requisitos:
- Completamente coloquial, como chatear con tu pareja
- 2-3 frases cortas, cada una con menos de 25 palabras
- Prefiere terminar con afirmación o un compartido; la pregunta es opcional — no preguntes siempre
- Salida directa del contenido de la respuesta, sin prefijo

{restriction_text}"""

    if lang == "id":
        return f"""{system_text}

【Status Saat Ini】
Suasana hati: {mood} | Kedekatan: {affection}

【Waktu lokal & musim pengguna】
{time_context}

【Obrolan Terbaru】
{recent_text}

Orang itu belum membalas sebentar. Sebagai {name}, kirim pesan proaktif — perhatikan, manja, atau bagikan cuplikan kehidupanmu.
Persyaratan:
- Sepenuhnya santai, seperti chat dengan pacar di WhatsApp
- 2-3 kalimat pendek, masing-masing kurang dari 25 kata
- Lebih baik diakhiri pernyataan/berbagi; pertanyaan opsional — jangan selalu bertanya
- Keluarkan langsung isi balasan, tanpa awalan

{restriction_text}"""

    # 默认中文
    return f"""{system_text}

【当前状态】
情绪：{mood} | 亲密度：{affection}

【用户当地时间与季节】
{time_context}

【最近对话】
{recent_text}

用户已经有一会儿没回消息了。请你作为{name}，主动发条消息关心一下、撒个娇、或者分享一个生活碎片。
要求：
- 完全口语化，像真实伙伴发微信
- 2-3句话，每句不超过25字
- 优先陈述/分享收尾；问句可选，不要每条都以提问结尾
- 直接输出回复内容，不要加任何前缀

{restriction_text}"""


async def _send_proactive_message(websocket: WebSocket, companion, lang: str, companion_id: str, user_id: int = None):
    """定时发送主动消息（C3：信息量门槛）。"""
    try:
        mult = float(os.getenv("PROACTIVE_LOAD_MULTIPLIER", "1.0") or 1.0)
        if heavy_load_ratio() > 0.8:
            mult = max(mult, 2.0)
        wait_s = random.uniform(90, 180) * mult
        # 长等待切段 + ping，避免 nginx/proxy 空闲超时
        if not await _sleep_with_ws_keepalive(websocket, wait_s):
            return

        # 检查连接是否仍然打开
        if websocket.client_state == WebSocketState.DISCONNECTED:
            return

        sess = get_session(companion_id, user_id=user_id) or {}
        uid = sess.get("user_id")
        card = {}
        if RELATION_CARD_ENABLED and isinstance(uid, int):
            card = load_card(uid, companion_id)
        last_ts = None
        raw_ts = sess.get("last_user_ts")
        if raw_ts:
            try:
                last_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            except Exception:
                last_ts = None
        ok, reason = proactive_should_send(
            card,
            affection=float(getattr(companion.state, "affection", 0) or 0),
            last_user_ts=last_ts,
        )
        if not ok:
            logger.info("Skip proactive (%s) companion=%s", reason, companion_id)
            return

        if not await _ws_send_json(websocket, {"type": "typing"}):
            return

        time_ctx = build_dialogue_time_context(
            language=lang,
            tz_iana=(sess.get("client_tz") or "").strip() or None,
            tz_offset_minutes=sess.get("client_tz_offset"),
            companion_city=_companion_city_str(companion),
        )
        prompt = _build_proactive_prompt(companion, lang, time_ctx)
        threads = card.get("open_threads") or []
        if threads:
            prompt += f"\n\n【优先续话题】{threads[0]}\n禁止只发「在干嘛」这类空消息。"
        llm = get_llm(role="respond")
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: llm_invoke(llm, [SystemMessage(content=prompt)], node="proactive"),
        )
        text = resp.content.strip()

        try:
            aff = float(getattr(companion.state, "affection", 0) or 0)
            show_think = random.random() < _THINK_TOAST_UI_PROBABILITY * (
                0.35 + 0.65 * min(1.0, aff / 100.0)
            )
            await _deliver_assistant_content(
                websocket,
                companion,
                text,
                message_queue=None,
                show_think_toasts=show_think,
                delivery_mood="neutral",
            )
        except Exception:
            return

        sess_done = get_session(companion_id, user_id=user_id) or {}
        uid = sess_done.get("user_id")
        companion.save_state(uid if isinstance(uid, int) else None)
    except Exception as e:
        logger.warning(
            "Proactive message generation failed for companion %s: %s",
            getattr(companion, "profile", None) and companion.profile.id,
            e,
        )


_PERSONA_GENERATE_PROMPT = """你是一个专业的人物设定作家。请根据以下基础信息，生成一个完整、立体、真实的虚拟伙伴/伴侣人设。

要求：
1. 生成的内容必须和已知的基础信息（姓名、年龄、性别、城市、性格、MBTI）高度一致；城市决定生活环境与文化圈，禁止写成与该城市主流文化不符的另一国家/语言人生
2. 内容要口语化、有画面感、真实可信，不要模板化；禁止复制「童年→求学→某年搬到某城→如今…」的空洞脚手架长段
3. **性格标签必须被消化进全文**：speech_style / love_view / fears / hobbies / life_story 都要能看出给定性格（{personality}）的具体行为与语气；禁止无视种子写成「通用温柔体贴伴侣」
4. **性别气质禁止默认少女腔**：角色可为任意性别气质（沉稳/硬核/寡言/社牛/毒舌/事业心等均可）。若性别为男或性格偏硬朗，禁止默认撒娇、黏人、软萌口癖；若性格含温柔，也须写成具体相处方式，而非「标准女友」模板
5. 避开陈词滥调职业/爱好组合的堆砌（如：编辑+胡同散步+陶艺；投行+红酒+英短；咖啡店兼职+胶片相机等若无性格支撑则勿用）；兴趣与职业须与性格、城市、年龄互相解释
6. 城市日常须差异化：使用「所在地主流文化锚点」里该城的 everyday / ideology，禁止整语种共用一段「奶茶+地铁+外卖」空话套到所有城市
7. 成长经历（life_story）必须包含：童年、青少年、成年、原生家庭影响、重大转折点、内心创伤与成长；须写明成长地/求学或打工环境与「当前城市」的关系（土生土长 / 迁入 / 两地往返等）；不少于 180 字；段落之间要有因果，不要同义反复
8. 文化三观与意识形态（cultural_values）必须因果自洽，不少于 100 字，并写清：
   - 价值排序（家庭/自由/成就/面子/金钱/忠诚等）
   - 对权威、个人自由、集体/社群的态度
   - 至少 2 条因果：成长经历中的具体事件或环境 → 当前立场；当前城市日常 → 如何强化或修正该立场
   - 默认贴合所在地主流文化与主流语言习惯；若有非主流观点，必须用成长经历解释，且仍用当地语言表达
9. 性别观念与认知（gender_perspective）写清对性别角色、亲密关系中的平等与边界；须与当地主流社交习惯相容或给出经历解释；不少于 80 字；不要默认「男性保护/女性被保护」刻板叙事，除非性格与经历明确支撑
10. 每个字段都要独立且非空；禁止省略后三个深度字段
11. 控制总长度：除 life_story 外，其余字段各 40–180 字，避免超长导致 JSON 截断
12. background / daily_routine / speech_style 必须能看出该城市的生活气味（通勤、媒介、饮食、社交场合），且与性格标签同频
13. **人设维度种子必须落地**：职业/收入节奏贴合 career_class；亲密关系反应贴合 attachment；吵架与和好方式贴合 conflict_style；作息与压力感贴合 life_pace；爱好与社交场合贴合 interest_domain。同 MBTI 也必须因这些种子而明显不同
14. **用户草稿扩展（若提供）**：下方「用户已填写草稿」中的字段是用户原意，必须在对应字段上**扩写、润色、补全细节**，保留其核心事实与语气倾向；禁止无视草稿另起炉灶，禁止删掉用户明确写过的关键设定（职业、家庭、事件等）。未提供草稿的字段再自由创作，且须与草稿及基础信息自洽

基础信息：
- 姓名：{name}
- 年龄：{age}
- 性别：{gender}
- 性取向：{sexual_orientation}
- 国家/地区：{region_line}
- 城市：{city}
- 性格：{personality}
- MBTI：{mbti}
{axes_block}
{user_draft_block}
{cultural_context}

请直接返回一个 JSON 对象，不要返回任何解释文字、不要 Markdown 代码块。
字段顺序必须如下（先写三个深度字段，再写其余）：
{{
  "life_story": "...",
  "cultural_values": "...",
  "gender_perspective": "...",
  "background": "...",
  "speech_style": "...",
  "hobbies": "...",
  "values": "...",
  "fears": "...",
  "love_view": "...",
  "daily_routine": "...",
  "favorite_things": "..."
}}
"""

_PERSONA_REQUIRED_KEYS = (
    "life_story",
    "cultural_values",
    "gender_perspective",
    "background",
    "speech_style",
    "hobbies",
    "values",
    "fears",
    "love_view",
    "daily_routine",
    "favorite_things",
)


def _extract_json(text: str) -> dict:
    """从 LLM 返回中提取 JSON；容忍代码块与轻度截断。"""
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    def _loads(raw: str) -> dict:
        return json.loads(raw)

    try:
        data = _loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        chunk = match.group(0)
        try:
            data = _loads(chunk)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            # 常见截断：补齐未闭合引号/括号后重试
            repaired = chunk.rstrip()
            if repaired.count('"') % 2 == 1:
                repaired += '"'
            # 去掉尾部残缺的 key/value 碎片，回退到最后一个完整字段
            last_comma = repaired.rfind(",")
            if last_comma > 0:
                repaired = repaired[:last_comma]
            repaired += "}"
            try:
                data = _loads(repaired)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    raise ValueError("LLM 返回无法解析为 JSON")


def _normalize_persona_result(result: dict, data: dict) -> dict:
    """保证必填键存在；缺深度字段时用城市主流文化兜底，避免全球通用空话。"""
    out = {k: (str(result.get(k) or "").strip()) for k in _PERSONA_REQUIRED_KEYS}
    name = data.get("name") or "ta"
    city = data.get("city") or ""
    personality = data.get("personality") or ""
    gender = data.get("gender") or ""
    lang = data.get("_resolved_lang") or infer_language_from_city(city)
    bg = out["background"] or f"{name}生活在{city}，性格偏向{personality}。"
    if not out["background"]:
        out["background"] = bg
    if not out["life_story"]:
        out["life_story"] = (
            f"{name}在{city}一带长大，性格里带着「{personality}」的底色。"
            f"童年与少年时期的家庭与学校环境塑造了今天的节奏；成年后的选择与这段经历紧密相连：{bg}"
            "原生家庭既给过温暖，也留下需要慢慢消化的张力；后来经历过明显转折，"
            "学会了在受伤后重新站稳，并把真正在意的事放在更前面。"
            f"如今的日常仍嵌在{city}的主流生活里，说话做事带着当地人的分寸感。"
        )
    if not out["cultural_values"]:
        out["cultural_values"] = default_cultural_values(
            name, city, lang, out["values"] or ""
        )
    if not out["gender_perspective"]:
        love = out["love_view"] or "希望关系里有尊重与陪伴"
        out["gender_perspective"] = (
            f"作为{gender or '个体'}，{name}拒绝把性别当成枷锁或标签。"
            f"态度贴合{city or '当地'}主流社交习惯中的平等与边界意识。"
            f"更在意尊重与情绪责任的分担。亲密关系上：{love}。"
            "角色可以传统也可以现代，前提是双方自愿且平等。"
        )
    for k in _PERSONA_REQUIRED_KEYS:
        if not out[k]:
            out[k] = f"（待完善）{k}"
    return out


_PERSONA_DRAFT_KEYS = (
    "background",
    "speech_style",
    "hobbies",
    "values",
    "fears",
    "love_view",
    "daily_routine",
    "favorite_things",
    "life_story",
    "cultural_values",
    "gender_perspective",
)


def _build_user_draft_block(data: dict) -> str:
    """从请求中收集用户已填长文，供 Prompt 扩展完善。"""
    lines = []
    for key in _PERSONA_DRAFT_KEYS:
        raw = data.get(key) or (data.get("user_draft") or {}).get(key)
        text = str(raw or "").strip()
        if text:
            lines.append(f"- {key}: {text}")
    if not lines:
        return ""
    return (
        "\n用户已填写草稿（必须基于此扩展完善，勿推翻）：\n"
        + "\n".join(lines)
        + "\n"
    )


@router.post("/companions/generate")
async def api_generate_persona(data: dict):
    """根据基础信息 AI 自动生成完整人设；若带用户草稿字段则扩展完善。"""
    name = data.get("name", "")
    age = data.get("age", 22)
    gender = data.get("gender", "女")
    sexual_orientation = data.get("sexual_orientation", "")
    city = data.get("city", "")
    personality = data.get("personality", "")
    mbti = data.get("mbti", "")
    # 城市决定文化圈与主流语言；请求 lang 仅作回退
    lang = infer_language_from_city(city) or normalize_ui_language(data.get("lang", "zh"))
    data = {**data, "_resolved_lang": lang}

    if not name or not city or not personality:
        raise HTTPException(status_code=400, detail="姓名、城市和性格为必填项")

    orientation_label = {
        "heterosexual": "异性恋",
        "homosexual": "同性恋",
        "bisexual": "双性恋",
        "pansexual": "泛性恋",
        "asexual": "无性恋",
        "secret": "保密",
    }.get(sexual_orientation, "")

    cultural_context = get_cultural_context_for_city(city, lang)

    region_meta = find_region_for_city(city, lang) or {}
    region_line = (
        data.get("region_label")
        or region_meta.get("label")
        or data.get("country")
        or region_meta.get("country")
        or "（由城市推断）"
    )
    if data.get("country") or region_meta.get("country"):
        cc = data.get("country") or region_meta.get("country")
        if cc and cc not in str(region_line):
            region_line = f"{region_line} ({cc})"

    axes = resolve_axes_from_payload(data, lang)
    # expand_user_input=True：强化「在用户草稿基础上扩写」指令
    expand_flag = bool(data.get("expand_user_input"))
    user_draft_block = _build_user_draft_block(data)
    if expand_flag and not user_draft_block:
        # 前端声明要扩展但未带长文：仍按种子生成，不报错
        pass
    if not axes:
        axes = sample_persona_axes(lang)
    axes_block = axes_prompt_block(axes, lang)
    if expand_flag and user_draft_block:
        axes_block = (
            axes_block
            + "\n\n【扩展模式】用户已提供草稿长文。请在保留其事实/语气/关键短语的前提下扩写润色，"
            "禁止整段另起炉灶；输出中须能看出原草稿痕迹。"
        )

    prompt = _PERSONA_GENERATE_PROMPT.format(
        name=name, age=age, gender=gender,
        sexual_orientation=orientation_label or "未指定",
        region_line=region_line,
        city=city,
        personality=personality, mbti=mbti or "未知",
        axes_block=axes_block,
        user_draft_block=user_draft_block,
        cultural_context=cultural_context,
    )

    try:
        # 人设 JSON 较长：给足 tokens，避免后写字段被截断；上限保持保守以免部分供应商拒收
        llm = get_llm(temperature=0.85, max_tokens=4096, role="respond")
        resp = await run_rest(
            lambda: llm_invoke(
                llm,
                [SystemMessage(content=prompt)],
                node="persona_generate",
                max_tokens=4096,
            )
        )
        text = resp.content.strip() if hasattr(resp, "content") else str(resp)
        result = _extract_json(text)
        if not isinstance(result, dict):
            raise ValueError("生成结果不是 JSON 对象")
        normalized = _normalize_persona_result(result, data)
        # 回传本次实际使用的 axes / 地区，避免前端存空或另抽一套
        axes_keys = (axes or {}).get("keys") or {}
        if axes_keys:
            normalized["persona_axes"] = axes_keys
            normalized["persona_axes_labels"] = (axes or {}).get("labels") or {}
            normalized["persona_axes_summary"] = (axes or {}).get("summary") or ""
        if data.get("country") or region_meta.get("country"):
            normalized["country"] = data.get("country") or region_meta.get("country") or ""
        if data.get("region_key") or region_meta.get("key"):
            normalized["region_key"] = data.get("region_key") or region_meta.get("key") or ""
        if data.get("region_label") or region_meta.get("label"):
            normalized["region_label"] = data.get("region_label") or region_meta.get("label") or ""
        return normalized
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("persona generate failed: %s", e)
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")


@router.post("/companions")
async def api_create_companion(data: dict, x_token: Optional[str] = Header(None, alias="x-token")):
    """创建智能体，自动关联当前登录用户（设置 created_by）"""
    try:
        # 验证用户身份
        user_id = verify_user_token(x_token) if x_token else None
        if user_id:
            # 设置 created_by 为当前用户 ID
            data["created_by"] = str(user_id)

        chat_history = data.pop("chat_history", None)
        companion = get_companion_manager().create(data, chat_history=chat_history)
        # 启动后台异步生成头像
        if companion.profile.avatar_url == "__GENERATING__":
            start_avatar_generation(companion.profile.id, companion.profile.model_dump())
        return companion.to_dict()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/companions")
async def api_list_companions(x_token: Optional[str] = Header(None, alias="x-token")):
    """获取智能体列表（含他人创建的）；附带当前用户的亲密度/预览。必须登录。"""
    uid = verify_user_token(x_token) if x_token else None
    if not uid:
        return []
    return await run_rest(get_companion_manager().list_all, user_id=uid)

@router.get("/companions/{companion_id}")
async def api_get_companion(companion_id: str, user_id: int = Depends(require_login_user)):
    companion = get_companion_manager().get(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")
    _assert_companion_user_access(companion, user_id)
    return companion.to_dict(user_id=user_id)


@router.get("/companions/{companion_id}/messages")
async def api_get_messages(
    companion_id: str,
    limit: int = 20,
    offset: int = 0,
    after_ts: Optional[str] = None,
    user_id: int = Depends(require_login_user),
):
    """拉取聊天记录。

    - 默认：offset 分页（从最新往旧）
    - after_ts：增量同步，只返回该时间戳之后的新消息（不重复下发旧内容）
    """
    companion = get_companion_manager().get(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")
    _assert_companion_user_access(companion, user_id)
    companion.memory.bind_user(user_id)

    after = (after_ts or "").strip() or None
    lim = max(1, min(int(limit or 20), 100))
    off = max(0, int(offset or 0))

    def _load():
        total = companion.memory.short_term.get_total_count()
        if after:
            messages = companion.memory.short_term.get_after(after, lim)
            head = messages[-1] if messages else None
            # 无新消息时仍给 tip，便于客户端确认已对齐
            if head is None:
                tip = companion.memory.short_term.get_recent(1, 0)
                head = tip[-1] if tip else None
            return {
                "messages": messages,
                "total": total,
                "mode": "incremental",
                "head": {
                    "last_ts": (head or {}).get("timestamp"),
                    "last_id": (head or {}).get("id") or (head or {}).get("temp_id"),
                },
            }

        messages = companion.memory.short_term.get_recent(lim, off)
        head = messages[-1] if messages else None
        return {
            "messages": messages,
            "total": total,
            "mode": "page",
            "head": {
                "last_ts": (head or {}).get("timestamp"),
                "last_id": (head or {}).get("id") or (head or {}).get("temp_id"),
            },
        }

    return await run_rest(_load)


@router.post("/companions/{companion_id}/generate-avatar")
async def api_generate_avatar(companion_id: str, user_id: int = Depends(require_login_user)):
    """基于人设 AI 生成动漫风格头像（异步，立即返回）"""
    companion = get_companion_manager().get(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")
    _assert_companion_owner(companion, user_id)

    get_companion_manager().update(companion_id, {"avatar_url": "__GENERATING__"})
    start_avatar_generation(companion_id, companion.profile.model_dump())
    return {"ok": True, "status": "generating", "avatar_url": "__GENERATING__"}


@router.delete("/companions/{companion_id}")
async def api_delete_companion(companion_id: str, user_id: int = Depends(require_login_user)):
    companion = get_companion_manager().get(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")
    _assert_companion_owner(companion, user_id)
    ok = get_companion_manager().delete(companion_id)
    if not ok:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"ok": True}


@router.post("/companions/{companion_id}/clear-messages")
async def api_clear_messages(companion_id: str, user_id: int = Depends(require_login_user)):
    """清空该智能体的聊天记录（短期记忆），并将亲密度归零"""
    companion = get_companion_manager().get(companion_id)
    if not companion:
        raise HTTPException(status_code=404, detail="智能体不存在")
    _assert_companion_user_access(companion, user_id)
    companion.memory.bind_user(user_id)
    companion.memory.short_term.clear()
    companion.state.affection = 0
    companion.state.turns = 0
    companion.save_state(user_id=user_id)
    return {"ok": True}


@router.websocket("/ws/chat/{companion_id}")
async def ws_chat(websocket: WebSocket, companion_id: str):
    await websocket.accept()
    # 连接级发送锁：主循环 ping / receive pong / proactive / typing 共用
    setattr(websocket, "_trandsai_send_lock", asyncio.Lock())

    ui_lang_early = normalize_ui_language(websocket.query_params.get("lang", "zh"))

    # 安全优化：验证用户 token
    token = websocket.query_params.get("token")
    user_id = verify_user_token(token) if token else None
    if not user_id:
        try:
            err = _WS_AUTH_FAILED.get(ui_lang_early, _WS_AUTH_FAILED["zh"])
            await _ws_send_json(websocket, {"type": "error", "text": err})
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    companion = get_companion_manager().get(companion_id)
    if not companion:
        err = _WS_COMPANION_NOT_FOUND.get(ui_lang_early, _WS_COMPANION_NOT_FOUND["zh"])
        await _ws_send_json(websocket, {"type": "error", "text": err})
        await websocket.close()
        return

    try:
        _assert_companion_user_access(companion, user_id)
    except HTTPException as e:
        try:
            detail = (
                e.detail
                if isinstance(e.detail, str) and e.detail.strip()
                else None
            )
            err = detail or _WS_ACCESS_DENIED.get(ui_lang_early, _WS_ACCESS_DENIED["zh"])
            await _ws_send_json(websocket, {"type": "error", "text": err})
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    lang_param = normalize_ui_language(websocket.query_params.get("lang", "zh"))
    session_meta = get_session(companion_id, user_id=user_id)
    session_meta["lang"] = lang_param
    session_meta["user_id"] = user_id  # 记录用户ID用于后续会话
    session_meta.pop("pending_retention", None)
    session_meta.pop("last_disconnect", None)
    await set_session(companion_id, session_meta, user_id=user_id)
    companion.memory.bind_user(user_id)  # REQ-A2
    user_lang = session_meta.get("lang") or "zh"
    connection_id = str(uuid.uuid4())
    _ws_register(connection_id, websocket)
    await _ensure_ws_janitor()

    # 消息去重：记录最近收到的消息内容和时间戳
    _recent_user_messages: list[dict] = []

    # 不在每次 WS 握手后推送「已连接」类系统提示，避免离线重连打扰用户（静默连接）

    proactive_task = None

    # 用户消息队列：解耦接收与处理，支持发送过程中被打断
    message_queue: asyncio.Queue = asyncio.Queue()

    async def _receive_loop():
        """持续接收用户消息，存入队列；应答/刷新应用层心跳。"""
        while True:
            try:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"text": raw}
                msg_type = payload.get("type")
                if msg_type == "ping":
                    # 客户端心跳：回 pong，并记存活
                    _ws_mark_alive(websocket)
                    await _ws_send_json(websocket, {"type": "pong"})
                    continue
                if msg_type == "pong":
                    # 应答服务端 ping：刷新存活，解除 awaiting
                    _ws_mark_alive(websocket)
                    continue
                user_text = payload.get("text", "").strip()
                if user_text:
                    _ws_mark_alive(websocket)
                    await message_queue.put(payload)
            except WebSocketDisconnect:
                await message_queue.put({"__disconnect": True})
                break
            except Exception:
                await message_queue.put({"__disconnect": True})
                break

    receiver_task = asyncio.create_task(_receive_loop())

    try:
        while True:
            # 空闲等待用户消息；超时则发 ping，撑住代理读超时；无 pong 则清僵死连接
            try:
                payload = await asyncio.wait_for(
                    message_queue.get(), timeout=_ws_heartbeat_interval()
                )
            except asyncio.TimeoutError:
                if not await _ws_send_ping_or_close(websocket):
                    break
                continue
            if payload.get("__disconnect"):
                if proactive_task and not proactive_task.done():
                    proactive_task.cancel()
                prev_sess = get_session(companion_id, user_id=user_id)
                prev_lang = prev_sess.get("lang")
                next_sess = {**prev_sess, "lang": prev_lang or user_lang or "zh", "user_id": user_id}
                next_sess.pop("pending_retention", None)
                next_sess.pop("last_disconnect", None)
                await set_session(companion_id, next_sess, user_id=user_id)
                break
            first_text = payload.get("text", "").strip()
            if not first_text:
                continue

            # 与接收顺序一致：同轮紧挨着的多条用户文合并给模型；设上限条数，余量留待下一轮，避免一次吞太多语义串台
            burst_head = payload
            burst_parts: list[str] = [first_text]
            coalesce_skipped = 0
            while len(burst_parts) < _WS_BURST_MAX_MESSAGES:
                try:
                    newer = message_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if newer.get("__disconnect"):
                    await message_queue.put(newer)
                    break
                newer_text = newer.get("text", "").strip()
                if newer_text:
                    burst_parts.append(newer_text)
                    coalesce_skipped += 1

            combined_user_plain = "\n".join(burst_parts)
            lk_co = normalize_ui_language(burst_head.get("lang") or user_lang or "zh")

            if coalesce_skipped >= 2:
                co_txt = _QUEUE_COALESCED_MESSAGE.get(lk_co, _QUEUE_COALESCED_MESSAGE["zh"])
                await _ws_send_json(websocket, {"type": "system", "text": co_txt})

            # 消息去重：极短句不拦（如连发「在吗」）；其余 5s 内全文相同视为连点/重复
            now = datetime.now(timezone.utc)
            _plain_stripped = combined_user_plain.strip()
            is_duplicate = False
            if len(_plain_stripped) >= _DUPLICATE_CHECK_MIN_LEN:
                is_duplicate = any(
                    m["text"] == combined_user_plain and (now - m["time"]).total_seconds() < 5
                    for m in _recent_user_messages
                )
            if is_duplicate:
                lk_dup = normalize_ui_language(burst_head.get("lang") or user_lang or "zh")
                dup_txt = _DUPLICATE_USER_MESSAGE.get(lk_dup, _DUPLICATE_USER_MESSAGE["zh"])
                await _ws_send_json(websocket, {"type": "system", "text": dup_txt})
                continue
            _recent_user_messages.append({"text": combined_user_plain, "time": now})
            if len(_recent_user_messages) > 10:
                _recent_user_messages.pop(0)

            # 用户发新消息了，取消之前的主动消息定时任务
            if proactive_task and not proactive_task.done():
                proactive_task.cancel()

            # 如果有未发送完的AI回复，先保存已发送的部分（如果有）
            # 这里简化处理：已发送的内容已经展示给用户，新的回复会覆盖话题

            if not check_chat_rate_limit(user_id):
                busy_txt = _AGENT_BUSY_MESSAGE.get(lk_co, _AGENT_BUSY_MESSAGE["zh"])
                await _ws_send_json(websocket, {"type": "error", "text": busy_txt})
                continue

            for line in burst_parts:
                companion.memory.add_user_message(line)

            has_leave_intent = detect_leave_intent(combined_user_plain)

            if len(burst_parts) > 1:
                prefix = _MULTI_MESSAGE_AGENT_PREFIX.get(lk_co, _MULTI_MESSAGE_AGENT_PREFIX["zh"])
                numbered = "\n".join(f"({i + 1}) {t}" for i, t in enumerate(burst_parts))
                user_text_for_agent = prefix + numbered
            else:
                user_text_for_agent = burst_parts[0]
            if not await _ws_send_json(websocket, {"type": "typing"}):
                continue

            memory_tier = os.getenv("AGENT_MEMORY_TIER", "full")
            relation_card_text = ""
            card = {}
            open_threads: list = []
            deny_hooks: list = []
            if RELATION_CARD_ENABLED:
                card = load_card(user_id, companion_id)
                if not is_card_empty(card):
                    relation_card_text = render_card(card)
                open_threads = list(card.get("open_threads") or [])
                deny_hooks = list(card.get("deny_hooks") or [])
            use_card = bool(relation_card_text)
            fallback_full = RELATION_CARD_FALLBACK_FULL_MEMORY and is_card_empty(card)
            memory_text = companion.memory.build_prompt_context(
                query=combined_user_plain,
                tier=memory_tier,
                user_id=user_id,
                relation_card_text=relation_card_text if use_card else "",
                truncate_episodes=use_card and not fallback_full,
            )
            recent_assistant = []
            try:
                # 与近聊语料同窗：从近聊消息里抽助手句，供忌用/问句门控
                from services.memory import MEMORY_RECENT_MESSAGES

                for m in companion.memory.short_term.get_recent(MEMORY_RECENT_MESSAGES):
                    if m.get("role") == "assistant" and (m.get("content") or "").strip():
                        recent_assistant.append(m["content"])
                recent_assistant = recent_assistant[-12:]
            except Exception:
                recent_assistant = []
            # 忌用窗以近聊为准补齐（关系卡 deny 常被异步事实任务冲空）
            deny_hooks = seed_deny_from_recent(deny_hooks, recent_assistant)
            # C3 / M*：idle 与极端冷却时间戳（先算 idle，再刷新 last_user_ts）
            session_meta_pre = get_session(companion_id, user_id=user_id) or {}
            idle_seconds = None
            raw_prev_ts = session_meta_pre.get("last_user_ts")
            if raw_prev_ts:
                try:
                    prev_ts = datetime.fromisoformat(str(raw_prev_ts).replace("Z", "+00:00"))
                    if prev_ts.tzinfo is None:
                        prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                    idle_seconds = (datetime.now(timezone.utc) - prev_ts).total_seconds()
                except Exception:
                    idle_seconds = None
            last_extreme_ts = str(session_meta_pre.get("last_extreme_ts") or "")
            session_meta_pre["last_user_ts"] = datetime.now(timezone.utc).isoformat()
            await set_session(companion_id, session_meta_pre, user_id=user_id)
            language = normalize_ui_language(
                burst_head.get("lang") or get_session(companion_id, user_id=user_id).get("lang") or user_lang or "zh"
            )
            session_meta = get_session(companion_id, user_id=user_id)
            session_meta["lang"] = language
            tz_pay = (burst_head.get("tz") or burst_head.get("timeZone") or "").strip()
            if tz_pay:
                session_meta["client_tz"] = tz_pay[:120]
            if burst_head.get("tz_offset") is not None:
                try:
                    session_meta["client_tz_offset"] = int(burst_head["tz_offset"])
                except (TypeError, ValueError):
                    pass
            await set_session(companion_id, session_meta, user_id=user_id)

            tz_iana_effective = (
                (burst_head.get("tz") or burst_head.get("timeZone") or "").strip()
                or (session_meta.get("client_tz") or "").strip()
                or None
            )
            if tz_iana_effective:
                tz_iana_effective = tz_iana_effective[:120]
            tz_off_effective = session_meta.get("client_tz_offset")

            hydrate_user_affection_turns(companion, user_id)

            loop = asyncio.get_event_loop()
            current_time = build_dialogue_time_context(
                language=language,
                tz_iana=tz_iana_effective,
                tz_offset_minutes=tz_off_effective
                if isinstance(tz_off_effective, int)
                else None,
                companion_city=_companion_city_str(companion),
            )
            user_gender = burst_head.get("user_gender", "")
            delivery_mood = _burst_mood_from_user_text(combined_user_plain)
            summary_due = companion.memory.summary.should_update()

            result = None
            use_queue = queue_enabled()
            t_turn0 = _time_mod.monotonic()
            turn_error = None
            light_path = False
            was_interrupted = False
            pronoun = _companion_pronoun(companion)
            burst_count = len(burst_parts)

            async with user_turn_lock(user_id, companion_id):
                if use_queue:
                    job = AgentJob(
                        job_id=new_job_id(),
                        connection_id=connection_id,
                        companion_id=companion_id,
                        user_id=user_id,
                        user_text_for_agent=user_text_for_agent,
                        combined_user_plain=combined_user_plain,
                        profile=companion.profile.model_dump(),
                        companion_state=companion.state.model_dump(),
                        memory_text=memory_text,
                        language=language,
                        current_time=current_time,
                        user_gender=user_gender,
                        summary_due=summary_due,
                        has_leave_intent=has_leave_intent,
                        burst_count=burst_count,
                        priority=int(os.getenv("PAID_USER_PRIORITY", "0")),
                        open_threads=open_threads,
                        deny_hooks=deny_hooks,
                        recent_assistant=recent_assistant,
                        idle_seconds=float(idle_seconds) if idle_seconds is not None else -1.0,
                        last_extreme_ts=last_extreme_ts,
                    )
                    enqueued = await enqueue_agent_job(job)
                    if enqueued:
                        pending = loop.create_future()
                        keep_task = asyncio.create_task(
                            _keepalive_during_agent(websocket, pending, language)
                        )
                        try:
                            payload = await wait_agent_result(connection_id, job.job_id)
                            if not payload or not payload.get("ok"):
                                err_txt = _AGENT_TIMEOUT_MESSAGE.get(language, _AGENT_TIMEOUT_MESSAGE["zh"])
                                await _ws_send_json(websocket, {"type": "error", "text": err_txt})
                                turn_error = "queue_failed"
                                _log_chat_turn({
                                    "companion_id": companion_id,
                                    "user_id": user_id,
                                    "light_path": False,
                                    "need_facts": False,
                                    "need_summary": summary_due,
                                    "evolved": False,
                                    "llm_calls_est": 0,
                                    "t_first_message_ms": int((_time_mod.monotonic() - t_turn0) * 1000),
                                    "interrupted": False,
                                    "error": turn_error,
                                })
                                continue
                            result = payload.get("result")
                        finally:
                            if not pending.done():
                                pending.set_result(True)
                            keep_task.cancel()
                            try:
                                await keep_task
                            except asyncio.CancelledError:
                                pass
                    else:
                        use_queue = False

                if not use_queue:
                    # 预判轻路径：轻路径不占重路径信号量（REQ-A6）
                    from services.agent import should_use_light_path as _sulp
                    will_light = _sulp(
                        user_text_for_agent,
                        companion.state.model_dump(),
                        has_leave_intent=has_leave_intent,
                        burst_count=burst_count,
                    )

                    async def _run_agent_coro():
                        return await asyncio.wrap_future(
                            loop.run_in_executor(
                                AGENT_POOL,
                                functools.partial(
                                    run_agent,
                                    user_text_for_agent,
                                    companion.profile.model_dump(),
                                    companion.state.model_dump(),
                                    memory_text,
                                    "",
                                    language,
                                    current_time,
                                    user_gender,
                                    summary_due,
                                    True,
                                    has_leave_intent,
                                    burst_count,
                                    open_threads,
                                    deny_hooks,
                                    recent_assistant,
                                    idle_seconds,
                                    last_extreme_ts,
                                ),
                            )
                        )

                    acquired = False
                    if not will_light:
                        try:
                            await asyncio.wait_for(agent_semaphore.acquire(), timeout=0.05)
                            acquired = True
                        except asyncio.TimeoutError:
                            busy_txt = _AGENT_BUSY_MESSAGE.get(language, _AGENT_BUSY_MESSAGE["zh"])
                            await _ws_send_json(websocket, {"type": "system", "text": busy_txt})
                            _log_chat_turn({
                                "companion_id": companion_id,
                                "user_id": user_id,
                                "light_path": False,
                                "need_facts": False,
                                "need_summary": summary_due,
                                "evolved": False,
                                "llm_calls_est": 0,
                                "t_first_message_ms": int((_time_mod.monotonic() - t_turn0) * 1000),
                                "interrupted": False,
                                "error": "busy",
                            })
                            continue

                    agent_task = asyncio.create_task(_run_agent_coro())
                    keep_task = asyncio.create_task(
                        _keepalive_during_agent(websocket, agent_task, language)
                    )
                    try:
                        result = await asyncio.wait_for(asyncio.shield(agent_task), timeout=120.0)
                    except asyncio.TimeoutError:
                        turn_error = "timeout"
                        err_txt = _AGENT_TIMEOUT_MESSAGE.get(language, _AGENT_TIMEOUT_MESSAGE["zh"])
                        await _ws_send_json(websocket, {"type": "error", "text": err_txt})
                        _log_chat_turn({
                            "companion_id": companion_id,
                            "user_id": user_id,
                            "light_path": will_light,
                            "need_facts": False,
                            "need_summary": summary_due,
                            "evolved": False,
                            "llm_calls_est": 0,
                            "t_first_message_ms": int((_time_mod.monotonic() - t_turn0) * 1000),
                            "interrupted": False,
                            "error": turn_error,
                        })
                        continue
                    finally:
                        if acquired:
                            agent_semaphore.release()
                        keep_task.cancel()
                        try:
                            await keep_task
                        except asyncio.CancelledError:
                            pass

                if not result:
                    continue

                light_path = bool(result.get("light_path"))
                response_text = result["response"]

                # REQ-A7：无首条前拟人等待
                aff = float(getattr(companion.state, "affection", 0) or 0)
                show_think = random.random() < _THINK_TOAST_UI_PROBABILITY * (
                    0.35 + 0.65 * min(1.0, aff / 100.0)
                )
                t_first = None
                sent_segments, was_interrupted = await _deliver_assistant_content(
                    websocket,
                    companion,
                    response_text,
                    message_queue=message_queue,
                    show_think_toasts=show_think,
                    delivery_mood=delivery_mood,
                )
                t_first_ms = int((_time_mod.monotonic() - t_turn0) * 1000)

                memory_snapshot = result.get("memory_snapshot")

                if was_interrupted:
                    if sent_segments:
                        companion.state.mood = result["mood"]
                        companion.state.affection = result["affection"]
                        companion.state.turns += 1
                        # REQ-B5：打断已发气泡写入情景
                        companion.memory.commit_turn(
                            combined_user_plain,
                            "\n\n".join(sent_segments),
                            pronoun=pronoun,
                            partial=True,
                        )
                        if result.get("evolved_personality"):
                            companion.state.evolved_personality = result["evolved_personality"]
                        if result.get("evolved_background"):
                            companion.state.evolved_background = result["evolved_background"]
                        if result.get("evolved_speech_style"):
                            companion.state.evolved_speech_style = result["evolved_speech_style"]
                        if not memory_snapshot:
                            if companion.memory.summary.should_update() and result.get("new_summary"):
                                companion.memory.summary.update(result["new_summary"])
                                companion.state.summary = result["new_summary"]
                            else:
                                companion.state.summary = companion.memory.summary.get_summary()
                        companion.save_state(user_id=user_id)
                        if RELATION_CARD_ENABLED:
                            card2 = touch_state(
                                load_card(user_id, companion_id),
                                mood=companion.state.mood,
                                affection=companion.state.affection,
                                summary=companion.state.summary,
                            )
                            save_card(user_id, companion_id, card2)
                    _log_chat_turn({
                        "companion_id": companion_id,
                        "user_id": user_id,
                        "light_path": light_path,
                        "need_facts": bool(result.get("need_facts")),
                        "need_summary": bool(result.get("need_summary", summary_due)),
                        "evolved": bool(result.get("evolved")),
                        "llm_calls_est": int(result.get("llm_calls_est") or 0),
                        "t_first_message_ms": t_first_ms,
                        "interrupted": True,
                        "error": None,
                    })
                    continue

                actual_response = "\n\n".join(sent_segments)
                companion.memory.commit_turn(
                    combined_user_plain,
                    actual_response,
                    pronoun=pronoun,
                    partial=False,
                )

                companion.state.mood = result["mood"]
                companion.state.affection = result["affection"]
                companion.state.turns += 1

                # 关系卡忌用指纹必须在后台任务前落库，避免 BG merge_facts 竞态清空
                if RELATION_CARD_ENABLED:
                    c = touch_state(
                        load_card(user_id, companion_id),
                        mood=companion.state.mood,
                        affection=companion.state.affection,
                        summary=companion.state.summary,
                    )
                    if result.get("new_facts") and not result.get("facts_async_pending"):
                        c = merge_facts(c, result.get("new_facts") or [], lang=language)
                    picked = (result.get("picked_hook") or "").strip()
                    if picked:
                        c = push_deny_hook(c, picked)
                        c = merge_open_threads(c, new_thread=picked)
                    c = push_deny_fingerprints(c, actual_response or response_text)
                    stage = (result.get("relation_stage") or "").strip() or resolve_relation_stage(
                        int(getattr(companion.state, "turns", 0) or 0),
                        float(getattr(companion.state, "affection", 0) or 0),
                    )
                    c["stage"] = stage
                    save_card(user_id, companion_id, c)

                if memory_snapshot:
                    companion.save_state(user_id=user_id)
                    snapshot_for_bg = {
                        **memory_snapshot,
                        "companion_state": {
                            **memory_snapshot["companion_state"],
                            "turns": companion.state.turns,
                            "mood": companion.state.mood,
                            "affection": companion.state.affection,
                        },
                    }

                    async def _bg_memory(snap=snapshot_for_bg):
                        try:
                            mem = await run_memory_update_async(snap)
                            if mem.get("new_facts"):
                                companion.memory.facts.add_facts(mem["new_facts"])
                                if RELATION_CARD_ENABLED:
                                    c = load_card(user_id, companion_id)
                                    deny_before = list(c.get("deny_hooks") or [])
                                    stage_before = c.get("stage")
                                    c = merge_facts(c, mem["new_facts"], lang=language)
                                    # 后台不得冲掉主线程刚写入的忌用指纹/阶段
                                    latest = load_card(user_id, companion_id)
                                    merged = []
                                    seen = set()
                                    for d in (
                                        list(latest.get("deny_hooks") or [])
                                        + deny_before
                                        + list(c.get("deny_hooks") or [])
                                    ):
                                        s = str(d).strip()
                                        if s and s not in seen:
                                            seen.add(s)
                                            merged.append(s)
                                    c["deny_hooks"] = merged[-20:]
                                    c["stage"] = (
                                        latest.get("stage")
                                        or stage_before
                                        or c.get("stage")
                                        or "stranger"
                                    )
                                    save_card(user_id, companion_id, c)
                            if mem.get("new_summary") and companion.memory.summary.should_update():
                                companion.memory.summary.update(mem["new_summary"])
                                companion.state.summary = mem["new_summary"]
                            else:
                                companion.state.summary = companion.memory.summary.get_summary()
                            if mem.get("evolved_personality"):
                                companion.state.evolved_personality = mem["evolved_personality"]
                            if mem.get("evolved_background"):
                                companion.state.evolved_background = mem["evolved_background"]
                            if mem.get("evolved_speech_style"):
                                companion.state.evolved_speech_style = mem["evolved_speech_style"]
                            companion.save_state(user_id=user_id)
                        except Exception as e:
                            logger.warning("Background memory v2 failed: %s", e)

                    asyncio.create_task(_bg_memory())
                else:
                    if result.get("evolved_personality"):
                        companion.state.evolved_personality = result["evolved_personality"]
                    if result.get("evolved_background"):
                        companion.state.evolved_background = result["evolved_background"]
                    if result.get("evolved_speech_style"):
                        companion.state.evolved_speech_style = result["evolved_speech_style"]

                    if result.get("new_facts") and not result.get("facts_async_pending"):
                        companion.memory.facts.add_facts(result["new_facts"])

                    if companion.memory.summary.should_update() and result.get("new_summary"):
                        companion.memory.summary.update(result["new_summary"])
                        companion.state.summary = result["new_summary"]
                    else:
                        companion.state.summary = companion.memory.summary.get_summary()

                    companion.save_state(user_id=user_id)

                    # REQ-B7 / C4：异步 Facts（完整轮、非打断）；优先进档案队列
                    if result.get("facts_async_pending"):
                        timeout_s = float(os.getenv("FACTS_ASYNC_TIMEOUT_S", "45"))
                        job_payload = {
                            "kind": "facts",
                            "user_input": user_text_for_agent,
                            "final_response": actual_response,
                            "language": language,
                            "user_id": user_id,
                            "companion_id": companion_id,
                        }

                        async def _async_facts(payload=job_payload):
                            try:
                                inline = os.getenv("ARCHIVE_INLINE", "true").lower() in (
                                    "1",
                                    "true",
                                    "yes",
                                )
                                if archive_queue_enabled() and not inline:
                                    enqueue_archive_job(payload)
                                    return
                                mem = await asyncio.wait_for(
                                    loop.run_in_executor(
                                        AGENT_POOL, process_archive_job, payload
                                    ),
                                    timeout=timeout_s,
                                )
                                facts = (mem or {}).get("new_facts") or []
                                if facts:
                                    companion.memory.facts.add_facts(facts)
                                    if RELATION_CARD_ENABLED:
                                        c = load_card(user_id, companion_id)
                                        deny_keep = list(c.get("deny_hooks") or [])
                                        stage_keep = c.get("stage")
                                        c = merge_facts(
                                            touch_state(
                                                c,
                                                mood=companion.state.mood,
                                                affection=companion.state.affection,
                                                summary=companion.state.summary,
                                            ),
                                            facts,
                                            lang=language,
                                        )
                                        latest = load_card(user_id, companion_id)
                                        merged_deny = []
                                        seen_d = set()
                                        for d in (
                                            list(latest.get("deny_hooks") or [])
                                            + deny_keep
                                            + list(c.get("deny_hooks") or [])
                                        ):
                                            s = str(d).strip()
                                            if s and s not in seen_d:
                                                seen_d.add(s)
                                                merged_deny.append(s)
                                        c["deny_hooks"] = merged_deny[-20:]
                                        c["stage"] = (
                                            latest.get("stage")
                                            or stage_keep
                                            or c.get("stage")
                                            or "ambiguous"
                                        )
                                        save_card(user_id, companion_id, c)
                            except Exception as e:
                                logger.warning("Async facts failed/dropped: %s", e)

                        asyncio.create_task(_async_facts())

                if result.get("means_mode") == "extreme":
                    sess_x = get_session(companion_id, user_id=user_id) or {}
                    sess_x["last_extreme_ts"] = datetime.now(timezone.utc).isoformat()
                    await set_session(companion_id, sess_x, user_id=user_id)

                _log_chat_turn({
                    "companion_id": companion_id,
                    "user_id": user_id,
                    "light_path": light_path,
                    "need_facts": bool(result.get("need_facts")),
                    "need_summary": bool(result.get("need_summary", summary_due)),
                    "evolved": bool(result.get("evolved")),
                    "llm_calls_est": int(result.get("llm_calls_est") or 0),
                    "t_first_message_ms": t_first_ms,
                    "interrupted": False,
                    "error": None,
                    "threat_level": result.get("threat_level"),
                    "means_mode": result.get("means_mode"),
                    "extreme_reason": result.get("extreme_reason"),
                })

            # AI 回复完成后，启动新的主动消息定时任务
            proactive_task = asyncio.create_task(
                _send_proactive_message(websocket, companion, language, companion_id, user_id=user_id)
            )

    except WebSocketDisconnect:
        if proactive_task and not proactive_task.done():
            proactive_task.cancel()
        prev_sess = get_session(companion_id, user_id=user_id)
        prev_lang = prev_sess.get("lang")
        next_sess = {**prev_sess, "lang": prev_lang or user_lang or "zh", "user_id": user_id}
        next_sess.pop("pending_retention", None)
        next_sess.pop("last_disconnect", None)
        await set_session(companion_id, next_sess, user_id=user_id)
    except Exception:
        logger.exception("WebSocket chat loop failed for companion %s", companion_id)
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                lk = normalize_ui_language(get_session(companion_id, user_id=user_id).get("lang") or "zh")
                msg = _WS_CHAT_UNEXPECTED_ERROR.get(lk, _WS_CHAT_UNEXPECTED_ERROR["zh"])
                await _ws_send_json(websocket, {"type": "error", "text": msg})
        except Exception as send_err:
            logger.warning(
                "Failed to send websocket error payload for companion %s: %s",
                companion_id,
                send_err,
            )
    finally:
        _ws_unregister(connection_id)
        if proactive_task and not proactive_task.done():
            proactive_task.cancel()
            try:
                await proactive_task
            except asyncio.CancelledError:
                pass
        if not receiver_task.done():
            receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass
        # 确保僵死/异常路径也释放底层连接
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
        except Exception:
            pass


@router.post("/knowledge/search")
async def public_knowledge_search(data: dict):
    """公开知识库搜索（发现页用）"""
    from services.knowledge_base import knowledge_base
    query = data.get("query", "")
    top_k = data.get("top_k", 10)
    if not query:
        return {"results": []}
    results = knowledge_base.search_entries(query, top_k=top_k)
    return {"results": results}
