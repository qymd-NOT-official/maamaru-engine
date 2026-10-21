# 程序文件与用户数据

まあ丸把可重新安装的程序文件和不可随升级丢失的用户数据分开保存。

## 程序文件

安装器的目标位置为 `%LOCALAPPDATA%\Programs\Maamaru`。发布包中的程序、OCR 模型、识别模板、官方 profiles、面板资源和默认配置都视为只读文件。每个发布包携带 `manifest.json`，记录程序版本、数据结构版本和资源哈希。

未来自动更新应把新程序放进独立版本目录，校验清单后再切换当前版本；失败时切回旧版本，而不是覆盖正在运行的 EXE。

## 用户数据

安装版默认使用 `%LOCALAPPDATA%\Maamaru`，源码开发默认使用 `%LOCALAPPDATA%\Maamaru-Dev`。`MAAMARU_DATA_DIR` 可以为测试或便携场景指定其他位置。

```text
Maamaru/
├─ config/                 本丸、面板和远征排班配置
├─ profiles/overrides/     用户识别素材覆盖
├─ state/                  日课、远征、库存和会话状态
├─ logs/                   面板数据库与启动器日志
├─ debug/                  调试输出
├─ backups/                迁移前备份
├─ updates/                未来更新下载暂存
├─ migration.json          旧版迁移记录
└─ data-version.json       用户数据结构版本
```

官方 profiles 留在程序目录。识别时先载入官方素材，再用 `profiles/overrides` 中同名文件覆盖，避免旧版整套素材阻止官方新增内容生效。

## 首次启动和迁移

启动器先创建目录，再从以下明确位置寻找旧版数据：现有用户数据根目录、其 `data` 子目录、程序/源码根目录及其 `data` 子目录。不会扫描整块硬盘。

旧版配置、状态、日志、调试文件和用户 profiles 只复制，不覆盖新目录中的有效文件，也不删除来源。实际迁移内容先备份到 `backups/legacy-时间/`，结果写入 `migration.json`；成功记录存在时重复启动不会再次迁移。缺少配置才从公开模板创建。

## 安装、更新和卸载约束

- Windows 安装器默认安装到 `%LOCALAPPDATA%\Programs\Maamaru`，无需管理员权限；开始菜单快捷方式默认创建，桌面快捷方式由用户选择。
- 安装和更新不得把真实用户配置打进发布包。
- 更新前必须校验 `manifest.json`，停止工作进程，并保留至少一个可回滚版本。
- 数据结构升级前必须备份，并用 `data-version.json` 决定迁移步骤。
- 卸载默认只移除程序。删除用户数据必须由用户单独明确确认。
