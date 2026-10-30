---
name: pyeyedata-study-config
description: 将眼动实验设计整理为可追溯的 PYeyedata 研究配置包，涵盖原生 .pyeplan、数据转换、预处理、统计边界和操作说明。用于设计转配置，不用于代替真实数据分析或把方案当作数据。
---

# PYeyedata 研究配置技能

将眼动实验设计整理为供主试执行、可追溯的研究配置包。明确区分已知事实、待确认事项、临时建议、配置结构有效性、可运行条件和真实小样本试运行证据。有效的 `.pyeplan` 仅保存设置，不包含输入数据、导入映射、成员与同步依据或分析结果。

提问、配置包及操作说明默认使用**简体中文**。PYeyedata 原生 JSON 键、参数名、指标 ID、方法名、扩展名和界面原文须保持不变；可用中文解释其含义。

## 工作流程

1. 读取实验设计和已有文件，先列出已知事实，再集中询问无法安全推断且会影响执行的关键信息；等待答复时继续完成不依赖这些信息的部分。使用[研究信息收集与流程编排](references/intake-and-orchestration.md)。
2. 选参数前先建立参数台账和阶段清单。每项标记 `known`、`proposed`、`missing` 或 `not_applicable`，注明证据和取值理由。`ProcessingConfig` 的程序默认值是工程缺省，不是研究建议。
3. 逐项追踪输入映射、单位/有效性解释、规范化、预处理、指标、聚合和统计。需要转换时，根据真实表头和样例生成可执行脚本；参见[映射、转换与数据追溯](references/conversions-and-data.md)。
4. 使用当前 PYeyedata schema 和指标目录取得真实指标 ID 与参数类型。原生 `.pyeplan` 的 `configuration` 只有六组：`selection`、`experimental`、`processing`、`metrics`、`group`、`statistics_design`。不得增加额外键。眼别选择和事件检测是指标/方法参数，不是新增顶层配置组。
5. 遵守[原生预处理与统计边界](references/native-processing-and-statistics.md)。不得为了通过连续型检验，把未知、二元、计数、比例/有界、删失或其他非连续结果强行标成连续。
6. 在项目检出目录中，通过 `MainWindow.analysis_plan()` 导出原生基线，再基于实时结构调整；使用 `analysis_plan.read_plan`、`validate_plan` 和 `pipeline.get_analysis_schema` 验证。不要凭记忆补造原生必填字段。`scripts/validate_plan.py` 可离屏调用真实方案导出和校验 API，但不会运行分析。
7. 按[研究配置包与主试操作手册](references/study-pack-and-runbook.md)交付。无法随请求附上整个目录时，使用 `SINGLE_FILE_SKILL.md`；该文件内含全部参考文档和完整校验工具源码。

## 决策门槛

- 不得仅凭数值大小或列名猜测设备、时间基准、有效性代码、被试/团队独立性、结果类型、AOI 语义、同步关系或科学参数。
- 只有写明影响和确认责任人、且不会被误认为已确认事实时，才可把建议值放入草案。缺失的必需证据会阻塞相应阶段和最终 `runnable` 状态。
- 阶段状态、配置状态和真实小样本试运行状态分别记录。原生 schema 校验只证明设置对象可由带指纹的当前源码读取，不能证明数据兼容、科学设计合理、设备受支持或分析成功。
- 如实报告 `unsupported`、`unavailable`、`partial`、`failed` 和 `not_run`。不得承诺“零错误”、保证适配任意 AI、宣称科学有效，或虚报真实设备验证。
- 用户数据、`.pyea` 运行档案、`.pyeplan` 设置、导入配置和研究配置清单是不同对象，分别保存。

## 参考资料

- 研究信息、阶段衔接和阻塞规则：[references/intake-and-orchestration.md](references/intake-and-orchestration.md)
- 原生预处理字段和统计能力：[references/native-processing-and-statistics.md](references/native-processing-and-statistics.md)
- 输入映射、转换与追溯：[references/conversions-and-data.md](references/conversions-and-data.md)
- 交付结构、状态和主试操作手册：[references/study-pack-and-runbook.md](references/study-pack-and-runbook.md)
- 当前源码指纹、方案校验工具和检查：`scripts/validate_plan.py`
