# -*- coding: utf-8 -*-
"""素材包（touken/server_pack.py）测试：逻辑名解析、原始值兼容、路径安全、越界。

覆盖 docs/dual-server-pipeline.md 3.2 的契约：
- 逻辑引用 @name 从本服包解析；原始值按旧语义返回
- 缺引用 / 坏路径 / 越界 / 类型不对 → ValueError 子类
- cn 包签到映射与 resource/base/flows/signin.json 里的旧字面值逐一相同
- jp 包只收录真实存在的出阵模板，不虚构 OCR/ROI/签到素材
"""

import json
import unittest
from pathlib import Path

from touken.runtime_paths import RESOURCE_DIR
from touken.server_pack import (
    PackPathError,
    PackValueError,
    ServerPack,
    ServerPackError,
    UnknownReferenceError,
    UnknownServerError,
    load_server_pack,
)


def _write_pack(root: Path, server: str, data: dict) -> Path:
    folder = root / "server_packs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{server}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _touch(root: Path, rel: str) -> None:
    path = root / "image" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


class RealPacksTest(unittest.TestCase):
    """仓里真实的 cn/jp 包：能加载、结构对、签到映射和旧字面值一致。"""

    def test_cn_pack_loads(self):
        pack = load_server_pack("cn")
        self.assertEqual(pack.base_size, (1280, 720))
        self.assertEqual(pack.server, "cn")

    def test_jp_pack_loads(self):
        pack = load_server_pack("jp")
        self.assertEqual(pack.base_size, (1508, 1088))

    def test_cn_signin_ocr_matches_legacy_flow(self):
        """签到官方流程（signin.json）里的旧字面值必须能在 cn 包按逻辑名取回。"""
        pack = load_server_pack("cn")
        self.assertEqual(pack.resolve_ocr("@signin/announce"), "公告")
        self.assertEqual(pack.resolve_ocr("@signin/claim"), "领取奖励")
        self.assertEqual(pack.resolve_ocr("@signin/detail"), "道具详情")
        self.assertEqual(pack.resolve_roi("@signin/announce"), [1100, 100, 1280, 680])
        self.assertEqual(pack.resolve_roi("@signin/claim"), [750, 550, 1100, 670])
        self.assertEqual(pack.resolve_roi("@signin/detail"), [450, 80, 830, 140])
        self.assertEqual(pack.resolve_point("@signin/tab"), (550, 55))
        self.assertEqual(pack.resolve_point("@signin/detail-close"), (945, 105))
        self.assertEqual(pack.resolve_point("@signin/click-through"), (640, 400))
        self.assertEqual(pack.resolve_point("@signin/close"), (1195, 30))

    def test_cn_expedition_entries_match_example_config(self):
        """远征派遣抽出的识别素材必须能在 cn 包按逻辑名取回，值与
        touken_config.example.json 的 expedition 节逐一相同（不硬造新值）。"""
        pack = load_server_pack("cn")
        self.assertEqual(pack.resolve_navigation("@expedition/nav"), "远征")
        self.assertEqual(pack.resolve_ocr("@expedition/team-ui"), "部队选择")
        self.assertEqual(pack.resolve_ocr("@expedition/start"), "远征开始")
        self.assertEqual(pack.resolve_ocr("@expedition/confirm"), "确认")
        self.assertEqual(pack.resolve_ocr("@expedition/running"), "远征中")
        self.assertEqual(pack.resolve_roi("@expedition/team-ui"), [506, 1, 774, 77])
        self.assertEqual(pack.resolve_roi("@expedition/deploy"),
                         [1110, 580, 1279, 710])
        self.assertEqual(pack.resolve_roi("@expedition/start"),
                         [1100, 560, 1280, 700])
        self.assertEqual(pack.resolve_roi("@expedition/confirm"),
                         [650, 520, 950, 640])
        self.assertEqual(pack.resolve_roi("@expedition/running"),
                         [60, 280, 1230, 580])
        self.assertEqual(pack.resolve_roi("@expedition/map-ocr"),
                         [60, 280, 1230, 580])

    def test_cn_expedition_templates_exist_on_disk(self):
        """cn 包收录的远征模板必须真实存在（同源铁律，不虚构素材）。"""
        pack = load_server_pack("cn")
        for rel in ("battle/ui重伤.png", "远征开始.png"):
            self.assertTrue((RESOURCE_DIR / "image" / rel).is_file(), rel)

    def test_cn_pack_referenced_by_official_expedition_flow(self):
        """官方远征流程单引用的每个 @逻辑名都能在 cn 包解析（加载期已全量校验，
        这里守的是「流程单 ↔ 包」两边的契约对齐）。"""
        flow_path = RESOURCE_DIR / "flows" / "expedition.json"
        flow = json.loads(flow_path.read_text(encoding="utf-8"))
        pack = load_server_pack("cn")
        refs = set()
        for step in flow["steps"]:
            params = step["params"]
            for key in ("template", "ocr_expected", "roi", "point"):
                value = params.get(key)
                if isinstance(value, str) and value.startswith("@"):
                    refs.add(value[1:])
            if step["type"] == "navigate":
                target = params.get("target", "")
                self.assertTrue(target.startswith("@"), target)
                refs.add(target[1:])
        self.assertIn("expedition/nav", refs)
        for name in sorted(refs):
            self.assertTrue(name.startswith("expedition/"), name)
            # 解析不抛错即存在（模板还顺带验了文件在盘上）
            if name in pack.templates:
                pack.resolve_template("@" + name)
            elif name in pack.ocr:
                pack.resolve_ocr("@" + name)
            elif name in pack.rois:
                pack.resolve_roi("@" + name)
            elif name in pack.points:
                pack.resolve_point("@" + name)
            elif name in pack.navigation:
                pack.resolve_navigation("@" + name)
            else:
                self.fail(f"cn 包里没有 {name}")

    def test_jp_templates_all_exist_on_disk(self):
        """jp 包收录的模板必须真实存在（同源铁律，不虚构素材）。"""
        pack = load_server_pack("jp")
        self.assertEqual(len(pack.templates), 28)
        for rel in pack.templates.values():
            self.assertTrue((RESOURCE_DIR / "image" / rel).is_file(), rel)

    def test_jp_pack_has_no_invented_signin_assets(self):
        """jp 包不虚构 signin 素材：五张表一律不准出现 signin/ 开头的键。
        （旧断言要求整表为空太死——双服阶段 2 契约收录的远征导航项是合法内容。）"""
        pack = load_server_pack("jp")
        for table in (pack.templates, pack.ocr, pack.rois, pack.points,
                      pack.navigation):
            for key in table:
                self.assertFalse(key.startswith("signin/"), key)
        # 远征官方流程单（两服共用）的导航目标，jp 包按契约收录
        self.assertEqual(pack.navigation.get("expedition/nav"), "远征")

    def test_all_errors_are_value_error_subclass(self):
        for cls in (UnknownServerError, UnknownReferenceError,
                    PackPathError, PackValueError):
            self.assertTrue(issubclass(cls, ValueError), cls)
            self.assertTrue(issubclass(cls, ServerPackError), cls)


class RawValueCompatTest(unittest.TestCase):
    """原始值（不带 @）按旧语义返回，同时做类型/边界校验。"""

    def setUp(self):
        self.pack = load_server_pack("cn")

    def test_raw_template_passthrough(self):
        self.assertEqual(self.pack.resolve_template("menu/出阵.png"), "menu/出阵.png")
        self.assertEqual(self.pack.resolve_template("通用_确定.png"), "common/confirm.png")

    def test_raw_ocr_passthrough(self):
        self.assertEqual(self.pack.resolve_ocr("公告"), "公告")
        self.assertEqual(self.pack.resolve_ocr("任意文字都行"), "任意文字都行")

    def test_raw_roi_passthrough(self):
        self.assertEqual(self.pack.resolve_roi([1100, 100, 1280, 680]),
                         [1100, 100, 1280, 680])
        self.assertEqual(self.pack.resolve_roi((0, 0, 1, 1)), [0, 0, 1, 1])

    def test_raw_roi_accepts_tuple_returns_list(self):
        result = self.pack.resolve_roi((100, 100, 200, 200))
        self.assertIsInstance(result, list)

    def test_raw_point_forms(self):
        self.assertEqual(self.pack.resolve_point([550, 55]), (550, 55))
        self.assertEqual(self.pack.resolve_point((550, 55)), (550, 55))
        self.assertEqual(self.pack.resolve_point({"x": 550, "y": 55}), (550, 55))

    def test_raw_navigation_passthrough(self):
        self.assertEqual(self.pack.resolve_navigation("本丸"), "本丸")
        self.assertEqual(self.pack.resolve_navigation(["open_menu", "click 公告"]),
                         ["open_menu", "click 公告"])


class BoundaryAndTypeTest(unittest.TestCase):
    """类型与坐标边界：坏类型、越界、倒序、bool 混进来都要翻车。"""

    def setUp(self):
        self.pack = load_server_pack("cn")

    def test_roi_allows_full_frame_but_rejects_zero_area(self):
        self.assertEqual(self.pack.resolve_roi([0, 0, 1280, 720]), [0, 0, 1280, 720])
        for bad in ([10, 10, 10, 10],      # 零面积：x1==x2
                    [10, 10, 10, 50],      # 零宽
                    [10, 10, 50, 10]):     # 零高
            with self.assertRaises(PackValueError, msg=repr(bad)):
                self.pack.resolve_roi(bad)

    def test_roi_out_of_bounds(self):
        with self.assertRaises(PackValueError):
            self.pack.resolve_roi([0, 0, 1281, 720])
        with self.assertRaises(PackValueError):
            self.pack.resolve_roi([-1, 0, 100, 100])
        with self.assertRaises(PackValueError):
            self.pack.resolve_roi([100, 100, 50, 200])  # 倒序

    def test_roi_wrong_shape_and_type(self):
        for bad in ([1, 2, 3], [1, 2, 3, 4, 5], "1100,100,1280,680",
                    [1.5, 0, 10, 10], [True, 0, 10, 10], None):
            with self.assertRaises(PackValueError, msg=repr(bad)):
                self.pack.resolve_roi(bad)

    def test_point_out_of_bounds(self):
        with self.assertRaises(PackValueError):
            self.pack.resolve_point([1280, 0])   # 点合法范围 0..1279
        with self.assertRaises(PackValueError):
            self.pack.resolve_point([0, 720])
        with self.assertRaises(PackValueError):
            self.pack.resolve_point([-1, 5])

    def test_point_wrong_type(self):
        for bad in ([1.5, 2], ["1", "2"], [1], [1, 2, 3], "550,55",
                    {"x": 1}, {"x": 1, "y": 2, "z": 3}, {"x": True, "y": 2}, None):
            with self.assertRaises(PackValueError, msg=repr(bad)):
                self.pack.resolve_point(bad)

    def test_template_rejects_non_string(self):
        for bad in (123, None, ["a"]):
            with self.assertRaises(PackValueError, msg=repr(bad)):
                self.pack.resolve_template(bad)

    def test_ocr_rejects_non_string_and_empty(self):
        with self.assertRaises(PackValueError):
            self.pack.resolve_ocr(123)
        with self.assertRaises(PackValueError):
            self.pack.resolve_ocr("")

    def test_navigation_rejects_garbage(self):
        with self.assertRaises(PackValueError):
            self.pack.resolve_navigation({"not": "a path"})
        with self.assertRaises(PackValueError):
            self.pack.resolve_navigation(123)


class UnknownReferenceTest(unittest.TestCase):
    def setUp(self):
        self.pack = load_server_pack("cn")

    def test_missing_template_reference(self):
        with self.assertRaises(UnknownReferenceError):
            self.pack.resolve_template("@menu/出阵")

    def test_missing_ocr_roi_point_navigation(self):
        with self.assertRaises(UnknownReferenceError):
            self.pack.resolve_ocr("@signin/nope")
        with self.assertRaises(UnknownReferenceError):
            self.pack.resolve_roi("@signin/nope")
        with self.assertRaises(UnknownReferenceError):
            self.pack.resolve_point("@signin/nope")
        with self.assertRaises(UnknownReferenceError):
            self.pack.resolve_navigation("@home")

    def test_bare_at_sign(self):
        with self.assertRaises(UnknownReferenceError):
            self.pack.resolve_template("@")

    def test_at_ref_type_mismatch(self):
        """@name 查得到但表里类型不对（如 ROI 里塞了字符串）也要翻车。"""
        pack = ServerPack(server="t", base_size=(100, 100), resource_dir=Path("."),
                          rois={"bad": "not-a-roi"})
        with self.assertRaises(PackValueError):
            pack.resolve_roi("@bad")


class PathSafetyTest(unittest.TestCase):
    """模板路径安全：绝对路径和越级引用拒绝；包内路径必须真实存在。"""

    def test_raw_absolute_path_rejected(self):
        pack = load_server_pack("cn")
        with self.assertRaises(PackPathError):
            pack.resolve_template("C:/Windows/system32.png")
        with self.assertRaises(PackPathError):
            pack.resolve_template("/etc/passwd.png")

    def test_raw_parent_traversal_rejected(self):
        pack = load_server_pack("cn")
        with self.assertRaises(PackPathError):
            pack.resolve_template("../../secret.png")
        with self.assertRaises(PackPathError):
            pack.resolve_template("sub/../..\\escape.png")
        with self.assertRaises(PackPathError):
            pack.resolve_template("..\\evil.png")          # 反斜杠也是分隔符

    def test_whitelist_only_png(self):
        """白名单与旧流程一致：只认 .png 模板。"""
        pack = load_server_pack("cn")
        with self.assertRaises(PackPathError):
            pack.resolve_template("menu/sally.jpg")
        with self.assertRaises(PackPathError):
            pack.resolve_template("menu/sally")            # 没扩展名不行
        with self.assertRaises(PackPathError):
            pack.resolve_template("menu/sally.png.bak")

    def test_whitelist_rejects_empty_and_dot_segments(self):
        pack = load_server_pack("cn")
        for bad in ("menu//sally.png", "menu/sally.png/", "./menu/sally.png",
                    "menu/./sally.png", "."):
            with self.assertRaises(PackPathError, msg=repr(bad)):
                pack.resolve_template(bad)

    def test_backslash_treated_as_separator(self):
        """反斜杠按路径分隔符处理，合法输入归一成 '/' 分隔返回。"""
        pack = load_server_pack("cn")
        self.assertEqual(pack.resolve_template("menu\\sally.png"),
                         "menu/sally.png")
        with self.assertRaises(PackPathError):
            pack.resolve_template("menu\\..\\sally.png")

    def test_pack_template_whitelist_enforced(self):
        """@包解析出的路径同样过白名单（类型是 PackPathError 系）。"""
        pack = ServerPack(server="t", base_size=(100, 100), resource_dir=Path("."),
                          templates={"not-png": "menu/sally.jpg",
                                     "empty-seg": "menu//sally.png",
                                     "backslash-escape": "menu\\..\\sally.png"})
        for ref in ("@not-png", "@empty-seg", "@backslash-escape"):
            with self.assertRaises(PackPathError, msg=ref):
                pack.resolve_template(ref)

    def test_pack_template_missing_file(self):
        pack = ServerPack(server="t", base_size=(100, 100), resource_dir=Path("."),
                          templates={"ghost": "jp/none.png"})
        with self.assertRaises(PackPathError):
            pack.resolve_template("@ghost")

    def test_pack_template_unsafe_path(self):
        pack = ServerPack(server="t", base_size=(100, 100), resource_dir=Path("."),
                          templates={"evil": "../evil.png"})
        with self.assertRaises(PackPathError):
            pack.resolve_template("@evil")

    def test_pack_load_rejects_whitelist_violations(self):
        """加载期全量校验：包条目本身违反白名单时坏包当场翻车。"""
        _touch(self.root, "menu/sally.jpg")
        _write_pack(self.root, "cn", {
            "base_size": [1280, 720],
            "templates": {"bad": "menu/sally.jpg"},
        })
        with self.assertRaises(PackPathError):
            load_server_pack("cn", resource_dir=self.root)

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class LoadPackTest(unittest.TestCase):
    """load_server_pack：未知服务器、坏包、缺 base_size、加载期全量校验。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_server(self):
        with self.assertRaises(UnknownServerError):
            load_server_pack("kr", resource_dir=self.root)

    def test_server_name_path_traversal(self):
        with self.assertRaises(UnknownServerError):
            load_server_pack("../cn", resource_dir=self.root)

    def test_broken_json(self):
        folder = self.root / "server_packs"
        folder.mkdir(parents=True)
        (folder / "cn.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(UnknownServerError):
            load_server_pack("cn", resource_dir=self.root)

    def test_missing_base_size(self):
        _write_pack(self.root, "cn", {"templates": {}})
        with self.assertRaises(PackValueError):
            load_server_pack("cn", resource_dir=self.root)

    def test_bad_base_size(self):
        _write_pack(self.root, "cn", {"base_size": [0, 720]})
        with self.assertRaises(PackValueError):
            load_server_pack("cn", resource_dir=self.root)
        _write_pack(self.root, "cn", {"base_size": ["1280", 720]})
        with self.assertRaises(PackValueError):
            load_server_pack("cn", resource_dir=self.root)

    def test_non_dict_table(self):
        _write_pack(self.root, "cn", {"base_size": [1280, 720], "ocr": ["公告"]})
        with self.assertRaises(PackValueError):
            load_server_pack("cn", resource_dir=self.root)

    def test_load_time_validation_catches_bad_entries(self):
        """包里的坏条目（越界 ROI、缺文件模板）在加载时就翻车，不留到运行时。"""
        _write_pack(self.root, "cn", {
            "base_size": [1280, 720],
            "rois": {"bad": [0, 0, 9999, 10]},
        })
        with self.assertRaises(PackValueError):
            load_server_pack("cn", resource_dir=self.root)

        _write_pack(self.root, "jp", {
            "base_size": [1508, 1088],
            "templates": {"ghost": "jp/ghost.png"},
        })
        with self.assertRaises(PackPathError):
            load_server_pack("jp", resource_dir=self.root)

    def test_valid_tmp_pack_roundtrip(self):
        _touch(self.root, "jp/menu/sally.png")
        _write_pack(self.root, "jp", {
            "base_size": [1508, 1088],
            "templates": {"menu/sally": "jp/menu/sally.png"},
            "ocr": {"home": "本丸"},
            "rois": {"home": [0, 0, 1508, 200]},
            "points": {"ok": [1507, 1087]},
            "navigation": {"home": ["click menu"]},
        })
        pack = load_server_pack("jp", resource_dir=self.root)
        self.assertEqual(pack.resolve_template("@menu/sally"), "jp/menu/sally.png")
        self.assertEqual(pack.resolve_ocr("@home"), "本丸")
        self.assertEqual(pack.resolve_roi("@home"), [0, 0, 1508, 200])
        self.assertEqual(pack.resolve_point("@ok"), (1507, 1087))
        self.assertEqual(pack.resolve_navigation("@home"), ["click menu"])

    def test_unknown_table_keys_are_ignored(self):
        """包 JSON 里多出来的键不碍事（向前兼容）。"""
        _write_pack(self.root, "cn", {
            "base_size": [1280, 720],
            "future_feature": {"whatever": 1},
        })
        pack = load_server_pack("cn", resource_dir=self.root)
        self.assertEqual(pack.base_size, (1280, 720))


if __name__ == "__main__":
    unittest.main()
