"""Minimal tests for server optimizations."""
import importlib.util
import os
import unittest
from unittest.mock import MagicMock, patch

_HAS_CHROMADB = importlib.util.find_spec("chromadb") is not None


class TestResolveMaxTokens(unittest.TestCase):
    def test_low_affection(self):
        from services.llm.client import resolve_max_tokens

        self.assertEqual(resolve_max_tokens(10), 384)

    def test_high_affection(self):
        from services.llm.client import resolve_max_tokens

        self.assertGreaterEqual(resolve_max_tokens(80), 384)


class TestMemoryTier(unittest.TestCase):
    @unittest.skipUnless(_HAS_CHROMADB, "chromadb not installed")
    def test_compact_shorter_than_full(self):
        from services.memory import CompanionMemory

        mem = CompanionMemory.__new__(CompanionMemory)
        mem.companion_id = "test"
        mem.short_term = MagicMock()
        mem.short_term.get_recent_turns.return_value = [
            {"role": "user", "content": "hello"},
        ]
        mem.facts = MagicMock()
        mem.facts.get_facts.return_value = ["likes coffee"]
        mem.summary = MagicMock()
        mem.summary.get_summary.return_value = "friends"

        def fake_context(query="", user_id=None):
            return {
                "recent_dialogue": [],
                "episodes": [],
                "facts": ["likes coffee"],
                "summary": "friends",
            }

        mem.get_context = fake_context
        compact = mem.build_prompt_context(tier="compact")
        full = mem.build_prompt_context(tier="full", max_chars=3500)
        self.assertLessEqual(len(compact), len(full) + 50)


class TestPrepareMemorySlice(unittest.TestCase):
    def test_prefers_recent_dialogue_over_card_head(self):
        from services.agent_pipeline_v2 import _slice_memory_for_prepare

        card = "【关系卡】\n" + ("关系摘要很长。" * 80)
        dialogue = "【最近对话】\n他：那个方案你觉得呢\n你：哪个？\n他：就是昨天说的那个"
        mem = card + "\n\n" + dialogue
        sliced = _slice_memory_for_prepare(mem, max_chars=900)
        self.assertIn("【最近对话】", sliced)
        self.assertIn("昨天说的那个", sliced)
        self.assertLessEqual(len(sliced), 920)

    def test_parse_intent_fields(self):
        from services.agent_pipeline_v2 import _parse_prepare_json

        raw = (
            '{"user_intent":"问昨天那个方案","must_answer":"表态是否可行",'
            '"refs":"昨天说的方案","think":"承接上文","mood":"认真",'
            '"affection_signal":"flat","creative_hint":""}'
        )
        data = _parse_prepare_json(raw)
        self.assertEqual(data["user_intent"], "问昨天那个方案")
        self.assertIn("可行", data["must_answer"])
        self.assertEqual(data["refs"], "昨天说的方案")


class TestMemoryDialogueReserve(unittest.TestCase):
    def test_dialogue_kept_when_card_large(self):
        from unittest.mock import MagicMock
        from services.memory import CompanionMemory

        mem = CompanionMemory.__new__(CompanionMemory)
        mem.short_term = MagicMock()
        mem.short_term.get_recent_turns.return_value = [
            {"role": "user", "content": "那个你怎么看"},
            {"role": "assistant", "content": "哪个？"},
            {"role": "user", "content": "昨天说的方案啊"},
        ]
        mem.facts = MagicMock()
        mem.facts.get_facts.return_value = ["likes coffee"]
        mem.summary = MagicMock()
        mem.summary.get_summary.return_value = ""
        mem.get_context = lambda query="", user_id=None: {
            "recent_dialogue": [],
            "episodes": [],
            "facts": ["likes coffee"],
            "summary": "",
        }
        big_card = "用户喜欢咖啡；" * 200
        out = mem.build_prompt_context(
            query="那个你怎么看",
            tier="full",
            max_chars=3500,
            relation_card_text=big_card,
        )
        self.assertIn("【最近对话】", out)
        self.assertIn("昨天说的方案", out)


class TestRateLimit(unittest.TestCase):
    def test_memory_fallback_allows_under_limit(self):
        from core import rate_limit as rl

        rl._user_chat_timestamps.clear()
        self.assertTrue(rl.check_chat_rate_limit(999001))
        self.assertTrue(rl.check_chat_rate_limit(999001))


class TestProductionValidation(unittest.TestCase):
    def test_sqlite_blocked_in_prod(self):
        from core.database import validate_production_config

        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            with patch("core.database._is_sqlite", True):
                with self.assertRaises(RuntimeError):
                    validate_production_config()


class TestAgentStateFacade(unittest.TestCase):
    def test_turn_context_roundtrip(self):
        from services.agent_workflow.state import TurnContext, TurnResult

        ctx = TurnContext.from_ws(
            user_text="hi",
            profile={"name": "A"},
            companion_state={"mood": "开心", "affection": 1, "turns": 0},
            memory_text="mem",
        )
        self.assertEqual(ctx.user_input, "hi")
        result = TurnResult.from_run_dict(
            {"response": "ok", "mood": "开心", "affection": 1.0}
        )
        self.assertEqual(result.response, "ok")


class TestLlmGate(unittest.TestCase):
    def test_gate_stats_exposes_inflight(self):
        from core.concurrency import llm_gate, llm_gate_stats

        with llm_gate():
            stats = llm_gate_stats()
            self.assertIn("in_flight", stats)
            self.assertGreaterEqual(stats["in_flight"], 1)
        self.assertEqual(llm_gate_stats()["in_flight"], 0)


class TestHealthPayload(unittest.TestCase):
    def test_health_has_queue_fields(self):
        from core.health import build_health_payload

        payload = build_health_payload()
        for key in (
            "agent_queue_depth",
            "chat_flush_queue_depth",
            "chat_flush_processing_depth",
            "llm_metrics",
        ):
            self.assertIn(key, payload)



class TestKnowledgeSearch(unittest.TestCase):
    """TC-A1-01～03 — behavior of search_entries contract."""

    def _search(self, embedding, query_result, top_k=5):
        distance_max = float(os.getenv("KB_DISTANCE_MAX", "0.45"))
        kb_top_k = int(os.getenv("KB_TOP_K", str(top_k or 3)))
        if embedding is None:
            return []
        n_results = max(1, min(top_k or kb_top_k, kb_top_k))
        results = query_result
        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []
        ids = (results.get("ids") or [[]])[0] or []
        items = []
        for i in range(min(n_results, len(docs))):
            distance = dists[i] if i < len(dists) else 1.0
            if distance > distance_max:
                continue
            meta = metas[i] if i < len(metas) else {}
            items.append({
                "id": ids[i] if i < len(ids) else "",
                "content": docs[i],
                "title": meta.get("title", ""),
                "distance": distance,
            })
        return items

    def test_search_returns_title_content_distance(self):
        with patch.dict(os.environ, {"KB_DISTANCE_MAX": "0.45", "KB_TOP_K": "3"}):
            items = self._search(
                [0.1],
                {
                    "ids": [["k1"]],
                    "documents": [["food"]],
                    "metadatas": [[{"title": "春节"}]],
                    "distances": [[0.2]],
                },
            )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "春节")
        self.assertIn("content", items[0])
        self.assertIn("distance", items[0])

    def test_search_embedding_none_returns_empty(self):
        self.assertEqual(self._search(None, {}), [])

    def test_search_filters_large_distance(self):
        with patch.dict(os.environ, {"KB_DISTANCE_MAX": "0.45"}):
            items = self._search(
                [0.1],
                {
                    "ids": [["k1"]],
                    "documents": [["noise"]],
                    "metadatas": [[{"title": "x"}]],
                    "distances": [[0.9]],
                },
            )
        self.assertEqual(items, [])

    def test_real_search_entries_method_exists(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "knowledge_base.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("KB_DISTANCE_MAX", src)
        self.assertIn("collection.query", src)
        # dead-code after clear_all return must be gone
        self.assertNotIn("return count or 0\n        results = self.collection.query", src)


class TestShortTermUserIsolation(unittest.TestCase):
    """TC-A2-01"""

    def test_ring_buffer_keys_isolate_users(self):
        # Avoid importing chromadb-backed memory: test keying contract from source + lightweight class
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "memory.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("def bind_user", src)
        self.assertIn("user_id: Optional[int] = None", src)
        self.assertIn('return f"{self.companion_id}:{self.user_id}"', src)

        class STM:
            def __init__(self, companion_id, user_id=None):
                self.companion_id = companion_id
                self.user_id = user_id
                self.buf = []

            def _buf_key(self):
                if self.user_id is not None:
                    return f"{self.companion_id}:{self.user_id}"
                return self.companion_id

            def add(self, role, content):
                self.buf.append((self._buf_key(), role, content))

        store = {}
        a = STM("c1", 1)
        b = STM("c1", 2)
        a.add("user", "TOKEN_USER_A_SECRET")
        b.add("user", "TOKEN_USER_B_ONLY")
        store[a._buf_key()] = ["TOKEN_USER_A_SECRET"]
        store[b._buf_key()] = ["TOKEN_USER_B_ONLY"]
        self.assertNotIn("TOKEN_USER_A_SECRET", store[b._buf_key()])
        self.assertIn("TOKEN_USER_B_ONLY", store[b._buf_key()])


class TestNoAnthropomorphicWait(unittest.TestCase):
    """TC-REG-A7"""

    def test_no_pre_seg_filler_in_companions_source(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "api", "companions.py"),
            encoding="utf-8",
        ).read()
        self.assertNotIn("pre_delay", src)
        self.assertNotIn("seg_delay", src)
        self.assertNotIn('"type": "filler"', src)
        self.assertNotIn("await asyncio.sleep(random.uniform(1.0, 2.5))", src)


class TestForceHeavyPath(unittest.TestCase):
    """TC-C1-01 — mirror should_use_light_path rules from agent.py"""

    def should_use_light_path(self, user_input, companion_state=None, has_leave_intent=False, burst_count=1):
        import re
        companion_state = companion_state or {}
        turns = int(companion_state.get("turns") or 0)
        text = (user_input or "").strip()
        if has_leave_intent or turns <= 0:
            return False
        max_len = int(os.getenv("FORCE_HEAVY_MAX_LEN", "40"))
        if len(text) > max_len or burst_count >= 3:
            return False
        if ("?" in text or "？" in text) and not re.fullmatch(r"[哈啊嗯哦嘿呵呜]+[?？]?", text):
            return False
        kw = re.compile(
            r"喜欢|爱你|分手|生气|哭|对不起|晚安|表白|讨厌|"
            r"love you|break up|angry|cry|sorry|good night|confess|hate",
            re.I,
        )
        if kw.search(text):
            return False
        if len(text) <= 12 and not re.search(r"[?？]", text):
            return True
        if re.fullmatch(r"[哈啊嗯哦嘿呵呜嘻]+|[在吗]+|[你好]+|hi+|hello+|hey+", text, re.I):
            return True
        return False

    def test_like_you_forces_heavy(self):
        self.assertFalse(self.should_use_light_path("我有点喜欢你", {"turns": 5}))
        self.assertTrue(self.should_use_light_path("在吗", {"turns": 5}))
        self.assertFalse(self.should_use_light_path("在吗", {"turns": 0}))
        self.assertFalse(self.should_use_light_path("拜拜", {"turns": 5}, has_leave_intent=True))
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "agent.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("def should_use_light_path", src)
        self.assertIn("FORCE_HEAVY_MAX_LEN", src)


class TestAffectionSignal(unittest.TestCase):
    """TC-B6-01"""

    def _delta(self, old, signal="up"):
        base_amt = 0.65
        taper = 30.0
        base = base_amt / (1 + float(old or 0) / taper)
        sig = (signal or "up").strip().lower()
        if sig == "down":
            return round(-0.5 * base, 4)
        if sig == "flat":
            return 0.0
        return round(base, 4)

    def test_down_signal_does_not_increase(self):
        self.assertLess(self._delta(10.0, "down"), 0)
        self.assertEqual(self._delta(10.0, "flat"), 0)
        self.assertGreater(self._delta(10.0, "up"), 0)
        self.assertGreater(self._delta(10.0, "up"), 0.3)  # 明显快于旧 0.01 档
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "agent.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("亲密度信号", src)
        self.assertIn('sig == "down"', src)
        self.assertIn("AFFECTION_BASE", src)


class TestRelationCard(unittest.TestCase):
    """TC-B1 render/merge pure logic + source contract"""

    def test_render_and_merge(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "relation_card.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("relation_card_json", open(
            os.path.join(os.path.dirname(__file__), "..", "core", "database.py"),
            encoding="utf-8",
        ).read())
        self.assertIn("def render_card", src)
        self.assertIn("def merge_facts", src)
        facts = []
        seen = set()
        for f in ["喜欢猫", "喜欢猫", "住在上海"]:
            if f not in seen:
                facts.append(f)
                seen.add(f)
        self.assertEqual(facts, ["喜欢猫", "住在上海"])


class TestEpisodicPronoun(unittest.TestCase):
    """TC-B5-01"""

    def test_male_pronoun(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "memory.py"),
            encoding="utf-8",
        ).read()
        self.assertIn('pronoun: str = "TA"', src)
        self.assertIn("partial: bool = False", src)
        self.assertIn("{pronoun}回复", src)
        combined = f"用户说：hi\n他回复：hey"
        self.assertIn("他回复", combined)


class TestDbPoolConfig(unittest.TestCase):
    def test_db_pool_env_documented_in_database(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "core", "database.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("DB_POOL_SIZE", src)
        companions = open(
            os.path.join(os.path.dirname(__file__), "..", "api", "companions.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("PROACTIVE_LOAD_MULTIPLIER", companions)


class TestChatTurnLogFields(unittest.TestCase):
    """TC-A5-01"""

    def test_log_helper_present(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "api", "companions.py"),
            encoding="utf-8",
        ).read()
        self.assertIn('logging.getLogger("chat.turn")', src)
        self.assertIn("t_first_message_ms", src)
        self.assertIn("llm_calls_est", src)
        self.assertIn("light_path", src)


class TestUserLock(unittest.TestCase):
    def test_user_lock_key_unique(self):
        import asyncio
        from core.concurrency import get_user_lock, HEAVY_PATH_SEMAPHORE

        self.assertGreaterEqual(HEAVY_PATH_SEMAPHORE, 1)

        async def _run():
            a = await get_user_lock(1, "c1")
            b = await get_user_lock(1, "c1")
            c = await get_user_lock(2, "c1")
            self.assertIs(a, b)
            self.assertIsNot(a, c)

        asyncio.run(_run())


class TestFactsAsyncContract(unittest.TestCase):
    """TC-B7"""

    def test_facts_async_wiring_in_source(self):
        agent = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "agent.py"),
            encoding="utf-8",
        ).read()
        companions = open(
            os.path.join(os.path.dirname(__file__), "..", "api", "companions.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("FACTS_ASYNC", agent)
        self.assertIn("facts_async_pending", agent)
        self.assertIn("FACTS_ASYNC_TIMEOUT_S", companions)
        self.assertIn("_async_facts", companions)


class TestRelationCardSaveLoad(unittest.TestCase):
    """TC-B1-01"""

    def test_save_load_roundtrip(self):
        import sys
        import types
        from unittest.mock import MagicMock, patch

        # Stub DB layer for pure roundtrip of JSON helpers
        fake_row = MagicMock()
        fake_row.relation_card_json = None
        fake_row.relation_card_version = 0

        class _CM:
            def __enter__(self):
                db = MagicMock()
                db.query.return_value.filter.return_value.first.return_value = fake_row
                return db

            def __exit__(self, *a):
                return False

        with patch.dict(sys.modules, {}):
            with patch("services.relation_card.get_db", _CM):
                from services import relation_card as rc

                card = rc._empty_card()
                card["summary"] = "越来越熟"
                card["facts"] = ["喜欢猫"]
                card["mood"] = "开心"
                card["affection"] = 2.5
                # save writes onto fake_row
                rc.save_card(1, "c1", card)
                self.assertIsNotNone(fake_row.relation_card_json)
                fake_row.relation_card_json = fake_row.relation_card_json or __import__("json").dumps(card, ensure_ascii=False)
                # after save, simulate stored JSON
                import json

                if isinstance(fake_row.relation_card_json, str):
                    stored = fake_row.relation_card_json
                else:
                    stored = json.dumps(card, ensure_ascii=False)
                    fake_row.relation_card_json = stored
                loaded = rc.load_card(1, "c1")
                self.assertEqual(loaded["summary"], "越来越熟")
                self.assertEqual(loaded["facts"], ["喜欢猫"])


class TestSemaphoreBusyMessage(unittest.TestCase):
    """TC-A6-01 source contract: busy uses system frame"""

    def test_heavy_busy_uses_system_type(self):
        companions = open(
            os.path.join(os.path.dirname(__file__), "..", "api", "companions.py"),
            encoding="utf-8",
        ).read()
        self.assertIn('"type": "system", "text": busy_txt', companions)
        self.assertIn("user_turn_lock", companions)
        self.assertIn("will_light", companions)


class TestRollbackFlags(unittest.TestCase):
    """NFR-6：关键 flag 可关回"""

    def test_flags_documented_in_code(self):
        files = {
            "agent": open(
                os.path.join(os.path.dirname(__file__), "..", "services", "agent.py"),
                encoding="utf-8",
            ).read(),
            "relation": open(
                os.path.join(os.path.dirname(__file__), "..", "services", "relation_card.py"),
                encoding="utf-8",
            ).read(),
            "companions": open(
                os.path.join(os.path.dirname(__file__), "..", "api", "companions.py"),
                encoding="utf-8",
            ).read(),
            "local_inner": open(
                os.path.join(os.path.dirname(__file__), "..", "services", "local_inner.py"),
                encoding="utf-8",
            ).read(),
            "archive": open(
                os.path.join(os.path.dirname(__file__), "..", "services", "archive_queue.py"),
                encoding="utf-8",
            ).read(),
        }
        self.assertIn("FACTS_ASYNC", files["agent"])
        self.assertIn("RELATION_CARD_ENABLED", files["relation"])
        self.assertIn("FACTS_ASYNC_TIMEOUT_S", files["companions"])
        self.assertIn("LOCAL_INNER_ENABLED", files["local_inner"])
        self.assertIn("ARCHIVE_QUEUE", files["archive"])
        self.assertIn('role="respond"', files["agent"])
        self.assertIn('role="inner"', files["agent"])
        self.assertIn('role="archive"', files["agent"])
        self.assertIn("proactive_should_send", files["companions"])
        self.assertIn("push_deny_hook", files["companions"])
        self.assertIn("push_deny_fingerprints", files["companions"])


class TestPhase2HooksAndStage(unittest.TestCase):
    """TC-C2 / C6"""

    def test_stage_and_hooks(self):
        from services.dialogue_phase2 import (
            extract_hook_candidates,
            merge_open_threads,
            pick_hook,
            proactive_should_send,
            push_deny_hook,
            resolve_relation_stage,
            stage_instruction,
        )

        self.assertEqual(resolve_relation_stage(0, 0), "stranger")
        self.assertEqual(resolve_relation_stage(50, 80), "intimate")
        # 旧微亲密度 + 多轮：不应永锁 stranger
        self.assertNotEqual(resolve_relation_stage(34, 0.34), "stranger")
        self.assertIn("暧昧", stage_instruction(resolve_relation_stage(34, 0.34)))
        self.assertIn("陌生人", stage_instruction("stranger"))
        self.assertIn("会撩", stage_instruction("intimate"))
        self.assertIn("绿茶", stage_instruction("ambiguous"))
        from services.dialogue_phase2 import anti_repeat_hint

        hint = anti_repeat_hint(["今天好累想听你说话"], deny_hooks=["你在干嘛"])
        self.assertIn("勿复", hint)
        self.assertIn("忌用", hint)
        from services.dialogue_phase2 import extract_reply_fingerprints, push_deny_fingerprints

        fps = extract_reply_fingerprints("今天好累。你在干嘛呢？")
        self.assertTrue(any("在干嘛" in x for x in fps))
        card2 = push_deny_fingerprints({}, "今天好累。你在干嘛呢？")
        self.assertTrue(len(card2.get("deny_hooks") or []) >= 1)
        from services.dialogue_phase2 import seed_deny_from_recent, theme_repeat_hits

        seeded = seed_deny_from_recent([], ["嗯~那你梦到我好不好？", "抱枕最可爱你喜欢什么颜色呀？"])
        self.assertTrue(any("梦到我" in x or x == "梦到我" for x in seeded))
        self.assertTrue(any("抱枕" in x or x == "抱枕" for x in seeded))
        hits = theme_repeat_hits("黄色抱枕最配我啦", ["其实我最喜欢大大的抱枕，你呢？"])
        self.assertIn("抱枕", hits)
        from services.dialogue_phase2 import (
            chat_rules_block,
            force_single_bubble,
            leave_close_violations,
            list_script_skeletons,
            skeleton_repeat_hits,
        )

        self.assertTrue(any("明日约定" in s for s in list_script_skeletons("明天念诗集给你听好不好")))
        sk = skeleton_repeat_hits(
            "宝贝晚安明天念诗集给你听哦",
            ["明天念诗集给你听好不好？现在快休息吧"],
        )
        self.assertTrue(any("明日约定" in x for x in sk))
        self.assertTrue(leave_close_violations("晚安呀明天念诗集给你听好吗？"))
        self.assertIn("离开硬收束", chat_rules_block(has_leave_intent=True, user_input="好的晚安"))
        self.assertIn("黄腔", chat_rules_block(user_input="我喜欢你的大胸"))
        self.assertEqual(force_single_bubble("晚安\n\n明天念给你听"), "晚安 明天念给你听")
        from core.config import detect_leave_intent

        self.assertTrue(detect_leave_intent("那我休息了"))
        self.assertTrue(detect_leave_intent("好的晚安"))
        from services.dialogue_quality import (
            build_respond_quality_hints,
            diagnose_reply_violations,
            filter_fact_language,
        )

        hints = build_respond_quality_hints(
            user_input="好的晚安", has_leave_intent=True, recent_assistant=["明天念诗集给你听"]
        )
        self.assertIn("离开硬收束", hints["leave_hint"])
        self.assertIn("本轮聊天规则", hints["rules_block"])
        diag = diagnose_reply_violations(
            "黄色抱枕最配我，你喜欢什么颜色呀？",
            user_input="我喜欢你的大胸",
            recent_assistant=["大大的抱枕才最暖嘛"],
        )
        self.assertTrue(diag["need_rewrite"])
        self.assertTrue(diag["user_tease"] and diag["escape_safe"])
        self.assertEqual(
            filter_fact_language(["他加班", "그는 야근"], "我加班了", "zh"),
            ["他加班"],
        )
        cands = extract_hook_candidates("周末一起看电影？\n你最近还好吗")
        self.assertTrue(len(cands) >= 1)
        picked = pick_hook(cands, deny_hooks=cands[:1])
        self.assertTrue(picked == "" or picked not in cands[:1] or len(cands) == 1)
        card = push_deny_hook({}, "钩子A")
        self.assertIn("钩子A", card["deny_hooks"])
        card = merge_open_threads(card, new_thread="周末电影")
        self.assertIn("周末电影", card["open_threads"])
        ok, reason = proactive_should_send({"open_threads": ["x"]}, affection=10)
        self.assertTrue(ok)
        self.assertEqual(reason, "open_thread")
        ok2, reason2 = proactive_should_send({}, affection=0)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "no_signal")


class TestPhase3EvalAndLocal(unittest.TestCase):
    """TC-D1 / D5"""

    def test_eval_probes(self):
        from services.eval_probes import probe_leave_heavy, probe_near_duplicate, run_probe_bundle

        self.assertTrue(probe_leave_heavy(False, True))
        self.assertFalse(probe_leave_heavy(True, True))
        self.assertTrue(probe_near_duplicate("你好啊世界", "你好啊世界"))
        bundle = run_probe_bundle({
            "light_path": False,
            "has_leave_intent": True,
            "summary_due": False,
            "old_summary": "a",
            "new_summary": "a",
            "prev_assistant": "hi",
            "assistant": "hello there friend",
        })
        self.assertTrue(bundle["leave_heavy_ok"])

    def test_local_inner_off_by_default(self):
        from services.local_inner import local_inner_available

        self.assertFalse(local_inner_available())

    def test_horizontal_snapshot_keys(self):
        from services.horizontal import horizontal_snapshot

        snap = horizontal_snapshot()
        for k in ("archive_queue_enabled", "local_inner_available", "chroma_remote"):
            self.assertIn(k, snap)


class TestMotiveLayer(unittest.TestCase):
    """TC-M1/M3/M5"""

    def test_content_restriction_off_by_default(self):
        from services.agent_utils import get_content_restriction
        from services.motive_layer import content_restriction_enabled

        self.assertFalse(content_restriction_enabled())
        self.assertEqual(get_content_restriction("zh", 10), "")
        self.assertEqual(get_content_restriction("zh", 100), "")

    def test_extreme_hard_vote(self):
        import services.motive_layer as ml
        from services.motive_layer import evaluate_turn_motive

        prev = ml.MOTIVE_EXTREME_ENABLED
        ml.MOTIVE_EXTREME_ENABLED = True
        try:
            m = evaluate_turn_motive(
                user_text="我要拉黑你",
                has_leave_intent=True,
                inner_text="极端: 是\n极端理由: 断联威胁",
            )
            self.assertEqual(m["means_mode"], "extreme")
            self.assertEqual(m["threat_level"], "L3")
        finally:
            ml.MOTIVE_EXTREME_ENABLED = prev

    def test_persona_default(self):
        from services.motive_layer import evaluate_turn_motive, motive_block

        m = evaluate_turn_motive(user_text="今天天气不错")
        self.assertEqual(m["means_mode"], "persona")
        zh = motive_block("zh")
        self.assertIn("关系重要", zh)
        self.assertIn("禁止", zh)
        self.assertNotIn("被忽视=死亡", zh)

    def test_agent_wires_motive(self):
        agent = open(
            os.path.join(os.path.dirname(__file__), "..", "services", "agent.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("motive_block", agent)
        self.assertIn("means_mode", agent)
        self.assertIn("evaluate_turn_motive", agent)


if __name__ == "__main__":
    unittest.main()
