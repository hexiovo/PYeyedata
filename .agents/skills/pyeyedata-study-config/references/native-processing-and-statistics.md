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
