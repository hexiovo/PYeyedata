# 统计方法与支持范围（方法版本 1.1.0）

计算入口为 `run_statistics(metrics, design, performance=None, cancel=None, progress=None)`，返回无 GUI 依赖的 `AnalysisBundle`：`statistics`、`descriptives`、`diagnostics` 表，以及严格 JSON 元数据中的 `audit`。输入必须符合共享 `contracts.py`；非法输入合同错误由调用者显示为失败。推断本身的设计阻断、后端失败、取消均有结果状态与具体原因。

## 设计必须显式提供

`DesignSpec` 或同字段字典指定 `design_type`、`outcome_type`、`analysis_unit`、ID 列、独立性确认、绩效列与连接键。独立单位只支持明确的 subject/individual/participant 或 team/interaction；未知设计和未知单位不能从行数、列名或相关显著性猜出。字段可检查依赖，但不证明随机化。

支持的 `design_type` 为 `independent`、`team_independent`、`paired`、`repeated`、`lmm`、`correlation`、`within_correlation`。组间 × 组内设计以及缺失/不平衡的单因素重复条件会执行指定的随机截距 LMM。配对设计需要两条件，每单位条件唯一；重复试次不被无提示平均。不同 session/trial 不被当作独立被试。

连续推断需明确 `outcome_type="continuous"`，可用 `options.metric_types` 为各 metric 单独确认。二元、计数、强边界比例、删失时间和未知类型保留描述结果并阻断推断。将比例视作连续近似需要研究者的明确选择；程序不会从观测值自动替研究者作决定。

每组独立单位、完整配对或信息有效被试少于 3 时阻断推断；3–19 的独立单位显示小样本警告。这些是软件保护，不能代替设计、功效分析或方法假设。团队渐近 LMM 少于 10 个独立团队时阻断。

普通个体推断检测到共享团队成员会阻断；同一人跨团队及交叉成员结构不在当前模型范围。pair 行不能贡献独立 N。团队 pair 分析必须声明 `aggregate_pairs=true` 并给 `team_members` 或 `pair_members`，按团队 × 声明因子取不加权均值后推断。估计目标是团队内 pair 均值，N 是团队数，审计保留聚合输入量及成员信息。不会把一个四人团队的六对当作六个独立团队。

## 实际执行的方法

| 方法 | 估计与检验 | 区间、效应和边界 |
| --- | --- | --- |
| Welch 两组 t | 第一组减第二组均值；异方差标准误和 Welch–Satterthwaite df；双侧 p | 均值差 t 区间；Hedges g 用合并样本 SD 与精确 gamma 偏差修正 J(df)，不声称使用等方差 t；g 的重采样 CI |
| 配对 t | 完整单位的第一条件减第二条件；差值 t、df=N_pairs−1 | 均值差 t 区间；dz=差值均值/差值样本 SD；整对重采样 dz CI |
| Welch 单因素 ANOVA | 至少三组；真实 Welch 有限样本 F 修正及分母自由度 | 报告组均值与后续均值对比，不把普通合并方差 eta² 冒充 Welch 效应量 |
| Games–Howell | 所有组对；q=abs(diff)/sqrt((s1²/n1+s2²/n2)/2)，pairwise Welch df | Studentized-range 分布，组数 k；同时 95% CI；p 已在当前 k 组全部 pair 的族内校正 |
| 单因素 RM ANOVA | 完整平衡 subject × condition；实际分解条件与被试内误差 SS | 默认真实 Greenhouse–Geisser 修正 df/p；partial eta²=SS_condition/(SS_condition+SS_error)，整被试矩阵行 bootstrap CI |
| Pearson | 一个独立单位一行、双侧线性相关；SciPy 精确相关 null p | 整行重采样 r CI；效应与区间单位为 1 |
| Spearman | 显式等级/单调目标；并列秩用平均秩；SciPy 渐近 p | 整行重采样重新排名，rho CI；小样本警告保留；重复 Spearman 未验证，明确阻断 |
| 重复 Pearson / rmcorr | 在每被试内去均值；残差相关；df=N_retained_rows−N_informative_subjects−1 | 整被试簇 bootstrap CI；假设独立被试、各被试截距不同但共同 within 斜率，以及条件残差独立同方差。不能解释为被试间效应，也不能使用普通逐行 Pearson df |

重复相关会排除少于两行或在特征/绩效任一项上无被试内变异的被试，明确保存其 ID 和原因。不让这些被试增加独立 N 或残差自由度。完美 within 关系导致零残差方差时阻断 t 推断。

声明 within/repeated/LMM 目标却只剩每单位一行时，不能改做普通被试间相关，整项明确阻断。选择 `design_type=lmm` 且提供重复绩效数据时，实际把所选绩效作为连续预测变量，拟合分开的 within/between 固定系数；不会无提示换成 rmcorr。共享团队成员必须使用 `nested_team`，即使声称使用 LMM，普通 subject 截距仍会阻断；嵌套模板的关联也保留团队与其内 subject 随机结构，独立 N 为团队数。

GG 计算：P=I−11ᵀ/k，C=P·sample_covariance·P，ε=trace(C)²/[(k−1)trace(C²)]，界于 1/(k−1) 与 1；修正两项 df，F 本身不变。`AnovaRM` 仅作为未修正 F 的参考，不被当作已实现 GG 的后端。

## 有限混合模型模板

固定部分为存在的明确组间因子 × 条件，外加预设连续协变量。分类因素使用 Treatment 编码；参考水平来自 `reference_levels`，未覆盖时采用稳定的字符串排序首水平，并把实际选择写入审计。连续协变量按当前模型完整观测集的总均值中心化，中心数值保存。默认最终估计 REML，可明确选 ML。

`options.selected_levels` 可按已声明的 `group_column` / `condition_column` 提供实际 JSON 标量水平列表，以固定本次假设的组间或组内条件范围。未提供或设为 `null` 时使用全部实际水平；空对象、显式空列表、未知列、无效/过期水平均阻断，不会扩回全部水平。字符串、整数、小数与布尔值按原始类型匹配（例如 `false`、`0` 和 `"0"` 是不同值）。省略某个已声明列的选择仍对该列使用全部水平。

水平筛选发生在现有依赖结构检查后、逐目标完整观测筛选前。每个 metric × stratum × performance 假设审计请求与实际水平、筛选前后行/ID、按水平排除记录；原计划目标保留，即使请求水平在该片段缺失、显式为空，或完整观测中某个所选水平没有数据，也会将该目标标记为阻断。配对和重复模型仍以原独立单位及条件匹配；筛选不能绕过团队成员、stream、overlap 或一对一绩效连接保护。描述统计明确标记为 `all_levels` 或 `selected_levels`，并使用相同组别/条件范围。参考水平仍只负责顺序与对比方向；参考水平不在子集内时阻断并保留原因。

| `lmm_template` | 随机部分与限制 |
| --- | --- |
| `subject_intercept` | 声明独立单位的随机截距；每单位至少两次观测。团队重复聚合结果使用团队为单位 |
| `subject_condition_slope` | 被试随机截距及完整条件随机斜率协方差；每单位覆盖每水平且每个 cell 至少两次观测，避免不能区分随机斜率与残差方差 |
| `nested_team` | interaction/team 随机截距与其内 subject 方差分量；成员不能跨团队；至少十个团队 |
| `within_between` | 被试随机截距；指定连续 `association_predictor` 的 within 值减被试均值，between 值为被试均值减所有被试均值的不加权均值；两种变异都需存在 |

后端是实际环境锁定的 statsmodels MixedLM。每次只用 LBFGS、最多 500 次迭代；不自动改变模型结构或试选显著结果。固定系数双侧 z/p 和 95% 正态 Wald CI 是渐近推断，无小样本 df 校正，不声称 Satterthwaite/Kenward–Roger。

固定矩阵秩亏、精确被试内残差为零、拟合不收敛、非有限协方差/标准误，或随机协方差/方差分量的相对边界阈值 1e−8 触发失败/阻断。保留可获得的后端诊断、警告、随机协方差、残差方差、log likelihood、式子、中心化与编码；不静默退化到独立 t。调用者可在新运行中明确选择更简单的已说明模板。

可声明 `compare_fixed_terms` 为 reduced 固定项（group/condition/interaction）的列表。只支持严格嵌套的固定子空间，相同模型完整观测、相同随机结构；full/reduced 两者都用 ML 比较 likelihood-ratio chi²，保存 ML log likelihood、df 差、式子和同样本记录。最终系数仍可来自 REML。未收敛、奇异或 likelihood 次序非法的比较不能给 p。

## 完整观测、重采样及比较族

每个 metric × 显式 stratum × performance 目标独立筛选完整观测。记录输入/保留的行、subject/team/session/trial ID、数量和逐行排除原因；配对另外记录缺侧单位；LMM 按实际所需变量排除并记录。异常值默认不自动删除。LMM 不解决非随机缺失。

绩效连接必须声明列和连接键，并且在当前 metric/stratum 与绩效表两侧都一对一、键不空；重复键和碰撞列名阻断。不会将多个绩效列合并成一个结果 N，也不会插补缺失绩效。ordinal 绩效需要明确 Spearman 目标；binary/count/bounded/censored/unknown 绩效不能被强套相关。

重采样默认且本方法版本锁定为 5,000 次、种子 20261011、95% CI。独立样本可用 percentile 或 BCa；独立相关的 x/y 整行、配对的条件整对、RM 的 subject 矩阵行、rmcorr 的完整 subject 簇一起采样。重复簇的副本通过预先去均值残差保持为独立重抽副本，不因相同 ID 合并。团队数据先合法聚合或进入明确团队模型。不支持逐时间采样点 bootstrap。BCa 的 jackknife 按实际独立样本/完整配对，多个独立组按各组 n 缩放影响量，与 SciPy 对照。

任何非有限重抽、所有重抽同值或 BCa 退化时，CI 为 NA 并保存 `degenerate_bootstrap`/`degenerate_bca`；不偷偷删除失败 replicate，不返回伪 CI，不更换 CI 方法。原本合法的检验可保留并显示 CI 不可用警告。小 N/常量等主要推断不合法时整项阻断。

原始 omnibus、相关或模型系数检验在预先声明 `comparison_family` 内校正。探索默认 BY；确认性 `confirmatory=true` 默认 Holm。只有明确 `bh_dependence_justified=true` 时可选 BH，否则采用预设的保守 BY 并记日志。失败/NA 项不成为有效检验，族的原始目标与失败项保留在审计，不能按显著性事后拆族。

Games–Howell 在每个 metric/stratum 的本次实际比较全部 k 组 pair 内已经同时校正，使用单独的 `.../GamesHowell/H...` 族 ID 与该比较完整 pair 数，p_adj=p，不再套 BY。若目标因缺失所选水平而阻断，不会把剩下的比较冒称为原计划完整覆盖。这个范围不承诺跨不同 metric/stratum 的共同 FWER。原始 omnibus 的 BY/Holm 族与 GH pairwise 族分别公开；不把二者写成同一个已全局控制的族。

## 可用选项及结果单位

`DesignSpec.options` 接收 `strata_columns`、`metric_types`、`selected_levels`、`reference_levels`、`lmm_template`、`covariates`、`covariate_units`、`association_predictor`、`predictor_unit`、`fit_method`、`compare_fixed_terms`、`association_method`、`performance_types`、`performance_units`、`bootstrap_method`、`comparison_family`、`family_correction`（也接受 correction）、`confirmatory`、`bh_dependence_justified`、`aggregate_pairs`、`team_members`、`pair_members`。直接字典支持同名顶层字段。seed/resamples/alpha/confidence_level 与已验证固定值不同会明确阻断，不能声称实际执行了被忽略的参数。

原始输入单位保存为 `input_unit`；均值差/类别系数 CI 使用输入单位；r/rho、标准化效应量、partial eta² 和统计量为无量纲 1。连续协变量系数为结果单位/协变量单位；未提供协变量单位时明确为 unknown predictor unit。每项还保存独立 N、观测行 N、统计量名、自由度、原始/校正 p、效应定义、CI 方法、族、真实后端版本与重采样信息。

## 验证与来源

方法版本 `1.1.0` 增加了已声明组别/条件水平的真实子集筛选与逐目标审计，不改变 `CONTRACT_VERSION` 或原统计公式。没有 `selected_levels` 的新调用继续使用所有水平；历史方法版本 `1.0.0` 的运行记录保持原样，其档案不应被解释为曾由用户选择过水平。

本任务真实 oracle 证据位于 HexiPlan RUN 的 `evidence/statistics-oracle-results.json`，包含数值、容差、真实版本和定向测试历史。临时测试脚本/夹具按用户要求在交接前删除；有效数值记录和本方法说明永久保留。验证不是对所有真实研究设计的认证，拒绝的设计域不被标记为可用算法。

对照包含 SciPy Welch/paired t 与 CI、statsmodels Welch ANOVA、SciPy studentized-range 定义的 GH、AnovaRM 未修正 F 和 GG 解析协方差 ε 边界、SciPy BCa、SciPy Pearson/Spearman 及 ties、rmcorr 手算 df/残差相关与 subject bootstrap，以及 statsmodels 四种固定 MixedLM 模板的显式模型。BH/BY/Holm 与可信参考和手算排序校正对照。

参考：Welch (1951)；Games & Howell (1976)；Greenhouse & Geisser (1959)；Bakdash & Marusich (2017), Repeated Measures Correlation；Efron & Tibshirani (1993)。软件定义见 [SciPy ttest_ind](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_ind.html)、[SciPy bootstrap](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html)、[statsmodels MixedLM](https://www.statsmodels.org/stable/mixed_linear.html)、[statsmodels multipletests](https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html)。
