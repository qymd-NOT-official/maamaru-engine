# 部队状态与出阵共用边界

部队状态由客户端读取并转换成 `touken.team_state.TeamSnapshot`。玩法通过
`touken.team_state_reader.read_team_snapshot` 使用同一接口，不直接解析客户端字段。
国服默认由 `game_state.team_snapshot` 转换；其他客户端可提供
`agent.team_state_reader(team_no, max_age=..., now=...)`。

## 状态约定

- 成员以 `slot` 和 `serial_id` 确认身份，同名刀不能代替实例编号。
- `observed_at` 是名单被游戏确认的时间。生存、疲劳、刀装各自携带观察时间，读取时间不能刷新它们。
- 生存缺失时 `injury_known=False`，与已确认健康的 `max_injury=None` 区分。
- 疲劳和刀装缺失或过期时为 `None`，不能补成满疲劳或空刀装。三格刀装用布尔值表示是否装备；破坏判断还须与本次任务的原有刀装比较。
- 转换器负责确认成员绑定、数据是否晚于最新战斗和伤势状态含义；共用读取入口再核对部队号与各字段时间。
- 自定义读取器失败或给出其他部队时，只返回未知，不能转而连接默认客户端。
- 缓存只在单次安全检查链内复用。每次新的行军决策和手入后复查都重新读取。

## 判断与执行

`team_state` 共用伤势阈值、换队长疲劳差和层末整理判断，不产生点击。
OCR 定位和拖动验收仍由现有客户端执行器负责。

安全出阵继续走 `BattleMixin._safe_depart_stream` 和 `_confirm_departure`。
安全返程由 `BattleMixin._return_home_from_march` 共用：先取消重伤警告，
再核对返程正文，最后确认本丸画面。宿主若提供 `_watch_battle_frame`，
返程取到的观察帧仍交给它记录；返程不要求继承合战场玩法。

剩余次数使用 `flow_control.BattleProgress`，异常安全停止使用 `FlowAborted`。
`injury_recovery.RecoveryProgress` 暂保留为同一类型的兼容名称。
指定楼层、进度接续参数、手入策略和活动伤害规则仍由对应玩法保管，
不在本次整理中改变操作含义。
