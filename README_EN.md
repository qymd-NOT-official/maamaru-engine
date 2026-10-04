# Maamaru `🦊` — A Honmaru Steward for Touken Ranbu ONLINE China

[**简体中文**](README.md) · **English**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![GitHub stars](https://img.shields.io/github/stars/qymd-NOT-official/maamaru-engine?style=social)](https://github.com/qymd-NOT-official/maamaru-engine)

> **You decide what your Honmaru should do today. Maamaru handles the routine, keeps watch, wraps up, and keeps the records.**

**[Download the latest release](https://github.com/qymd-NOT-official/maamaru-engine/releases/latest)** · **[User guide (Chinese)](docs/user-manual.md)** · [What's new in v1.0.1 (Chinese)](docs/releases/v1.0.1.md) · [Report an issue](https://github.com/qymd-NOT-official/maamaru-engine/issues)

Maamaru supports the **Simplified Chinese client on the China server**. The app is currently in Simplified Chinese.

## From today's plan to a new page in your Honmaru

To hand over repetitive work, pick a task, check the team, spending permissions, and stop conditions, and try a short supervised run. When you want balances, bookkeeping, or a budget, choose Read Game Balances (读取游戏家底) in the warehouse. As run-time and yield records accumulate, use them to plan work and schedules.

**Start with a task, or read your balances to begin bookkeeping. Use what you need.**

**1. Return to My Honmaru**

Recent game state, new swords, and your own notes stay here.

![1. Return to My Honmaru](docs/assets/v1-home.png)

**2. Save a daily routine**

Arrange the daily steps in order and save them for reuse.

![2. Save a daily routine](docs/assets/v1-daily-workflow.png)

**3. Put work on the timetable**

Set Regiment Battle runs and a start time, or schedule a saved workflow separately.

![3. Put work on the timetable](docs/assets/v1-timetable.png)

**4. See what is happening**

The task desk shows the current job and its execution log.

![4. See what is happening](docs/assets/v1-running-log.png)

**5. Review the ledger**

Check balances, gains, and spending, then follow the records for details.

![5. Review the ledger](docs/assets/v1-ledger.png)

You arrange the day's work; when you return, you can see what got done: completed runs, where resources came from, and which swords came home. Open the report or ledger when you want the details.

## Hand over the repetitive work

I have played Touken Ranbu for nine years, and I still want to keep playing. I just no longer want to click through every repetitive task myself—or maintain an Excel sheet by hand to track my resources. That is why I built Maamaru: to look after my Honmaru when needed, and leave records I can read when I return.

### Planning: arrange work once you have records

Read balances support budgeting; run times and yields need task or manual activity records. You do not need a full plan to get started. Use your Honmaru's records to estimate the remaining event work, daily runs, and koban budget. Recommended Regiment Battle runs can go into the timetable, with the time and run count still editable. Saved workflows can be scheduled separately.

Change Konnosuke's resource focus and the expedition recommendations follow it. If a team cannot reach a useful map, Maamaru explains the missing sword type, total-level requirement, or occupied map. Existing team presets can also be used for expeditions.


### Workflows: save your daily routine

Combine dailies, expeditions, sorties, and events in your preferred order, then save the workflow for reuse. Each step retains its own team, run count, and spending settings. You can also launch one task directly.

Before departure, Maamaru checks the team, injuries, and equipment. During the run, it follows your settings for repairs, troop replenishment, and common interruptions. It resumes where recovery is supported and stops with an explanation when it cannot confirm the situation. At the end, your chosen action can exit the game, close the emulator, or put the PC to sleep.


### Inventory and sword archive: balances with a history

The ledger translates game resource, item, and event records into readable transactions alongside Maamaru's execution records. Koban reserved for scheduled Regiment Battle runs is included in the budget, so you can see what remains available.

The sword archive uses unique IDs from the game's owned-sword list, keeping duplicate swords separate. Forging, battle drops, and inbox collection leave acquisition records; confirmed refinement, Ranbu fusion, and dismantling update the specific swords involved. Your favorites and training marks stay with their entries.

Game records are read during synchronization or at the end of related tasks, rather than monitored continuously. Unconfirmed sources remain unknown.


### My Honmaru: look back on the day

New swords, returning expeditions, forging, and sword sorting become journal posts with names and results. Add your own notes, change your avatar, and edit your Saniwa profile to make the Honmaru feel like yours.

Switch between washi-paper and pixel themes whenever you like. Kogitsunemaru and Konnosuke are here in the courtyard, too.


## First-time setup

Choose **Start Maamaru** in the launcher, open the emulator, and enter the game's Honmaru. Then choose the path that suits you:

**Just run a task**

Pick a standalone task at the task desk, check the team, spending permissions, and stop conditions, and try a short supervised run. You can start directly without using the ledger, reading balances, or making a plan. Save a workflow when you want to combine several tasks.

**Review balances, keep records, or plan work**

Open the warehouse and click **Read Game Balances (读取游戏家底)**. Maamaru reads game records, then checks the forging and inventory screens for resources, koban, and tokens. It does not run daily tasks, forge, or depart. Each reading method reports its result; retry if either is incomplete.

Old-ledger import and goals are optional. Current balances support budgeting; run times and yields need task or manual activity records. Use the corresponding planning advice and schedules as those records accumulate.

The standalone ledger entry and Android APK are paused. Launch Maamaru and open Warehouse to read game balances and manage records.

## Where to start

| What you want to do | Which entry to open |
|---|---|
| Run dailies, sorties, events, or scheduled work | **Launch Maamaru** in the launcher |
| Review records, enter transactions, budget, or import old records | **Launch Maamaru → Warehouse** |

Excel/CSV imports and exports are supported, with import previews and backups before writing. Existing local records are preserved.

Supported tasks include dailies and expeditions, normal battle maps, Chapter 1 of Iko, Underground Treasure Chest, Edo Castle E4, Treasure Trove, Regiment Battle (including the seaside map), and the Pumpkin event. Detailed settings, prerequisites, and stop conditions are in the [user guide (Chinese)](docs/user-manual.md).

## The Saniwa keeps control

| You decide | Maamaru handles |
|---|---|
| Goals, task order, teams, and formation rules | Repeating the saved steps |
| Injury stop conditions and whether to repair and resume | Checking before departure and following those conditions |
| Whether to replenish passes, spend koban, or use items | Spending within your permissions and stopping when uncertain |
| Which swords may be dismantled or used for refinement and Ranbu fusion | Following the selection and protection rules |

Reading game lists and records does not change the game. Applying a team preset, departing, and spending resources are actual actions that require you to start the corresponding task. Resource advice does not change teams on its own, and Regiment Battle advice only starts on a schedule after you save an arrangement.

## Download and Get Started

Download a package from [GitHub Releases](https://github.com/qymd-NOT-official/maamaru-engine/releases):

- `maamaru-setup-v*.exe`: Windows installer;
- `maamaru-launcher-v*.zip`: portable Windows package—extract it, then run `まあ丸启动器.exe`;

> [!CAUTION]
> This is an independent fan project and is not affiliated with the game's developer, publisher, or operators. The normal steward mode automates interactions with the game and may violate its terms of service, resulting in account penalties, unintended actions, or data loss. The standalone ledger does not connect to the game. Decide for yourself whether to use automation, and test every new task with a short supervised run—especially after a game update.

> [!NOTE]
> **Maamaru is under active development.** Its primary test environment is MuMu Player 12 with the game at 1280×720. It has seen long-running use on a real account and installation by non-technical users, but a new map, another emulator, a different resolution, or a game UI update may still break visual recognition.

## Where Your Data Lives

- The installed version stores configuration, logs, reports, journal entries, plans, and backups in `%LOCALAPPDATA%\Maamaru`. Updating the app does not remove this data.
- The source version uses `%LOCALAPPDATA%\Maamaru-Dev` by default, keeping development records separate from the installed app's ledger.
- Reports do not store game screenshots. An exported feedback bundle contains the app version, a system summary, and text logs—not configuration files, secrets, inventory data, or the state database.
- Android data stays in the app's directory on the phone. There is currently no sync or export, and uninstalling the APK erases its preview data. Do not use it as the only copy of information you cannot recreate.

## Current Limits

- Edo Castle automation supports E4 only; other difficulties are not planned. Iko currently supports Chapter 1 only.
- Team presets and equipment changes are still in trial. Start with a team you can easily restore, and check the result before using a preset for an important unattended run.
- When the game controls marching, it also controls routes and formations. Maamaru chooses forks and formations only in its manual-marching mode.
- The Pumpkin event automation never buys extra tokens. It stops safely when none remain.
- Retreating before a boss, reading fatigue, captain rotation by drag, and reconnecting after a dropped connection all depend on the actual screen. Maamaru stops instead of guessing when it cannot verify the state.
- Secretary chat and remote notifications through QQ or Telegram have not completed real-device validation by the maintainer and are not primary entry points.
- Emergency Stop terminates the task process immediately, so it cannot guarantee a final inventory check or a return to the Honmaru. Check the emulator screen before starting another task.

## What's Next

v1.0.0 connects the honmaru journal, game sword archive, resource ledger, and planning. The next priority is real-device feedback about stalls, recognition mistakes, mismatched records, and changes that are hard to correct—especially while checking preset teams and equipment. Desktop–Android ledger sync remains a request to evaluate against actual use.

## Reporting a Problem

If a task stops, visual recognition fails, or installation goes wrong, preserve the current emulator screen first. Then export a feedback bundle from the panel or launcher and open a [GitHub Issue](https://github.com/qymd-NOT-official/maamaru-engine/issues) with the version, task, and steps to reproduce.

<details>
<summary><strong>Running from source and project structure</strong></summary>

Requirements: Windows and Python 3.12+. Normal steward mode also requires MuMu Player and ADB.

```powershell
git clone https://github.com/qymd-NOT-official/maamaru-engine.git
cd maamaru-engine
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe maamaru_app.py
```

You can also double-click `启动源码版.cmd`. The panel opens at `http://127.0.0.1:8080` by default. Running the installed and source versions at the same time is not recommended.

Maamaru combines Python orchestration, ADB, MaaFramework, FastAPI, and Vue 3 with TypeScript. Program files and user data are kept strictly separate. Migration from an old data directory copies, backs up, and verifies data before use; it never deletes the source automatically. See [User Data and Migration](docs/data-layout.md) and [Structured Runtime Data](docs/telemetry-data.md) for the detailed contracts (Chinese).

```text
maamaru-engine/
├─ launcher/            Launcher, updates, and data migration
├─ panel/               FastAPI backend and Vue Honmaru panel
├─ ledger_app/          Standalone ledger and Android offline edition
├─ touken/flows/        Dailies, sorties, events, expeditions, and recovery
├─ resource/base/       OCR, models, and recognition templates
├─ profiles/            Event runtime configuration
└─ docs/                User guide, release notes, and developer documentation
```

</details>

## Acknowledgements

Maamaru uses [MaaFramework](https://github.com/MaaXYZ/MaaFramework) for visual recognition and is built with open-source projects including [FastAPI](https://github.com/tiangolo/fastapi), [Vue](https://github.com/vuejs/core), [Vite](https://github.com/vitejs/vite), [pywebview](https://github.com/r0x0r/pywebview), [Fusion Pixel](https://github.com/TakWolf/fusion-pixel-font), and [Kenney Game Icons](https://kenney.nl/assets/game-icons). See [NOTICE](NOTICE) for complete license information.

Maamaru itself is also a collaboration between a human and AI. The human brings real use cases, defines gameplay rules and safety boundaries, chooses among proposed solutions, and validates them on a real device. Kimi Code K3, Codex, and WorkBuddy collaborate on implementation, debugging, testing, and releases.

Maamaru is open source under the [AGPL-3.0-or-later](LICENSE) license.

<details>
<summary>Repository easter egg: MCS (Multi-Cow System) 🐂🌙</summary>

We jokingly call our multi-agent workflow the MCS: the frontend cow, automation cow, outsourced-IT cow, and README cow each take a part of the job, while the human carries context between them, makes the decisions, and signs off on the result. Why cows? Codex's desktop pet is named **NULL**, which sounds a lot like the Chinese word for cow when you say it quickly.

### The Scripture Forged Beneath the Moon `🌙`

> _This sacred text was not wrought by one hand alone. In faithful observance of the ancient art of vibe coding, we hereby record every fellow cultivator's contribution at its source._

In ancient times, Moonshot AI sent Kimi K3 down from the heavens to forge the backend. Then came GPT-5.6 of the High Heavens to preside over the frontend. WorkBuddy DeepSeek V4 offered one final touch of enlightenment, and thus the *Maamaru Sutra* was complete.

> _This Honmaru follows the Great Python, with Java as its aide. If there are bugs, such is the Mandate of Heaven; if there are none, all credit belongs to our fellow cultivators._

</details>
