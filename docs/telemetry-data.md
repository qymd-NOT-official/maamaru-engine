# 结构化运行数据（Schema v9）

这套数据用于前端统计和后续智能建议。调用方不得解析中文运行日志；日志只给人看，
稳定机器字段统一来自 `%LOCALAPPDATA%/Maamaru*/logs/telemetry.db` 和以下 API。

## API

- `GET /api/data/summary?days=30`：时间窗口内的任务、OCR、事件聚合，并附当前库存、
  日课、远征、内番状态。
- `GET /api/data/events?limit=100&event_type=&script=`：最近的结构化玩法事件。
- `GET /api/data/ocr?limit=100&script=&matched=`：OCR 观测明细。
- `GET/POST/PUT/DELETE /api/data/manual-inventory`：审神者手动抄入或从旧账导入的家底历史；只允许修改或撤销 `manual_entry` / `manual_import`，不会碰游戏截图和任务快照。
- `GET/POST/PUT/DELETE /api/data/manual-sessions`：审神者手动活动记录；与自动任务 `runs`
  分表返回，不参与まあ丸任务次数和圈速聚合。
- `POST /api/data/human-reports/batch`：同一次手动操作的多资源收支；每种资源仍按独立明细精确归因，共用 `group_id`，可通过 `PUT/DELETE /api/data/human-reports/group/{group_id}` 整组修改或撤销。
- `GET /api/data/ledger-export?format=xlsx|csv`：导出账本。Excel 包含使用说明、完整流水、当前家底、每日汇总和可再次导入表；CSV 是带 UTF-8 BOM 的完整流水。
- `POST /api/data/ledger-import/preview?filename=<name>`：以请求体上传 `.xlsx` / `.csv`，只做解析、重复与冲突预览，不写入账本。
- `POST /api/data/ledger-import/apply`：提交 `preview_id` 和 `accept_conflicts`。重新检查冲突，确认有可写内容后先用 SQLite backup 生成一致备份，再只增加手动记录。
- `GET/POST /api/data/ledger-onboarding`：空账本首次设置状态。GET 会检查全历史有效家底观察；老账本直接返回 `not_needed`，空账本才显示。POST 只接受 `start`、`advance`、`complete`、`dismiss`，状态保存在用户数据目录的 `status/ledger_onboarding.json`。
- `GET /api/data/resource-ledger?days=7` 或 `?from=<ts>&to=<ts>`：资源总账（见下文），
  from/to（Unix 秒）优先于 days，days 默认 7。聚合全部在服务端完成，
  前端不要拉原始 events 自己算。

所有响应都带 `schema_version`。前端遇到不认识的更高版本时，应保留未知字段，
不要因新增字段报错。

## OCR observation

```json
{
  "id": 1,
  "ts": 1786600000.0,
  "run_id": "abc123",
  "script": "osaka",
  "kind": "match",
  "expected": "当前层数",
  "match_mode": "contains",
  "matched": true,
  "roi": [825, 270, 455, 85],
  "tokens": [{
    "text": "当前层数",
    "score": 0.98,
    "center": [1000, 310],
    "box": [900, 290, 200, 40]
  }],
  "error": null
}
```

`kind=all` 没有期望值，`matched` 为 `null`。这里只保存文字和识别元数据，不保存截图。

## Event

```json
{
  "id": 1,
  "ts": 1786600000.0,
  "run_id": "abc123",
  "script": "osaka",
  "event_type": "osaka.floor_completed",
  "payload": {"completed": 34, "target": 120, "selected_floor": 88}
}
```

当前事件类型：

- `game_update.detected`、`game_update.recovered`
- `osaka.floor_completed`
- `repair.queued`、`repair.skipped`、`repair.session_completed`
- `team_record.saved`、`equipment.restored`
- `injury_warning.denied`
- `practice.result`
- `sortie.loop_started`、`sortie.completed`、`sortie.retreated_before_boss`、`sortie.interrupted`（逐圈事实，见下文「每圈出阵事实」）
- `sword.obtained`（掉落认人成功；经 `run_id` + payload `sequence` + `attempt`
  关联到具体一圈的某次出发。2026-09 之前的旧事件没有 `attempt`，消费时按
  「归属未知」处理，不得猜成某次尝试）
- `yosari.fragments`（异去一圈末的碎片库存读数与差分）、`yosari.milestone_claimed`
- `raid.round_completed`
- `pumpkin.sortie_completed`、`pumpkin.board_completed`、`pumpkin.token_used`、`pumpkin.sword_obtained`
- `forge.started`、`forge.collected`
- `expedition.dispatched`、`expedition.settled`
- `task_rewards.claimed`
- `inventory.captured`、`inventory.peek`、`osaka.koban_session`
- `resource.change`（通用资源流水，见下文）
- `yosari.ticket_refilled`（归城提灯补充完成；金额识别失败时仍保留事实）
- `ticket.refilled`（活动手形补充完成；江户城记录固定票价并计入小判支出，v0.4.1 的江户城旧事件按 300 小判/张兼容回算）

新增事件应使用 `领域.过去式动作`，payload 只放数据，不放展示文案。轻量的玩法事件和
审神者报备长期保留，用于跨月、跨年的成绩单；体积较大的 OCR 观察明细默认保留 90 天。
当前状态 JSON 仍保留原有接口，便于旧前端渐进迁移。

## 每圈出阵事实（sortie 逐圈事件，2026-09 扩展）

出阵/异去的每一圈是一条可长期积累的事实，作为地图时间与掉落矩阵的底座。
一圈以 `sortie.loop_started` 为开始边界；正常生命周期随后恰好有一个结束事件
（`sortie.completed` / `sortie.retreated_before_boss` / `sortie.interrupted`）。
**但 `loop_started` 允许没有结束事件**：面板手动停止和看门狗会直接终止子进程，
未闭合的 `loop_started` 表示进程被外部终止、崩溃或脚本来不及见证结局；
消费者必须把它当「结果未知」，不得为了闭合编造 outcome，也不得把任务结束
状态冒充出阵结果。只记录真实可证的状态；证明不了的字段写 `null` 并给原因，
也绝不按地图固定节点数猜战斗数。

共用 payload 字段（`loop_started` 只有前 8 个）：

```json
{"mode": "sortie|yosari", "chapter": 5, "map_no": 4, "team_no": 3,
 "sequence": 2, "attempt": 1, "march_mode": "script|delegated",
 "outcome": "completed|retreated_before_boss|interrupted|unknown",
 "duration_seconds": 301.5,
 "battle_count": 4, "battle_count_basis": "battle_result_page_edges",
 "drop_observation": "confirmed_none", "drops_recognized": 0,
 "interrupt_reason": "auto_march_stopped",
 "drop_observation_reason": "auto_march_skips_obtain_animation",
 "battle_count_note": "..."}
```

- `sequence`：第几圈；`attempt`：该圈的第几次出发（中断后原圈重试会 +1，
  同一 `sequence` 可能出现 `interrupted` + `completed` 多条，按 `attempt` 区分）。
- `duration_seconds`：从确认全部通过、部队真正出发，到回本丸/回异去小图页的
  纯游戏流程耗时，第一圈也有精确起点；`loop_started` 的 `ts` 是同一边界。
  这些精确数据**为后续替换旧近似提供底座**：当前 `gameplay_planning` 的圈速和
  `run_summary` 的 `average_loop_seconds` 仍按相邻完成事件的写库时间戳估算，
  消费方尚未切换。
- `outcome`：`completed`（正常打完王点）/ `retreated_before_boss`（王点前撤退，
  事件类型为 `sortie.retreated_before_boss`）/ `interrupted`（伤势中断且已确认
  安全回本丸）/ `unknown`（监控超时或返回本丸失败，队伍最终状态未被见证）。
- `battle_count`：结算页「戦闘結果」模板（`battle/ui战斗结果.png`）出现沿计数，
  消失后重新武装，同一画面重复帧不重数；锚点经 2026-09-05 异去委托行军 148 帧
  运行实录校准（9 场全中、场间空窗 ≥6 帧）。**正常完成却数到 0 场 = 锚点失明
  （王点战必有结算页），此时写 `null` + `battle_count_note`，不写 0**。
  中断/撤退圈 0 场是合法真值，照常记录。
- `drop_observation`：`recognized`（本圈认到掉落，`drops_recognized` 给数量，
  明细在 `sword.obtained`）；`confirmed_none`（脚本手动行军且全程逐帧盯屏、
  认人流程无异常，确实没掉，**可进掉率分母**）；`not_observed`（观察不成立，
  **不进掉率分母**），原因见 `drop_observation_reason`：
  `auto_march_skips_obtain_animation`（委托自动行军游戏自己跳过获得动画）/
  `observation_lost`（结局未知）/ `recognizer_error`（认人流程内部异常）。
- `sortie.interrupted` 不算完成圈，不进 `run_summary` 的 loops 和圈速分母。
- 资源不摊到单圈：整轮库存差值、途中 `inventory.peek`、地图随机资源点仍归整轮
  任务，逐圈事件不带资源字段。

## 远征结算观察（expedition.settled，2026-09 诚实契约补齐）

结算屏观察哨是 `ExpeditionMixin.observe_expedition_settlement`，三条路径同口径：
专用收菜流程（`via=collect`）、登录/收尾扫地（`via=popup_sweep`）、导航开目录被
结算屏挡路（`via=open_menu`）。观察哨只读不点，翻页/跳过永远是调用方的动作。

payload 契约：

```json
{"sequence": 1, "via": "collect", "team_no": 2, "era": 1, "slot": 1,
 "map_name": "鸟羽·伏见之战", "header": "一-一 鸟羽·伏见之战",
 "result": "大成功", "rewards": {"木炭": 15, "玉钢": 22},
 "rewards_status": {"木炭": "ok", "玉钢": "ok", "冷却材": "zero", "砥石": "zero"},
 "special_rewards": [], "special_status": "none", "special_ink": 0.0}
```

- `result` ∈ `成功` / `大成功` / `失败` / `unknown`——结果字样读不出就是
  `unknown`，绝不默认成成功。
- `rewards` 只放 OCR 确认的正值；`rewards_status` 逐行 `ok`/`zero`/`unknown`，
  某行没读清只影响该行，unknown 行不进 `resource.change`（不猜金额）。
- `special_rewards` 是「获得道具」栏（小判/委托符/加速符等）的已确认明细；
  `special_status`：`none`（栏体墨水低于 `empty_ink_max`，判空栏）/
  `ok`（栏内图标全部模板命中且数量读出）/ `unknown`（栏里有内容但认不出，
  或读数失败）。`special_ink` 是墨水占比实测值，供日后校准阈值和图标模板。
  道具图标模板（`settlement_rewards.special_items.templates`）待真机道具栏
  取帧后校准；未配置时该栏只能判空或 unknown。`special_status=unknown`
  且为新结算屏时，自动把读账用的同源稳定帧存到用户数据目录
  `debug/expedition/`（`save_screenshot(force=False)`，不另走 ADB），
  每个唯一结算屏至多一张、判空不留、存盘失败只记日志不挡流程；
  payload 的 `special_sample_saved` 记录是否留成。攒够样本再校准模板。
- 同一屏去重靠像素指纹（「第X部队」标签区 + 各资源行数字区）：静止画面跨帧
  逐像素一致（实测同屏三帧 meanabs=0.0），同一屏没翻动只记一次；不同队伍的
  结算「第X部队」字样必然不同，OCR 全灭也分得开屏。指纹列表按观察轮次
  （一次收菜/扫地/导航）持有，不跨轮复用。
- 确认的收益仍逐笔写 `resource.change`（source `expedition.settlement`），
  特殊道具用 evidence `settlement_special_ocr` 区分。

## 当前本丸共用档案（honmaru-profile，schema_version 1）

`touken/honmaru_profile.py` · 读取入口 `get_honmaru_profile(store=None)` ·
只读 API `GET /api/data/honmaru-profile`。第一版只做事实层：无 UI、无自动
选人/换人/派遣。数据全部来自本库，不另造第二套事实库。

**地基契约（telemetry schema v10）**：`sword_snapshots` 新增 `source`
（`owned_inventory` 所持刀剑一览盘点 / `album` 刀帐图鉴 / `unknown`）与
`completeness`（`complete` 对账平 / `partial` 有缺口 / `unknown`）。
历史库回填只凭行形态这一确定证据：图鉴写入器的 `sword_id` 恒为
`album_NNN`，一览盘点恒为名册目录 id——全 album 行回填 album、零 album
行回填 owned_inventory、混排/空快照保持 unknown，不硬猜。

**晋升规则**：只有 source=owned_inventory 且 completeness=complete 的
最新盘点才能成为 `candidate_pool`——走 `TelemetryStore.latest_sword_snapshot`
的 SQL 无窗口查询，较新的 partial/failed/album/unknown 快照攒得再多
也挤不掉可信档案；它们记进 `skipped_newer_snapshots` 留证（展示证据，
保留最近 200 条窗口）。`/api/data/sword-inventory/latest` 同样走无窗口
查询、只服务 owned_inventory。

**行身份**：一振一行，同名多振保留，绝不按名字/目录 id 去重。
`sword_catalog_id` 只是刀种目录；`observation_id = "{snapshot_id}:{row_id}"`
只在该快照内有效，跨快照不伪造永久实例 ID。每行带 `unknown_fields`
（读不出就列出，不补默认值）、`observed_at`、`source_snapshot_id`。

**同队互斥键**：目录里普通/极化共用一条 `sword_catalog_id` 记录（127 条
目录实测无重名），同位刀（普通+极化、同名多振）游戏规则上不能同队。
候选条目和 roster 链接输出都带 `same_team_exclusion_key`（当前值 =
sword_catalog_id；身份未知保持 null，不拿名字/徽章硬猜）。
纯函数 `formation_conflicts(entries)` 返回共享同一非空互斥键的冲突组，
空 key 不参与判定——供未来规划器复用，本层不做选人/换人。

**编队链接层（roster）**：每队取最新一条 `team_roster.observed`，逐槽
链接候选池：目录 id（缺了用名字）匹配唯一 → `linked` + observation_id；
多候选 → `ambiguous` + candidate_ids，不拿第一把同名刀顶替；无身份/无候选
→ `unknown` + 原因；空位/未占用 → `not_applicable`。原始槽位观察在
`observed` 字段原样保留。

**人工标注合并（telemetry schema v12 起）**：候选池输出行在
`form_status` 为 `unknown` 时合并 `sword_annotations` 的人工形态确认
（以人工为准，证据追加「人工确认（YYYY-MM-DD）」）；机器
kiwame/normal/ambiguous + 人工确认且与机器不同 → 人工改判，以人工为准
（`form_overridden=true`，机器原值留在 `machine_form_status`，机器证据
保留，追加「人工改判（YYYY-MM-DD）：原识别=极/普通/存疑」）；人工与机器
一致只追加确认证据，不算改判。机器 `level` 读不出（None）时补人工
确认等级（v13 起），并把它从 `unknown_fields` 摘掉；机器有值一律信
机器——等级会随练级涨，人填的会过期，只补空缺永不覆盖。只有指纹唯一
命中（一标注对一行、一行对一标注）才合并；同名多振同日显现等撞车
情形保持原样，交「刀帐档案」标 stale/duplicate 让人处理。

## 刀帐档案（sword-archive）

`touken/sword_archive.py` · 读取入口 `get_sword_archive(store=None)` ·
只读 API `GET /api/data/sword-archive`。地基与「当前本丸共用档案」同一份
最新完整盘点（机器形态结论 = 盘点落盘事实 + 编队页直读 + 图鉴极标的
完整管线），人工标注按指纹 `(sword_catalog_id, kiwame_date)` 挂到具体
某一振，合成「机器观察 + 人工确认」的固定档案，供界面确认形态、补等级、
标记要练的刀。没有可信盘点时如实返回 `done=false` 骨架（summary 全 0）。

**人工标注表（sword_annotations，telemetry schema v12 建表 /
v13 加列 / v14 加列）**：

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 标注 id |
| sword_catalog_id | TEXT NOT NULL | 名册目录 id（指纹一半） |
| kiwame_date | TEXT NOT NULL | 显现日期（指纹另一半，每振终身不变） |
| level_at_mark | INTEGER NULL | 标记时的等级 |
| level_confirmed | INTEGER NULL | v13 起：人工确认的等级（1~99）；只补机器读不出的空缺，永不覆盖机器读数 |
| form_confirmed | TEXT NULL | `kiwame` / `normal` |
| keeper | INTEGER NOT NULL DEFAULT 0 | 要练的刀 |
| favorite | INTEGER NOT NULL DEFAULT 0 | v14 起：常用（玩家偏好，后端零消费） |
| watch | INTEGER NOT NULL DEFAULT 0 | v14 起：特别关心（玩家偏好，后端零消费） |
| note | TEXT NULL | 备注（≤300 字） |
| created_at / updated_at | REAL | 创建 / 最近更新时间 |
| revoked | INTEGER NOT NULL DEFAULT 0 | 软删标记 |

- 同一指纹最多一条有效标注：`POST` 同指纹再保存 = 更新传入的非空字段
  （`updated_at` 刷新，软删的除外）；撤销是软删（`revoked=1`），历史不丢。
- 标注挂行规则：一标注多行（同名多振同日显现）→ 每行 `human` 都带且
  `stale=true`，形态/等级都不合并，attention 记 `duplicate_fingerprint`；
  标注匹配不到任何行（刀解了/快照过期）→ 不进 entries，attention 记
  `stale_annotation`，`name_zh` 按 sword_db 目录反查。

**端点**（`keeper` / `favorite` / `watch` 布尔出入，confirmed_at 为标注
updated_at epoch）：

- `GET /api/data/sword-archive` → 档案本体，契约如下。
- `POST /api/data/sword-archive/annotations`，body：
  `{"sword_catalog_id": str, "kiwame_date": str, "level_at_mark": int|null,
    "level_confirmed": int|null, "form_confirmed": "kiwame"|"normal"|null,
    "keeper": bool|null, "favorite": bool|null, "watch": bool|null,
    "note": str|null}`
  → `{"ok": true, "annotation": {...}}`；校验失败（空指纹/形态值非法/
  等级非整数/确认等级不在 1~99）→ 400。
- `DELETE /api/data/sword-archive/annotations/{annotation_id}` →
  `{"ok": true}`；标注不存在 → 400。

**响应契约**：

```json
{"done": true, "reason": null,
 "observed_at": 1787219985.79, "snapshot_id": 19,
 "summary": {"total": 199, "human_confirmed": 12, "keepers": 3,
             "attention_count": 68},
 "entries": [{"observation_id": "19:12", "sword_catalog_id": "touken_xxx",
              "name_zh": "包丁藤四郎", "sword_type": "短刀",
              "level": 99, "tou_level": 5, "kiwame_date": "2024/5/1",
              "form_status": "unknown",
              "machine_form_status": null, "form_overridden": false,
              "form_evidence": ["..."],
              "unknown_fields": ["..."],
              "human": {"id": 3, "form": "kiwame", "level": 88,
                        "keeper": true, "favorite": false, "watch": false,
                        "note": "...",
                        "confirmed_at": 1787219985.79, "stale": false},
              "hints": ["同名 2 振中等级最高", "同名中显现最早"]}],
 "attention": [{"observation_id": "19:12", "sword_catalog_id": "touken_xxx",
                "name_zh": "包丁藤四郎", "level": null,
                "kiwame_date": "2024/5/1",
                "reasons": ["form_unknown", "level_unknown"],
                "hints": ["同名 2 振中等级最高"]}]}
```

- `summary`：`total` = 行数；`human_confirmed` = 人工形态确认干净挂上的
  行数；`keepers` = 人工标了「要练」且干净挂上的行数（撞车 stale 的标注
  不算确认下来）；`attention_count` = attention 条数。
- `form_status` 合并口径：机器 unknown + 人工确认 → 以人工为准
  （`form_evidence` 追加「人工确认（YYYY-MM-DD）」，日期取标注
  updated_at 本地格式化）；机器 kiwame/normal/ambiguous + 人工确认且与
  机器不同 → 人工改判（`form_overridden=true`，机器原值留在
  `machine_form_status`，机器证据保留，追加「人工改判（YYYY-MM-DD）：
  原识别=极/普通/存疑」）；人工与机器一致只追加确认证据、不改结论。
  改判后 `form_status` 是确定值，attention 的 form_unknown/form_ambiguous
  自然不再触发。
- `level` 合并口径：机器读出等级（非 None）一律信机器；机器空缺且
  人工有 `level_confirmed` → 用人工值，`unknown_fields` 摘掉 `"level"`；
  合并后仍空缺 → 进 attention（`level_unknown`）。`human.level` 恒带
  （无人工等级为 null），前端用它标「这等级是你填的」——即使机器有值
  不被采用，人工填的原值也如实展示。
- `attention` reasons：`form_unknown`（合并后仍 unknown）>
  `level_unknown`（合并后仍无等级）> `form_ambiguous` >
  `duplicate_fingerprint`（一行多标注或一标注多行）>
  `stale_annotation`（标注没挂到任何行）；一行可同时带多个 reason
  （按上述优先级排列），清单按最高优先级排序、同类内按 `name_zh`。
- `hints` 只对同名多振组（同 sword_catalog_id ≥2 行）出：同名中等级
  最高（并列都给）/ 同名中显现最早（并列都给）；显现日期解析不了
  （如 OCR 残文）就不出那条，不猜。等级提示用合并后的等级（机器空缺
  时含人工补值）。
- `sword_type` 按 sword_catalog_id 从名册目录（`touken/sword_db.py`）
  反查；`human` 无标注时为 null。

## 手动活动（manual-sessions）

手动记录只保存玩法、圈数、起止时间和可选备注。服务端据此计算总用时与平均圈速，
但绝不创建 `runs` 或玩法事件。规划页可以单独选用这份圈速，不能与まあ丸实测混合求平均。

## 账本导入导出

导入边界是“只增手账，不改自动事实”：まあ丸的 `runs`、带 `run_id` 的 events 和自动库存观察只能出现在导出中，不能通过表格回写。まあ丸导出的 Excel 只从“可再次导入”工作表读取；其中的手动收支、手动家底和手动活动都能再次导入。同一时间同一项目已有相同值时跳过，有不同值时标为冲突并要求玩家明确确认。

导入真正写入前，服务端会在用户数据目录的备份区创建 `ledger-import-<时间>/telemetry.db` 和 `manifest.json`。整份文件全是重复项时既不写入，也不制造空备份。预览只在当前进程短期保存，过期后必须重新选择文件。

空账本引导不以最近 7 天是否有数据为准，而是检查全历史家底观察。已有任何有效库存快照、途中观察、带前后余额的资源流水或大阪城小判实验时，都不会打扰老用户。真正开始引导后，抄完家底会继续到可选旧账和可选目标；完成或明确选择“不需要引导”后持久隐藏。

## 资源总账（resource-ledger，schema_version 3）

`GET /api/data/resource-ledger?days=7` / `?from=<ts>&to=<ts>`，聚合窗口内八种资源的账目。
资源全集（顺序固定）：木炭、玉钢、冷却材、砥石、小判、甲州金、委托符、加速符。

顶层结构：

```json
{
  "schema_version": 3,
  "generated_at": 1787219985.79,
  "window": {"from": 1786615185.79, "to": 1787219985.79,
             "timezone": "Asia/Shanghai", "days": 7.0},
  "per_resource": [{
    "resource": "小判", "opening": 549656, "closing": 788506,
    "total_delta": 238850, "attributed_delta": 43000, "unattributed_delta": 195850,
    "observation_count": 24, "confidence": "low"
  }],
  "daily_series": [{
    "date": "2026-08-20", "resource": "小判",
    "opening": 745056, "closing": 788506,
    "total_delta": 43450, "attributed_delta": 42850, "unattributed_delta": 600,
    "observation_count": 2, "confidence": "high",
    "gap_ids": [], "attribution_ids": ["a2"]
  }],
  "balance_series": [{
    "ts": 1787150000.0, "date": "2026-08-20",
    "values": {"小判": 788506, "木炭": 12800, "玉钢": 9600}
  }],
  "gaps": [{
    "id": "gap-1786768892-1786791169", "from": 1786768892.0, "to": 1786791169.0,
    "resources": {"小判": -1500},
    "reason": "no_observation", "human_report_ids": [2]
  }],
  "attributions": [{
    "id": "a2", "ts": 1787150000.0, "resource": "小判", "delta": 42850,
    "source": "osaka.koban_session", "script": "osaka", "run_id": "abc123",
    "event_id": 123, "label": "挖地小判 +42850", "confidence": "confirmed"
  }]
}
```

### 核心语义

- **total_delta 保留符号、禁止截断**：恒满足 `total = attributed + unattributed`，
  三者可正可负（confirmed 收入 +100、净变化 +10 → 未归因 −90）。
- **opening/closing**：opening = 窗口（或当日）前最近一次观察，没有窗前基线则用
  窗口内首观察；closing = 窗口（或当日）内末次观察。观察不足形成不了 pair 时
  `total_delta = null`（不是 0），但 confirmed 明细仍保留在 `attributions` 里。
- **daily_series**：按 Asia/Shanghai 日期分桶；跨日 run 按观察发生日记账，
  不按 run 归属日。
- **balance_series**（schema_version 3 起）：余额折线图取点序列，与
  opening/closing 同一条观察链。每个元素是一个观察时刻：`ts`（Unix 秒）、
  `date`（Asia/Shanghai 日期）、`values`（该时刻读到的资源余额，只含有读数
  的资源，缺的不补零，前端画成断点）。同一时刻多笔读数（如大阪城
  before/after 同 ts）只留最新一笔，保证一个时刻只对应一个点；窗口前的
  基线观察不进序列。

### 观察点来源与优先级

| 来源 | 覆盖资源 | 优先级 |
|---|---|---|
| `osaka.koban_session` 的 before/after（读游戏界面真数值） | 小判 | 3（最高） |
| `resource.change` 的 before/after（如异去补充提灯的购买页余额） | 按 payload 的 resource | 3 |
| `inventory.captured` 的 resources（任何 phase 都算观察） | 全部 8 项 | 2 |
| `inventory.peek` | 顶栏五资源（木炭/玉钢/冷却材/砥石/甲州金） | 1 |

`inventory.peek` **永远不含小判/委托符/加速符**（契约固定），聚合层按白名单过滤，
即使脏 payload 带了小判也不许污染小判观察链。

**去重规则**：同一资源、时间相差 5 秒内且数值一致的多来源观察 = 同一点，只算一次，
保留全部证据 event id，来源升到最高优先级。同一事件的 before/after 本来就该不同值，
不算冲突；不同来源贴脸读数不一致记 `conflicting_evidence` 缺口并降低置信度。

### 归因（attributions）

窗口内每条可确认资源变化一条记录。当前 confirmed 来源：

- `osaka.koban_session`：小判 `delta`（读数差值）。
- `repair.session_completed`：加速符 `−speedups`。**加速符去重**：同一 run 内
  已存在 `repair.confirm_screen` 的加速符逐笔 `resource.change` 时，此汇总归因
  让位（逐笔粒度更细更准，事件本身保留在事件流）；没有逐笔记录的老数据照常
  计入。run_id 缺失时按窗口内是否存在逐笔记录兜底。
- `resource.change` / `yosari.ticket_refill`：补充归城提灯时，以购买页前后余额确认小判支出。
- `resource.change` / `expedition.settlement`：远征结算页 OCR 确认的四项基础资源收益。
- `resource.change` / `forge.started`：锻刀点火按配置 `forge.recipe` 负扣
  木炭/玉钢/冷却材/砥石 + 委托符 −1（evidence `known_recipe`，机制已知值，不 OCR）。
- `resource.change` / `repair.confirm_screen`：手入确认界面 OCR 的四资源成本
  （evidence `repair_confirm_ocr`）；勾了加速符的修理另记加速符 −1
  （evidence `known_recipe`，勾是我们亲手勾的，确定事实）。
- `resource.change` / `task_rewards.reward_popup`：任务「报酬一览」弹窗按格
  图标模板匹配 + 数量 OCR 确认的收益（evidence `reward_popup_ocr`）。

### 统一资源流水约定

玩法事件负责说明“发生了什么”，所有能确认的八资源收支另写一条
`resource.change`。新流程应通过 `ToukenAgent.record_resource_change()` 交账，并在
`source_event_id` 中关联玩法事件；库存快照只负责首末余额核对。统计器可以保留旧事件
兼容读取，但不得继续把玩法专用事件当作新记账接口。

### 八资源覆盖审计（2026-08-29）

| 变化来源 | 当前记录 | 金额证据 | 状态与缺口 |
| --- | --- | --- | --- |
| 完整/手动家底 | `inventory.captured` | 游戏 OCR / 审神者输入 | 已覆盖；它是余额观察，不冒充玩法流水 |
| 途中顶栏 | `inventory.peek` | 游戏 OCR | 只观察木炭、玉钢、冷却材、砥石、甲州金，不拿单点读数算收益 |
| 锻刀点火 | `forge.started` + `resource.change` | 已知配方 | 已覆盖四资源与委托符，已接统一入口 |
| 手入 | `resource.change` | 确认页 OCR；加速符为已知操作 | 已覆盖；OCR 失败时明确记 unknown，不猜金额 |
| 任务奖励 | `task_rewards.claimed` + `resource.change` | 奖励弹窗图标 + 数量 OCR | 已接统一入口；“完成远征 3 次”等任务确认会给加速符，现有模板覆盖四资源、委托符、小判，缺加速符模板；陌生图标或同种资源重复命中时不猜类别，并在本地 `debug/` 自动留取同源运行帧 |
| 远征结算 | `expedition.settled` + `resource.change` | 结算页 OCR | 四项基础资源逐行带 ok/zero/unknown 状态，读不清记 unknown 不猜数；结果字样 成功/大成功/失败/unknown；获得道具栏墨水判空 + 图标模板待校准，栏里有内容认不出记 unknown；收菜/扫地/导航三条路径同口径记账（via 字段区分），同一屏像素指纹去重 |
| 刀解 | `dismantle.completed` + `resource.change` | 选择页四资源收益预览 OCR | 已覆盖；只在二次确认完成后落账，单项读不出时明确记 unknown，不猜数值 |
| 异去补提灯 | `yosari.ticket_refilled` + `resource.change` | 购买页前后小判 | 读全时已覆盖；读不全只留补充事实，不猜金额 |
| 江户城补手形 | `ticket.refilled` + `resource.change` | 当前活动固定 300 小判/张 | 已覆盖；v0.4.1 历史事实由兼容层回算 |
| 联队战/南瓜补手形 | `ticket.refilled` | 补充完成流程 | 部分覆盖：先记事实，实际票价和购买数量待活动开放后用同源画面确认 |
| 大阪城小判 | `osaka.koban_session` | 开工/收场小判差值 | 实验性净变化；不是逐笔掉落，关闭实验开关时不记录 |
| 地图随机资源点 | 库存首末差值 | 后续完整家底 | 未逐笔归因，保留为 unknown，不按地图规则猜收益 |
| 审神者报备 | `human_reports` | 人工说明 | 只解释指定资源缺口，不改库存、不波及其他资源 |
| 甲州金变化 | 仅库存观察 | 完整/途中 OCR | 当前自动化没有已确认的甲州金收支路径 |

**双写兼容**：未来玩法流程可发射 `resource.change` 事件；payload 带
`source_event_id` 指向旧事件 id 时，聚合层跳过旧事件那一份，不重复聚合。

```json
{"event_type": "resource.change", "run_id": "可选", "script": "osaka",
 "resource": "小判", "delta": 42850, "before": 745656, "after": 788506,
 "source": "osaka.koban_session", "source_event_id": 123,
 "attribution": "confirmed|observed|estimated|unknown",
 "evidence": "direct_before_after|settlement_ocr|rule_estimate|known_recipe|repair_confirm_ocr|reward_popup_ocr|...",
 "note": "可选"}
```

**delta 允许 null**（仅 `attribution="unknown"` 时）：资源确实发生了变化但数值
读取失败（如 OCR 翻车），用 null 保留「发生过」的事实，聚合层不进 attributions、
不影响 attributed_delta，note 里必须写明原因。

新增配置键：

- `forge.recipe`：锻刀点火配方 `[木炭, 玉钢, 冷却材, 砥石]`，缺省 `[700,700,700,700]`；
  点火记账按此配置负扣，改配方账自动跟着变。
- `repair.cost_rois`：手入选人界面左面板「所需资源」四行成本数字的黑框 ROI
  （顺序同上），修复开始前 OCR 记账用；不配则手入不记成本账。

### 缺口（gaps）与置信度

- `no_observation`：相邻快照跨 run 有差值（沿用 `inventory_gaps` 的配对语义，
  即前一条 phase 为 after/无、后一条为 before 且 run 不同），说明两段观察之间
  的账目没有覆盖。gap id 由边界时间戳生成，稳定可引用。
- `human_reported`：窗口内的人工报备（`human_reports` 表）——能通过
  `gap_key` 或时间落入挂到某个 no_observation 缺口上就挂上去
  （填入 `human_report_ids`），挂不上就单独成条。**人工报备只降置信度，
  不改写库存数值**。
- `conflicting_evidence`：同资源 5 秒内不同来源读数不一致。

confidence 规则（per_resource 和 daily_series 通用；schema_version 2 起）：

- `high`：有 confirmed 归因覆盖且观察链完整（opening/closing 都可靠、无缺口）。
- `medium`：只有观察差值、无归因覆盖。
- `low`：观察缺失 / 有波及该资源的缺口 / 该资源证据冲突。
  缺口按 `resources` 点名的波及范围降置信度：只动了小判的缺口不会把木炭
  打成 low；人工报备没挂到缺口上、单独成条时范围未知（resources 为空），
  窗口内所有资源一起降。


### 客户端收据优先（2026-10-03）

客户端 `reward` / `item` 奖励清单中的小判和已校准四资源、委托符，
直接生成 `resource.change`，证据为 `client_reward_list`。余额差先扣除
同响应的明确奖励，剩余变化保持待确认，不把混合净差算作奖励。
缺少直接余额时，不把按奖励推算的余额写成流水的 before/after。
补活动手形、刀解、签到、万屋购买已接操作名称；没有明确清单的金额
仍走唯一操作与余额差核对，不凭操作名称猜价格。尚未校准的道具不冒充八资源。
此变更只影响新导入记录，不自动改写历史账本，也未增加后台定期采集。


### 客户端资源覆盖补齐（2026-10-03）

- 加速符：道具编号 8 的库存、锻刀响应 `assist_item_id=8` 的
  `assist_item_num`、奖励清单 `item_type=1/item_id=8` 统一接入八资源。
  真实十连加速响应的余额每次递减 10；其余未校准道具不套用该映射。
- 收信箱：缓存已读箱内清单，只按成功领取响应返回的 `serial_ids`
  生成资源收据（`client_inbox_receipt`）；未领取、无清单、领取失败不推算收益。
- 十连锻刀：成功响应的 `multiple` 数量乘本次请求四材料配方，
  作为材料支出证据（`client_forge_recipe`）；委托符优惠仍按余额核对。
- 出阵推进响应中直接列出的资源奖励按「出阵资源奖励」记账。
- 查询页面和签到查询不作为消费动作；多个真实操作之间的剩余差额仍待确认。
- 稀疏响应只保存本次实读字段，不把历史或推算余额写成新观察。
  汇总窗口为八资源分别查找最近的实读基线。
- 现有样本尚无客户端手入记录；手入继续使用已有确认页 OCR 记账。
  不猜未验证端点或费用，不创建无收支的操作历史。


### 共用客户端道具与装备库存（2026-10-03）

`GET /api/data/client-inventory` 返回逐项库存及原始观察时间，供账房和预设共用。
只读取客户端实际观察，不把普通稀疏响应缺键当成零；已确认是完整道具库存的
`/login/start`、`/sally`、`/shop/list`，未持有的已知道具可记零。
小判箱仅折算箱内储备，保持实际小判与潜在总量分开，不开箱、不记为收入。
折算参考[国服官方小判说明](https://touken.youzu.com/m/guide/5056103.html)。
异去目标将客户端碎片名称对应到目标名，并与已有 OCR 按原始观察时间选择较新读数。
暖心福袋、堆肥只提供库存，不新增使用行为；御守在预设中提示所持数量。
客户端部队、刀装和宝物白名单字段保存为 `game_assets.captured`，保留装备关联，
不保存账户信息或请求凭据；预设换装仍使用原有画面核对，不根据未经校准的编号猜名称。
所有新增记录沿用既有 telemetry 事件表，不迁移或改写旧记录。
