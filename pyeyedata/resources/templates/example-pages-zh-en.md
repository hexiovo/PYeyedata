# Synthetic input examples / 合成输入示例页

Resource set `c03-resources/v3`; import contract `1.3.0`. These examples show file shape and ordinary import behavior. Every value is synthetic (`SYNTHETIC_ONLY`); replace or remove example data before real research. These pages do not show computed gaze metrics or imply participant, device, synchronization, or physiological validation.

资源集`c03-resources/v3`；导入合同`1.3.0`。示例只说明文件结构和普通导入路径，所有数据均为合成数据。真实研究前请替换或删除示例行。这里没有计算结果，也不表示参与者、设备、同步或生理验证。

## Signals / 眼动信号

Files: `signals-example-contract-1.3.0.csv`, or the `example` sheet of `signals-contract-1.3.0.xlsx`. Use `empty_template` for a new data table; use `fields` only as documentation.

```csv
subject_id,session_id,time_s,gaze_x,gaze_y,gaze_valid,gaze_left_valid,gaze_right_valid,pupil_left,pupil_right,stimulus_id,condition,trial
SYNTHETIC_ONLY_TEMPLATE_P01,SYNTHETIC_ONLY_TEMPLATE_S01,0.0,500,300,1,1,1,3.2,3.1,SYNTHETIC_ONLY_STIMULUS_01,SYNTHETIC_ONLY_condition,1
```

The source row provides explicit subject/session IDs and one time value. Confirm the time and coordinate semantics in the ordinary importer. `original_index` is generated from source row order; no participant or device status is inferred.

该行显式提供主体/会话ID和一个时间值。通过普通导入确认时间及坐标语义。`original_index`由导入器按源行顺序生成；不会推断参与者或设备状态。

## Segments / 区间标注

Files: `segments-example-contract-1.3.0.csv`, or the `example` sheet of `segments-contract-1.3.0.xlsx`.

```csv
subject_id,session_id,segment_id,segment_name,start_s,end_s,condition,trial,stimulus_id
SYNTHETIC_ONLY_TEMPLATE_P01,SYNTHETIC_ONLY_TEMPLATE_S01,SYNTHETIC_ONLY_TEMPLATE_SEG01,合成演示区间,0.0,1.0,SYNTHETIC_ONLY_condition,1,SYNTHETIC_ONLY_STIMULUS_01
```

The interval is left-closed/right-open and must have `end_s > start_s`. Shared interaction scope needs an explicit generated/real common clock declaration and member-session mapping; never use the synthetic map as proof of hardware synchronization.

区间采用左闭右开规则，必须`end_s > start_s`。共享互动范围需要显式的共同时间基准声明和成员—会话映射；合成映射不能作为硬件同步证据。

## AOIs / ROI/AOI 几何

The empty CSV is `aois-contract-1.3.0.csv`. The synthetic CSV is `aois-example-contract-1.3.0.csv`; the workbook has `empty_template`, `example`, and `fields` sheets. The example contains two overlapping polygons with explicit unique priorities. AOI domain schema is `2.0.0`, and the declared method version is `static-aoi-geometry-2.0.0`; global contract `1.3.0` remains unchanged.

空CSV模板为`aois-contract-1.3.0.csv`。合成CSV示例为`aois-example-contract-1.3.0.csv`；XLSX含`empty_template`、`example`和`fields`工作表。示例包含两个重叠多边形，并显式给出唯一优先级。AOI域schema为`2.0.0`，方法版本为`static-aoi-geometry-2.0.0`；全局合同`1.3.0`保持不变。

```csv
aoi_id,stimulus_id,shape,aoi_name,xmin,ymin,xmax,ymax,vertices,unit,coordinate_frame,overlap_policy,priority
SYNTHETIC_ONLY_TEMPLATE_AOI01,SYNTHETIC_ONLY_STIMULUS_01,polygon,合成主要区域,,,,,"[[0,0],[100,0],[100,100],[0,100]]",px,synthetic_top_left,exclusive,1
SYNTHETIC_ONLY_TEMPLATE_AOI02,SYNTHETIC_ONLY_STIMULUS_01,polygon,合成重叠区域,,,,,"[[50,50],[150,50],[150,150],[50,150]]",px,synthetic_top_left,exclusive,2
```

`aoi_name`是必填来源文本；中文名称被保留，稳定身份仍由`aoi_id`承担。`vertices`是JSON文本中的有限Nx2数值数组，示例用隐式闭合。两行都使用`exclusive`，因此任何同时命中的AOI必须有唯一有限优先级，最低数值优先。矩形和多边形边/顶点都属于闭边界命中。

`aoi_name` is required source text; Chinese names are preserved, while `aoi_id` remains the stable identity. `vertices` is a JSON-text finite Nx2 numeric array; the example uses implicit closure. Both rows use `exclusive`, so any simultaneously hit AOIs need unique finite priorities and the lowest value wins. Rectangle and polygon edges/vertices are inside under the closed-boundary policy.

These rows are labeled `SYNTHETIC_ONLY` and illustrate file structure only. The resource owner successfully ran the public AOI normalizer and static geometry audit on the bundled example. That resource check is not independent C05 acceptance, scientific validation, or device/physiology evidence.

这些行只展示文件结构，并标记为`SYNTHETIC_ONLY`。资源所有者已对随附示例运行公开 AOI normalizer 和静态几何审计并通过；这不等于独立 C05 验收、科学验证或设备/生理证据。

## Performance / 参与者表现

Files: `performance-example-contract-1.3.0.csv`, or the `example` sheet of `performance-contract-1.3.0.xlsx`.

```csv
subject_id,session_id,condition,trial,accuracy,reaction_time_s
SYNTHETIC_ONLY_TEMPLATE_P01,SYNTHETIC_ONLY_TEMPLATE_S01,SYNTHETIC_ONLY_condition,1,1,0.62
```

The row provides one binary accuracy value and one continuous reaction time. Select join keys explicitly and require one-to-one matches. The example does not claim that a selected statistic supports binary accuracy or that any association was calculated.

该行提供一个二元准确率和一个连续反应时。连接键必须显式选择，且匹配满足一对一。示例不声称当前统计方法支持二元准确率，也不表示已计算关联。
