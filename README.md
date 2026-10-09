# まあ丸 `🦊` — 《刀剑乱舞 ONLINE》本丸管家

**简体中文** · [English](README_EN.md)

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/qymd-NOT-official/maamaru-engine?style=social)](https://github.com/qymd-NOT-official/maamaru-engine)

玩刀剑乱舞九年了，现在还是想玩，只是不想再亲自点完每一轮重复操作，也不想继续手拉 Excel 计算资源。所以我做了まあ丸：需要时替我照看本丸，回来时能翻翻这一天留下的记录。

![我的本丸：庭院、动态与今天的时间表](docs/assets/product/01-honmaru-home.png)

> **你决定今天的本丸要做什么，执行、照看、收尾和记录交给まあ丸。**

国服可以交给它跑任务，也可以只用来记账；日服可以连接游戏浏览器，边玩边更新本丸记录。

**[下载最新版](https://github.com/qymd-NOT-official/maamaru-engine/releases/latest)** · **[使用指南](docs/user-manual.md)** · [v1.3.2 更新内容](docs/releases/v1.3.2.md) · [提交问题](https://github.com/qymd-NOT-official/maamaru-engine/issues)

## 还想玩，只是不想每天再点一遍日课

签到、演练、内番、锻刀刀解、当期活动……在「规划 → 今日安排」勾选今天要做的事，核对各项设置，再选好规划远征和收工后的动作。回到首页，点「一键执行今日安排」，まあ丸就接下这一份安排：先确认到点远征派出，再按顺序跑日课。

![规划：今日安排与一键日课](docs/assets/product/02-today-daily.png)

## 缺什么，就让远征朝着什么去

限锻烧掉一大截资源，想先补玉钢？下次活动还没来，想多攒些小判？在「规划」设好资源目标，或者直接选择这阵子最想攒的东西，远征建议就会跟着你的关注项走。

**不用自己逐张地图算收益、挨队核对能不能去。** まあ丸会结合参与安排的部队，给出远征目的地、时间与预计收益；去不了时，会说明缺少的刀种、总等级差距或地图占用。需要换编队，可以选已经保存的部队预设，核对后把建议变成今天的安排。

## 离活动毕业还差多少？今天到底要打几圈？

想拿到目标奖励，又不想天天拿计算器除剩余天数。在「规划」填好活动目标，まあ丸会结合当前进度、剩余时间和已经跑出来的每圈收获与耗时，估算**还要打多少圈、每天要花多久、补手形需要多少小判**。没积累够记录时，也可以先填估计，跑几圈再调整。

联队战可以把建议圈数接进今日安排；开启「使用建议圈数」后，手形不足时会用小判补充。大阪城可以盯活动结束时想留下的小判，江户城可以按钥匙目标估算补票预算。你定目标和花费，まあ丸帮你把剩下的工作算清楚；估算会随记录更新，不保证每圈收益。

<details>
<summary>晚些开工，或者让远征队出发前补好花</summary>

在规划的时间表里安排远征、联队战或保存好的任务流，拖动调整开工时间。远征可以选择部队预设，也可以开启「出发前补花」：只处理这队需要补花的成员，恢复原队伍和装备并核对后再派遣。补花会占用时间，也会覆盖游戏部队记录一，请先看[刷花与远征补花](docs/user-manual.md#刷花与远征补花)。

![规划时间表](docs/assets/v1.2-timetable.png)

到点开工需要面板保持运行、电脑没有休眠。详细操作见[规划与定时开工](docs/user-manual.md#规划与定时开工)。

</details>

## 今天只想跑这些，也可以

「功能」用来运行**单项任务或一条自定义任务流**。只想打几圈，就在「玩法设置」选地图、部队和次数；想连着做几件事，就在「流程搭建」添加步骤、调整顺序，再运行这条流程。**一键日课在「规划」设置**，日课、单项玩法和任务流各自保存参数。

<table>
<tr><th width="50%">选一项，跑这一趟</th><th width="50%">把几件事串成一条流程</th></tr>
<tr><td><a href="docs/assets/product/10-single-task-settings.png"><img src="docs/assets/product/10-single-task-settings.png" alt="单项合战场设置"></a></td><td><a href="docs/assets/product/08-workflow-builder.png"><img src="docs/assets/product/08-workflow-builder.png" alt="自定义流程搭建"></a></td></tr>
<tr><td>伤势停止、手入、刀装和换队长，按这次保存的设置执行。</td><td>每步可以单独填参数，保存下来，下次再用。</td></tr>
<tr><th>正在做什么，看执务台</th><th>需要哪一步，就加哪一步</th></tr>
<tr><td><a href="docs/assets/product/07-task-desk.png"><img src="docs/assets/product/07-task-desk.png" alt="执务台与任务日志"></a></td><td><a href="docs/assets/product/09-workflow-steps.png"><img src="docs/assets/product/09-workflow-steps.png" alt="添加任务流步骤"></a></td></tr>
<tr><td>当前任务、进度和停止原因留在这里，切去别页也能回来查看。</td><td>从开模拟器、登录到出阵和后勤，按自己的习惯组合。</td></tr>
</table>

出阵前会检查队伍、伤势和刀装，确认重伤时拦截。任务停下来，先看最后几条消息和游戏画面，再决定怎样继续。[看看支持的玩法](docs/user-manual.md#出阵与活动任务) · [任务流与一键日课](docs/user-manual.md#任务流与一键日课)

## 攒资源、等限锻，先看看家底够不够

限锻前看看攒了多少，锻完再看看花了多少；小判要留给活动，四资源还要留给手入。喜欢看数字一点点涨，也想知道它们怎么突然少了一截——「仓库」把家底、走势和每笔收支留在一起，不必每天抄进表格。

<table>
<tr><th width="50%">本丸的家底</th><th width="50%">每一振的刀账</th></tr>
<tr><td><a href="docs/assets/product/03-inventory.png"><img src="docs/assets/product/03-inventory.png" alt="家底与资源走势"></a></td><td><a href="docs/assets/product/04-sword-archive.png"><img src="docs/assets/product/04-sword-archive.png" alt="刀账档案与成长履历"></a></td></tr>
<tr><td>小判、资源、道具，看看还剩多少，也看看最近怎么变的。</td><td>找刀、标记要练的刀，展开成长与履历，翻翻已经记下的变化。</td></tr>
</table>

国服第一次记账，把游戏停在本丸，点「读取游戏家底」，读取期间暂时不要操作模拟器。旧账可以导入，额外收支可以补记；没读完整可以重试。记录需要同步，倒计时到了也不代表已经领取。[仓库与成绩单](docs/user-manual.md#仓库与成绩单怎么看) · [刀账](docs/user-manual.md#刀账与所持名单)

## 这振练到哪了，乱舞还差几把？

同一把刀有几振，一振在练级，一振留着收藏，还有几振等习合。「刀账」按具体一振记等级、累计经验、乱舞习合值与入手日期，同名刀分开，特别关心和要练的刀也能各自标记。

**练级看累计经验，乱舞看还差多少，内番看已经养成了多少生存与侦察。** 展开「成长与履历」，就能翻到已经记下的变化；乱舞所需振数会标为估算，内番连续没涨也会说明是否喂满尚未确认。哪一天来到本丸、从哪里来，能确认的也留在档案里。

## 上次活动那队怎么配的？存好，下次再用

练级队、活动队、远征队，可以在「刀剑 → 部队预设」选好具体一振，保存刀装、马、御守与宝物设置，供支持预设的任务使用。留空的位置保持原样，找不到指定成员或装备时会停下来。

![部队预设与装备设置](docs/assets/product/05-team-presets.png)

部队预设与换装仍在试用。先拿容易复原的队伍短跑，核对结果，再用于重要安排。[部队预设使用说明](docs/user-manual.md#部队预设试用)

## 捞到了谁、哪炉出了货，都值得记一笔

捞刀时想记下「今天这张图带回了谁」，限锻时想留住「什么配方、谁当近侍、这一炉花了多少」。这些不用只靠截图和回忆：「全部记录」按日期收好任务与收支，掉落按地图整理，锻刀手记逐炉保留配方、近侍、结果与花费。

忙完回来，既能查一趟任务做了哪些事，也能翻翻新刀入手那一天。未确认的来源会保留为未知；记录从采集后逐渐积累，不会补造过去没记下的经历。

<table>
<tr><th width="50%">按日期翻看本丸记录</th><th width="50%">按地图整理掉落</th></tr>
<tr><td><a href="docs/assets/product/06-records.png"><img src="docs/assets/product/06-records.png" alt="日历与全部记录"></a></td><td><a href="docs/assets/v1.2-drops.png"><img src="docs/assets/v1.2-drops.png" alt="按地图查看掉落统计"></a></td></tr>
<tr><td>展开一趟任务，看看完成了哪些事，收支从哪里来。</td><td>掉落和锻刀各有记录，想查的时候再翻。</td></tr>
</table>

喜欢自己做表也没关系：旧账可以导入，额外收支可以补记，流水和每日汇总可以导出 Excel，完整流水也可以导出 CSV。まあ丸替你记，表格仍然归你整理。

回到「我的本丸」，还能写自己的小记、换头像、整理审神者档案，或切换和纸与像素主题。数字之外，也留一点自己的本丸生活。

## 只想记账？可以。想让它动手？也可以。

| 能做什么 | 国服本丸 | 日服本丸 |
|---|---|---|
| 我的本丸、档案与小记 | ✓ | ✓ |
| 家底、道具与刀账记录 | ✓ | ✓ |
| 更新记录 | 读取游戏家底、更新刀账，相关任务收工后同步 | 连接专用游戏浏览器，游玩期间更新 |
| 单项任务、自定义任务流、一键日课 | ✓ | — |
| 自动出阵、远征与定时执行 | ✓ | — |

启动器选择对应本丸。两服的配置、档案、小记与记录分别保存；日服入口提供账房与本丸展示。[日服本丸使用说明](docs/user-manual.md#日服本丸账房)

## 开始使用

国服自动任务目前主要使用环境是 **Windows、MuMu 12、1280×720 游戏画面**。界面为简体中文。

1. [下载最新版](https://github.com/qymd-NOT-official/maamaru-engine/releases/latest)，选择 `maamaru-setup-v*.exe` 安装；免安装版下载 `maamaru-launcher-v*.zip`，完整解压后运行 `まあ丸启动器.exe`。
2. 国服：打开 MuMu 和游戏，进入本丸。启动器检查完成后点「启动まあ丸」；找不到模拟器时，点「选择模拟器」指定安装目录。日服：选择「日服本丸」，按面板提示连接专用游戏浏览器。
3. 想跑任务，在「功能 → 玩法设置」选好地图、部队、伤势与资源设置，先设 **1 圈**，观察这一圈是否按自己的意思跑完。想记账，先到「仓库」读取游戏家底。

不必先录完整刀账或做预算才能跑普通任务。熟悉以后，再去规划里安排一键日课。

**详细操作 → [使用指南](docs/user-manual.md)**。启动提醒看[安装与启动](docs/user-manual.md#安装与启动)，任务停下来看[排查停止](docs/user-manual.md#任务为什么停了)。

## 使用前知道这些

本项目与游戏运营方无关，包含游戏自动操作，可能违反游戏服务条款并带来账号处罚、误操作或数据损失风险，请自行判断是否使用。第一次使用新任务或游戏更新后，先跑少量次数确认。

- 部队、伤势条件、资源消耗和保护名单由你决定，执行前请核对设置。
- 游戏更新、不同分辨率或新地图可能影响识别；江户城目前只智能跑 E4。具体玩法范围见使用指南。
- 独立账房入口与 Android APK 暂停提供；账房从完整面板的「仓库」进入。
- 配置与记录保存在本机：安装版位于 `%LOCALAPPDATA%\Maamaru`，源码版位于 `%LOCALAPPDATA%\Maamaru-Dev`，更新不会删除用户数据。

遇到问题，保留模拟器当前画面，从面板或启动器导出「反馈错误」，在 [GitHub Issues](https://github.com/qymd-NOT-official/maamaru-engine/issues) 附上版本、任务和复现步骤。[反馈错误说明](docs/user-manual.md#反馈错误)

<details>
<summary><strong>开发者运行与项目结构</strong></summary>

环境要求：Windows、Python 3.12+；正常管家模式还需要 MuMu 模拟器和 ADB。

```powershell
git clone https://github.com/qymd-NOT-official/maamaru-engine.git
cd maamaru-engine
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe maamaru_app.py
```

也可以直接双击 `启动源码版.cmd`。默认面板地址为 `http://127.0.0.1:8080`；安装版和源码版不建议同时运行。

まあ丸由 Python 长任务编排、ADB、MaaFramework、FastAPI 与 Vue 3 / TypeScript 组成。程序文件和用户数据严格分开，旧目录迁移会先复制、备份并校验，不自动删除来源。详细契约见 [用户数据与迁移](docs/data-layout.md) 和 [结构化运行数据](docs/telemetry-data.md)。

```text
maamaru-engine/
├─ launcher/            启动器、更新与数据迁移
├─ panel/               FastAPI 后端和 Vue 本丸面板
├─ ledger_app/          独立账房与 Android 离线版
├─ touken/flows/        日课、出阵、活动、远征与恢复流程
├─ resource/base/       OCR、模型和识别模板
├─ profiles/            活动运行配置
└─ docs/                使用说明、版本记录与开发文档
```

支持仓库协作的 Agent 可以先阅读 [まあ丸 Agent 使用协助规范](docs/agent-user-guide.md)，再帮助玩家检查环境、修改配置或定位停止原因。

</details>

## 鸣谢

まあ丸使用 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 提供的视觉识别能力，并由 [FastAPI](https://github.com/tiangolo/fastapi)、[Vue](https://github.com/vuejs/core)、[Vite](https://github.com/vitejs/vite)、[pywebview](https://github.com/r0x0r/pywebview)、[Fusion Pixel](https://github.com/TakWolf/fusion-pixel-font) 与 [Kenney Game Icons](https://kenney.nl/assets/game-icons) 等开源项目共同支撑。完整许可证信息见 [NOTICE](NOTICE)。

まあ丸本身也由人类与 AI 共同开发：人类提出真实场景、定义玩法规则和安全边界、判断方案并实机验收；Kimi Code K3、Codex 与 WorkBuddy 作为协作者参与实现、排障、测试和发布。

本项目以 [AGPL-3.0-or-later](LICENSE) 开源。

<details>
<summary>仓库彩蛋：MCS（Multi-Cow System，多牛协同生产系统）🐂🌙</summary>

仓库里把多 Agent 协作戏称为 MCS：前端牛、脚本牛、IT 外包牛和 README 牛各干一摊，再由人类传递信息、拍板和验收。为什么都是牛？因为 Codex 的桌宠叫 **NULL**，读快了很像“牛”。

### 月下铸经 `🌙`

> _此真经非一人之力，谨遵 vibe coding 之古训，特铭众道友功德于源流。_

昔有月之暗面，遣基米可叁下凡，铸其后端；又有接屁踢五点六昊天，司前端之事；复得工作伙伴深度求索威肆，稍加点化，终成《麻麻露真经》。

> _本丸以大蛇为主，爪哇为辅。若有 Bug，皆属天命；若无 Bug，皆赖诸位道友相助。_

</details>
