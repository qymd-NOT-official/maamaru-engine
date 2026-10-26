# -*- coding: utf-8 -*-
"""流程工坊后端测试。

覆盖：流程 CRUD（新建/改/删/复制/400 校验/404）、开发版门禁（冻结即 503）、
步骤目录形状、模板列表、ROI 存档读取、单步试跑（认类真识别 / 点类只预览
不真点 / 结构类 400 / 账房 503 / 坏步骤 400）、存储备份式写入与坏文件恢复。

隔离红线（照 test_template_lab.py 惯例）：模块头的 MAAMARU_DATA_DIR 环境
补丁只为 import panel.server 用，全量跑时可能已失效——真正的隔离靠每个
用例 patch touken.flow_engine.STATUS_DIR / panel.flow_lab.RESOURCE_DIR /
panel.template_lab.DEBUG_DIR 到临时目录。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# server 模块导入时完成脚本注册（含 custom_flow）；在临时数据目录下做，
# 不碰真实用户数据。
_module_tmp = tempfile.TemporaryDirectory(prefix="flow_lab_module_test_")
with patch.dict("os.environ", {"MAAMARU_DATA_DIR": _module_tmp.name}):
    import panel.server  # noqa: F401  (import 即完成脚本注册)
    from panel import flow_lab, template_lab
    from fastapi.testclient import TestClient

from touken import flow_engine  # noqa: E402
from touken.device_adapter import Point  # noqa: E402  (假 adapter 的返回类型)


def _step(id_, type_, params=None, on_fail=None, label=None):
    step = {"id": id_, "type": type_, "params": params or {}}
    if on_fail:
        step["on_fail"] = on_fail
    if label:
        step["label"] = label
    return step


def _valid_steps():
    return [_step("s1", "navigate", {"target": "本丸"}),
            _step("s2", "click_template", {"template": "event/入口.png",
                                           "roi": [100, 100, 300, 200]}),
            _step("s3", "click_point", {"x": 640, "y": 360}, on_fail="continue")]


class FlowLabTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flow_lab_test_")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._patches = [
            patch.object(flow_engine, "STATUS_DIR", self.dir / "status"),
            patch.object(flow_engine, "RESOURCE_DIR", self.dir / "resource"),
            patch.object(flow_lab, "RESOURCE_DIR", self.dir / "resource"),
            patch.object(template_lab, "DEBUG_DIR", self.dir / "debug"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(panel.server.app)
        self.addCleanup(self.client.close)

    def _create(self, name="活动流程", steps=None, expect=200):
        response = self.client.post("/api/flow-lab/flows", json={
            "name": name, "steps": steps if steps is not None else _valid_steps()})
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return response.json() if expect == 200 else response


class FlowCrudTests(FlowLabTestBase):
    def test_create_then_list_roundtrip(self):
        created = self._create()
        flow = created["flow"]
        self.assertTrue(flow["id"])
        path = flow_engine._flows_path()
        self.assertTrue(path.is_file())
        listed = self.client.get("/api/flow-lab/flows").json()["flows"]
        self.assertEqual([f["id"] for f in listed], [flow["id"]])
        self.assertEqual(listed[0]["name"], "活动流程")

    def test_update_flow(self):
        flow = self._create()["flow"]
        response = self.client.put(f"/api/flow-lab/flows/{flow['id']}", json={
            "name": "改名了",
            "steps": _valid_steps() + [
                _step("s4", "sleep", {"seconds": 1})],
        })
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()["flow"]
        self.assertEqual(updated["name"], "改名了")
        self.assertEqual(len(updated["steps"]), 4)

    def test_delete_flow(self):
        flow = self._create()["flow"]
        response = self.client.delete(f"/api/flow-lab/flows/{flow['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/flow-lab/flows").json()["flows"], [])

    def test_duplicate_flow_gets_new_id_and_copy_name(self):
        flow = self._create(name="原始流程")["flow"]
        response = self.client.post(f"/api/flow-lab/flows/{flow['id']}/duplicate")
        self.assertEqual(response.status_code, 200, response.text)
        cloned = response.json()["flow"]
        self.assertNotEqual(cloned["id"], flow["id"])
        self.assertIn("副本", cloned["name"])
        self.assertEqual(cloned["steps"], flow["steps"])
        self.assertEqual(len(self.client.get("/api/flow-lab/flows").json()["flows"]), 2)

    def test_missing_flow_is_404(self):
        self.assertEqual(self.client.put(
            "/api/flow-lab/flows/ghost", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.delete(
            "/api/flow-lab/flows/ghost").status_code, 404)
        self.assertEqual(self.client.post(
            "/api/flow-lab/flows/ghost/duplicate").status_code, 404)

    def test_invalid_payload_is_400_and_not_persisted(self):
        for steps in (
                [_step("s1", "nope_type")],
                [_step("s1", "click_point", {"x": 9999, "y": 1})],
                [_step("s1", "check", {"template": "../evil.png"})],
                [_step("s1", "builtin", {"name": "hack"})],
                [_step("s1", "click_point", {"x": 1, "y": 1}, on_fail="ignore")],
                [_step("s1", "check", {"template": "a.png"}),
                 _step("s2", "jump_if", {"when": "hit", "target": "ghost"})]):
            self._create(steps=steps, expect=400)
        self.assertEqual(self.client.get("/api/flow-lab/flows").json()["flows"], [])

    def test_bad_name_is_400(self):
        self._create(name="  ", expect=400)
        self._create(name="长" * 31, expect=400)


class StepCatalogTests(FlowLabTestBase):
    def test_steps_shape(self):
        response = self.client.get("/api/flow-lab/steps")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        steps = {s["type"]: s for s in body["steps"]}
        for type_ in ("wait_landmark", "check", "click_hit", "click_point",
                      "click_template", "click_ocr", "swipe", "skip_safe",
                      "sleep", "note", "navigate", "jump_if", "builtin"):
            self.assertIn(type_, steps)
        categories = {s["category"] for s in body["steps"]}
        self.assertEqual(categories, {"认", "点", "结构"})
        for step in body["steps"]:
            self.assertEqual(set(step), {"type", "label", "desc", "category", "params"})
        self.assertEqual([b["name"] for b in body["builtins"]],
                         ["home", "open_menu", "popup_sweep", "reset_location", "safe_depart"])

    def test_builtin_step_params_offer_select_with_all_three(self):
        steps = {s["type"]: s for s in
                 self.client.get("/api/flow-lab/steps").json()["steps"]}
        names = [opt[0] for opt in steps["builtin"]["params"][0]["options"]]
        self.assertEqual(names, ["home", "open_menu", "popup_sweep", "reset_location", "safe_depart"])


class DevGateTests(FlowLabTestBase):
    def test_frozen_build_gets_503_everywhere(self):
        with patch.object(flow_lab, "_is_dev", lambda: False):
            self.assertEqual(self.client.get("/api/flow-lab/flows").status_code, 503)
            self.assertEqual(self.client.get("/api/flow-lab/steps").status_code, 503)
            self.assertEqual(self.client.get("/api/flow-lab/templates").status_code, 503)
            self.assertEqual(self.client.get("/api/flow-lab/rois").status_code, 503)
            self.assertEqual(self.client.post(
                "/api/flow-lab/flows",
                json={"name": "x", "steps": _valid_steps()}).status_code, 503)


class TemplateAndRoiTests(FlowLabTestBase):
    def test_templates_listed_relative_to_image_dir(self):
        image_dir = flow_lab._image_dir()
        (image_dir / "event").mkdir(parents=True)
        (image_dir / "event" / "入口.png").write_bytes(b"png")
        (image_dir / "菜单.png").write_bytes(b"png")
        (image_dir / "忽略.txt").write_text("x")
        response = self.client.get("/api/flow-lab/templates")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["templates"],
                         ["event/入口.png", "菜单.png"])

    def test_templates_empty_without_image_dir(self):
        response = self.client.get("/api/flow-lab/templates")
        self.assertEqual(response.json()["templates"], [])

    def test_rois_read_from_template_lab_store(self):
        response = self.client.post("/api/template-lab/rois", json={
            "name": "活动入口", "x": 10, "y": 20, "w": 200, "h": 100})
        self.assertEqual(response.status_code, 200, response.text)
        rois = self.client.get("/api/flow-lab/rois").json()["rois"]
        self.assertEqual([r["name"] for r in rois], ["活动入口"])
        self.assertEqual({k: rois[0][k] for k in ("x", "y", "w", "h")},
                         {"x": 10, "y": 20, "w": 200, "h": 100})

    def test_rois_empty_without_store(self):
        self.assertEqual(self.client.get("/api/flow-lab/rois").json()["rois"], [])

    def test_template_image_serves_png_with_whitelist(self):
        image_dir = flow_lab._image_dir()
        (image_dir / "event").mkdir(parents=True)
        (image_dir / "event" / "入口.png").write_bytes(b"png-bytes")
        good = self.client.get("/api/flow-lab/template-image",
                               params={"path": "event/入口.png"})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.headers["content-type"], "image/png")
        self.assertEqual(good.content, b"png-bytes")
        # 路径穿越 400、不存在 404
        self.assertEqual(self.client.get(
            "/api/flow-lab/template-image",
            params={"path": "../secret.png"}).status_code, 400)
        self.assertEqual(self.client.get(
            "/api/flow-lab/template-image",
            params={"path": "ghost/没有.png"}).status_code, 404)
        # 开发版门禁同样管住发图
        with patch.object(flow_lab, "_is_dev", lambda: False):
            self.assertEqual(self.client.get(
                "/api/flow-lab/template-image",
                params={"path": "event/入口.png"}).status_code, 503)


class FakeProbeAdapter:
    """单步试跑的假 adapter：模板永远命中，OCR 给固定文本。"""

    def __init__(self):
        self.calls = []

    def init(self):
        return True

    def screenshot(self, force=False):
        self.calls.append(("screenshot", force))
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        self.calls.append(("template_match", template, roi, threshold))
        return Point(110, 120)

    def template_match_score(self, template, roi=None, threshold=0.5):
        self.calls.append(("template_match_score", template, roi))
        return 0.91

    def ocr(self, expected, roi=None, match_mode="contains"):
        self.calls.append(("ocr", expected, roi, match_mode))
        return Point(210, 220)

    def ocr_all(self, roi, image=None):
        self.calls.append(("ocr_all", roi))
        return [("决定", Point(210, 220))]


class TestStepTests(FlowLabTestBase):
    def _test_step(self, step, expect=200):
        adapter = FakeProbeAdapter()
        with patch.object(flow_lab, "_create_adapter", return_value=adapter):
            response = self.client.post("/api/flow-lab/test-step",
                                        json={"step": step})
        self.assertEqual(response.status_code, expect,
                         f"{response.status_code} {response.text}")
        return (response.json() if expect == 200 else response), adapter

    def test_check_step_recognizes_with_score(self):
        body, adapter = self._test_step(
            _step("s1", "check", {"template": "event/入口.png",
                                  "roi": [100, 100, 300, 200], "threshold": 0.8}))
        self.assertEqual(body["kind"], "recognize")
        self.assertIs(body["hit"], True)
        self.assertAlmostEqual(body["score"], 0.91)
        self.assertEqual(body["point"], [110, 120])
        # ROI 以 Region 传入（screenshot 之后先取分数再匹配点位）
        _name, _tpl, roi = adapter.calls[1]
        self.assertEqual(roi.to_tuple(), (100, 100, 200, 100))

    def test_click_template_previews_without_clicking(self):
        body, adapter = self._test_step(
            _step("s1", "click_template", {"template": "event/按钮.png"}))
        self.assertEqual(body["kind"], "preview")
        self.assertEqual(body["point"], [110, 120])
        self.assertFalse(any(c[0] == "click" for c in adapter.calls))

    def test_click_point_previews_coordinates(self):
        body, _adapter = self._test_step(
            _step("s1", "click_point", {"x": 640, "y": 360}))
        self.assertEqual(body, {"kind": "preview", "action": "click",
                                "point": [640, 360]})

    def test_swipe_preview_shows_line(self):
        body, _adapter = self._test_step(_step(
            "s1", "swipe", {"x1": 100, "y1": 600, "x2": 100, "y2": 120}))
        self.assertEqual(body["action"], "swipe")
        self.assertEqual(body["from"], [100, 600])
        self.assertEqual(body["to"], [100, 120])

    def test_structure_steps_are_400(self):
        self._test_step(_step("s1", "navigate", {"target": "本丸"}), expect=400)
        self._test_step(_step("s1", "jump_if", {"when": "hit", "target": "s9"}),
                        expect=400)
        self._test_step(_step("s1", "builtin", {"name": "home"}), expect=400)

    def test_bad_step_is_400(self):
        self._test_step({"id": "s1", "type": "nope"}, expect=400)
        self._test_step(_step("s1", "click_point", {"x": 99999, "y": 1}),
                        expect=400)
        self._test_step("不是对象", expect=400)

    def test_ocr_check_returns_texts(self):
        body, _adapter = self._test_step(
            _step("s1", "check", {"ocr_expected": "决定"}))
        self.assertEqual(body["texts"], ["决定"])
        self.assertIs(body["hit"], True)

    def test_ledger_mode_is_503(self):
        with patch.dict("os.environ", {"MAAMARU_LEDGER_MODE": "1"}):
            response = self.client.post("/api/flow-lab/test-step", json={
                "step": _step("s1", "check", {"template": "a.png"})})
        self.assertEqual(response.status_code, 503, response.text)


class StorageTests(FlowLabTestBase):
    def test_update_creates_bak_and_bad_file_recovers(self):
        self._create(name="第一条")
        path = flow_engine._flows_path()
        first_bytes = path.read_bytes()
        self._create(name="第二条")
        backup = path.with_suffix(".json.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), first_bytes)

        # 手改坏文件：备份后重置为空，面板不崩
        path.write_text("{ 坏掉", encoding="utf-8")
        self.assertEqual(self.client.get("/api/flow-lab/flows").json()["flows"], [])
        self.assertEqual(len(list((self.dir / "status").glob("custom_flows.json.bad-*"))), 1)

    def test_oversized_flow_is_400(self):
        steps = [_step(f"s{i}", "sleep", {"seconds": 0.1})
                 for i in range(flow_engine.MAX_STEPS + 1)]
        self._create(steps=steps, expect=400)


class OfficialFlowTests(FlowLabTestBase):
    """官方流程（真实 resource/base/flows/signin.json）的只读/复制/撞号行为。"""

    def setUp(self):
        super().setUp()
        # 指回真仓库的官方流程目录（基础类为了隔离把它挪去了临时目录）
        from touken.runtime_paths import RESOURCE_DIR as real_resource_dir
        patcher = patch.object(flow_engine, "RESOURCE_DIR", real_resource_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_official_listed_first_readonly_and_copyable(self):
        body = self.client.get("/api/flow-lab/flows").json()
        self.assertEqual(body["flows"][0]["id"], "builtin-signin")
        self.assertIs(body["flows"][0]["official"], True)
        self.assertEqual(body["warnings"], [])
        self.assertGreaterEqual(len(body["flows"][0]["steps"]), 10)
        put = self.client.put("/api/flow-lab/flows/builtin-signin", json={"name": "改"})
        self.assertEqual(put.status_code, 403, put.text)
        delete = self.client.delete("/api/flow-lab/flows/builtin-signin")
        self.assertEqual(delete.status_code, 403, delete.text)
        copied = self.client.post("/api/flow-lab/flows/builtin-signin/copy")
        self.assertEqual(copied.status_code, 200, copied.text)
        flow = copied.json()["flow"]
        self.assertNotEqual(flow["id"], "builtin-signin")
        self.assertNotIn("official", flow)
        self.assertIn("副本", flow["name"])
        self.assertEqual(flow["steps"], body["flows"][0]["steps"])
        # 官方还在最前，副本以私货身份进列表和存储
        listed = self.client.get("/api/flow-lab/flows").json()["flows"]
        self.assertEqual(listed[0]["id"], "builtin-signin")
        self.assertIn(flow["id"], [f["id"] for f in listed])
        self.assertIn(flow["id"], [f["id"] for f in flow_engine.load_flows()])

    def test_conflict_prefers_official_and_warns(self):
        flow_engine.save_flows([{
            "id": "builtin-signin", "name": "私货撞号",
            "steps": flow_engine.load_official_flows()[0]["steps"]}])
        body = self.client.get("/api/flow-lab/flows").json()
        matches = [f for f in body["flows"] if f["id"] == "builtin-signin"]
        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0]["official"], True)
        self.assertTrue(any("撞了编号" in w for w in body["warnings"]))

    def test_official_step_test_run_works(self):
        official = flow_engine.load_official_flows()[0]
        step = next(s for s in official["steps"]
                    if s["type"] == "check"
                    and s["params"].get("ocr_expected") == "@signin/announce")
        adapter = FakeProbeAdapter()
        with patch.object(flow_lab, "_create_adapter", return_value=adapter):
            response = self.client.post("/api/flow-lab/test-step", json={"step": step})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["hit"], True)

    def test_custom_flow_script_runs_official(self):
        made = []

        def fake_make_agent(config_path):
            made.append(config_path)

            class _Stub:
                pass
            return _Stub()

        with patch.object(panel.server, "_make_agent", fake_make_agent):
            messages = list(panel.server._build_custom_flow(
                "fake-config.json", {"flow_id": "builtin-signin"}))
        self.assertEqual(made, ["fake-config.json"])
        self.assertTrue(any("▶ 开跑" in m for m in messages))


class ScriptRegistrationTests(unittest.TestCase):
    def test_custom_flow_registered_hidden(self):
        from panel.script_runner import _SCRIPTS
        self.assertIn("custom_flow", _SCRIPTS)
        self.assertTrue(_SCRIPTS["custom_flow"].get("hidden"))
        self.assertEqual(_SCRIPTS["custom_flow"]["params"][0]["key"], "flow_id")


if __name__ == "__main__":
    unittest.main()
