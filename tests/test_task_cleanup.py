import unittest
from unittest.mock import patch

from panel.server import _build_daily_standalone, _wrap_inventory


class FakeAgent:
    """只测 _wrap_inventory / _build_daily_standalone 收尾约定的最小假 Agent。"""

    def __init__(self, location=None, nav_ok=True, peek_ok=True,
                 raise_on_nav=False):
        self.current_location = location
        self.nav_ok = nav_ok
        self.peek_ok = peek_ok
        self.raise_on_nav = raise_on_nav
        self.peek_calls = []
        self.sweep_calls = []
        self.daily_args = None

    def navigate_to_stream(self, target):
        if self.raise_on_nav:
            raise RuntimeError("导航模拟异常")
        yield f"[NAV] 正在去 {target}"
        if self.nav_ok:
            self.current_location = target

    def _popup_sweep(self, max_rounds=30, *, verify_navigation=True):
        self.sweep_calls.append(max_rounds)
        self.verify_navigation = verify_navigation
        return True

    def quick_peek(self, tag="", force=False, *, data_only=False):
        self.peek_calls.append({"tag": tag, "force": force, "data_only": data_only})
        return self.peek_ok

    def daily_stream(self, **kwargs):
        self.daily_args = kwargs
        yield "[日课] 跑完了"


def _simple_runner(msg):
    def _fn(agent, config_path, params):
        yield msg
    return _fn


class TaskCleanupTests(unittest.TestCase):
    def test_cleanup_navigates_home_and_forces_peek(self):
        """收尾成功：回本丸一次，只合并已有数据。"""
        agent = FakeAgent()
        wrapped = _wrap_inventory("TEST", _simple_runner("done"))
        with patch("panel.server._make_agent", return_value=agent):
            msgs = list(wrapped("config.json", {}))

        self.assertIn("done", msgs)
        self.assertIn("[TEST] 收尾：已回本丸，同步已有资源数据", msgs)
        self.assertEqual(len(agent.peek_calls), 1)
        self.assertEqual(agent.peek_calls[0]["tag"], "TEST·收尾")
        self.assertTrue(agent.peek_calls[0]["force"])
        self.assertTrue(agent.peek_calls[0]["data_only"])
        self.assertFalse(agent.verify_navigation)
        self.assertEqual(agent.current_location, "本丸")

    def test_cleanup_warns_when_not_home(self):
        """导航失败：任务原结果保留，出现醒目警告，不调用 peek。"""
        agent = FakeAgent(location="出阵", nav_ok=False)
        wrapped = _wrap_inventory("TEST", _simple_runner("done"))
        with patch("panel.server._make_agent", return_value=agent):
            msgs = list(wrapped("config.json", {}))

        self.assertIn("done", msgs)
        self.assertTrue(
            any("收尾没能回到本丸" in m for m in msgs),
            f"未找到导航失败警告，消息为：{msgs}",
        )
        self.assertEqual(agent.peek_calls, [])

    def test_cleanup_exception_does_not_pollute_result(self):
        """收尾抛异常：吞掉、只出日志，任务原结果不被污染。"""
        agent = FakeAgent(raise_on_nav=True)
        wrapped = _wrap_inventory("TEST", _simple_runner("done"))
        with patch("panel.server._make_agent", return_value=agent):
            msgs = list(wrapped("config.json", {}))

        self.assertIn("done", msgs)
        self.assertTrue(
            any("收尾导航/Peek 失败" in m for m in msgs),
            f"未找到异常兜底日志，消息为：{msgs}",
        )
        self.assertEqual(agent.peek_calls, [])

    def test_daily_cleanup_navigates_home_and_forces_peek(self):
        """一键日课独立入口也不再拍屏或重复导航。"""
        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent), \
                patch("panel.server._load_panel_settings", return_value={}), \
                patch("panel.scheduler.load_config",
                      return_value={"common_plan": []}):
            msgs = list(_build_daily_standalone("config.json", {}))

        self.assertIn("[日课] 跑完了", msgs)
        self.assertIn("[日课] 收尾：已回本丸，同步已有资源数据", msgs)
        self.assertEqual(len(agent.peek_calls), 1)
        self.assertEqual(agent.peek_calls[0]["tag"], "日课·收尾")
        self.assertTrue(agent.peek_calls[0]["force"])
        self.assertTrue(agent.peek_calls[0]["data_only"])
        self.assertFalse(agent.verify_navigation)
        self.assertEqual(agent.current_location, "本丸")

    def test_daily_cleanup_warns_when_not_home(self):
        """一键日课导航失败：保留日课结果，出警告，不调用 peek。"""
        agent = FakeAgent(location="出阵", nav_ok=False)
        with patch("panel.server._make_agent", return_value=agent), \
                patch("panel.server._load_panel_settings", return_value={}), \
                patch("panel.scheduler.load_config",
                      return_value={"common_plan": []}):
            msgs = list(_build_daily_standalone("config.json", {}))

        self.assertIn("[日课] 跑完了", msgs)
        self.assertTrue(
            any("收尾没能回到本丸" in m for m in msgs),
            f"未找到导航失败警告，消息为：{msgs}",
        )
        self.assertEqual(agent.peek_calls, [])

    def test_daily_cleanup_skipped_when_after_logout(self):
        """安排了下班（退出游戏/关模拟器/休眠）时跳过收尾导航——
        游戏都关了还回本丸只会撞死在离线设备上（9-04 凌晨翻车冤案）。"""
        for after in ("logout", "shutdown", "sleep"):
            with self.subTest(after=after):
                agent = FakeAgent()
                with patch("panel.server._make_agent", return_value=agent), \
                        patch("panel.server._load_panel_settings",
                              return_value={}), \
                        patch("panel.scheduler.load_config",
                              return_value={"common_plan": []}):
                    msgs = list(_build_daily_standalone(
                        "config.json", {"after": after}))

                self.assertIn("[日课] 跑完了", msgs)
                self.assertTrue(
                    any("跳过收尾回本丸" in m for m in msgs),
                    f"未找到跳过收尾的消息，消息为：{msgs}",
                )
                self.assertNotIn("[日课] 收尾：导航回本丸", msgs)
                self.assertEqual(agent.peek_calls, [])

    def test_cleanup_sweeps_return_screens_before_peek(self):
        """收尾回本丸后仍处理归来结算，再合并已有资源数据。"""
        for label, build in (
                ("单任务", lambda: _wrap_inventory("TEST", _simple_runner("done"))),
                ("日课", lambda: _build_daily_standalone)):
            with self.subTest(kind=label):
                agent = FakeAgent()
                with patch("panel.server._make_agent", return_value=agent), \
                        patch("panel.server._load_panel_settings",
                              return_value={}), \
                        patch("panel.scheduler.load_config",
                              return_value={"common_plan": []}):
                    msgs = list(build()("config.json", {}))
                self.assertTrue(
                    any("扫一遍归来结算屏" in m for m in msgs),
                    f"{label} 未找到扫地消息，消息为：{msgs}")
                self.assertEqual(agent.sweep_calls, [6])  # 限定轮数防磨蹭
                self.assertEqual(len(agent.peek_calls), 1)


if __name__ == "__main__":
    unittest.main()
