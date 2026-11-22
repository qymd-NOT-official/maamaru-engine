# -*- coding: utf-8 -*-
"""「自动送修行」开关 API：配置缺键一律读关、写开即落盘、只动这一个键。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import server


class TrainingAutoSendApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "touken.json"
        # 老配置：daily 区没有 training_auto_send 键
        self.cfg.write_text(json.dumps(
            {"daily": {"sortie": {"mode": "none"}}}), encoding="utf-8")
        self.patch = patch.object(server, "_CONFIG_PATH", self.cfg)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(server.app)

    def test_missing_key_reads_off(self):
        result = self.client.get("/api/training-auto-send")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["enabled"])

    def test_put_on_persists_and_roundtrips(self):
        result = self.client.put("/api/training-auto-send",
                                 json={"enabled": True})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["enabled"])
        self.assertTrue(self.client.get("/api/training-auto-send")
                        .json()["enabled"])
        saved = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertIs(saved["daily"]["training_auto_send"], True)
        # 只动这一个键，别的配置原样
        self.assertEqual(saved["daily"]["sortie"], {"mode": "none"})

    def test_put_off_persists(self):
        self.client.put("/api/training-auto-send", json={"enabled": True})
        result = self.client.put("/api/training-auto-send",
                                 json={"enabled": False})
        self.assertFalse(result.json()["enabled"])
        self.assertFalse(self.client.get("/api/training-auto-send")
                         .json()["enabled"])


if __name__ == "__main__":
    unittest.main()
