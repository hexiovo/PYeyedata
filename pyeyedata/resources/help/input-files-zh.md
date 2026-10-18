# 文件怎么填：先知道用途，再照着例子准备

不需要把所有文件都准备齐。**主要信号是基础输入**；区间、区域、表现和辅助资料只在所选分析需要时添加。下面的例子全是合成数据，编号以 `SYNTHETIC_ONLY` 开头，不能替代真实记录。

CSV 第一行是列名，每行一条记录。用小数点写数字，不在数字后面写单位。XLSX 明确选择数据工作表；模板里的 `empty_template` 是空表，`example` 是示例，`fields` 只有说明，不能作为数据导入。文件中的列名与界面的“源列”对应后才会生效。

所有示例都可用帮助窗口下方的按钮导出。空白模板用于填真实数据；示例用于看写法。时间单位要在导入页明确选择。下文样例均使用秒，`start_s=0.2` 表示第 0.2 秒，结束必须晚于开始。

## signals · 主要信号：设备连续记录的数字

**何时需要**：每次分析都需要。至少要有参与者、会话和时间；要算瞳孔就加瞳孔列，要算注视坐标就加坐标列。

**每列怎么填**：`subject_id` 是参与者编号，例如 P01；`session_id` 是一次记录的编号，例如 S01；`time_s` 是该次记录开始后的秒数；`pupil_left` 是左瞳孔数值；`pupil_left_valid` 表示设备认为该点是否有效，示例 1 为有效、0 为无效。正式数据的有效性编码要按设备说明明确填写，不能照猜。

**可照做的例子**：`signals-practice.csv` 有 201 行，用于完整练习。前三行写法如下：

```csv
subject_id,session_id,time_s,pupil_left,pupil_left_valid
SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,0.00,3.000,1
SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,0.01,3.003,1
SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,0.02,3.006,1
```

**导入怎么选**：把这五列分别选到对应的源列；在映射表的“单位”列将左瞳孔选为 mm，在时间与单位面板也填 mm，两处一致。时间选秒，核对后确认时间。`original_index` 和 `time_raw` 由软件整理生成，不需要自己造两列。

**常见错误**：时间实际是毫秒却选秒，会让时长差 1000 倍；瞳孔单位没填，会阻止导入或相关指标。缺测不要擅自填 0，保留缺失并说明有效性。

## segments · 区间标注：只分析某段任务

**何时需要**：要区分“看视频”和“答题”等时间段时。单人区间要与主要信号的参与者和会话一致。

**每列怎么填**：`segment_id` 是不重复的区间编号；`segment_name` 是看得懂的名称；`start_s/end_s` 是起止秒；`subject_id/session_id` 与主表同一次记录对应。

**文件**：`segments-example.csv`。

```csv
subject_id,session_id,segment_id,segment_name,start_s,end_s
SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,SYNTHETIC_ONLY_SEG01,练习前半段,0.0,1.0
```

**导入怎么选**：映射这六列，选择参与者会话范围。`[0,1)` 包括 0 秒，不包括正好 1 秒的点。

**常见错误**：结束等于开始、名称留空或编号对不上。多人共享区间还需要真实互动编号、成员会话表和明确时间基准；不要把单人例子直接冒充共享区间。

## aois · 关注区域：例如屏幕上的图片或按钮

**何时需要**：要算看某块区域的时长、次数或区域间转换时。AOI 就是你事先画定的关注区域。

**每列怎么填**：`aoi_id` 是稳定区域编号，`aoi_name` 是名称，`stimulus_id` 指哪张画面；`shape=rectangle` 是矩形；四个边界值是左右和上下边。`unit=px` 表示像素，`coordinate_frame` 要与信号采用同一个坐标基准。

**文件**：`aois-example.csv`。

```csv
aoi_id,stimulus_id,shape,aoi_name,xmin,ymin,xmax,ymax,unit,coordinate_frame,overlap_policy,priority
SYNTHETIC_ONLY_A01,SYNTHETIC_ONLY_IMAGE,rectangle,左侧图片,0,0,500,400,px,synthetic_top_left,exclusive,1
```

**导入怎么选**：映射编号、名称、刺激、形状和边界，确认与主要信号同单位、同坐标系。多边形写法可看现有 AOI 模板的 example 表。

**常见错误**：画面编号与信号不同、px 和归一化坐标混用、两块区域重叠却未声明归属规则。使用 exclusive 表示一次只归一个区域；重叠区域需唯一数值优先级，数值越小越优先。

## performance · 任务表现：正确率或反应时间

**何时需要**：要研究眼动指标与任务表现的关系时。只算平均瞳孔等描述指标时可不填。

**每列怎么填**：参与者和会话用于明确配对；`accuracy` 示例只用 0/1 表示单次答错/答对；`reaction_time_s` 是秒。汇总正确率和单题答对与否不是同一种数据，统计类型要按真实含义选。

**文件**：`performance-example.csv`。

```csv
subject_id,session_id,accuracy,reaction_time_s
SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,1,0.62
```

**导入怎么选**：导入后到统计设计声明使用哪个表现列，以及哪些列能唯一找到同一条分析记录。例如 subject_id + session_id；若还有多个条件或试次，就需要相应条件/试次列一起确定。

**常见错误**：只凭参与者编号把多次试次混在一起；一条眼动结果匹配到多条表现。软件会检查缺失和重复键。0/1 结果不能因为是数字就直接当成连续反应时。

## external_events · 厂商或外部事件

**何时需要**：所选方法要使用设备导出的注视、扫视等事件时；普通瞳孔均值练习不需要。

**每列怎么填**：`event_id` 每个事件唯一；`event_type` 如 fixation（注视）；`start_s/end_s` 是事件起止；参与者和会话须对上主表。可另外填 `source_refs` 说明从哪个设备文件、用什么方法得到。

**文件**：`external_events-example.csv`，在“上下文资料 → 厂商/外部事件”导入。

```csv
event_id,event_type,start_s,end_s,subject_id,session_id,source_refs
SYNTHETIC_ONLY_E01,fixation,0.1,0.3,SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,SYNTHETIC_ONLY_export_note
```

**导入怎么选**：映射全部必填列，时间选秒，填写真实来源引用，点“载入完整表”。参考事件被读入不等于方法已接受它，仍要做可用条件检查。

**常见错误**：事件没有编号或时间范围超出原始记录。不要用自己随手标的事件冒充设备已认证的输出。

## target_movement_intervals · 目标移动区间

**何时需要**：要区分画面中的目标何时移动，例如追踪任务。

**每列怎么填**：参与者和会话指一次记录；`start_s/end_s` 写目标移动的起止时间；`target_id` 是可选目标编号。

**文件**：`target_movement_intervals-example.csv`。

```csv
start_s,end_s,subject_id,session_id,target_id
0.2,0.8,SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01,SYNTHETIC_ONLY_TARGET
```

**导入怎么选**：在同名上下文页映射起止、参与者和会话，选秒后载入完整表。

**常见错误**：把整个实验时长当作移动区间，或使用刺激电脑的时间却没有转换到本次记录的同一时间基准。

## eyelid_intervals · 眼睑区间

**何时需要**：方法要求明确闭眼区间及其有效性时。

**每列怎么填**：`closed=1` 表示闭合，`valid=1` 表示此标记可靠；0 表示否。起止、参与者和会话仍须明确。设备如果使用其他编码，先依据导出说明整理成明确布尔含义。

**文件**：`eyelid_intervals-example.csv`。

```csv
start_s,end_s,closed,valid,subject_id,session_id
0.4,0.5,1,1,SYNTHETIC_ONLY_P01,SYNTHETIC_ONLY_S01
```

**导入怎么选**：映射上述六列，选秒。不要把“没有追踪到瞳孔”自动等同于闭眼。

**常见错误**：把每个采样点直接当一个区间，或只填 closed 不填 valid。

## reference_fixations · 参考注视

**何时需要**：某些扫描路径比较方法需要一条事先确定的参考注视序列。

**每列怎么填**：`reference_id` 是同一套参考的名字；`fixation_index` 是顺序；`start_x/start_y` 是坐标；`duration_s` 是停留秒数。

**文件**：`reference_fixations-example.csv`。

```csv
reference_id,fixation_index,start_x,start_y,duration_s
SYNTHETIC_ONLY_REF,1,100,120,0.2
SYNTHETIC_ONLY_REF,2,240,120,0.3
```

**导入怎么选**：映射五列，时间选秒，明确参考 ID、坐标单位和坐标系；本示例用 px、synthetic_top_left。随后按所选方法填写匹配的参考参数。

**常见错误**：同一序号重复、持续时间为负、参考坐标和主数据不是同一套单位。样例只展示结构，不保证任意比较方法的样本量和条件都已满足。

## reference_trajectory · 参考轨迹

**何时需要**：要比较眼睛轨迹与已知目标路径时。

**每列怎么填**：`time_s` 是参考时刻，`gaze_x/gaze_y` 在这里表示参考位置，不是新采到的眼睛位置；`reference_valid=1` 表示参考点可用。

**文件**：`reference_trajectory-example.csv`。

```csv
time_s,gaze_x,gaze_y,reference_valid,reference_id
0.0,100,120,1,SYNTHETIC_ONLY_REF
0.1,110,120,1,SYNTHETIC_ONLY_REF
```

**导入怎么选**：映射时间和位置，时间选秒，参考 ID 填 SYNTHETIC_ONLY_REF，单位 px、坐标系 synthetic_top_left 仅用于练习；正式数据填写自己的真实定义。

**常见错误**：漏填参考 ID、时间未对齐、坐标原点不同。成功载入后还须检查对应方法是否满足条件。

## reading_annotations · 阅读标注

**何时需要**：需要把阅读区域、行号或阅读方向作为说明留在档案里。**当前这类表只作追溯，不自动进入阅读指标计算**。

**每列怎么填**：`reading_scope_id` 是阅读范围编号；`stimulus_id` 指画面；`line_id` 指行；`reading_direction=ltr` 是从左到右，rtl 是从右到左。

**文件**：`reading_annotations-example.csv`。

```csv
reading_scope_id,stimulus_id,line_id,start_s,end_s,reading_direction
SYNTHETIC_ONLY_READ,SYNTHETIC_ONLY_IMAGE,1,0.0,1.0,ltr
```

**导入怎么选**：至少映射 reading_scope_id；如果提供起止列，仍要确认时间单位。阅读指标真正需要的 AOI、方向和方法参数要在相应位置另外设置。

**常见错误**：以为导入一张阅读标注表，就已经计算了阅读顺序或回视次数。请以所选指标的实际条件和结果为准。
