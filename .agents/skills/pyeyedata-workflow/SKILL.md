---
name: pyeyedata-workflow
description: Maintain and diagnose PYeyedata entry points, scientific analysis, cache reuse, archives, plans and Windows delivery. Use for work in the PYeyedata repository.
---

# PYeyedata 维护与交付

先读项目 [接手说明](../../../CODEX_HANDOFF.md)、[版本记录](../../../version.md) 最新条目及 [部署说明](../../../docs/DEPLOYMENT.md)，检查分支和工作区，保留已有改动。这里的相对路径从本文件所在目录解析。正式入口是 `start.cmd` → `tools/manage.ps1 -Action Run` → `pyeyedata_launcher.py`；固定检查使用同一个 launcher 的诊断参数。

## 真实入口与来源

- 合并启动、补丁、辅助模块或构建修改后，检查实际加载模块的 `__file__` 与包 `__path__`，不能仅测试独立 helper。`verify_runtime_sources()` 在进入任务前拒绝混用程序目录；诊断 `process.module_origins` 和检查汇总记录实际路径。路径核验不等于内容哈希或 API 契约验证。
- 从程序目录外执行正式 launcher 检查，覆盖用户工作目录与同名旧模块的干扰。修改分发文件列表时核对 `build_manifest.py`、`build_identity.py` 和 PyInstaller 入口；新增根目录运行依赖也必须纳入来源清单。
- 不绕过摘要、来源或必要 API 检查来消除报错；保留实际/预期实现与异常证据。磁盘更新不会自动替换已运行进程的模块。

## 复用、恢复和状态

- 当前 `ExecutionCache` 是有界、显式启用的会话内事务缓存，**没有跨启动自动断点接续**。`.pyea` 是运行档案，`.pyeplan` 是设置方案，均不是自动恢复 checkpoint。不能套用 HyperEEG 的 run/taskId 恢复契约，也不能据计时归零推断重算。
- 缓存命中只据实际 audit 报告，区分匹配缺失、预算限制、完整性失败、取消与过期。缓存完整性失败可以按当前输入重算，但必须保留拒绝原因；档案/方案损坏必须明确报错，不能当作空历史。
- 缓存发布必须通过操作成功、当前代际和取消检查；不复用未提交工作。参数、输入、单位、来源及算法版本兼容性要分别验证。UI/帮助改动不应无理由改变科学算法版本。
- 若明确新增持久接续能力，另行设计契约：完成位只记 0/1，科学结果独立存储；提交产物并核验后才写完成位；兼容旧历史、缺指针、损坏、并发锁和失败原子性都有回归。未实现前不作恢复承诺。
- 阶段、核验、实际复用和新计算分别显示；百分比是进度指标，不是耗时比例。本次计时不是历史累计用时；ETA 是估计，不能当作冻结或完成证据。

## 科学、性能与清理

- 保留单位、原始有效性、来源、参数、状态和追溯。`partial`、`unavailable` 可为正确结果；不把缺失证据填成有效值。不把相关 pair、片段或通道当作独立被试。合成检查不代表真实设备验证。
- 遇到慢任务先定位阶段、实际推进、重复读取/哈希/复制、单任务内存与缓存预算，再调整并行或缓存。按可用内存和测得峰值建议并发数，不照搬其它项目的 worker 数，也不宣称有动态防溢出机制。
- 仅清理已确认不再被后续计算、追溯、导出或恢复引用的临时文件。失败现场、用户输入、预处理和必要来源保留；清理目标须解析后位于明确允许目录内。

## 验证与交付

针对根因和相邻边界补固定诊断，再运行 `powershell -NoProfile -ExecutionPolicy Bypass -File tools/manage.ps1 -Action Check`。核对三场景及来源证据，保存可重放工具，不留下零散临时脚本。版本记录写本轮实际范围、证据、局限；不把 helper 检查等同正式入口通过，不把源码检查等同 EXE 已验证。

发布须先同步代码/资源/版本/来源清单并提交，再执行 `release.cmd`；构建中不改输入。不提交用户数据、档案、环境和发布包。普通维护不自动启用历史 HexiPlan。
