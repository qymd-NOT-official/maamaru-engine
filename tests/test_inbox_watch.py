"""收件箱临期提醒：30 天保存期、两档阈值、每封每档只喊一次。"""
import json
from datetime import datetime

from panel import inbox_watch
from panel.daily_workflow import make_template

NOW = datetime(2026, 11, 14, 12, 0, 0).timestamp()


def mail(**overrides):
    base = {"mailbox_id": "100", "item_type": "1", "item_id": "51",
            "item_num": "5000", "name": "小判", "reason": "任务奖励",
            "inbox_at": "2026-11-01 12:00:00"}
    base.update(overrides)
    return base


def test_days_left_uses_thirty_day_retention():
    assert inbox_watch.days_left(mail(), NOW) == 17.0
    assert inbox_watch.days_left(mail(inbox_at="2026-10-18 12:00:00"), NOW) == 3.0
    assert inbox_watch.days_left(mail(inbox_at="2026-10-16 12:00:00"), NOW) == 1.0
    assert inbox_watch.days_left(mail(inbox_at="不是日期"), NOW) is None
    assert inbox_watch.days_left(mail(inbox_at=""), NOW) is None


def test_due_reminders_hit_the_right_threshold():
    fresh = mail(mailbox_id="a")                                   # 剩 17 天
    three = mail(mailbox_id="b", inbox_at="2026-10-18 12:00:00")   # 剩 3 天
    one = mail(mailbox_id="c", inbox_at="2026-10-16 12:00:00")     # 剩 1 天
    due = inbox_watch.due_reminders([fresh, three, one], NOW, {})
    assert [m["mailbox_id"] for m in due.get(3, [])] == ["b"]
    assert [m["mailbox_id"] for m in due.get(1, [])] == ["c"]


def test_three_day_reminder_does_not_silence_the_one_day_one():
    one = mail(mailbox_id="c", inbox_at="2026-10-16 12:00:00")
    # 3 天档喊过，剩 1 天时必须再喊（更急的是新一档，不是重复）
    due = inbox_watch.due_reminders([one], NOW, {"c": [3]})
    assert [m["mailbox_id"] for m in due.get(1, [])] == ["c"]
    # 1 天档也喊过才真正闭嘴
    assert inbox_watch.due_reminders([one], NOW, {"c": [1, 3]}) == {}
    # 同档不重复
    three = mail(mailbox_id="b", inbox_at="2026-10-18 12:00:00")
    assert inbox_watch.due_reminders([three], NOW, {"b": [3]}) == {}


def test_first_scan_at_one_day_marks_both_thresholds(tmp_path):
    state = tmp_path / "state.json"
    one = mail(inbox_at="2026-10-16 12:00:00")
    lines = list(inbox_watch.process_scan([one], NOW, path=state, send=False))
    assert any("已提醒主君" in line for line in lines)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert sorted(saved["reminded"]["100"]) == [1, 3]
    # 紧接着再查一次：没新邮件临期，安静
    lines = list(inbox_watch.process_scan([one], NOW + 60, path=state, send=False))
    assert any("没有临期邮件" in line for line in lines)


def test_process_scan_records_summary_and_prunes_gone_mails(tmp_path):
    state = tmp_path / "state.json"
    three = mail(mailbox_id="b", inbox_at="2026-10-18 12:00:00")
    list(inbox_watch.process_scan([three], NOW, path=state, send=False))
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["pending_count"] == 1
    assert saved["mails"][0]["days_left"] == 3.0
    assert inbox_watch.scanned_today(saved, NOW)
    assert not inbox_watch.scanned_today(saved, NOW + 86400)
    # 邮件被领走/消失了：档案清掉，同编号新邮件不会被旧记录误封口
    list(inbox_watch.process_scan([], NOW + 3600, path=state, send=False))
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["reminded"] == {}
    assert saved["pending_count"] == 0


def test_mail_label_names_items_and_counts():
    assert inbox_watch.mail_label(mail()) == "小判×5000"
    assert inbox_watch.mail_label(mail(item_num="1")) == "小判"
    assert inbox_watch.mail_label(mail(name="", mailbox_id="77", item_num="1")) == "邮件#77"
    sword = mail(item_type="2", item_id="228", item_num="1")
    from touken.inbox_receipts import sword_catalog
    assert inbox_watch.mail_label(sword) == sword_catalog("228")


def test_messages_name_the_mails():
    three = mail(inbox_at="2026-10-18 12:00:00")
    qq, ntfy = inbox_watch._messages(3, [three])
    assert "小判×5000" in qq and "3 天内到期" in ntfy
    qq, ntfy = inbox_watch._messages(1, [three])
    assert "明天前" in qq and "明天前到期" in ntfy
    many = [mail(mailbox_id=str(i)) for i in range(10)]
    _, ntfy = inbox_watch._messages(3, many)
    assert "等 10 封" in ntfy


def test_scan_registered_everywhere_and_old_users_untouched():
    from panel import server, workflow
    assert "inbox_scan" in server.list_scripts()
    assert "inbox_scan" in workflow.NODE_REGISTRY
    assert "收件箱体检" in server._DAILY_STEPS
    template = make_template({"params": {}}, {}, server._DAILY_STEPS)
    assert any(node["type"] == "inbox_scan" for node in template["nodes"])
    # 老用户存过的步骤清单不含体检：保持原样，绝不自动加塞
    saved = {"params": {"daily": {"steps": ["登录", "签到"]}}}
    template = make_template(saved, {}, server._DAILY_STEPS)
    assert not any(node["type"] == "inbox_scan" for node in template["nodes"])
