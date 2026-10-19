# PYeyedata — Codex 接手入口

先读 `CODEX_HANDOFF.md`、`version.md` 最新条目及 `docs/DEPLOYMENT.md`。

- 默认在 `master` 工作；先检查 `git status`，保留已有改动。未经要求不要提交输入数据、运行档案、虚拟环境或构建临时文件。
- Windows 10/11 x64，CPython 3.12.1+（仅 3.12 系列），已验证 3.12.10。`setup.cmd` 准备依赖，`start.cmd` 启动；命令不依赖原主机磁盘目录。
- 检查：`powershell -NoProfile -ExecutionPolicy Bypass -File tools/manage.ps1 -Action Check`。证据在输出提示的临时目录。已有固定诊断位于 `pyeyedata/gui/diagnostic.py`。
- 发布：先完成所有代码、资源、版本和文档改动并提交，再运行 `release.cmd`。不要在打包期间改动输入文件。发布包和 EXE 不入 Git。
- 每次更新维护 `version.md`；发布版本需同步 `pyeyedata/app_info.py`、资源索引和许可证来源清单，并更新索引里的清单 SHA-256。
- 科学结果必须保留单位、有效性、数据来源、参数、状态和可追溯信息。合成诊断不代表真实设备验证；不要将 unavailable/partial 强行转成 complete。
- 用户分析数据、`.pyea` 档案、`.pyeplan` 方案属于不同对象，避免破坏兼容性。算法、归档契约、缓存更改需针对相应边界验证。
- 临时测试脚本和无用文档及时删除；保留可复用检查工具。长任务避免频繁轮询。
- 历史 HexiPlan 路径只是旧审计来源。普通开发不需要启用该模式，也不应依赖这些目录。
