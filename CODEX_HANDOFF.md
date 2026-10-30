# PYeyedata 项目现状与接手指南

更新：2026-10-30；应用版本 **0.9.8**，PRD 0.9，数据契约 1.3.0。包含启动 Splash、10 个统计预设与可选瞳孔预处理。此前源码检查及 HexiPlan 独立验收已完成；本次完整 EXE 构建、源码/冻结诊断、原生依赖核对和精确提交以输出目录的 `release-info.json` 为准。默认分支 `master`。本次只作本机交付，远端发布状态另行核实。

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

## 0.9.8 功能与验证边界

- 单文件启动在 PyInstaller bootloader 阶段显示静态“正在启动，请稍候…”画面；Qt 主窗口可见后关闭，启动异常也会先清理画面。CPython 3.12.10 / PyInstaller 6.11.1 控制 PATH harness 的 success 和注入失败分支均已验证；这不是完整产品 EXE 构建，也不表示解压耗时消失。
- 统计设计页增加 10 个常见预设和简单 / 高级字段视图。预设提供明确标注、可编辑的连续结果起点，但不会从数据推断变量类型；独立两组与多组按过滤后实际水平路由。预设不会自动确认独立性、填写研究依据、猜团队 ID 或绩效连接。细节见 [`docs/STATISTICS_PRESETS.md`](docs/STATISTICS_PRESETS.md)。
- 质量页增加可选瞳孔速度 MAD、眨眼 / 跟踪丢失边界 padding 和 PCHIP 插值；均默认关闭，启用插值后默认线性。原始值和来源有效性保留，派生值、状态和逐行来源可审计。细节与最新论文边界见 [`docs/PREPROCESSING_OPTIONS.md`](docs/PREPROCESSING_OPTIONS.md)。
- 便携 ZIP 清单包含上述两份指南；资源索引和许可证来源清单已更新到启动图与 Splash 原生依赖。三项源码固定诊断均通过，但证据合并了不同运行：合成流程结果复用自 Check2，可靠性与可用性来自之后的定向重跑。本次完整产品冻结检查与原生组件最终核对由 release.cmd 执行；事实来源是产物中的 release-info.json。此前 HexiPlan 源码任务已完成，不依赖外部 run 目录才能启动或构建。

## 现有功能和入口

- 新增 52 个常见指标（34 个事件分布、9 个 AOI 注视/命中、9 个阅读指标），在指标页选择；阅读标注与顺序复用已有辅助来源。首次注视与原有采样进入时间分开输出，未知观察不作未命中；新增覆盖确认只声明分类范围，不替代设备验证。定义、操作及兼容规则见 [`docs/COMMON_METRICS.md`](docs/COMMON_METRICS.md)。`common_metrics.py` 负责计算，`gui/diagnostic_common_metrics.py` 提供固定手算/生成/集成检查，已接入 reliability-smoke。个体算法为 `individual-0.3.0`；档案/方案 schema 未变，旧方案只补新增字段的安全 None/False 缺省。

- 可选瞳孔预处理仅在质量页显式应用：默认关闭速度 MAD 拒绝、眨眼 / 丢失 padding 和 pupil 插值；PCHIP 仅在四个邻近有效锚点和连续时间支持成立、且缺口仍符合现有限制时使用。短遮罩缺口可能被插值，但源值、来源有效性、逐行遮罩旗标和替代值 provenance 分开保留。gaze 不变；详见 [`docs/PREPROCESSING_OPTIONS.md`](docs/PREPROCESSING_OPTIONS.md)。旧 `.pyeplan` v1 加入新字段的安全默认；没有更改数据或 `.pyea` schema。固定诊断已接入 reliability-smoke；此前源码检查已通过，本次发布检查以输出记录为准。

- GUI 七步工作流：导入/映射、质量与处理、指标、群体上下文、统计设计、运行、结果与追溯。支持数据质量门控、个体/群体/高级信号分析、统计、图表和 CSV/XLSX/PDF 等导出。
- `.pyeplan` 保存分析设置，排除输入数据与结果；`.pyea` 保存 full/results_only 运行档案。关闭窗口会提醒归档未保存运行。
- 0.9.6 新增六项工作流入口：导入映射预设、逐文件批量队列、质量时间轴定位、静态图片 AOI 编辑、历史运行比较和缺失原因导航。逐项操作及限制见 [`docs/WORKFLOW_ENHANCEMENTS.md`](docs/WORKFLOW_ENHANCEMENTS.md)。导入预设独立于 `.pyeplan`；批量默认每个文件独立分析，群体流程另行启动。
- 批量队列会为每项生成 CSV、results-only `.pyea` 与状态清单；results-only 档案不含原始/处理轨迹，所以不能用作历史时间轴数据源。AOI 图片需要显式绑定刺激，坐标只有符合现有坐标语义检查才可用于分析。个体 AOI 指标还要求在导入页对当前数据修订明确确认 AOI 坐标映射；这项数据来源确认与群体页的跨成员共享 AOI 坐标声明分开管理，并随修订变化失效。
- 内置 F1 新手指南、十类输入说明、合成练习与模板、41 页中文图解 PDF；指南内容版本仍为 0.9.3，不随本次部署版本虚增。
- 后台任务会显示当前阶段、已用时间和预计剩余时间；没有同类完成样本时使用明确的非零初始值，再以本次会话中同类成功/partial 任务校准，不落盘。导入来源、辅助 context 来源和结果表筛选各自隔离计时；归档准备在用户确认前结束计时。超时、取消、映射确认和工作表选择等待分别显示或暂停，固定可靠性诊断覆盖计时生命周期。
- 0.9.4 品牌图标已纳入本次提交，包含 EXE/窗口图标与稳定的 Windows AppUserModelID。0.9.5 增加部署、发布和接手入口，不改科学算法。

## 代码地图

| 位置 | 作用 |
| --- | --- |
| `pyeyedata_launcher.py`, `pyeyedata_bootstrap.py` | 启动前准备用户目录、日志、Matplotlib 缓存；诊断入口 |
| `pyeyedata/gui/app.py`, `main_window.py`, `session.py`, `tasks.py` | Qt 应用、界面、会话、后台任务与取消 |
| `pyeyedata/gui/bridge.py` | GUI 与科学 pipeline 的事务边界，运行/缓存发布 |
| `pyeyedata/gui/diagnostic.py`, `task_timing.py`, `diagnostic_timing.py` | 启动与合成固定诊断、阶段用时估计和计时生命周期核对 |
| `pyeyedata/gui/import_profiles.py`, `batch_queue.py`, `batch_export.py` | 独立导入预设、逐文件队列状态与结果导出 |
| `pyeyedata/gui/signal_timeline.py`, `aoi_editor.py`, `run_comparison.py`, `remediation.py` | 质量页时间轴、静态图 AOI 编辑、历史比较与原因导航组件 |
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

固定诊断使用合成数据和 Qt offscreen，**尚未声明真实设备验证、干净新主机验收、原生多显示器/DPI 覆盖**。六项增强与新指标已纳入 0.9.6；具体入口、当前限制与版本范围见 `version.md` 最新条目和两份补充说明（便携 ZIP 同附）。无代码签名证书。后续改动要按影响补针对性验证，不能从 smoke 推断全部科学方法均独立验证过。

旧 `version.md` 含原机器 E:/H:/F: 审计路径和阶段性 pending 记录，按日期阅读，不能将早期记录当作最新结论。这些目录不随仓库转移，构建和普通开发不依赖它们。`plans/portable-release.md` 记录本次部署改造方案；没有隐藏的外部接手任务清单。

2026-10-28 入口与复用审查：项目专用维护 skill 位于 `.agents/skills/pyeyedata-workflow/SKILL.md`。launcher 在任务开始前核对实际模块来源，固定诊断和检查汇总保存 `process.module_origins`；旧目录模块或混合包搜索路径会被拒绝。新增启动模块已加入构建来源哈希。会话缓存日志显示实际未复用原因；当前没有跨启动自动 checkpoint 接续，不能将 `.pyea` 或 `.pyeplan` 视为该能力。此轮三项源码 Check 已通过；没有构建或验证新的冻结 EXE。

下一步可优先在另一台 Windows 主机验证下载、首次启动、练习、中文 PDF、归档重开及不同 DPI。若继续科学方法开发，先阅读对应模块和 `STATISTICAL_METHODS.md`，明确基线与适用条件再修改。
