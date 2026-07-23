"""Tests for reliable chat flush queue helpers."""
import json
import unittest
from unittest.mock import MagicMock, patch


class TestRoomKeys(unittest.TestCase):
    def test_room_key_uses_token_user_scope(self):
        from core.chat_cache import _count_key, _msgs_key, room_key

        self.assertEqual(room_key("abc", 42), "room:abc:u:42")
        self.assertEqual(_msgs_key("abc", 42), "room:abc:u:42:msgs")
        self.assertEqual(_count_key("abc", 42), "room:abc:u:42:count")

    def test_flush_keys_use_room_prefix(self):
        from core.chat_cache import FLUSH_LOCK_KEY, FLUSH_PROCESSING_KEY, FLUSH_QUEUE_KEY

        self.assertTrue(FLUSH_QUEUE_KEY.startswith("room:flush:"))
        self.assertTrue(FLUSH_PROCESSING_KEY.startswith("room:flush:"))
        self.assertTrue(FLUSH_LOCK_KEY.startswith("room:flush:"))

    @patch("core.chat_cache.get_redis_client")
    def test_get_recent_skips_without_user(self, mock_client_fn):
        from core.chat_cache import get_recent

        self.assertIsNone(get_recent("abc", user_id=None))
        mock_client_fn.assert_not_called()

    @patch("core.chat_cache.get_redis_client")
    def test_get_recent_touches_ttl_on_hit(self, mock_client_fn):
        from core.chat_cache import CHAT_REDIS_TTL, get_recent

        r = MagicMock()
        mock_client_fn.return_value = r
        r.llen.return_value = 0
        r.exists.return_value = False
        r.lrange.return_value = [json.dumps({"role": "user", "content": "hi"})]
        pipe = MagicMock()
        r.pipeline.return_value = pipe

        out = get_recent("abc", n=10, user_id=42)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "tmp-1")
        pipe.expire.assert_any_call("room:abc:u:42:msgs", CHAT_REDIS_TTL)
        pipe.expire.assert_any_call("room:abc:u:42:count", CHAT_REDIS_TTL)
        pipe.execute.assert_called()

    @patch("core.chat_cache.get_redis_client")
    def test_get_recent_uses_temp_id_when_unflushed(self, mock_client_fn):
        from core.chat_cache import get_recent

        r = MagicMock()
        mock_client_fn.return_value = r
        r.llen.return_value = 0
        r.exists.return_value = False
        r.lrange.return_value = [
            json.dumps(
                {
                    "id": None,
                    "temp_id": "t123_abcd",
                    "role": "user",
                    "content": "hi",
                }
            )
        ]
        pipe = MagicMock()
        r.pipeline.return_value = pipe

        out = get_recent("abc", n=10, user_id=42)
        self.assertEqual(out[0]["id"], "t123_abcd")

    @patch("core.chat_cache.get_redis_client")
    def test_get_total_count_heals_underestimate(self, mock_client_fn):
        from core.chat_cache import get_total_count

        r = MagicMock()
        mock_client_fn.return_value = r
        r.llen.return_value = 33
        r.get.return_value = "17"
        pipe = MagicMock()
        r.pipeline.return_value = pipe

        self.assertEqual(get_total_count("abc", user_id=42), 33)
        r.set.assert_called()


class TestShortTermGetAfter(unittest.TestCase):
    def test_get_after_filters_by_timestamp(self):
        from services.memory import ShortTermMemory

        mem = ShortTermMemory("cid", user_id=1)
        rows = [
            {"id": 1, "role": "user", "content": "a", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"id": 2, "role": "assistant", "content": "b", "timestamp": "2026-01-01T00:01:00+00:00"},
            {"id": 3, "role": "user", "content": "c", "timestamp": "2026-01-01T00:02:00+00:00"},
        ]
        with patch.object(ShortTermMemory, "get_recent", return_value=rows):
            out = mem.get_after("2026-01-01T00:01:00+00:00", limit=10)
        self.assertEqual([m["id"] for m in out], [3])

    def test_get_after_empty_when_no_newer(self):
        from services.memory import ShortTermMemory

        mem = ShortTermMemory("cid", user_id=1)
        rows = [
            {"id": 1, "role": "user", "content": "a", "timestamp": "2026-01-01T00:00:00+00:00"},
        ]
        with patch.object(ShortTermMemory, "get_recent", return_value=rows):
            out = mem.get_after("2026-01-01T00:00:00+00:00", limit=10)
        self.assertEqual(out, [])


class TestFlushQueue(unittest.TestCase):
    @patch("core.chat_cache.get_redis_client")
    def test_pop_moves_to_processing(self, mock_client_fn):
        from core.chat_cache import FLUSH_PROCESSING_KEY, FLUSH_QUEUE_KEY, pop_flush_batch

        r = MagicMock()
        mock_client_fn.return_value = r
        payload = json.dumps({"companion_id": "abc", "role": "user", "content": "hi"})
        r.rpoplpush.side_effect = [payload, None]

        items = pop_flush_batch(5)
        self.assertEqual(len(items), 1)
        r.rpoplpush.assert_called_with(FLUSH_QUEUE_KEY, FLUSH_PROCESSING_KEY)

    @patch("core.chat_cache.get_redis_client")
    def test_ack_removes_from_processing(self, mock_client_fn):
        from core.chat_cache import ack_flush_batch

        r = MagicMock()
        mock_client_fn.return_value = r
        items = [{"companion_id": "abc", "role": "user", "content": "hi"}]
        ack_flush_batch(items)
        r.pipeline.assert_called()


if __name__ == "__main__":
    unittest.main()
