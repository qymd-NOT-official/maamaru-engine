# -*- coding: utf-8 -*-
"""日志存储测试：追加/最近拉取/after_id 增量/run_id 回看（跑况时间线的历史数据源）。"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from panel.log_store import LogStore


class LogStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="log_store_test_")
        self.addCleanup(self._tmp.cleanup)
        self.store = LogStore(Path(self._tmp.name) / "logs.db")
        self.addCleanup(self.store.close)  # LIFO：先关连接再放行临时目录

    def test_append_and_get_recent_roundtrip(self):
        self.store.append("run-a", "daily", "[日课] 开工")
        self.store.append("run-a", "daily", "[日课] ✓ 收工")
        rows = self.store.get_recent(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["run_id"], "run-a")
        self.assertEqual(rows[0]["script"], "daily")
        self.assertEqual(rows[0]["message"], "[日课] 开工")
        self.assertEqual(rows[1]["message"], "[日课] ✓ 收工")
        # id 递增、时间戳在场（时间线靠它算每步耗时）
        self.assertLess(rows[0]["id"], rows[1]["id"])
        self.assertGreater(rows[0]["ts"], 0)

    def test_after_id_pulls_only_newer(self):
        self.store.append("run-a", "daily", "one")
        self.store.append("run-a", "daily", "two")
        last = self.store.get_last_id()
        self.assertEqual(self.store.get_recent(limit=10, after_id=last), [])
        self.store.append("run-a", "daily", "three")
        rows = self.store.get_recent(limit=10, after_id=last)
        self.assertEqual([r["message"] for r in rows], ["three"])

    def test_opening_existing_database_preserves_old_chat_history(self):
        path = Path(self._tmp.name) / "old-logs.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE chat_history (id INTEGER PRIMARY KEY, ts REAL, role TEXT, content TEXT)")
            conn.execute("INSERT INTO chat_history VALUES (1, 0, 'user', 'old message')")

        store = LogStore(path)
        try:
            store.append("run-a", "daily", "new log")
            self.assertEqual(store.get_recent()[0]["message"], "new log")
        finally:
            store.close()

        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT content FROM chat_history").fetchall(), [("old message",)])

    def test_run_id_filter_returns_that_run_in_order(self):
        self.store.append("run-a", "daily", "a1")
        self.store.append("run-b", "smith", "b1")
        self.store.append("run-a", "daily", "a2")
        rows = self.store.get_recent(limit=50, run_id="run-a")
        self.assertEqual([r["message"] for r in rows], ["a1", "a2"])
        self.assertTrue(all(r["run_id"] == "run-a" for r in rows))
        # after_id 与 run_id 可以叠加（增量跟跑同一次 run）
        rows = self.store.get_recent(limit=50, run_id="run-a",
                                     after_id=rows[0]["id"])
        self.assertEqual([r["message"] for r in rows], ["a2"])
        self.assertEqual(self.store.get_recent(limit=50, run_id="ghost"), [])

    def test_run_id_limit_is_capped(self):
        for i in range(6000):
            self.store.append("run-big", "daily", f"line {i}")
        rows = self.store.get_recent(limit=99999, run_id="run-big")
        self.assertEqual(len(rows), 5000)


if __name__ == "__main__":
    unittest.main()
