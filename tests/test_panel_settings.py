import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import server


class PanelSettingsTests(unittest.TestCase):
    def test_scenery_persists_without_overwriting_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "_SETTINGS_FILE", Path(tmp) / "settings.json"):
                client = TestClient(server.app)
                client.post("/api/saved-settings", json={"theme": "pixel", "params": {"daily": {"runs": 5}}})
                for scenery in (
                    "autumn", "moonview", "winter", "after_rain", "seaside_day",
                    "seaside_sunset", "wisteria", "osaka_hall", "random", "spring",
                ):
                    client.post("/api/saved-settings", json={"scenery": scenery})
                    self.assertEqual(client.get("/api/saved-settings").json()["scenery"], scenery)
                client.post("/api/saved-settings", json={"scenery": "../../private"})
                saved = client.get("/api/saved-settings").json()
                self.assertEqual(saved["scenery"], "spring")
                self.assertEqual(saved["theme"], "pixel")
                self.assertEqual(saved["params"], {"daily": {"runs": 5}})

    def test_backdrop_saved_lowercased_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "panel_settings.json"
            with patch.object(server, "_SETTINGS_FILE", settings_file):
                client = TestClient(server.app)

                resp = client.post("/api/saved-settings", json={"backdrop": "#C4C6B5"})
                self.assertTrue(resp.json()["ok"])
                # 非法色值不得覆盖已保存的背景
                client.post("/api/saved-settings", json={"backdrop": "pink!"})

                saved = json.loads(settings_file.read_text("utf-8"))
                self.assertEqual(saved["backdrop"], "#c4c6b5")
                self.assertEqual(
                    client.get("/api/saved-settings").json()["backdrop"], "#c4c6b5")

    def test_backdrop_does_not_disturb_theme_and_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "panel_settings.json"
            with patch.object(server, "_SETTINGS_FILE", settings_file):
                client = TestClient(server.app)
                client.post("/api/saved-settings",
                            json={"theme": "pixel", "params": {"daily": {"runs": 5}}})
                client.post("/api/saved-settings", json={"backdrop": "#43503f"})

                saved = client.get("/api/saved-settings").json()
                self.assertEqual(saved["theme"], "pixel")
                self.assertEqual(saved["params"], {"daily": {"runs": 5}})
                self.assertEqual(saved["backdrop"], "#43503f")


if __name__ == "__main__":
    unittest.main()
