# PYeyedata 0.9.6 常见眼动指标

0.9.6 增加 52 个可独立选择的指标：事件分布 34 个、注视事件 AOI 指标 9 个、阅读指标 9 个。本页适用于源码启动和该版本构建的 EXE；便携 ZIP 随附本说明。

## 入口与必要数据

在“指标”页搜索中文名称或下列 ID，勾选后在“Typed 参数 → 个体分析”填写所需条件，再检查可用性。AOI 坐标映射仍需在导入页对当前数据修订单独确认。

计算沿用现有可靠事件来源、时间、眼别、分段和坐标检查。本地方法生成的合格事件保留方法证据；外部事件需保留真实来源、分类版本、时间单位、事件 ID、来源引用和作用域。外部事件不因用户勾选确认就自动成为设备验证。

“来源覆盖事件类型”使用 JSON，例如 `["fixation", "saccade", "blink"]`。它是明确的分类类型声明，不从空事件表猜测。眨眼还需独立确认可靠来源；缺失采样不作眨眼。

## 事件分布：34 个

| ID 前缀 | 新增统计量 | 单位 |
| --- | --- | --- |
| `individual.fixation.duration.` | `sd`, `iqr`, `p10`, `p25`, `p75`, `p90` | s |
| `individual.saccade.duration.` | `median`, `sd`, `iqr`, `p10`, `p25`, `p75`, `p90` | s |
| `individual.blink.duration.` | 同上 | s |
| `individual.saccade.amplitude.` | 同上 | 已声明坐标单位 |
| `individual.blink.interval.` | 同上 | s |

事件等权；标准差是样本标准差（ddof=1），少于两个事件不可用。分位数采用线性插值，IQR=Q75−Q25。时长取完整事件与当前半开分析窗的正交集；跨窗事件的分布明确标记 partial，不改源事件边界。

扫视振幅只纳入起点在当前窗内的事件，使用同坐标系完整事件端点距离；无源端点时，可使用有连续原始有效支持的源边界注视坐标。不会将像素距离自动当作角度。部分缺失、明确无效或单位不匹配的源端点不静默修补。眨眼间隔为相邻合格事件的起点之差，所用事件 ID 可追溯；它不证明期间没有漏测眨眼。混有不合格事件的分布保留 partial。

## AOI 注视与命中：9 个

| ID（统一前缀 `individual.aoi.`） | 定义 |
| --- | --- |
| `fixation_ttff` | 参考时刻到首次合格 AOI 注视起点的秒数 |
| `fixation_first_duration` | 窗内首次起始的 AOI 注视在窗内的时长贡献 |
| `fixation_total_duration` | AOI 注视在窗内时长之和 |
| `fixation_mean_duration` | 上述总时长 / 合格 AOI 注视数 |
| `fixations_before_first` | 参考时刻起、首次 AOI 注视之前的合格注视起点数 |
| `hit` | 当前观察单元是否至少有一次合格 AOI 注视，1/0/未知 |
| `trial_hit_rate` | 每被试已知观察试次中命中试次的比例 |
| `trial_miss_rate` | 每被试已知观察试次中未命中试次的比例 |
| `participant_hit_rate` | 已知观察被试中至少命中过一次的被试比例 |

AOI 分配使用完整可靠注视事件内、原始有效连续采样的时间加权位置中心，以及已有静态 AOI 几何和重叠规则。插值、时间无效、源有效性为假、长缺口或物理分区边界不能提供原始观察支持。多标签 AOI 可以多重命中，不能据此假定各 AOI 占比相加为 1。

首次注视参考时刻使用“首次注视参考时刻（秒）”，须处于当前分析窗内。没有 AOI 注视时，TTFF 和首次时长保留缺失，不填 0。窗前已经开始的注视不会重新计为窗内首次起点；末端截断的首次时长保留 partial。

**原有 `individual.aoi.ttff` 仍表示首次有效采样区间进入 AOI；原有访问、驻留指标含义不变。** 新的 `fixation_ttff` 表示注视事件，二者分别输出。

已确认命中有直接事件依据；要将没有事件的观察判为“未命中”，还须勾选“注视分类覆盖观察窗已确认”，声明分类确实覆盖全部已观察试次/时间窗。观察支持必须完整，且不能有未分配或不合格事件。只提供选中的注视事件时，不应勾选。

命中率按会话、刺激、条件、段 ID、分析流和 AOI 几何身份分别汇总。试次率要求明确 trial ID；同一试次的重复标签或物理分区只计一次。被试率每被试只计一次，不按试次数加权。分母仅为实际已观察且结果已知的单位；未知数单独记录，存在未知时为 partial。没有已知单位时为 unavailable；不推断未导入的试次或未参加的被试。

## 阅读：9 个

先在现有“阅读标注与 AOI 顺序”辅助来源中映射以下列：

```csv
reading_scope_id,stimulus_id,aoi_id,reading_order
sentence_01,stimulus_01,word_A,1
sentence_01,stimulus_01,word_B,2
sentence_01,stimulus_01,word_C,3
```

同一范围/刺激内 AOI 和正整数 reading_order 均须唯一。AOI 必须已定义。顺序按阅读推进递增，包括从右向左或跨行文本；不从屏幕横坐标猜测。填写“阅读范围 ID”和 `ltr`/`rtl`，确认“AOI 阅读推进顺序”以及“注视分类覆盖观察窗”。观察窗应覆盖该文本的完整呈现/阅读试次。旧标注缺少新增列时仍可存档追溯，但新阅读计算被阻断。

以下 ID 统一前缀为 `individual.aoi.reading.`：

| ID | 定义 |
| --- | --- |
| `first_fixation_duration` | 首遍进入目标区域的首个注视时长 |
| `first_pass_duration` | 首次进入目标区域后、首次退出到其他阅读区域前的目标注视时长之和 |
| `single_fixation_duration` | 整个已观察序列仅一次目标注视且有观察到的退出时，该次时长 |
| `skipping` | 在首次目标注视之前已看过后续区域则为 1；首遍到达目标为 0；未到达且无推进证据为未知 |
| `rereading_duration` | 目标总注视时长减首遍目标注视时长；本产品明确采用此操作定义 |
| `go_past_duration` | 从首遍首次目标注视起，到首次推进退出之前的全部注视时长，包含回到前面区域的注视 |
| `first_pass_regression` | 首遍退出至前面的阅读区域为 1，推进退出为 0 |
| `skipping_rate` | 每被试已知观察试次中的首遍跳读比例 |
| `first_pass_regression_rate` | 每被试已知观察试次中的首遍回视比例 |

阅读时间单位为 s，二值/率为 proportion。已跳读区域不伪造首遍时间；没有观察到退出的首遍/Go-past 保留右删失。窗口左截断、缺口、不合格或未分配注视、多个重叠阅读区域等不能形成完整阅读序列时不可用。率的分母与未知状态规则沿用上节；未满足首遍定义的试次不填成回视 0。

阅读定义参考 [SR Research Reading Measures](https://www.sr-research.com/support/thread-336.html)。其中再阅读时间按上表独立命名的产品操作定义报告，不能与其他软件的同名变量直接混用。

## 结果、兼容与检查

结果新增 `individual_aoi_fixation_audit` 和 `individual_aoi_exposures` 表，保存事件、窗口、AOI 几何身份、眼别/流、支持来源及未知状态。参数引用记录统计定义、分位数规则、参考时刻、事件 ID、阅读标注内容哈希，以及汇总的分子、分母和单位身份。普通指标表仍沿用既有单位、状态和原因码契约，可导出并随 full/results-only `.pyea` 回读。

不更改数据、档案或 `.pyeplan` schema。打开旧 v1 方案时，仅为本次新增参数填入 None/False 安全缺省值；不自动确认来源、覆盖或阅读顺序。个体算法版本升为 `individual-0.3.0`，使旧个体计算缓存失效。

固定诊断入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/manage.ps1 -Action Check
# 单独重放新指标检查：
$env:QT_QPA_PLATFORM = 'offscreen'
.venv\Scripts\python.exe -B -m pyeyedata.gui.diagnostic_common_metrics
```

新检查已接入 reliability-smoke，包含手算序列、固定种子分布/阅读对照、缺口/有效性/来源/删失边界、GUI 参数、旧方案安全缺省、真实 pipeline 分流和 CSV/两种档案回读。它们是合成软件检查，不代表真实设备或生理有效性验证。
