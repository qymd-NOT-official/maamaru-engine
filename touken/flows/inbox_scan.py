# -*- coding: utf-8 -*-
"""收件箱体检：只读翻一遍待领邮件，一封都不领。

邮件清单走数据确认通道（点击前后配对 /receive/list），不截图、不点领取；
筛选先复位成「全部」，免得上次炼糖/收杂物留下的筛选挡住别的邮件。
临期判断和提醒在面板侧（panel/inbox_watch.py），这里只负责看。
"""

import time

from ..device_adapter import Point
from .sugar import SugarMixin

_INBOX_CLOSE = (1248, 32)  # 收件箱右上角 X（与炼糖线同一点位）


class InboxScanMixin:
    """收件箱体检。依赖宿主类的 navigate_to_stream、device、current_location。"""

    def _reset_inbox_filter(self):
        """筛选复位成「全部」：打开筛选面板 → 取消筛选 → 确定。
        「取消筛选」在没有筛选时点了也无害（同一块区域没有别的按钮）。"""
        self.device.click(Point(*SugarMixin._FILTER_OPEN))
        time.sleep(1.0)
        self.device.click(Point(*SugarMixin._FILTER_CLEAR))
        time.sleep(0.6)
        self.device.click(Point(*SugarMixin._FILTER_OK))
        time.sleep(1.5)

    def inbox_scan_stream(self):
        """导航到收件箱 → 复位筛选 → 抓邮件清单 → 收箱子。

        Returns: 待领邮件列表 [{"mailbox_id", "item_type", "item_id",
        "item_num", "name", "reason", "inbox_at"}]；导航或抓取失败返回 None。
        只读：全程不碰任何领取按钮。
        """
        yield "[收件箱体检] 正在导航到收件箱..."
        for nav_msg in self.navigate_to_stream("收件箱"):
            yield nav_msg
        if self.current_location != "收件箱":
            yield "[收件箱体检] ✗ 到达收件箱失败，本次跳过"
            return None
        time.sleep(1.5)

        from ..game_state import begin_operation, confirm_operation
        boundary = begin_operation(self.device)
        self._reset_inbox_filter()
        listed = confirm_operation(self.device, boundary, "inbox_list", timeout=2.0)
        entries = boundary.get("result_body", {}).get("receive") if listed is True else None
        if entries is None:
            yield "[收件箱体检] ✗ 没能确认邮件清单，本次跳过（一封都没动）"
            self.device.click(Point(*_INBOX_CLOSE))
            time.sleep(1.0)
            return None
        if isinstance(entries, dict):
            rows = list(entries.values())
        elif isinstance(entries, list):
            rows = entries
        else:
            rows = []
        mails = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("serial_id"):
                continue
            mails.append({
                "mailbox_id": str(row["serial_id"]),
                "item_type": str(row.get("item_type") or ""),
                "item_id": row.get("item_id"),
                "item_num": row.get("item_num"),
                "name": row.get("name") or "",
                "reason": row.get("reason"),
                "inbox_at": row.get("created_at") or "",
            })
        yield f"[收件箱体检] ✓ 待领邮件 {len(mails)} 封，一封未动"
        self.device.click(Point(*_INBOX_CLOSE))
        time.sleep(1.0)
        return mails
