# PYeyedata 项目现状与接手指南

更新：2026-10-19；应用版本 **0.9.5**，PRD 0.9，数据契约 1.3.0。默认分支 `master`。

## 给另一台主机的 Codex

建议克隆 [仓库](https://github.com/hexiovo/PYeyedata)，先读取根目录 `AGENTS.md`、本文和 `version.md` 最新条目，再查看 `git status` 和最近提交。下载 Release 的 `source.zip` 也可开发，但不含 Git 历史；需要提交或正式打包时先克隆仓库。

这是 Windows 10/11 x64 的 PySide6 桌面眼动分析程序，不需要服务端或数据库。最终用户从 [Releases](https://github.com/hexiovo/PYeyedata/releases/latest) 下载便携包即可使用；开发机需要 x64 CPython 3.12（最低 3.12.1，已验证 3.12.10）和 Git。

```powershell
git clone https://github.com/hexiovo/PYeyedata.git
cd PYeyedata
.\setup.cmd
.\start.cmd
# 非交互、离屏验证，不接管鼠标
powershell -NoProfile -ExecutionPolicy Bypass -File tools/manage.ps1 -Action Check
```

`setup.cmd -Python "C:\path\to\python.exe"` 可以明确选择解释器。依赖在 `requirements.txt` 精确锁定；现有完整环境通过 build manifest 检查，不要随意升级科学依赖。当前没有常规 `tests/` 测试套件；可复用检查从产品自身固定诊断运行，历史外置测试不能当作仓库可重放的测试。

## 现有功能和入口

- GUI 七步工作流：导入/映射、质量与处理、指标、群体上下文、统计设计、运行、结果与追溯。支持数据质量门控、个体/群体/高级信号分析、统计、图表和 CSV/XLSX/PDF 等导出。
- `.pyeplan` 保存分析设置，排除输入数据与结果；`.pyea` 保存 full/results_only 运行档案。关闭窗口会提醒归档未保存运行。
- 内置 F1 新手指南、十类输入说明、合成练习与模板、41 页中文图解 PDF；指南内容版本仍为 0.9.3，不随本次部署版本虚增。
- 0.9.4 品牌图标已纳入本次提交，包含 EXE/窗口图标与稳定的 Windows AppUserModelID。0.9.5 增加部署、发布和接手入口，不改科学算法。

## 代码地图

| 位置 | 作用 |
| --- | --- |
| `pyeyedata_launcher.py`, `pyeyedata_bootstrap.py` | 启动前准备用户目录、日志、Matplotlib 缓存；诊断入口 |
| `pyeyedata/gui/app.py`, `main_window.py`, `session.py`, `tasks.py` | Qt 应用、界面、会话、后台任务与取消 |
| `pyeyedata/gui/bridge.py` | GUI 与科学 pipeline 的事务边界，运行/缓存发布 |
| `pyeyedata/gui/diagnostic.py` | 启动、合成 pipeline、可靠性、易用性、档案重开固定诊断 |
| `contracts.py`, `io.py`, `store.py` | 数据契约、输入读取、底层存储 |
| `processing.py`, `eye_streams.py`, `events.py`, `aoi_geometry.py` | 预处理、左右眼、事件与 AOI 几何 |
| `pipeline.py`, `individual.py`, `group.py`, `advanced_signals.py` | 分析编排与科学计算模块 |
| `statistics.py`, `STATISTICAL_METHODS.md`, `scopes.py` | 统计设计、方法约束与分析范围 |
| `execution_cache.py` | 有界、显式 opt-in 缓存；默认无缓存，不得丢失来源与隔离 |
| `archive_session.py`, `gui/analysis_plan.py` | 运行档案与分析方案，各自版本化 |
| `exports.py`, `reporting.py`, `gui/charts.py` | 表格、报告与图表 |
| `resources/`, `resource_paths.py` | 模板、帮助、字体、许可证、图标；兼容冻结程序 |
| `build_windows.py`, `.spec`, `build_manifest.py`, `build_identity.py` | 单文件 EXE、PE 版本、构建输入哈希与运行身份 |
| `tools/manage.ps1`, `run_checks.py`, `release_windows.py` | 环境准备、源/冻结诊断、便携包与校验和 |

表中省略目录前缀的 Python 模块均在 `pyeyedata/` 下。唯一推荐源码启动入口是根目录 launcher，以便先初始化缓存与日志。

## 必须保持的语义

保留 source validity、时间/单位/坐标/眼别声明、同步证据、参数引用、run ID 和状态。`partial` 或 `unavailable` 可能是正确结果，不要为了界面好看忽略缺失统计设计、单位或证据。内置 201 行练习的左瞳孔均值应约为 3.3 mm；其整体 partial 状态是有意保留的。

统计单位和依赖结构必须匹配设计；不能把相关 pair 行当作独立样本。处理/插值不能悄悄改变原始有效性依据。缓存只在操作成功并完成当前代际/取消检查后发布，读取需要隔离和完整性验证。归档与方案使用不同契约；修改需验证回读、失败原子性与来源字段。

用户缓存、日志、设置在 `%LOCALAPPDATA%\PYeyedata\`；不写程序目录。输入、导出和档案由用户选择位置。升级替换便携 EXE 不会迁移或删除这些文件。

## 验证、发布和当前局限

`tools/run_checks.py` 在独立进程运行 `synthetic-pipeline`、`reliability-smoke`、`usability-smoke`，覆盖导入/计算/导出/档案、方案和退出等既有断言。`release.cmd` 对源码和最终 EXE 各运行三项，校验 PE 版本、内嵌源/资源哈希，生成 EXE、便携 ZIP、精确提交源码 ZIP、`release-info.json` 和 `SHA256SUMS.txt`。详见 `docs/DEPLOYMENT.md`。

Release 的 `release-info.json` 是本次构建的提交 SHA、实际校验结果、环境、EXE 哈希与限制的事实来源；GitHub Release 是否已发布以远端页面为准。构建前先提交所有修改，包括版本记录；构建后不要再修改已哈希输入来追记结果，否则源码会与 EXE 不符。

固定诊断使用合成数据和 Qt offscreen，**尚未声明真实设备验证、干净新主机验收、原生多显示器/DPI 覆盖**。无代码签名证书。后续改动要按影响补针对性验证，不能从三个 smoke 推断全部科学方法均独立验证过。

旧 `version.md` 含原机器 E:/H:/F: 审计路径和阶段性 pending 记录，按日期阅读，不能将早期记录当作最新结论。这些目录不随仓库转移，构建和普通开发不依赖它们。`plans/portable-release.md` 记录本次部署改造方案；没有隐藏的外部接手任务清单。

下一步可优先在另一台 Windows 主机验证下载、首次启动、练习、中文 PDF、归档重开及不同 DPI。若继续科学方法开发，先阅读对应模块和 `STATISTICAL_METHODS.md`，明确基线与适用条件再修改。
