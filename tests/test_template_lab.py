# -*- coding: utf-8 -*-
"""模板工坊后端测试。

覆盖：crop 坐标/尺寸/无 alpha/重名后缀/非法名 400/越界 400；
verify 贴合位置 score≈1.0、无关帧低分、撞车 confusion；
adopt 复制与覆盖备份；capture 假 adapter 存盘与错误收集、init 失败 503、账房 503；
会话列表倒序；session/name 路径穿越 400/404。

隔离红线（与 test_home_layout.py 同款）：模块头的 MAAMARU_DATA_DIR 环境补丁
只为 import panel.server 用，全量跑时可能已失效——真正的隔离靠
每个用例 patch template_lab.DEBUG_DIR / RESOURCE_DIR 到临时目录。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

# server 模块导入时填充脚本注册表 _SCRIPTS；在临时数据目录下做，
# 不碰真实用户数据。
_module_tmp = tempfile.TemporaryDirectory(prefix="template_lab_module_test_")
with patch.dict("os.environ", {"MAAMARU_DATA_DIR": _module_tmp.name}):
    import panel.server  # noqa: F401  (import 即完成脚本注册)
    from panel import template_lab
    from fastapi.testclient import TestClient

from touken import roi_overrides  # noqa: E402
from touken.device_adapter import Point, Region  # noqa: E402  (假 OCR 的返回类型)
from touken.roi_registry import ROI_REGISTRY  # noqa: E402


def _noise(width=320, height=180, seed=42):
    """合成 BGR 噪声帧（固定种子，结果可复现）。"""
    rng = np.random.default_rng(seed)
    return (rng.random((height, width, 3)) * 255).astype(np.uint8)


def _put_session(session_id, frames):
    """按工坊自己的命名/编码摆一个会话进临时 DEBUG_DIR。"""
    session_dir = template_lab._sessions_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    for idx, image in enumerate(frames):
        if image is not None:
            template_lab._save_bgr_png(image, session_dir / template_lab._frame_name(idx))
    return session_dir


class TemplateLabTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="template_lab_test_")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._patches = [
            patch.object(template_lab, "DEBUG_DIR", self.dir / "debug"),
            patch.object(template_lab, "RESOURCE_DIR", self.dir / "resource"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(panel.server.app)
        self.addCleanup(self.client.close)

    def _crop(self, session, frame, x, y, w, h, name, expect=200):
        response = self.client.post("/api/template-lab/crop", json={
            "session": session, "frame": frame, "x": x, "y": y,
            "w": w, "h": h, "name": name,
        })
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return response.json() if expect == 200 else response


class StatusTests(TemplateLabTestBase):
    def test_status_shape_in_dev(self):
        response = self.client.get("/api/template-lab/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body["enabled"], True)
        self.assertIs(body["ledger_mode"], False)
        self.assertIsInstance(body["adb_ready"], bool)

    def test_status_ledger_mode_disables(self):
        with patch.dict("os.environ", {"MAAMARU_LEDGER_MODE": "1"}):
            response = self.client.get("/api/template-lab/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body["ledger_mode"], True)
        self.assertIs(body["enabled"], False)


class CaptureTests(TemplateLabTestBase):
    def _fake_adapter(self, frames, init_ok=True):
        class FakeAdapter:
            def __init__(self):
                self.shots = 0

            def init(self):
                return init_ok

            def screenshot(self, force=False):
                self.shots += 1
                if self.shots > len(frames):
                    return None
                return frames[self.shots - 1]

        return FakeAdapter()

    def _capture(self, frames, init_ok=True, **body):
        adapter = self._fake_adapter(frames, init_ok=init_ok)
        with patch.object(template_lab, "_create_adapter", return_value=adapter), \
                patch.object(template_lab, "_sleep"):
            response = self.client.post("/api/template-lab/capture",
                                        json=body or {"count": 3, "interval_ms": 100})
        return response, adapter

    def test_capture_saves_frames_and_collects_errors(self):
        frames = [_noise(seed=1), None, _noise(seed=2)]
        response, adapter = self._capture(frames)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([f["idx"] for f in body["frames"]], [0, 2])
        self.assertEqual(len(body["errors"]), 1)
        self.assertEqual(len(body["frames"]), 2)
        for item, image in zip(body["frames"], [frames[0], frames[2]]):
            self.assertEqual(item["width"], image.shape[1])
            self.assertEqual(item["height"], image.shape[0])
            saved = template_lab._sessions_dir() / body["session"] / item["name"]
            self.assertTrue(saved.is_file())
        # 存盘的是 RGB PNG：PIL 读回应是 RGB 无 alpha
        from PIL import Image
        first = template_lab._sessions_dir() / body["session"] / body["frames"][0]["name"]
        self.assertEqual(Image.open(first).mode, "RGB")
        # 取帧接口能拿回这张图
        got = self.client.get("/api/template-lab/frame",
                              params={"session": body["session"], "idx": 2})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.headers["content-type"], "image/png")

    def test_capture_init_failure_is_503(self):
        response, _adapter = self._capture([_noise()], init_ok=False)
        self.assertEqual(response.status_code, 503, response.text)

    def test_capture_ledger_mode_is_503(self):
        with patch.dict("os.environ", {"MAAMARU_LEDGER_MODE": "1"}):
            response, _adapter = self._capture([_noise()])
        self.assertEqual(response.status_code, 503, response.text)

    def test_capture_count_is_clamped(self):
        response, adapter = self._capture([_noise(seed=i) for i in range(60)],
                                          count=999, interval_ms=100)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["frames"]), 50)
        self.assertEqual(adapter.shots, 50)

    def test_capture_with_memo_writes_meta(self):
        response, _adapter = self._capture([_noise()], memo="一花短刀正面\n")
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()["session"]
        session_dir = template_lab._sessions_dir() / session
        self.assertEqual(template_lab._read_session_memo(session_dir), "一花短刀正面")
        listed = self.client.get("/api/template-lab/sessions").json()["sessions"]
        self.assertEqual(listed[0]["memo"], "一花短刀正面")

    def test_capture_without_memo_has_no_meta(self):
        response, _adapter = self._capture([_noise()])
        session = response.json()["session"]
        session_dir = template_lab._sessions_dir() / session
        self.assertIsNone(template_lab._read_session_memo(session_dir))

    def test_capture_bad_body_is_400(self):
        adapter = self._fake_adapter([_noise()])
        with patch.object(template_lab, "_create_adapter", return_value=adapter), \
                patch.object(template_lab, "_sleep"):
            for bad in ({"count": "x"}, {"count": True}, {"interval_ms": "fast"}):
                response = self.client.post("/api/template-lab/capture", json=bad)
                self.assertEqual(response.status_code, 400, bad)


class SessionAndTraversalTests(TemplateLabTestBase):
    def test_sessions_sorted_by_id_descending_with_frame_meta(self):
        _put_session("20250101-010203", [_noise(seed=1), _noise(seed=2)])
        _put_session("20250102-030405", [_noise(seed=3)])
        response = self.client.get("/api/template-lab/sessions")
        self.assertEqual(response.status_code, 200, response.text)
        sessions = response.json()["sessions"]
        self.assertEqual([s["id"] for s in sessions],
                         ["20250102-030405", "20250101-010203"])
        older = sessions[1]
        self.assertEqual([f["idx"] for f in older["frames"]], [0, 1])
        for frame in older["frames"]:
            self.assertEqual(frame["width"], 320)
            self.assertEqual(frame["height"], 180)
            self.assertGreater(frame["mtime"], 0)

    def test_frame_path_traversal_rejected(self):
        _put_session("20250101-010203", [_noise()])
        for session in ("..", "../..", "..\\..", "20250101-010203/..", "abc"):
            response = self.client.get("/api/template-lab/frame",
                                       params={"session": session, "idx": 0})
            self.assertEqual(response.status_code, 400, session)

    def test_frame_missing_is_404(self):
        _put_session("20250101-010203", [_noise()])
        response = self.client.get("/api/template-lab/frame",
                                   params={"session": "20250101-010203", "idx": 9})
        self.assertEqual(response.status_code, 404, response.text)

    def test_session_memo_set_and_clear(self):
        _put_session("20250101-010203", [_noise()])
        response = self.client.post("/api/template-lab/session-memo", json={
            "session": "20250101-010203", "memo": "二花短刀反面"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["memo"], "二花短刀反面")
        listed = self.client.get("/api/template-lab/sessions").json()["sessions"]
        self.assertEqual(listed[0]["memo"], "二花短刀反面")
        # 留空清除
        cleared = self.client.post("/api/template-lab/session-memo", json={
            "session": "20250101-010203", "memo": "  "})
        self.assertIsNone(cleared.json()["memo"])

    def test_session_memo_missing_session_is_404_and_bad_id_400(self):
        for body, expect in (
                ({"session": "20990101-000000", "memo": "x"}, 404),
                ({"session": "../..", "memo": "x"}, 400)):
            response = self.client.post("/api/template-lab/session-memo", json=body)
            self.assertEqual(response.status_code, expect, repr(body))

    def test_crop_rejects_traversal_session(self):
        _put_session("20250101-010203", [_noise()])
        body = {"session": "../drafts", "frame": 0, "x": 0, "y": 0,
                "w": 10, "h": 10, "name": "x"}
        response = self.client.post("/api/template-lab/crop", json=body)
        self.assertEqual(response.status_code, 400, response.text)


class CropTests(TemplateLabTestBase):
    def setUp(self):
        super().setUp()
        self.frame = _noise(seed=7)
        self.patch_pixels = self.frame[40:70, 100:160].copy()
        _put_session("20250101-010203", [self.frame])

    def test_crop_geometry_and_pixels(self):
        body = self._crop("20250101-010203", 0, 100, 40, 60, 30, "刀装按钮")
        draft = body["draft"]
        self.assertEqual(draft, {"name": "刀装按钮", "width": 60, "height": 30})
        draft_path = template_lab._drafts_dir() / "刀装按钮.png"
        self.assertTrue(draft_path.is_file())
        # 输出 PNG 无 alpha、像素与源帧区域逐点一致（PNG 无损 + RGB 存盘约定）
        from PIL import Image
        self.assertEqual(Image.open(draft_path).mode, "RGB")
        decoded = template_lab._read_png(draft_path, color=True)
        np.testing.assert_array_equal(decoded, self.patch_pixels)

    def test_crop_duplicate_name_gets_suffix(self):
        first = self._crop("20250101-010203", 0, 0, 0, 10, 10, "btn")
        second = self._crop("20250101-010203", 0, 0, 0, 10, 10, "btn")
        third = self._crop("20250101-010203", 0, 0, 0, 10, 10, "btn")
        self.assertEqual(first["draft"]["name"], "btn")
        self.assertEqual(second["draft"]["name"], "btn-2")
        self.assertEqual(third["draft"]["name"], "btn-3")

    def test_crop_png_suffix_in_name_is_stripped(self):
        body = self._crop("20250101-010203", 0, 0, 0, 10, 10, "btn.png")
        self.assertEqual(body["draft"]["name"], "btn")

    def test_crop_invalid_names_are_400(self):
        for bad in ("../evil", "a/b", "a\\b", "", "..", ".", "x" * 81, 123):
            body = {"session": "20250101-010203", "frame": 0, "x": 0, "y": 0,
                    "w": 10, "h": 10, "name": bad}
            response = self.client.post("/api/template-lab/crop", json=body)
            self.assertEqual(response.status_code, 400, repr(bad))
        self.assertFalse((template_lab._drafts_dir()).exists()
                         or list(template_lab._drafts_dir().glob("*.png")))

    def test_crop_out_of_bounds_is_400(self):
        for rect in ({"x": -1, "y": 0, "w": 10, "h": 10},
                     {"x": 0, "y": 0, "w": 0, "h": 10},
                     {"x": 300, "y": 0, "w": 30, "h": 10},
                     {"x": 0, "y": 170, "w": 10, "h": 20}):
            body = {"session": "20250101-010203", "frame": 0, "name": "oob", **rect}
            response = self.client.post("/api/template-lab/crop", json=body)
            self.assertEqual(response.status_code, 400, rect)

    def test_crop_missing_frame_is_404(self):
        self._crop("20250101-010203", 9, 0, 0, 10, 10, "ghost", expect=404)

    def test_drafts_listing_and_fetch(self):
        self._crop("20250101-010203", 0, 0, 0, 10, 10, "list-a")
        self._crop("20250101-010203", 0, 20, 20, 15, 15, "list-b")
        response = self.client.get("/api/template-lab/drafts")
        self.assertEqual(response.status_code, 200, response.text)
        drafts = {d["name"]: d for d in response.json()["drafts"]}
        self.assertEqual(drafts["list-a"]["width"], 10)
        self.assertEqual(drafts["list-a"]["height"], 10)
        self.assertEqual(drafts["list-b"]["width"], 15)
        got = self.client.get("/api/template-lab/draft", params={"name": "list-b"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.headers["content-type"], "image/png")
        missing = self.client.get("/api/template-lab/draft", params={"name": "不存在"})
        self.assertEqual(missing.status_code, 404, missing.text)
        traversal = self.client.get("/api/template-lab/draft", params={"name": "../x"})
        self.assertEqual(traversal.status_code, 400, traversal.text)


class VerifyTests(TemplateLabTestBase):
    def setUp(self):
        super().setUp()
        # 会话一：两张帧，模板贴在 (100, 40)；会话二：纯无关噪声
        self.tpl = _noise(width=60, height=30, seed=99)
        self.pasted = _noise(seed=11)
        self.pasted[40:70, 100:160] = self.tpl
        _put_session("20250101-010203", [self.pasted, _noise(seed=12)])
        _put_session("20250102-030405", [_noise(seed=13)])
        body = self._crop("20250101-010203", 0, 100, 40, 60, 30, "贴合模板")
        self.draft_name = body["draft"]["name"]
        # verify 读的是存盘草稿：解码回来应与合成模板逐点一致
        self.draft_bgr = template_lab._read_png(
            template_lab._drafts_dir() / f"{self.draft_name}.png", color=True)
        np.testing.assert_array_equal(self.draft_bgr, self.tpl)

    def _verify(self, draft=None, sessions=None, threshold=0.7, expect=200):
        response = self.client.post("/api/template-lab/verify", json={
            "draft": draft or self.draft_name,
            "sessions": sessions or ["20250101-010203"],
            "threshold": threshold,
        })
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return response.json() if expect == 200 else response

    def test_pasted_template_scores_one_at_known_location(self):
        body = self._verify()
        self.assertEqual(body["draft"], self.draft_name)
        self.assertEqual(body["threshold"], 0.7)
        first = body["results"][0]
        self.assertEqual(first["frame"], 0)
        self.assertAlmostEqual(first["score"], 1.0, places=2)
        self.assertEqual(first["loc"], {"x": 100, "y": 40})
        self.assertIs(first["hit"], True)

    def test_unrelated_frames_score_low(self):
        body = self._verify(sessions=["20250101-010203", "20250102-030405"])
        by_key = {(r["session"], r["frame"]): r for r in body["results"]}
        self.assertAlmostEqual(by_key[("20250101-010203", 0)]["score"], 1.0, places=2)
        self.assertLess(by_key[("20250101-010203", 1)]["score"], 0.7)
        self.assertLess(by_key[("20250102-030405", 0)]["score"], 0.7)
        self.assertIs(by_key[("20250102-030405", 0)]["hit"], False)

    def test_threshold_is_echoed_and_hit_compares_against_it(self):
        body = self._verify(threshold=0.999)
        self.assertEqual(body["threshold"], 0.999)
        self.assertAlmostEqual(body["results"][0]["score"], 1.0, places=2)
        # 满分贴合 >= 0.999，照样命中；命中比较用的是原始分
        self.assertIs(body["results"][0]["hit"], True)

    def test_confusing_draft_is_reported_with_margin(self):
        # 干扰草稿：纯噪声，在贴满分的帧上必然低分；正主草稿分数更高 → 撞车必报
        noise_draft = _noise(width=60, height=30, seed=77)
        template_lab._save_bgr_png(noise_draft,
                                   template_lab._drafts_dir() / "干扰噪声.png")
        body = self._verify(draft="干扰噪声")
        hits = [c for c in body["confusion"]
                if c["other_draft"] == self.draft_name and c["frame"] == 0]
        self.assertEqual(len(hits), 1)
        self.assertGreaterEqual(hits[0]["other_score"], 0.7)
        self.assertGreater(hits[0]["margin"], 0.5)
        # 反过来验正主：没有别的草稿比它更贴
        body = self._verify()
        self.assertEqual(body["confusion"], [])

    def test_verify_missing_draft_is_404(self):
        self._verify(draft="不存在", expect=404)

    def test_verify_missing_session_is_404_and_bad_id_400(self):
        self._verify(sessions=["20990101-000000"], expect=404)
        self._verify(sessions=["../.."], expect=400)

    def test_verify_bad_threshold_is_400(self):
        response = self.client.post("/api/template-lab/verify", json={
            "draft": self.draft_name, "sessions": ["20250101-010203"],
            "threshold": 1.5,
        })
        self.assertEqual(response.status_code, 400, response.text)


class AdoptTests(TemplateLabTestBase):
    def setUp(self):
        super().setUp()
        self.frame = _noise(seed=21)
        _put_session("20250101-010203", [self.frame])
        self._crop("20250101-010203", 0, 5, 6, 30, 20, "采纳目标")

    def _adopt(self, draft="采纳目标", target="go_to_battle", expect=200):
        response = self.client.post("/api/template-lab/adopt",
                                    json={"draft": draft, "target": target})
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return response.json() if expect == 200 else response

    def test_adopt_copies_draft_into_resource_image(self):
        body = self._adopt()
        self.assertIs(body["ok"], True)
        dest = Path(body["path"])
        self.assertEqual(dest, self.dir / "resource" / "image" / "go_to_battle.png")
        self.assertTrue(dest.is_file())
        self.assertIsNone(body["backup"])
        np.testing.assert_array_equal(template_lab._read_png(dest, color=True),
                                      template_lab._read_png(
                                          template_lab._drafts_dir() / "采纳目标.png",
                                          color=True))

    def test_adopt_existing_target_creates_backup(self):
        first = self._adopt()
        self.assertIsNone(first["backup"])
        dest = Path(first["path"])
        old_bytes = dest.read_bytes()
        # 改草稿内容再采纳同目标：旧正式资源应被备份，新内容落位
        new_content = _noise(width=30, height=20, seed=33)
        template_lab._save_bgr_png(new_content,
                                   template_lab._drafts_dir() / "采纳目标.png")
        second = self._adopt()
        self.assertIsNotNone(second["backup"])
        backup = Path(second["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent, template_lab._adopt_backup_dir())
        # 备份的是旧版内容，目标文件已是新内容
        self.assertEqual(backup.read_bytes(), old_bytes)
        np.testing.assert_array_equal(template_lab._read_png(dest, color=True),
                                      new_content)

    def test_adopt_invalid_target_is_400(self):
        for bad in ("../evil", "a/b", "", 5):
            response = self.client.post("/api/template-lab/adopt",
                                        json={"draft": "采纳目标", "target": bad})
            self.assertEqual(response.status_code, 400, repr(bad))

    def test_adopt_into_existing_subdirectory(self):
        sub = self.dir / "resource" / "image" / "刀种"
        sub.mkdir(parents=True)
        body = self._adopt(target="刀种/一花短刀")
        dest = Path(body["path"])
        self.assertEqual(dest, sub / "一花短刀.png")
        self.assertTrue(dest.is_file())
        # 覆盖子目录目标时备份文件名里的 / 会被压平成 _
        again = self._adopt(target="刀种/一花短刀")
        self.assertIsNotNone(again["backup"])
        self.assertNotIn("/", Path(again["backup"]).name)

    def test_adopt_missing_subdirectory_is_400(self):
        self._adopt(target="不存在目录/一花短刀", expect=400)
        self._adopt(target="刀种/../../evil", expect=400)

    def test_adopt_missing_draft_is_404(self):
        self._adopt(draft="不存在", expect=404)


class RoiTests(TemplateLabTestBase):
    def _save(self, name="生存栏", x=10, y=20, w=200, h=40, expect=200):
        response = self.client.post("/api/template-lab/rois", json={
            "name": name, "x": x, "y": y, "w": w, "h": h})
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return response.json() if expect == 200 else response

    def test_save_then_list_roundtrip(self):
        saved = self._save()
        self.assertIs(saved["ok"], True)
        roi = saved["roi"]
        self.assertEqual({k: roi[k] for k in ("name", "x", "y", "w", "h")},
                         {"name": "生存栏", "x": 10, "y": 20, "w": 200, "h": 40})
        self.assertGreater(roi["updated"], 0)
        # 存档确实落在 rois.json，列表能原样读回来
        self.assertTrue(template_lab._rois_file().is_file())
        listed = self.client.get("/api/template-lab/rois").json()["rois"]
        self.assertEqual(listed, [roi])

    def test_list_sorted_by_name(self):
        self._save(name="刀装栏", x=0, y=0, w=10, h=10)
        self._save(name="生存栏", x=0, y=0, w=10, h=10)
        self._save(name="HP栏-2", x=0, y=0, w=10, h=10)
        names = [r["name"] for r in self.client.get("/api/template-lab/rois").json()["rois"]]
        self.assertEqual(names, sorted(names))

    def test_same_name_overwrites_with_fresh_updated(self):
        first = self._save(x=10, y=20, w=200, h=40)
        second = self._save(x=1, y=2, w=50, h=60)
        self.assertGreaterEqual(second["roi"]["updated"], first["roi"]["updated"])
        listed = self.client.get("/api/template-lab/rois").json()["rois"]
        self.assertEqual(len(listed), 1)
        self.assertEqual({k: listed[0][k] for k in ("x", "y", "w", "h")},
                         {"x": 1, "y": 2, "w": 50, "h": 60})

    def test_delete_roi(self):
        self._save()
        response = self.client.delete("/api/template-lab/rois/生存栏")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], True)
        self.assertEqual(self.client.get("/api/template-lab/rois").json()["rois"], [])
        self.assertNotIn("生存栏", template_lab._load_rois())

    def test_delete_missing_roi_is_404(self):
        response = self.client.delete("/api/template-lab/rois/不存在")
        self.assertEqual(response.status_code, 404, response.text)

    def test_save_invalid_names_are_400(self):
        for bad in ("../evil", "a/b", "a\\b", "", "..", ".", "x" * 81, 123):
            self._save(name=bad, expect=400)
        self.assertEqual(template_lab._load_rois(), {})

    def test_save_bad_rects_are_400(self):
        good = {"x": 10, "y": 20, "w": 100, "h": 40}
        for patch_rect in ({"w": 1}, {"h": 0}, {"x": -1}, {"y": -5},
                           {"x": 1270, "w": 11},          # 右缘 1281 > 1280
                           {"y": 700, "h": 21},           # 下缘 721 > 720
                           {"w": 1280, "h": 720},         # 从 (10,20) 起必然越界
                           {"x": 0.5}, {"y": "20"}, {"w": True}, {"h": None}):
            response = self.client.post("/api/template-lab/rois",
                                        json={"name": "越界测试", **good, **patch_rect})
            self.assertEqual(response.status_code, 400, repr(patch_rect))
        self.assertEqual(template_lab._load_rois(), {})

    def test_png_suffix_in_roi_name_is_stripped(self):
        saved = self._save(name="生存栏.png")
        self.assertEqual(saved["roi"]["name"], "生存栏")
        listed = self.client.get("/api/template-lab/rois").json()["rois"]
        self.assertEqual([r["name"] for r in listed], ["生存栏"])


class OcrTestTests(TemplateLabTestBase):
    def setUp(self):
        super().setUp()
        _put_session("20250101-010203", [_noise(seed=1), _noise(seed=2)])
        _put_session("20250102-030405", [_noise(seed=3)])
        self.client.post("/api/template-lab/rois", json={
            "name": "生存栏", "x": 100, "y": 40, "w": 120, "h": 30})

    def _fake_ocr_adapter(self):
        class FakeAdapter:
            def __init__(self):
                self.calls = []

            def ocr_all(self, roi, image=None):
                self.calls.append((roi, image))
                return [("生存 48/48", Point(160, 55))]

        return FakeAdapter()

    def _ocr_test(self, expect=200, sessions=None, name="生存栏"):
        adapter = self._fake_ocr_adapter()
        with patch.object(template_lab, "_create_ocr_adapter", return_value=adapter):
            response = self.client.post("/api/template-lab/ocr-test", json={
                "name": name,
                "sessions": sessions if sessions is not None else ["20250101-010203"],
            })
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return (response.json() if expect == 200 else response), adapter

    def test_reads_every_frame_with_roi_echoed(self):
        body, adapter = self._ocr_test(
            sessions=["20250101-010203", "20250102-030405"])
        self.assertEqual(body["roi"]["name"], "生存栏")
        self.assertEqual({k: body["roi"][k] for k in ("x", "y", "w", "h")},
                         {"x": 100, "y": 40, "w": 120, "h": 30})
        by_key = {(r["session"], r["frame"]): r["texts"] for r in body["results"]}
        self.assertEqual(by_key, {
            ("20250101-010203", 0): ["生存 48/48"],
            ("20250101-010203", 1): ["生存 48/48"],
            ("20250102-030405", 0): ["生存 48/48"],
        })
        # 每帧都喂给 ocr_all，且 ROI 矩形原样传入
        self.assertEqual(len(adapter.calls), 3)
        roi, image = adapter.calls[0]
        self.assertIsInstance(roi, Region)
        self.assertEqual(roi.to_tuple(), (100, 40, 120, 30))
        self.assertEqual(image.shape[:2], (180, 320))  # 合成帧默认 320×180

    def test_frame_count_is_capped_at_100(self):
        _put_session("20250103-050607", [_noise(width=32, height=18, seed=i)
                                         for i in range(120)])
        body, adapter = self._ocr_test(sessions=["20250103-050607"])
        self.assertEqual(len(body["results"]), 100)
        self.assertEqual(len(adapter.calls), 100)

    def test_bad_session_is_400_and_missing_is_404(self):
        self._ocr_test(sessions=["../.."], expect=400)
        self._ocr_test(sessions=["20990101-000000"], expect=404)
        self._ocr_test(sessions="20250101-010203", expect=400)
        self._ocr_test(sessions=[123], expect=400)

    def test_missing_roi_is_404_without_starting_ocr(self):
        factory = unittest.mock.Mock(
            side_effect=AssertionError("ROI 不存在就不该起 OCR 通道"))
        with patch.object(template_lab, "_create_ocr_adapter", factory):
            response = self.client.post("/api/template-lab/ocr-test", json={
                "name": "不存在", "sessions": ["20250101-010203"]})
        self.assertEqual(response.status_code, 404, response.text)
        factory.assert_not_called()

    def test_ocr_channel_failure_is_503(self):
        with patch.object(template_lab, "_create_ocr_adapter",
                          side_effect=RuntimeError("device 炸了")):
            response = self.client.post("/api/template-lab/ocr-test", json={
                "name": "生存栏", "sessions": ["20250101-010203"]})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("OCR 通道起不来", response.text)

    def test_unreadable_frame_is_skipped(self):
        with patch.object(template_lab, "_read_png", return_value=None):
            body, adapter = self._ocr_test()
        self.assertEqual(body["results"], [])
        self.assertEqual(adapter.calls, [])


class CodeRoiEndpointTests(TemplateLabTestBase):
    def setUp(self):
        super().setUp()
        # code-rois 走 roi_overrides 自己的 DEBUG_DIR，指到同一棵临时树
        p = patch.object(roi_overrides, "DEBUG_DIR", self.dir / "debug")
        p.start()
        self.addCleanup(p.stop)

    def test_list_shape_and_defaults(self):
        response = self.client.get("/api/template-lab/code-rois")
        self.assertEqual(response.status_code, 200, response.text)
        rois = response.json()["rois"]
        self.assertEqual(len(rois), len(ROI_REGISTRY))
        by_id = {item["id"]: item for item in rois}
        title = by_id["sword_inventory.title"]
        self.assertEqual(set(title),
                         {"id", "label", "used_in", "purpose", "default",
                          "override", "effective", "overridden"})
        self.assertEqual(title["default"], [400, 15, 880, 50])
        self.assertIsNone(title["override"])
        self.assertEqual(title["effective"], title["default"])
        self.assertIs(title["overridden"], False)

    def test_save_override_then_list_and_delete(self):
        response = self.client.post("/api/template-lab/code-rois", json={
            "id": "sword_inventory.title", "rect": [410, 20, 890, 60]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body["ok"], True)
        roi = body["roi"]
        self.assertEqual(roi["override"], [410, 20, 890, 60])
        self.assertEqual(roi["effective"], [410, 20, 890, 60])
        self.assertIs(roi["overridden"], True)
        # 真落盘了，且列表读回来还是覆盖态
        self.assertEqual(roi_overrides.get_roi("sword_inventory.title", None),
                         (410, 20, 890, 60))
        listed = {i["id"]: i for i in
                  self.client.get("/api/template-lab/code-rois").json()["rois"]}
        self.assertEqual(listed["sword_inventory.title"]["effective"],
                         [410, 20, 890, 60])
        # 删掉即恢复默认
        deleted = self.client.delete("/api/template-lab/code-rois/sword_inventory.title")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertIs(deleted.json()["ok"], True)
        listed = {i["id"]: i for i in
                  self.client.get("/api/template-lab/code-rois").json()["rois"]}
        self.assertIs(listed["sword_inventory.title"]["overridden"], False)
        self.assertEqual(listed["sword_inventory.title"]["effective"],
                         listed["sword_inventory.title"]["default"])

    def test_save_unknown_id_is_404(self):
        response = self.client.post("/api/template-lab/code-rois", json={
            "id": "不存在.id", "rect": [1, 2, 3, 4]})
        self.assertEqual(response.status_code, 404, response.text)

    def test_save_bad_rect_is_400(self):
        for rect in ([500, 10, 100, 60], [-1, 0, 10, 10], [0, 0, 1281, 10],
                     [0, 0, 10], [0, 0, 10, 720.5],
                     "abcd", [0, 0, 10, True], None):
            response = self.client.post("/api/template-lab/code-rois", json={
                "id": "sword_inventory.title", "rect": rect})
            self.assertEqual(response.status_code, 400, repr(rect))

    def test_delete_unknown_id_is_404(self):
        response = self.client.delete("/api/template-lab/code-rois/不存在.id")
        self.assertEqual(response.status_code, 404, response.text)

    def test_delete_without_override_is_ok(self):
        # 幂等恢复默认：注册表里有这个 id、从没存过覆盖也算成功
        response = self.client.delete("/api/template-lab/code-rois/formation_editor.list")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], True)


class OcrTestRectModeTests(TemplateLabTestBase):
    """ocr-test 直传 rect 模式：xywh 不查存储，name/rect 互斥。"""

    def setUp(self):
        super().setUp()
        _put_session("20250101-010203", [_noise(seed=1), _noise(seed=2)])

    def _fake_ocr_adapter(self):
        class FakeAdapter:
            def __init__(self):
                self.calls = []

            def ocr_all(self, roi, image=None):
                self.calls.append((roi, image))
                return [("生存 48/48", Point(160, 55))]

        return FakeAdapter()

    def test_rect_direct_mode_reads_every_frame(self):
        adapter = self._fake_ocr_adapter()
        with patch.object(template_lab, "_create_ocr_adapter", return_value=adapter):
            response = self.client.post("/api/template-lab/ocr-test", json={
                "rect": [100, 40, 120, 30], "sessions": ["20250101-010203"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # 直传模式 roi 原样回 {name: null, x, y, w, h}
        self.assertEqual(body["roi"],
                         {"name": None, "x": 100, "y": 40, "w": 120, "h": 30})
        self.assertEqual([item["texts"] for item in body["results"]],
                         [["生存 48/48"], ["生存 48/48"]])
        roi, _image = adapter.calls[0]
        self.assertEqual(roi.to_tuple(), (100, 40, 120, 30))

    def test_name_and_rect_both_is_400(self):
        self.client.post("/api/template-lab/rois", json={
            "name": "生存栏", "x": 100, "y": 40, "w": 120, "h": 30})
        response = self.client.post("/api/template-lab/ocr-test", json={
            "name": "生存栏", "rect": [100, 40, 120, 30],
            "sessions": ["20250101-010203"]})
        self.assertEqual(response.status_code, 400, response.text)

    def test_neither_name_nor_rect_is_400(self):
        response = self.client.post("/api/template-lab/ocr-test", json={
            "sessions": ["20250101-010203"]})
        self.assertEqual(response.status_code, 400, response.text)

    def test_bad_rect_is_400(self):
        for rect in ([100, 40, 1, 30], [100, 40, 120, 700], [-1, 0, 10, 10],
                     [100, 40, 120], [100.5, 40, 120, 30], "xywh"):
            response = self.client.post("/api/template-lab/ocr-test", json={
                "rect": rect, "sessions": ["20250101-010203"]})
            self.assertEqual(response.status_code, 400, repr(rect))


if __name__ == "__main__":
    unittest.main()
