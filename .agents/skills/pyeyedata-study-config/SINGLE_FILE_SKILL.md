# PYeyedata 实验配置技能｜单文件版

使用时，请将本文件与实验设计、现有数据字典和脱敏样例一并提供给支持读取 Markdown 附件的 AI 工具。本文件包含技能说明、全部参考流程和完整校验工具源码，无须依赖未附带的文档或本机绝对路径。如需调用原生校验，还须使用匹配版本的 PYeyedata 源码和运行环境；缺少时应将校验状态记为 `not_run`，不能声称方案可加载。请根据工具的实际能力和可核验证据如实报告。

## 技能说明
# PYeyedata 研究配置技能

将眼动实验设计整理为供主试执行、可追溯的研究配置包。明确区分已知事实、待确认事项、临时建议、配置结构有效性、可运行条件和真实小样本试运行证据。有效的 `.pyeplan` 仅保存设置，不包含输入数据、导入映射、成员与同步依据或分析结果。

提问、配置包及操作说明默认使用**简体中文**。PYeyedata 原生 JSON 键、参数名、指标 ID、方法名、扩展名和界面原文须保持不变；可用中文解释其含义。

## 工作流程

1. 读取实验设计和已有文件，先列出已知事实，再集中询问无法安全推断且会影响执行的关键信息；等待答复时继续完成不依赖这些信息的部分。使用[研究信息收集与流程编排](#研究信息收集与流程编排)。
2. 选参数前先建立参数台账和阶段清单。每项标记 `known`、`proposed`、`missing` 或 `not_applicable`，注明证据和取值理由。`ProcessingConfig` 的程序默认值是工程缺省，不是研究建议。
3. 逐项追踪输入映射、单位/有效性解释、规范化、预处理、指标、聚合和统计。需要转换时，根据真实表头和样例生成可执行脚本；参见[映射、转换与数据追溯](#输入映射、转换与追溯)。
4. 使用当前 PYeyedata schema 和指标目录取得真实指标 ID 与参数类型。原生 `.pyeplan` 的 `configuration` 只有六组：`selection`、`experimental`、`processing`、`metrics`、`group`、`statistics_design`。不得增加额外键。眼别选择和事件检测是指标/方法参数，不是新增顶层配置组。
5. 遵守[原生预处理与统计边界](#原生预处理与统计边界)。不得为了通过连续型检验，把未知、二元、计数、比例/有界、删失或其他非连续结果强行标成连续。
6. 在项目检出目录中，通过 `MainWindow.analysis_plan()` 导出原生基线，再基于实时结构调整；使用 `analysis_plan.read_plan`、`validate_plan` 和 `pipeline.get_analysis_schema` 验证。不要凭记忆补造原生必填字段。下文内嵌的 `validate_plan.py` 校验工具 可离屏调用真实方案导出和校验 API，但不会运行分析。
7. 按[研究配置包与主试操作手册](#研究配置包与主试操作手册)交付。无法随请求附上整个目录时，使用 `SINGLE_FILE_SKILL.md`；该文件内含全部参考文档和完整校验工具源码。

## 决策门槛

- 不得仅凭数值大小或列名猜测设备、时间基准、有效性代码、被试/团队独立性、结果类型、AOI 语义、同步关系或科学参数。
- 只有写明影响和确认责任人、且不会被误认为已确认事实时，才可把建议值放入草案。缺失的必需证据会阻塞相应阶段和最终 `runnable` 状态。
- 阶段状态、配置状态和真实小样本试运行状态分别记录。原生 schema 校验只证明设置对象可由带指纹的当前源码读取，不能证明数据兼容、科学设计合理、设备受支持或分析成功。
- 如实报告 `unsupported`、`unavailable`、`partial`、`failed` 和 `not_run`。不得承诺“零错误”、保证适配任意 AI、宣称科学有效，或虚报真实设备验证。
- 用户数据、`.pyea` 运行档案、`.pyeplan` 设置、导入配置和研究配置清单是不同对象，分别保存。

## 参考资料

- 研究信息、阶段衔接和阻塞规则：[references/intake-and-orchestration.md](#研究信息收集与流程编排)
- 原生预处理字段和统计能力：[references/native-processing-and-statistics.md](#原生预处理与统计边界)
- 输入映射、转换与追溯：[references/conversions-and-data.md](#输入映射、转换与追溯)
- 交付结构、状态和主试操作手册：[references/study-pack-and-runbook.md](#研究配置包与主试操作手册)
- 当前源码指纹、方案校验工具和检查：下文内嵌的 `validate_plan.py` 校验工具

## 研究信息收集与流程编排

# 研究信息收集与流程编排

## 先确认事实，再选择参数

先用简短的“已知 / 需要确认”摘要复述当前设计。对每个会阻断执行的未知项，提出简洁问题并给出具体示例。不要等所有问题都回答后才开始；先完成已有信息足以支持的阶段。不能用方便的默认值填补未知事实。

从设计说明和源文件中收集：

1. **研究问题与估计目标**：假设、结果/指标、方向、确认性或探索性、计划对比或关联，以及结果要描述的总体。
2. **设计与独立单位**：组间分组、被试内条件、配对、重复试次/会话、团队/交互和嵌套结构；随机分配方式；被试/团队 ID；排除规则；计划独立样本数 N。文件、行、眼、事件、配对、试次均不能自动视为独立单位。
3. **数据与设备**：厂商、设备/型号、软件与导出方式/版本、原始格式、真实表头和若干脱敏样例行、采样率或不规则采样、设备端滤波、输出眼别、视线表示形式，以及瞳孔测量含义（直径/面积和原始单位）。
4. **时间**：源时间列、单位和纪元、单调性、重置、帧率（如按帧计时）、各数据源时钟、同步事件及来源、可接受漂移、任务/事件时标，以及窗口/基线是相对事件还是绝对时间。
5. **有效性与事件**：设备有效性字段和代码含义表；眨眼/追踪丢失标记；事件来源与覆盖范围；事件是设备分类还是算法推导；试次/刺激标签与缺失情况。
6. **坐标与刺激**：像素/归一化/角度坐标；原点和轴方向；若换算角度，则需显示器分辨率、物理尺寸和观看距离；图像/视频/刺激 ID；AOI 形状/顶点、坐标系、重叠规则和标注来源。
7. **分析与统计**：所需指标及单位；按样本、事件、试次、被试或团队报告；聚合顺序；结果类型（连续、有序、二元、计数、有界、删失、未知）；绩效数据来源和连接键；分层因素；比较族、校正方式、bootstrap/置信区间要求，以及 PYeyedata 外部软件计划。
8. **复现与操作**：允许使用的工具/运行环境、目标系统、文件命名、隐私/脱敏要求、预期产物、主试经验和可确认假设的负责人。

提问示例：

- “表头是 `Recording timestamp [ms]`。这是经过时间、设备时钟，还是日期时间纪元？如果每个试次都会归零，是否要分别保留每个试次的时间基准？”
- “`pupil` 表示直径还是面积？导出文档中的单位是什么？请提供厂商定义；数值范围无法证明其含义。”
- “两个条件行是否来自同一名被试？如果是，请指出被试 ID 列，并确认是否要先汇总试次再做被试层推断。”
- “有效性值 `0` 表示无效、有效还是未知？请提供设备文档或代码本。”

暂时无法确认时，将对应台账项标为 `missing`，说明会阻塞哪个阶段；有条件时先不填写该参数，交付受阻的草案清单和操作说明。不能把草案说成可执行方案。

## 阶段间编排

每项实际转换都要有独立阶段记录，写清输入、输出、参数、验收、证据、状态和失败后的处理办法。

| 阶段 | 输入 → 输出 | 必需决策与检查 | 常见停止条件 |
| --- | --- | --- | --- |
| A. 研究信息收集 | 方案 + 数据字典 → 研究事实、缺失信息清单 | 明确独立单位、估计目标、设备和结果类型 | 设计层级或关键科学元数据未知 |
| B. 源数据映射 | 设备文件 + 真实表头 → 独立导入配置/映射规范 | 每个必需字段恰好映射一次；未识别字段保持未映射；注明源单位和有效性语义 | 源列含义模糊或重复映射；时间/有效性没有可信解释 |
| C. 数据转换 | 源表 → 可追溯的规范化表及转换日志 | 保留公式、输入/输出单位、键、行数、无效行和来源 | 依赖未记录算法、猜测几何或无法恢复的时钟重置 |
| D. 规范化与就绪检查 | 转换表 → PYeyedata 规范数据集 | 时间为秒、原始顺序/索引保留、ID 稳定、有效性和坐标契约明确；检查真实导入/前检 | 时间单位缺失、代码映射无效、键损坏或排序/删行原因不明 |
| E. 共用预处理 | 规范输入 → 原始/分析信号快照、质量与处理审计 | 每个 `ProcessingConfig` 参数都有证据；秒数与样本数限制同时列出；核对 Nyquist 和分段支持 | 无处理依据、滤波不适用于采样、所需基线无法表达 |
| F. 眼流/事件构造 | 原始事件快照 + 眼别/事件参数 → 分析眼流/事件及审计 | 确认眼别、表示形式、源有效性规则、事件方法、缺口限制和角度几何 | 本地方法需要但缺少角度校准、事件证据或连续支持 |
| G. 指标计算 | 处理信号/事件/上下文 → 个体/群体/高级指标 | 从当前目录选指标 ID，记录参数、输入单位、上下文要求、输出单位/状态和独立单位 | 方法未实现、实验功能未获批准、缺上下文或估计目标不符 |
| H. 聚合与连接 | 指标行 + 绩效/上下文 → 分析表 | 定义聚合层级和连接键；统计行数与独立单位数；检查重复键和一对一连接 | 隐式取平均、配对/试次伪重复、连接出现多对多 |
| I. 统计 | 分析表 + 预先指定的 DesignSpec → 检验、描述统计、诊断和审计 | 匹配设计、结果类型、单位、估计目标、比较与多重性；阻断不支持方法 | 结果超出原生支持范围，或关键依赖/独立性不清楚 |
| J. 导出与归档 | 结果/审计 → 导出表格、图形及 `.pyea` 运行档案 | 保留状态、单位、缺失情况、参数引用、代码/源码指纹和警告 | 导出丢失审计/单位/状态，或档案模式被误述 |

每个阶段都保留来源指针。一次转换至少记录源路径/哈希或用户提供的来源标识、输入/输出列、公式/代码版本、参数与单位、行数、排除/失败行；若写出文件，还要记输出哈希。不能覆盖原始文件。

## 状态与证据规则

研究清单分别维护以下三条就绪轴：

- `configuration`：`blocked`、`draft` 或 `structure_loadable`。只有严格 JSON 成功解析，且当前源码指纹匹配时原生读写/校验通过，才可标为 `structure_loadable`。
- `run`：`blocked`、`not_run`、`ready`、`running`、`complete`、`partial` 或 `failed`。`ready` 要求真实输入映射、数据已导入、必需元数据齐全，且当前 GUI 前检通过；校验工具不能设置此状态。
- `real_pilot`：`not_run`、`partial`、`passed` 或 `failed`，并提供真实输入/设备/来源证据。合成夹具只能标为 `synthetic_diagnostic`，不能推进此状态。

每个阶段另行标为 `not_started`、`blocked`、`draft`、`ready`、`complete`、`partial`、`failed` 或 `not_applicable`。前一阶段完成不代表下游阶段已就绪。

## 原生预处理与统计边界

# 原生预处理与统计边界

以当前检出目录中的真实 API 为准：`pyeyedata.pipeline.get_analysis_schema()` 和 `pyeyedata.gui.analysis_plan`。schema 快照绑定应用版本和源码指纹。仅版本号或 pipeline schema 相同，不能证明源码未变；相关代码更新后须重新生成快照。

## 原生方案与参数

原生方案外层结构固定（`schema`、`application_version`、`pipeline_schema_version`、`name`、`configuration`），`configuration` 正好包含六组：

- `selection`：显式选中的指标 ID，以及各模块是否运行。
- `experimental`：实验模块的显式启用布尔值。
- `processing`：`ProcessingConfig`。
- `metrics`：已选个体、群体和高级方法的参数。
- `group`：有类型约束的群体方法参数。
- `statistics_design`：公开的 `DesignSpec` 和当前支持的 `options`。

不要把映射、源文件、成员列表、同步确认、运行状态、分析结果、数据或研究清单写进 `.pyeplan`。导入配置、映射/单位/有效性说明、源数据、成员/同步证据、运行档案和研究清单分别存放。`validate_plan` 会在离屏环境中设置带类型的 GUI 字段并拒绝未知或不完整值；应由主窗口导出器生成当前设置基线。

`get_analysis_schema().typed_parameters` 提供当前 `processing`、`eye_selection`、`event_detection`、`scope` 和 `statistics_design` 的带类型字段；方法目录提供已实现指标及参数定义。眼别选择和事件检测通过 `metrics` 中对应的方法表单设置，不能虚构新的配置组。表单前缀和字段可能变化，应检查当前导出默认值、schema 和校验器。

## ProcessingConfig 字段矩阵

下表记录当前 `ProcessingConfig` 的工程默认值/限制，仅用于理解软件现状，**不是科学建议**。每个研究值都须有来源证据，否则标为 `proposed` 或 `missing`。

| 字段 | 当前原生默认值 | 含义与决策依据 |
| --- | --- | --- |
| `order_policy` | `require_monotonic` | 保留/要求源时间顺序，或显式排序；排序不能修复时间重置。 |
| `duplicate_policy` | `reject` | 拒绝重复时标或保留并标记；须有源数据证据解释重复含义。 |
| `support_gap_factor` | `3.0` | 根据典型时间间隔确定局部支持阈值；记录采样是否规则。 |
| `support_gap_max_s` | `null` | 可选的绝对支持上限。`null` 不代表任意大缺口都安全。 |
| `interpolate_gaze` | `false` | 只在明确支持范围内插值；视线插值不能代替事件证据。 |
| `interpolate_pupil` | `false` | 只影响瞳孔分析副本；原值和来源有效性仍须区分。 |
| `pupil_interpolation_method` | `linear` | `pchip` 需要两侧可信锚点和连续支持；不得外推。 |
| `pupil_speed_mad_enabled` | `false` | 可选速度 MAD 拒绝；设备已滤波可能破坏其前提。 |
| `pupil_speed_mad_n` | `20` | 中位数 + n×MAD 规则中的倍数；保留并检查阈值/标记。 |
| `pupil_blink_padding_enabled` | `false` | 可选的逐眼眨眼/丢失边缘遮罩。 |
| `pupil_blink_padding_before_s` | `0.050` | 种子前延伸的秒数。 |
| `pupil_blink_padding_after_s` | `0.150` | 种子后延伸的秒数。 |
| `gaze_max_gap_s` | `0.075` | 视线插值的最大时间缺口。 |
| `pupil_max_gap_s` | `0.200` | 瞳孔插值的最大时间缺口。 |
| `max_missing_samples` | `3` | 连续缺失样本数上限；同时报告按实测采样率折算的秒数。 |
| `allow_unverified_interpolation_bounds` | `false` | 明确例外项；不能为了填补缺口就启用，须说明理由。 |
| `lowpass_cutoff_hz` | `null` | 可选截止频率；需有采样率证据且严格低于 Nyquist 频率。 |
| `lowpass_order` | `4` | Butterworth 阶数；短数据段可能跳过滤波，记录每段实际应用/跳过状态。 |
| `pupil_baseline_window_s` | `null` | 可选的共享绝对规范时间窗；事件相对基线可能需要转换成带标签的 `baseline_start_s`/`baseline_end_s`。 |
| `baseline_min_coverage` | `0.80` | 判断基线可用性的最小覆盖率，不是通用质量标准。 |
| `baseline_min_length_s` | `0.200` | 请求基线的最短时长；后端允许零。 |

每个缺口上限都要同时用秒和样本数表示，并结合观测到的时间间隔。例如报告正 `Δt` 的中位数/分位数、`ceil(limit_s / median(Δt))` 得出的样本数，并确认独立的 `max_missing_samples` 上限。采样不规则时按分段计算支持范围，不能把单一采样率套到全数据。插值要求两个限制都通过，因此要同时列出。没有源文件或实测时间戳证据时，不能推断采样率。

低通滤波施加于共享信号：视线/瞳孔插值之后、瞳孔基线扣除之前。它按数据段/运行分段处理；短段、非正时间间隔、截止频率达到或超过 Nyquist、滤波长度不足时可能被跳过。要记录实际结果，不能暗示每段都过滤成功。基线扣除依据原始时间的支持/覆盖审计；不可用基线仍保持不可用。

重要的数据路由：共享插值/滤波/基线处理之前，会先捕获事件来源快照；眼流和本地事件从这个原始事件快照派生。不能声称经过滤波或插值的共享视线信号会自动进入事件检测。眼别选择和事件检测各自有参数、有效性规则、支持/缺口限制、坐标/几何前提及审计记录。

事件方法以当前 `EventDetectionConfig` 和方法表单为准，包括 I-VT/I-DT、有界本地扫视脉冲、仅外部事件或关闭。度/秒等阈值必须使用真实角度坐标；像素阈值不是视觉角度。外部厂商事件需有真实导出、有效事件 ID/来源引用、时间基准和分类来源。若并排比较本地/外部事件，须显式配置。不能声称本地检测器已通过设备验证。

## 当前原生统计后端支持范围

方法契约见 `pyeyedata/STATISTICAL_METHODS.md`（当前方法版本 1.1.0）；以当前源码为准。支持的设计名为 `independent`、`team_independent`、`paired`、`repeated`、`lmm`、`correlation` 和 `within_correlation`。独立单位按设计声明为被试/参与者或团队/交互。配对、试次、事件、通道、数据流和时间采样点不会因行数多就变成独立样本。

| 设计 | 原生方法/要求 |
| --- | --- |
| 独立两组 | Welch t 检验；明确两个组水平；独立单位满足设计。 |
| 独立三组及以上 | Welch ANOVA 和 Games–Howell 两两比较；Games–Howell 对其完整比较族作自身校正。 |
| 配对 | 配对 t 检验；恰好两个匹配条件，且每个独立单位/条件仅一行。 |
| 重复测量 | 完整平衡的单因素重复测量使用 Greenhouse–Geisser 校正的 RM ANOVA；受支持的不平衡/多因素结构走明示的 LMM 路径，否则阻断。不得静默平均重复试次。 |
| LMM | 有限模板：`subject_intercept`、`subject_condition_slope`、`nested_team`、`within_between`；使用 MixedLM，并显式检查收敛、奇异性/边界情况。它不是通用公式引擎。 |
| 相关 | 被试独立单位数据可用 Pearson，或显式指定 Spearman；必须声明绩效列和一对一连接键。 |
| 被试内相关 | 在声明的假设下使用重复 Pearson/rmcorr 或被试内/被试间 LMM 路径；重复 Spearman 会被阻断。 |

原生推断结果必须显式声明为连续型；指标类型可通过 `options.metric_types` 指定。二元、计数、有界/比例、删失时间、无明确理由时按连续处理的有序结果，以及未知类型，保留描述统计并阻断不受支持的推断。不能仅为通过校验就把命中/未命中、计数、比率、有界分数或生存时间改标成连续。如果所需方法超出原生契约，须另行说明外部分析流程、精确输入表、独立单位、代码/运行时/环境、输出和审计；`.pyeplan` 不会运行该外部方法。

最低样本数和保护规则：少于 3 个独立单位/配对时，大部分推断会被阻断；渐近 LMM 的团队数至少 10 个。共享团队成员关系会阻断普通被试独立分析；配对行需要有依据的聚合或团队模型。系统会按目标记录缺失值、重叠、选定水平和排除情况。配置校验成功并不会测试这些依赖数据的条件。

重采样/推断选项是固定约束：5,000 次重采样，seed `20261011`，95% 区间；只支持已验证的 percentile 或 BCa，cluster bootstrap 仅支持 percentile。时间序列不能按样本点 bootstrap。探索性校正默认 BY；确认性默认 Holm；BH 需要明确的依赖结构理由。Games–Howell 内部两两校正是独立比较族。不得声称采用了系统不支持的区间算法、次数或 seed；不支持的设置会被阻断。

## 需要依据的参数

只有在台账中引用了明确来源时才派生设置，例如厂商文档、研究方案、校准记录、实测采样、经验证的标注或分析人员的书面决定。常见例子：

- **时间规范化**：保留原时间；仅在确认单位后才可用 `/1000` 做 ms→s。帧编号需要经过核实的帧率及帧/时间映射。日期时间纪元须保留时区/纪元，并按明确原点换算经过时间。
- **坐标规范化**：像素→视觉角度需要显示器像素几何、物理尺寸、观看距离、原点/轴约定；优先使用厂商已校准角度导出。归一化坐标需要导出方定义。`0..1` 数值范围不能证明是归一化坐标。
- **事件阈值**：度/秒需要经过校准的角度位置和可信 `Δt`；缺校准时阻断基于角度的本地事件。
- **基线**：记录时间窗是绝对、分段相对还是事件相对，刺激/事件原点、边界开闭约定（若转换会用到）、最短时长、覆盖率和逐个单位的可用性。
- **滤波**：记录实测采样支持、Nyquist 余量、阶数/截止频率理由、分段长度和跳过段。不能只凭软件默认值推导“推荐”截止频率。
- **统计因素编码**：保留源值类型，在查看结果前定义水平子集/参照方向、固定比较族和目标独立样本数。

## 输入映射、转换与追溯

# 输入映射、转换与追溯

PYeyedata 原生导入支持 `pyeyedata.io.read_table` 实现的 CSV/TSV/TXT 表格和电子表格，不是通用专有 EDF/BIN 解析器。内置的 EyeLink Data Viewer 和 Tobii Pro Lab 表头只作候选映射。检查真实导出表头、样例行和设备设置后，再核对并保存实际映射。推荐模板不等于已确认设备语义。

## 转换决策矩阵

| 源数据情况 | 原生路径或转换方式 | 证据与保留的输出 |
| --- | --- | --- |
| CSV/TSV/TXT 或 XLSX 表格导出 | 使用原生读取器和显式导入配置/映射；按真实文件确定 sheet、分隔符和小数格式 | 源文件/哈希、表头、sheet/分隔符、映射列和单位；保留源行/索引 |
| 专有 EDF 或设备二进制 | 在实际安装的厂商支持环境中使用厂商导出器/SDK，将文档支持的样本/事件/元数据导出为表格，再映射 | 厂商/版本/导出设置、原始哈希、导出回执、丢失/转换字段清单、单位/代码；不能声称 PYeyedata 可直接读取专有格式 |
| 厂商有效性代码 | 只按文档代码含义映射为显式有效性值；保留原代码和源有效性状态 | 源代码本和完整的“原值→含义”表；未知代码保持未知，并阻断必需有效性检查 |
| 毫秒时间 | 仅在单位确认后转换为秒：`time_s = (timestamp_ms - origin_ms) / 1000`；保留原始值和所选原点 | 精确公式、源列/单位/原点、源行和输出哈希 |
| 帧索引时间 | 只有经核实的帧率/时间基准才可转换：`time_s = (frame - frame0)/fps`；有丢帧/可变帧率时优先用帧时标 | FPS 来源、帧原点、不规则/丢帧证据；若时标与名义 FPS 矛盾，不能假设名义 FPS |
| 日期时间纪元 | 按明确格式/时区解析；减去该时钟注明的原点；不能静默本地化或舍入 | 原始时间、解析器/时区、原点、解析失败、夏令时/时钟问题 |
| 时钟重置或多时钟 | 按采集时钟/试次/文件分区，保留独立 `clock_id`/时间基准；仅凭匹配同步证据对齐 | 重置点、各时钟范围、匹配脉冲/事件、漂移模型及残差诊断 |
| 设备时钟漂移 | 对匹配同步点，仅在方案允许时拟合有依据的仿射映射；比较残差。一个锚点只能校正偏移，不能校正漂移 | 公式 `t_ref = a + b*t_source`、锚点、拟合系数、残差和剔除锚点；不能通过最大化结果相关性推断同步 |
| 像素/归一化坐标换算为视觉角度 | 仅在屏幕几何、观看距离、坐标原点和投影公式已确认时转换；有厂商角度校准导出时优先使用 | 原始/转换坐标、假设、公式/代码版本、校准依据及有效性；几何未知时阻断角度阈值 |
| 宽表 → 长表样本 | 每条源观测生成一行，含 `source_id`、稳定原始行/索引、被试/会话/试次/刺激、时间和眼别列；保留原始左右眼字段 | 转换前后行数、键、pivot/unpivot 代码、重复/缺失键检查 |
| 长表 → 宽表 | 只有下游方法要求时才转换；按明确且唯一的观测键透视，保留眼别/时间；拒绝重复键 | 重复键报告、行数核对、禁止未记录聚合 |
| 试次窗口/事件相对基线 | 用稳定键连接有文档依据的事件标记。若转换为原生分段边界，根据已确认事件时刻和偏移逐试次计算绝对 `baseline_start_s`/`baseline_end_s`；保留事件来源和时间窗字段 | 事件 ID/来源、偏移、生成边界、时区/时间基准、缺失事件列表；核对边界是否落在试次内及基线时长 |
| AOI/ROI 几何 | 准备静态 AOI 表，写明刺激 ID、稳定 AOI ID、形状/顶点、坐标单位/坐标系、适用时间窗和重叠语义；只使用原生支持的几何 | 原始标注、变换、几何校验结果、重复/重叠状态和坐标依据；不能猜 AOI |
| 绩效或行为结果 | 结果表单独保存；按预先声明的被试/会话/试次键连接，类型/单位精确记录 | 键完整度、唯一性、目标层级上的一对一基数、连接行/排除行；不能模糊匹配 |
| 聚合 | 仅实施预先指定的目标层级；保留聚合前记录以及总行数 N 和独立单位数 N | 公式、权重、分组列、空组/部分组处理和前后核对 |

## 时间、有效性、单位、ID 和几何

- 时间规范化目标为秒，但同时保留原始时间和源行顺序。导入器会派生 `time_s`、`time_raw` 和 `original_index`；普通字段映射无需手工伪造 `time_raw` 或 `original_index`。导入后核验派生值和源行对应关系。
- 时间重置是分段边界，不是可以通过排序“修复”的负间隔。保留时钟/时间基准和物理分区。没有基于证据的对齐转换时，不能把独立时钟拼接成一个单调序列。
- 缺失、无效和未知要分别表示。除非厂商文档有此含义，否则不能把未知有效性映射为 `True`，也不能把 `0` 当作缺失值。不得编辑源有效性标记来让某行满足插值条件。
- 原样保留厂商瞳孔单位和语义（直径/面积）。没有明确形状/校准假设时不能把面积换算成直径。视线单位和坐标系要写清；像素距离不等于视觉角度。
- ID 保留类型且稳定。`false`、`0` 和 `"0"` 是不同值。每张表都按声明的粒度检查唯一性；不能用行号充当被试 ID。
- AOI 几何与指标参数、静态图片分开。保留刺激/AOI ID 和顺序。坐标系未知、形状不受支持、自交或映射未经确认时，阻断 AOI 计算。
- 绩效连接必须显式声明，并在目标分析单位上是一对一。重复键或列名冲突须停止并修复；不能随意选择“第一条匹配”。

## 生成实际转换脚本

需要转换时，应根据真实源文件样例生成该研究专用、可执行脚本。不能只给一个可选脚本菜单或写“按需转换”。脚本应当：

1. 只读取声明过的输入；检查必需表头、类型、单位标签、键唯一性、预期时钟范围和声明的 schema/版本。
2. 写入新文件且不覆盖源文件；带上原始列和稳定源行引用。
3. 执行明确的公式/映射；原始值与派生值并列保存，保留状态/有效性来源。
4. 生成转换报告，记录行数、未映射/未知代码、解析失败、重复 ID、连接缺失、重置点、漂移残差和越界值。语义未识别时必须失败关闭。
5. 使用固定、可复现的输出顺序，并保留测量所需精度。根据实际提供的脱敏代表行补充夹具/检查，覆盖出现过的无效代码、缺失、时间重置和边界值。
6. 对输入/输出产物计算哈希，并记录脚本 SHA-256、Python/运行时和依赖。不得静默安装依赖；使用获批运行时，否则明确写出所需的外部运行环境。

没有数据样例时，只提供转换规范并将其标为 `blocked`/`draft`；不能杜撰设备列名，也不能声称脚本已用真实导出验证。

每个阶段都保留来源引用。每项转换要记录源路径/哈希或用户提供的来源标识、输入/输出列、公式/代码版本、参数和单位、行数、排除/失败行及输出哈希（若有输出）。不覆盖原始源文件。

## 研究配置包与主试操作手册

# 研究配置包、清单与主试操作手册

## 配置包内容

只交付实验实际需要的文件。典型配置包如下：

```text
study-pack/
  study-pack-manifest.json       # 就绪状态、阶段、证据和文件索引
  parameter-ledger.csv           # 每项设置、来源和理由
  study-intake.md                # 已知事实、未知项、估计目标、设计和决策
  input-contract.md              # 文件/表格粒度、映射、单位、有效性、时间、ID
  import-profile.json            # 与 .pyeplan 分开；须有真实表头才能填写
  analysis-plan.pyeplan          # 仅含原生设置；由当前应用 API 保存
  source-contract.json           # 当前检出目录 API/schema 和源码指纹
  conversions/                   # 如有需要，放置实际研究转换脚本/报告
  statistics/                    # 仅在有需要时放置外部脚本/环境说明
  diagnostics/                   # 原生结构校验/合成诊断，明确标注类型
  operator-runbook.md            # 精确 GUI 操作、预期结果、停止和修复步骤
  evidence/                      # 依据、校验和、前检/小样本试运行回执
```

默认不把用户原始数据复制进配置包。导入配置不能混进方案设置；`.pyeplan` 不是完整项目；`.pyea` 是运行档案而不是配置。原生运行由应用保存为 `.pyea`，档案模式和位置由操作者决定。

## 研究清单结构

此清单由本 skill/编排层定义，不属于 PYeyedata。不得将其放入 `.pyeplan`。最小结构示例：

```json
{
  "schema": "pyeyedata-study-pack/v1",
  "study_id": "stable-local-id",
  "created_utc": "ISO-8601 timestamp",
  "readiness": {
    "configuration": "blocked|draft|structure_loadable",
    "run": "blocked|not_run|ready|running|complete|partial|failed",
    "real_pilot": "not_run|partial|passed|failed"
  },
  "source_contract": {
    "application_version": "...",
    "pipeline_schema_version": "...",
    "contract_version": "...",
    "fingerprint_sha256": "...",
    "snapshot_path": "source-contract.json"
  },
  "stage_manifest": [
    {"stage": "mapping", "status": "not_started|blocked|draft|ready|complete|partial|failed|not_applicable",
     "depends_on": [], "operation": "exact GUI action or literal command",
     "working_directory": "absolute path selected for the study pack or not_applicable",
     "environment": "exact executable/runtime and dependency lock, or not_applicable",
     "inputs": [], "outputs": [], "expected_output": "", "acceptance": [],
     "stop_and_remedy": [], "evidence_refs": [], "blockers": []}
  ],
  "parameter_ledger": [
    {"id": "stable-key", "stage": "processing", "parameter": "pupil_max_gap_s",
     "value": null, "unit": "s", "status": "known|proposed|missing|not_applicable",
     "evidence": [], "rationale": "", "impact_if_wrong": "", "native_config_path": "processing.pupil_max_gap_s"}
  ],
  "artifacts": [{"path": "analysis-plan.pyeplan", "role": "native-settings", "sha256": "...", "status": "validated"}],
  "blockers": [],
  "decisions": [],
  "evidence": []
}
```

实际清单必须填写单一真实枚举值，不能保留示例中的备选项。`created_utc` 使用生成时的真实时钟。`structure_loadable` 表示当前源码指纹匹配，且原生读取器和校验器接受该方案。`ready` 还要求操作者导入真实数据、确认独立映射和元数据并通过当前应用前检。`real_pilot=passed` 必须有真实小样本试运行文件、设备流程和留存证据；合成运行不能设为通过。

每个阶段都写明依赖、可复制的命令或精确 GUI 操作、工作目录、运行环境/依赖、预期输出、验收方式和停止/修复办法。未知信息应标为 `missing`，不适用项标 `not_applicable` 并说明理由；不得虚构路径/命令。台账每项只能标记 `known`、`proposed`、`missing` 或 `not_applicable`：`known` 必须附证据引用，`proposed` 是写清理由和确认人的临时选择，`missing` 会阻塞相关阶段，`not_applicable` 说明为何不适用。不能静默把建议升级成已知。

## 零基础主试操作手册

主试版操作手册默认使用简体中文。界面名称按当前 PYeyedata 版本核对。主窗口有七个分析页面，另有“运行日志”页：

| 界面步骤 | 主试操作 | 预期结果/通过标准 | 停止与处理方式 |
| --- | --- | --- | --- |
| **数据导入** | 打开真实信号表；选择工作表/分隔符；检查表头和样例行；每个必需源列仅映射一次；确认真实时间、瞳孔、坐标、有效性和 ID 含义；核对后另存导入配置。 | 导入预览能对应原始行；必需映射、单位和有效性状态可见；没有重复映射。 | 单位/代码未知、缺时间、列重复或缺失、解析警告解释不清时停止。向研究负责人/设备厂商索取代码本，修正映射并重新导入；不能按数值范围猜测。 |
| **数据预处理** | 查看质量和处理设置；记录当前参数后再加载研究方案；逐项核对参数台账；同时填写秒数和样本数限制；检查基线和滤波支持。 | 处理控件与已批准参数一致；质量报告保留原始值/来源有效性，并报告各分段实际应用/跳过状态。 | 采样、缺口、基线原点/覆盖率或 Nyquist 未知时停止。关闭无依据的操作，将阶段标为阻塞；补齐依据后重新生成/验证方案。 |
| **个体分析** | 只选择方案批准的个体指标 ID；核对指标需要的字段、眼别/单位、参数表单和上下文。 | 目录中的指标均已实现；所需上下文已接入；各指标单位和状态有定义。 | 方法未实现/实验功能未批准，或缺 AOI/事件/参考输入时停止；不能为通过界面检查随意替换指标。 |
| **多人分析** | 选择真实成员/交互和分析范围；检查时标对齐和重叠；确认同步方法和来源；设定群体指标参数。 | 成员键、时间范围、时间基准和重叠证据符合设计。 | 多个同时钟缺少同步依据，或团队/成员结构不清时停止。起始时刻相等、配对数相等不等于已同步，也不能证明独立样本数。 |
| **统计设计** | 声明设计、结果类型、独立单位、ID、组别/条件、绩效列与连接键、模型模板和预先指定的比较族。 | 每项声明都有方案/数据依据；目标推断在原生支持范围内；前检没有未解决的设计阻塞。 | 二元/计数/有界/删失/未知结果，或依赖关系未确认时停止；保留描述统计，把不支持的推断明确转为外部方法，不能重新标成连续。 |
| **高级指标** | 只启用研究方案要求的方法；按需附真实外部事件、目标运动、AOI、阅读标注、参考或同步来源。 | 来源 schema、单位、几何及方法来源通过当前可用性检查。 | 缺来源引用、AOI 几何未确认、事件覆盖未知或坐标转换不支持时停止。转换脚本只有通过基于实际来源样例的检查后才能使用。 |
| **结果与追溯** | 检查每行结果的单位、状态、有效 N、排除项、警告、统计审计和来源；将需要的 CSV/XLSX/PDF 导出到选定目录；保存 `.pyea` 运行档案。 | 输出表包含预期内容并保留 partial/unavailable 原因；档案模式和完整性有记录。 | 状态/单位缺失、结果与设计不符或导出丢审计时停止。保留日志，不能把 partial/failed 运行称为完成。 |
| **运行日志** | 出现警告或错误后打开此页，查看当前阶段、后端消息和错误回溯。 | 日志能指出当前操作或提供保留的诊断信息。 | 保存原始报错和源码指纹；按提示回到所属页面或输入进行修复，再运行前检和分析。日志页用于排障，不是科学分析阶段。 |

首次运行前，先点全局操作**检查可用条件**并阅读所有状态和原因。所有必需检查通过后再点**运行分析**；前检本身不会执行分析。保存设置用 **文件 → 保存分析方案…**（`Ctrl+Shift+S`），加载设置用 **文件 → 加载分析方案…**（`Ctrl+Shift+O`）。方案只替换设置；输入数据、历史记录、多人范围和同步证据仍需重新核对。不要查找或虚构 `--config` 命令行参数。

### 首次启动、文件类型和输出目录

- 使用发布便携包时，先完整解压，再从解压目录启动 `PYeyedata.exe`。使用源码目录时，在仓库根目录打开 PowerShell，运行 `.\start.cmd`；如当前目录尚无 `.venv`，先按项目说明运行 `.\setup.cmd`。常规研究操作通过 GUI 完成。
- 原始数据从**数据导入**打开；`.pyeplan` 从文件菜单加载；`.pyea` 是运行档案，在**结果与追溯**查看；导入配置在**数据导入**页选择。这些文件用途不同，不能放进错误的打开窗口。
- 在研究目录下新建由负责人选择的输出文件夹。原生设置保存为 `analysis-plan.pyeplan`，转换副本放 `converted/`，导出放 `exports/`，运行档案放 `runs/`。原始输入保持只读并放在生成文件夹之外，除非数据负责人明确要求复制。
- 如需在源码目录运行校验工具，从仓库根目录使用项目 Python 环境，例如：

  ```powershell
  .\.venv\Scripts\python.exe .agents\skills\pyeyedata-study-config\scripts\validate_plan.py export-default --project-root $PWD --out "<study-pack>\analysis-plan.pyeplan"
  .\.venv\Scripts\python.exe .agents\skills\pyeyedata-study-config\scripts\validate_plan.py validate "<study-pack>\analysis-plan.pyeplan" --project-root $PWD --snapshot "<study-pack>\source-contract.json"
  ```

  将 `<study-pack>` 换成实际目标目录，记录真实命令、工作目录和 Python 版本。这些命令只创建或校验设置与源码快照，不会导入数据、运行指标或证明研究已就绪。如果收到的是单文件版技能，将文件末尾嵌入的校验工具源码另存为 `validate_plan.py`，再配合选定的 PYeyedata 源码目录使用。没有匹配的源码和运行环境时，将原生校验标为 `not_run`。

### 外部转换或统计脚本

必须为确实需要的脚本提供实际命令、运行时和路径，例如：

```powershell
Set-Location "<study-pack-root>"
& "<approved-python-executable>" ".\conversions\convert_<study>.py" --input "<actual-source>" --output ".\converted\<output>"
```

操作手册还要写清真实可执行文件、Python/包版本和锁文件、参数、预期输出名、退出码含义、报告位置及修复办法。批量转换前用代表性样例验证。不能静默安装依赖，也不能保证项目环境一定能运行外部统计后端。缺少运行时或包时，停止并请负责人批准/安装；没有实际运行证据前，状态保持 `not_run`。

常见处理办法：

- 未知源列保持未映射；按真实代码本/表头修正后重新导入。
- 时间单位、有效性、坐标、必需上下文或同步阻塞需要证据，不能通过勾选绕过。
- `partial` 或 `unavailable` 可能是正确结果；保留原因，不能用 0 替换缺失结果。
- 方案被拒绝时，先确认应用/schema 兼容性并查看校验报告，再从当前应用导出基线、仅重新应用受支持字段并复验；不能随意删除未知原生字段。
- 配置或来源变化后重新跑当前前检，不能沿用旧的就绪回执。
- “保存分析方案”保存设置，“保存运行档案”保存运行。results-only 档案可能缺少重算所需数据；检查档案模式和完整性原因。
- 确实发生分析失败时，保留日志、精确源码指纹、参数台账、输入哈希和可用的 `.pyea`；不能把失败改标为完成。

## 示例场景

### 元数据缺失：正确阻断

输入：“用 EyeLink 导出数据比较两组瞳孔响应。”未提供文件、瞳孔语义/单位、真实表头/代码本、时间语义、组 ID 来源、独立单位说明、事件/窗口定义或结果类型。

预期配置包：研究信息摘要记录“两组比较”目标；台账将这些缺失事实标为 `missing`；映射/转换、瞳孔指标、统计和 `run` 就绪状态均阻断。可以包含未修改的原生默认值导出作为基线，但不能称作研究配置。提出简洁、针对性问题，同时继续准备数据清单和主试操作步骤。不能猜参数、伪造表头或声称可运行。

### 信息完整的简单设计：结构校验通过，但尚未进行真实试运行

仅作合成示例：一个左瞳孔平均值指标，连续结果，两个独立参与者组，以参与者 ID 为独立单位，组列 `condition`，并明确记录独立性依据。校验工具可以通过 `MainWindow.analysis_plan()` 导出当前默认值，仅更改所需字段，再用实时 schema 校验。产物标为 `synthetic_diagnostic`；因为没有真实数据、转换、GUI 前检或设备测试，`run=not_run` 且 `real_pilot=not_run`。真实使用前仍要确认瞳孔单位、时间元数据和真实组列来源。

## 内嵌校验工具：`validate_plan.py`

如需使用，请将以下源码另存为 `validate_plan.py`，并在目标 PYeyedata 检出目录及对应环境中运行。工具会调用所选源码的真实 API 校验配置，不会执行分析。

```python
#!/usr/bin/env python3
"""Fingerprint PYeyedata's live plan contract, export native settings, and validate.

This helper validates settings only. It never imports analysis data or runs the pipeline.
Run from an approved PYeyedata CPython 3.12 environment with PySide6 installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

MAX_PLAN_BYTES = 2 * 1024 * 1024
FINGERPRINT_SCHEMA = "pyeyedata-study-source-contract/v1"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number: {value}")
    return number


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _read_strict(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("plan exceeds the native 2 MiB size limit")
    value = json.loads(
        raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
        parse_constant=_constant, parse_float=_finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("plan root must be a JSON object")
    return value


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _assert_origin(module: Any, root: Path) -> None:
    origin = Path(module.__file__).resolve()
    if not _inside(origin, root):
        raise RuntimeError(f"refusing module outside selected checkout: {module.__name__} -> {origin}")


def _project_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not (root / "pyeyedata" / "__init__.py").is_file():
        raise ValueError(f"not a PYeyedata source checkout: {root}")
    return root


def _import_project(root: Path) -> tuple[Any, Any, Any]:
    """Put the requested checkout first, then refuse mixed or shadowed modules."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root_text = str(root)
    while root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)

    import pyeyedata
    import pyeyedata.pipeline as pipeline
    import pyeyedata.gui.analysis_plan as analysis_plan

    package_path = [Path(item).resolve() for item in pyeyedata.__path__]
    expected_package = (root / "pyeyedata").resolve()
    if package_path != [expected_package]:
        raise RuntimeError(f"refusing mixed pyeyedata package paths: {package_path}")
    for module in (pyeyedata, pipeline, analysis_plan):
        _assert_origin(module, root)
    return pyeyedata, pipeline, analysis_plan


def _ensure_qapplication() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["pyeyedata-study-config"])


def _source_files(root: Path) -> list[Path]:
    files = [path for path in (root / "pyeyedata").rglob("*.py") if "__pycache__" not in path.parts]
    files.extend(root / name for name in ("pyeyedata_bootstrap.py", "pyeyedata_startup.py")
                 if (root / name).is_file())
    return sorted({path.resolve() for path in files}, key=lambda path: path.relative_to(root).as_posix())


def _contract(root: Path, pyeyedata: Any, pipeline: Any) -> dict[str, Any]:
    schema = pipeline.get_analysis_schema()
    app_version = str(getattr(__import__("pyeyedata.app_info", fromlist=["APP_VERSION"]), "APP_VERSION"))
    files = []
    digest = hashlib.sha256()
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": relative, "sha256": file_hash})
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "schema": FINGERPRINT_SCHEMA,
        "application_version": app_version,
        "pipeline_schema_version": str(schema["pipeline_schema_version"]),
        "contract_version": str(schema["contract_version"]),
        "fingerprint_sha256": digest.hexdigest(),
        "source_files": files,
        "analysis_schema": schema,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(value), encoding="utf-8", newline="\n")


def _default_plan(root: Path, analysis_plan: Any, pipeline: Any, *, example: bool) -> dict[str, Any]:
    app = _ensure_qapplication()
    from pyeyedata.gui.main_window import MainWindow
    import pyeyedata.gui.main_window as main_window_module
    _assert_origin(main_window_module, root)

    main = MainWindow()
    payload = main.analysis_plan(
        name="SYNTHETIC_ONLY — independent-groups configuration example" if example else "Native PYeyedata default baseline"
    )
    if example:
        config = payload["configuration"]
        config["selection"] = {
            "individual_metric_ids": ["individual.pupil_left.mean"],
            "group_metric_ids": [],
            "advanced_metric_ids": [],
            "run_individual": True,
            "run_group": False,
            "run_advanced": False,
        }
        current = config["statistics_design"]
        current.update({
            "design_type": "independent",
            "outcome_type": "continuous",
            "analysis_unit": "subject",
            "subject_column": "participant_id",
            "group_column": "condition",
            "independence_confirmed": True,
            "independence_note": "Synthetic example only; confirm from the actual allocation and IDs.",
        })
        payload["name"] = "SYNTHETIC_ONLY — independent-groups example (not a study plan)"
    # Run the public validator against the live schema before returning.
    analysis_plan.validate_plan(payload, pipeline.get_analysis_schema())
    main.deleteLater()
    app.processEvents()
    return payload


def _validate(args: argparse.Namespace) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, analysis_plan = _import_project(root)
    app = _ensure_qapplication()
    payload = _read_strict(Path(args.plan).expanduser().resolve())
    # Also invoke the application's strict byte reader; duplicate/non-finite JSON is rejected above.
    payload = analysis_plan.read_plan(args.plan)
    schema = pipeline.get_analysis_schema()
    normalized = analysis_plan.validate_plan(payload, schema)
    contract = _contract(root, pyeyedata, pipeline)
    snapshot_state = "not_supplied"
    if args.snapshot:
        snapshot = _read_strict(Path(args.snapshot).expanduser().resolve())
        snapshot_state = (
            "match" if snapshot.get("fingerprint_sha256") == contract["fingerprint_sha256"] else "mismatch"
        )
    report = {
        "status": "native_validation_passed_current_source",
        "configuration_status": "structure_loadable" if snapshot_state == "match" else "draft",
        "plan_path": str(Path(args.plan).expanduser().resolve()),
        "application_version": contract["application_version"],
        "pipeline_schema_version": contract["pipeline_schema_version"],
        "contract_version": contract["contract_version"],
        "current_source_fingerprint_sha256": contract["fingerprint_sha256"],
        "snapshot_state": snapshot_state,
        "normalized_configuration_groups": sorted(normalized),
        "runnable": "not_assessed_by_plan_validation",
        "real_pilot": "not_run",
        "analysis_executed": False,
    }
    if args.report:
        _write_json(Path(args.report).expanduser().resolve(), report)
    print(_dumps(report), end="")
    del app
    return 2 if snapshot_state == "mismatch" else 0


def _export(args: argparse.Namespace, *, example: bool) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, analysis_plan = _import_project(root)
    payload = _default_plan(root, analysis_plan, pipeline, example=example)
    destination = Path(args.out).expanduser().resolve()
    analysis_plan.write_plan(destination, payload)
    saved = analysis_plan.read_plan(destination)
    normalized = analysis_plan.validate_plan(saved, pipeline.get_analysis_schema())
    contract = _contract(root, pyeyedata, pipeline)
    contract_path = Path(args.contract_out).expanduser().resolve() if args.contract_out else destination.with_name("source-contract.json")
    _write_json(contract_path, contract)
    report = {
        "status": "structure_loadable",
        "synthetic_only": bool(example),
        "plan_path": str(destination),
        "source_contract_path": str(contract_path),
        "application_version": contract["application_version"],
        "pipeline_schema_version": contract["pipeline_schema_version"],
        "contract_version": contract["contract_version"],
        "source_fingerprint_sha256": contract["fingerprint_sha256"],
        "native_configuration_groups": sorted(normalized),
        "runnable": "not_assessed_no_data_imported",
        "real_pilot": "not_run",
        "analysis_executed": False,
    }
    if args.report:
        _write_json(Path(args.report).expanduser().resolve(), report)
    print(_dumps(report), end="")
    return 0


def _snapshot(args: argparse.Namespace) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, _ = _import_project(root)
    contract = _contract(root, pyeyedata, pipeline)
    _write_json(Path(args.out).expanduser().resolve(), contract)
    print(_dumps({
        "status": "source_contract_captured",
        "snapshot_path": str(Path(args.out).expanduser().resolve()),
        "application_version": contract["application_version"],
        "pipeline_schema_version": contract["pipeline_schema_version"],
        "contract_version": contract["contract_version"],
        "fingerprint_sha256": contract["fingerprint_sha256"],
    }), end="")
    return 0


def _selfcheck(args: argparse.Namespace) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, analysis_plan = _import_project(root)
    contract = _contract(root, pyeyedata, pipeline)
    complete_plan = _default_plan(root, analysis_plan, pipeline, example=True)
    good = analysis_plan.validate_plan(complete_plan, pipeline.get_analysis_schema())
    checks: list[dict[str, Any]] = [{
        "check": "complete_synthetic_independent_design",
        "status": "passed",
        "synthetic_only": True,
        "native_configuration_groups": sorted(good),
        "design_type": good["statistics_design"]["design_type"],
        "metric_ids": good["selection"]["individual_metric_ids"],
        "analysis_executed": False,
    }]

    # Missing study facts remain blocked; this is a workflow fixture, not data inference.
    required_facts = {
        "source_header_and_mapping": None,
        "timestamp_unit_and_origin": None,
        "pupil_semantics_and_unit": None,
        "participant_id_source": None,
        "group_allocation_and_independence_evidence": None,
    }
    missing = sorted(key for key, value in required_facts.items() if value is None)
    assert missing
    checks.append({"check": "missing_metadata_blocks_readiness", "status": "passed",
                   "configuration": "draft", "run": "blocked", "missing": missing})

    schema = pipeline.get_analysis_schema()
    bad_version = json.loads(_dumps(complete_plan))
    bad_version["pipeline_schema_version"] = "not-the-live-schema"
    try:
        analysis_plan.validate_plan(bad_version, schema)
    except ValueError:
        checks.append({"check": "wrong_pipeline_schema_rejected", "status": "passed"})
    else:
        raise AssertionError("wrong pipeline schema was accepted")

    unknown = json.loads(_dumps(complete_plan))
    unknown["configuration"]["invented_group"] = {}
    try:
        analysis_plan.validate_plan(unknown, schema)
    except ValueError:
        checks.append({"check": "unknown_native_group_rejected", "status": "passed"})
    else:
        raise AssertionError("unknown configuration group was accepted")

    with tempfile.TemporaryDirectory(prefix="pyeyedata-study-config-") as temporary:
        directory = Path(temporary)
        for filename, content in (
            ("duplicate.json", '{"schema":"x","schema":"y"}'),
            ("nan.json", '{"value":NaN}'),
        ):
            path = directory / filename
            path.write_text(content, encoding="utf-8")
            try:
                _read_strict(path)
            except (ValueError, json.JSONDecodeError):
                checks.append({"check": f"strict_json_rejects_{path.stem}", "status": "passed"})
            else:
                raise AssertionError(f"strict reader accepted {filename}")
        plan_path = directory / "valid-example.pyeplan"
        analysis_plan.write_plan(plan_path, complete_plan)
        roundtrip = analysis_plan.read_plan(plan_path)
        analysis_plan.validate_plan(roundtrip, schema)
        checks.append({"check": "native_write_read_validate_roundtrip", "status": "passed"})

    report = {
        "schema": "pyeyedata-study-config-selfcheck/v1",
        "status": "passed",
        "source_fingerprint_sha256": contract["fingerprint_sha256"],
        "checks": checks,
        "limitations": ["synthetic configuration only", "no real data imported", "no analysis executed", "no device validation"],
    }
    _write_json(Path(args.out).expanduser().resolve(), report)
    print(_dumps(report), end="")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="capture current public schema and full source fingerprint")
    snapshot.add_argument("--project-root", required=True)
    snapshot.add_argument("--out", required=True)
    snapshot.set_defaults(handler=_snapshot)

    validate = commands.add_parser("validate", help="strict-read and validate an existing .pyeplan")
    validate.add_argument("plan")
    validate.add_argument("--project-root", required=True)
    validate.add_argument("--snapshot")
    validate.add_argument("--report")
    validate.set_defaults(handler=_validate)

    for name, example in (("export-default", False), ("example-independent", True)):
        export = commands.add_parser(name, help="export a real MainWindow plan and validate it offscreen")
        export.add_argument("--project-root", required=True)
        export.add_argument("--out", required=True)
        export.add_argument("--contract-out")
        export.add_argument("--report")
        export.set_defaults(handler=lambda args, mode=example: _export(args, example=mode))

    selfcheck = commands.add_parser("selfcheck", help="exercise strict JSON and native plan validation with synthetic settings")
    selfcheck.add_argument("--project-root", required=True)
    selfcheck.add_argument("--out", required=True)
    selfcheck.set_defaults(handler=_selfcheck)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```
